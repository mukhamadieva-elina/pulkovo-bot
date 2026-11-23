import contextlib
import json
import re
import sqlite3
import time
from pathlib import Path

from .migrations import migrate
from .resources import bind_answer
from .feedback import cancel_pending, record_activity, message_random_id
from .protection import admit, LIMIT_TEXT
from .dialogue_rules import GREETING_IDLE_SECONDS
from .sessions import close_session, ensure_session, expire_idle


DEFAULT_SETTINGS = {
    "search_strategy": "full", "search_limit": "8", "cover_mode": "false",
    "max_clarifications": "2", "consent_version": "draft-1", "consent_ready": "false",
    "consent_url": "", "consent_text": "Здравствуйте! Я виртуальный помощник аэропорта. Для обработки ваших вопросов и изображений используется сторонний ИИ-сервис GigaChat. Ознакомьтесь с отдельным согласием на обработку данных и выберите способ общения.",
    "handoff_text": "Обращение передано сотруднику. Автоматические ответы в этом диалоге приостановлены.",
    "retention_days": "30", "waiting_user_timeout_hours": "24", "feedback_enabled": "true",
}




class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        migrate(self.path)
        with self.connect() as con:
            con.executemany("INSERT OR IGNORE INTO settings VALUES (?,?)", DEFAULT_SETTINGS.items())
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=10000")
        try:
            with con:
                yield con
        finally:
            con.close()

    def all(self, sql, params=()):
        with self.connect() as con:
            return [dict(r) for r in con.execute(sql, params)]

    def one(self, sql, params=()):
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def execute(self, sql, params=()):
        with self.connect() as con:
            return con.execute(sql, params).lastrowid

    def settings(self):
        return {r["key"]: r["value"] for r in self.all("SELECT * FROM settings")}

    def setting(self, key):
        return self.settings().get(key, "")

    def set_settings(self, values):
        with self.connect() as con:
            con.executemany("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", [(k, str(v)) for k, v in values.items()])
            if values.get('feedback_enabled') == 'false':
                con.execute("UPDATE feedback_surveys SET status='cancelled',reason='disabled',updated_at=? WHERE status IN ('pending','sending')", (time.time(),))

    def _refresh_ticket_topics(self, con, peer_id):
        for ticket in con.execute("SELECT * FROM tickets WHERE peer_id=? AND status IN ('open','waiting_user') AND topic_source='auto'",(peer_id,)).fetchall():
            previous_closed=con.execute('SELECT coalesce(max(closed_at),0) FROM tickets WHERE peer_id=? AND id<?',(peer_id,ticket['id'])).fetchone()[0]
            found=con.execute("SELECT id,topic FROM requests WHERE peer_id=? AND created_at>? AND nullif(trim(topic),'') IS NOT NULL ORDER BY id DESC LIMIT 1",(peer_id,previous_closed)).fetchone()
            if found and (ticket['topic']!=found['topic'] or ticket['topic_request_id']!=found['id']):
                con.execute('UPDATE tickets SET topic=?,topic_request_id=?,topic_version=topic_version+1 WHERE id=?',(found['topic'],found['id'],ticket['id']))

    def refresh_ticket_topics(self, peer_id):
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            self._refresh_ticket_topics(con,peer_id)

    def set_ticket_topic(self, peer_id, ticket_id, topic, expected_version):
        topic=topic.strip()
        if not topic or len(topic)>250:
            raise ValueError('Укажите тему обращения длиной до 250 символов.')
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            result=con.execute("UPDATE tickets SET topic=?,topic_source='manual',topic_request_id=NULL,topic_version=topic_version+1 WHERE id=? AND peer_id=? AND topic_version=?",(topic,ticket_id,peer_id,expected_version))
            if not result.rowcount:
                raise ValueError('Тема уже изменена или обращение недоступно. Обновите страницу.')
            con.execute("INSERT INTO audit(action,target,created_at) VALUES ('ticket_topic_changed',?,?)",(str(ticket_id),time.time()))

    def ticket_details(self, peer_id, ticket_id):
        return self.one("""SELECT t.*,
            coalesce((SELECT max(previous.closed_at) FROM tickets previous
                      WHERE previous.peer_id=t.peer_id AND previous.id<t.id),0) period_start
            FROM tickets t WHERE t.peer_id=? AND t.id=?""",(peer_id,ticket_id))

    def ticket_rows(self, state, limit=None):
        rows=self.all("""SELECT t.*,r.tone,r.user_id,r.input_type,
            trim(coalesce(p.first_name,'')||' '||coalesce(p.last_name,'')) user_name,
            (SELECT x.text FROM requests x WHERE x.peer_id=t.peer_id
             AND x.created_at>coalesce((SELECT max(previous.closed_at) FROM tickets previous WHERE previous.peer_id=t.peer_id AND previous.id<t.id),0)
             AND x.created_at<=coalesce(t.closed_at,1e30) ORDER BY x.id DESC LIMIT 1) last_text
            FROM tickets t JOIN requests r ON r.id=t.request_id
            LEFT JOIN vk_profiles p ON p.user_id=r.user_id WHERE t.status=? ORDER BY t.created_at""",(state,))
        return rows if limit is None else rows[:limit]

    def cards(self, eligible=False):
        sql = "SELECT * FROM cards"
        params = ()
        if eligible:
            mode = "cover" if self.setting("cover_mode") == "true" else "normal"
            sql += " WHERE status='published' AND visibility='public' AND mode IN ('any',?)"
            params = (mode,)
        return self.all(sql + " ORDER BY topic,id", params)

    def dialogue_rows(self, state='all', limit=None):
        rows=self.all("""SELECT * FROM (
            SELECT c.peer_id,c.user_id,c.bot_paused_until,t.id,t.request_id,t.topic_source,t.topic_request_id,
            CASE WHEN c.human_mode=0 THEN 'bot' WHEN t.status='waiting_user' THEN 'waiting_user' ELSE 'open' END status,
            CASE WHEN c.human_mode=1 THEN coalesce(t.topic,'') WHEN q.id>t.request_id THEN q.topic ELSE coalesce(nullif(t.topic,''),q.topic,'') END topic,
            CASE WHEN c.human_mode=1 THEN coalesce(t.reason,'') ELSE '' END reason,
            CASE WHEN c.human_mode=1 THEN coalesce(t.assigned_to,'') ELSE '' END assigned_to,
            CASE WHEN c.human_mode=1 THEN coalesce(t.auto_close_paused,0) ELSE 0 END auto_close_paused,
            t.last_staff_reply_at,r.created_at,r.text last_text,
            max(r.updated_at,coalesce((SELECT max(created_at) FROM staff_replies s WHERE s.peer_id=c.peer_id),0)) last_activity,
            trim(coalesce(p.first_name,'')||' '||coalesce(p.last_name,'')) user_name
            FROM conversations c JOIN requests r ON r.id=(SELECT max(id) FROM requests WHERE peer_id=c.peer_id)
            LEFT JOIN tickets t ON t.id=(SELECT max(id) FROM tickets WHERE peer_id=c.peer_id)
            LEFT JOIN requests q ON q.id=(SELECT max(id) FROM requests WHERE peer_id=c.peer_id AND nullif(trim(topic),'') IS NOT NULL)
            LEFT JOIN vk_profiles p ON p.user_id=c.user_id
            ) WHERE ?='all' OR status=? ORDER BY last_activity DESC""",(state,state))
        return rows if limit is None else rows[:limit]

    def render_answer(self, card, resources=None):
        if resources is None:
            resources = {r["key"]: r["value"] for r in self.all("SELECT * FROM resources WHERE visibility='public'")}
        def replace(m):
            if not resources.get(m[1]):
                raise ValueError(f"Не заполнен справочник: {m[1]}")
            return resources[m[1]]
        return re.sub(r"\{([a-z][a-z0-9_]*)\}", replace, card["answer"])

    def save_card(self, data, card_id=None, expected_version=None, *, editor_resources=None):
        fields = ["topic", "title", "question_variants", "answer", "conditions", "mode", "action", "visibility", "status", "issues", "reviewed", "source", "source_key"]
        defaults = {"question_variants": "[]", "answer": "", "conditions": "", "mode": "any", "action": "answer", "visibility": "public", "status": "draft", "issues": "[]", "reviewed": 0, "source": "", "source_key": None}
        values = defaults | data
        for field in ("question_variants", "issues"):
            if isinstance(values[field], list):
                values[field] = json.dumps(values[field], ensure_ascii=False)
        if not values.get("title", "").strip() or not values.get("topic", "").strip():
            raise ValueError("Заполните тему и название.")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if editor_resources is not None and values['visibility']=='public':
                values['answer'] = bind_answer(con,values['answer'],editor_resources)
            if values["status"] == "published":
                if not values["reviewed"] or not values["answer"].strip():
                    raise ValueError("Для публикации нужен готовый ответ.")
                resources = {r['key']:r['value'] for r in con.execute("SELECT key,value FROM resources WHERE visibility='public'")}
                rendered = self.render_answer(values, resources)
                if re.search(r"\[имя\]|\[Вариант из строки|\(ссылка\)|\b[ХхXx]{2}\b|https?:\s+//", rendered, re.I):
                    raise ValueError("В ответе остались незаполненные подстановки или некорректная ссылка.")
                if len(rendered) > 3500:
                    raise ValueError("Ответ должен быть не длиннее 3500 символов.")
            now = time.time()
            if card_id:
                old = con.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
                if not old or expected_version != old["version"]:
                    raise ValueError("Карточка уже изменена. Обновите страницу перед сохранением.")
                values["source_key"] = old["source_key"]
                version = old["version"] + 1
                con.execute("UPDATE cards SET " + ",".join(f"{f}=?" for f in fields) + ",version=?,updated_at=? WHERE id=?", [values[f] for f in fields] + [version, now, card_id])
            else:
                version = 1
                cur = con.execute("INSERT INTO cards(" + ",".join(fields) + ",version,updated_at) VALUES (" + ",".join("?" for _ in fields) + ",?,?)", [values[f] for f in fields] + [version, now])
                card_id = cur.lastrowid
            row = dict(con.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone())
            con.execute("INSERT INTO card_versions(card_id,version,snapshot,created_at) VALUES (?,?,?,?)", (card_id, version, json.dumps(row, ensure_ascii=False), now))
            con.execute("INSERT INTO audit(action,target,created_at) VALUES ('save_card',?,?)", (str(card_id), now))
            return card_id

    def conversation(self, peer_id, user_id=None):
        self.execute("INSERT OR IGNORE INTO conversations(peer_id,user_id,updated_at) VALUES (?,?,?)", (peer_id, user_id or peer_id, time.time()))
        return self.one("SELECT * FROM conversations WHERE peer_id=?", (peer_id,))

    def consent_offer(self, peer_id):
        """Only a successfully delivered offer for the current dialogue state."""
        return self.one("""SELECT r.* FROM requests r JOIN conversations c ON c.peer_id=r.peer_id
            WHERE r.peer_id=? AND r.status='sent' AND r.outcome='consent_required'
            AND r.consent_prompt_version=(SELECT value FROM settings WHERE key='consent_version')
            AND r.consent_prompt_generation=c.generation AND r.updated_at>=?
            AND c.human_mode=0 AND c.bot_paused_until<=?
            AND NOT (c.consent_state='accepted' AND c.consent_version=r.consent_prompt_version)
            ORDER BY r.id DESC LIMIT 1""", (peer_id,time.time()-86400,time.time()))

    def record_consent(self, peer_id, user_id, action, event_key, *, text_snapshot=None, expected_version=None):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            settings=dict(con.execute('SELECT key,value FROM settings'))
            if expected_version is not None and expected_version!=settings['consent_version']:
                return False
            inserted = con.execute("INSERT OR IGNORE INTO consent_events(peer_id,user_id,action,version,document_url,text_snapshot,vk_event_key,created_at) VALUES (?,?,?,?,?,?,?,?)", (peer_id, user_id, action, settings["consent_version"], settings["consent_url"], text_snapshot if text_snapshot is not None else settings["consent_text"], event_key, time.time())).rowcount
            if not inserted:
                return False
            con.execute("UPDATE conversations SET consent_state=?,consent_version=?,generation=generation+1,clarification_count=0,updated_at=? WHERE peer_id=?", (action, settings["consent_version"], time.time(), peer_id))
            cancel_pending(con,peer_id,'consent_changed')
            if action != "accepted":
                con.execute("UPDATE requests SET text='',attachments='[]',image_descriptions='[]' WHERE peer_id=?", (peer_id,))
                con.execute("UPDATE requests SET status='cancelled',reply=NULL,keyboard=NULL WHERE peer_id=? AND status='ready' AND ai_generation IS NOT NULL", (peer_id,))
            return True

    def allowed(self, peer_id, generation=None):
        c = self.conversation(peer_id)
        return c["consent_state"] == "accepted" and c["consent_version"] == self.setting("consent_version") and not c["human_mode"] and c['bot_paused_until'] <= time.time() and (generation is None or c["generation"] == generation)

    def enqueue(self, peer_id, user_id, event_key, text="", attachments=None, payload="", *, vk_cmid=0, vk_date=0, is_feedback=False, protect=False):
        c = self.conversation(peer_id, user_id)
        # Переписка с сотрудником хранится для админки независимо от согласия на ИИ.
        # Передача в ИИ отдельно ограничена allowed() и human_mode.
        permitted = bool(c['human_mode']) or (c["consent_state"] == "accepted" and c["consent_version"] == self.setting("consent_version"))
        attachments = attachments or []
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            if con.execute('SELECT 1 FROM requests WHERE event_key=?',(event_key,)).fetchone():
                return None
            limited,notice = admit(con,peer_id,json.loads(payload or '{}'),c['human_mode'],time.time()) if protect else ('',False)
            if limited:
                cancel_pending(con,peer_id,'rate_limited')
            if limited and not notice:
                return None
            if not limited:
                expire_idle(con,time.time(),peer_id)
            cur = con.execute("INSERT OR IGNORE INTO requests(event_key,peer_id,user_id,text,attachments,input_type,payload,random_id,created_at,updated_at,vk_cmid,vk_date) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (event_key, peer_id, user_id, text if permitted else "", json.dumps(attachments) if permitted else "[]", "mixed" if text and attachments else "image" if attachments else "text", payload, message_random_id(con), time.time(), time.time(), vk_cmid, vk_date))
            if limited:
                con.execute("UPDATE requests SET status='ready',outcome='rate_limited',reason=?,reply=?,keyboard=? WHERE id=?",(limited,LIMIT_TEXT,json.dumps({'one_time':True,'buttons':[]}),cur.lastrowid))
            if cur.rowcount and not is_feedback:
                if not limited:
                    session_id=ensure_session(con,peer_id,time.time())
                    con.execute('UPDATE requests SET session_id=? WHERE id=?',(session_id,cur.lastrowid))
                con.execute("UPDATE tickets SET status='open' WHERE peer_id=? AND status='waiting_user'", (peer_id,))
                record_activity(con,peer_id,cur.lastrowid,time.time())
            elif cur.rowcount and is_feedback:
                token=json.loads(payload or '{}').get('token','')
                con.execute('UPDATE requests SET session_id=(SELECT r.session_id FROM feedback_surveys f JOIN requests r ON r.id=f.request_id WHERE f.peer_id=? AND f.token=?) WHERE id=?',(peer_id,token,cur.lastrowid))
            return cur.lastrowid

    def staff_replied(self, peer_id, event_key, admin_id, vk_cmid, sent_at, *, expected_ticket_id=None, text=None, attachments=None, source='vk', staff_name='', basis_request_id=None, history_only=False):
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            active = con.execute("SELECT * FROM tickets WHERE peer_id=? AND status IN ('open','waiting_user')", (peer_id,)).fetchone()
            # Сверка истории не должна заново открывать обращение, завершённое
            # сотрудником, пока выполнялся запрос к VK.
            if expected_ticket_id is not None and (not active or active['id'] != expected_ticket_id):
                return False
            latest = con.execute("""SELECT * FROM requests WHERE peer_id=? AND coalesce(json_extract(
                CASE WHEN json_valid(payload) THEN payload ELSE '{}' END,'$.action'),'')!='feedback' ORDER BY id DESC LIMIT 1""", (peer_id,)).fetchone()
            existing = con.execute('SELECT id,text,attachments,vk_cmid FROM staff_replies WHERE event_key=? OR (peer_id=? AND vk_cmid>0 AND vk_cmid=?)',(event_key,peer_id,vk_cmid)).fetchone()
            if existing:
                body = text if text is not None else existing['text']
                files = json.dumps(attachments,ensure_ascii=False) if attachments is not None else existing['attachments']
                if body!=existing['text'] or files!=existing['attachments'] or vk_cmid>existing['vk_cmid']:
                    con.execute('UPDATE staff_replies SET text=?,attachments=?,vk_cmid=max(vk_cmid,?),updated_at=? WHERE id=?',(body,files,vk_cmid,time.time(),existing['id']))
                return False
            if not latest:
                return False
            previous = con.execute('SELECT max(vk_cmid) cmid,max(created_at) sent_at FROM staff_replies WHERE peer_id=?', (peer_id,)).fetchone()
            con.execute('INSERT INTO staff_replies(event_key,peer_id,vk_cmid,admin_id,created_at,text,attachments,source,staff_name,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)', (event_key,peer_id,vk_cmid,admin_id,sent_at,text or '',json.dumps(attachments or [],ensure_ascii=False),source,staff_name,time.time()))
            if history_only:
                return True
            if (vk_cmid and previous['cmid'] and vk_cmid <= previous['cmid']) or (not vk_cmid and previous['sent_at'] and sent_at < previous['sent_at']):
                return True
            last_closed = con.execute("SELECT max(closed_at) FROM tickets WHERE peer_id=? AND status='closed'", (peer_id,)).fetchone()[0]
            if last_closed and sent_at <= last_closed:
                return True
            newer_incoming = (vk_cmid < latest['vk_cmid']) if vk_cmid and latest['vk_cmid'] else sent_at < (latest['vk_date'] or int(latest['created_at']))
            if basis_request_id is not None and latest['id'] > basis_request_id:
                newer_incoming = True
            status = 'open' if newer_incoming else 'waiting_user'
            ensure_session(con,peer_id,time.time())
            if active:
                con.execute('UPDATE tickets SET status=?,last_staff_reply_at=max(coalesce(last_staff_reply_at,0),?) WHERE id=?', (status,sent_at,active['id']))
            else:
                con.execute('INSERT INTO tickets(peer_id,request_id,reason,status,created_at,last_staff_reply_at) VALUES (?,?,?,?,?,?)', (peer_id,latest['id'],'staff_joined',status,time.time(),sent_at))
            self._refresh_ticket_topics(con,peer_id)
            con.execute('UPDATE conversations SET human_mode=1,generation=generation+1,updated_at=? WHERE peer_id=?', (time.time(),peer_id))
            cancel_pending(con,peer_id,'staff_joined')
            con.execute("UPDATE requests SET status='cancelled',reason='staff_joined',reply=NULL WHERE peer_id=? AND status='ready' AND outcome IN ('answer','clarify','handoff','thanks','greeting','flight_info')", (peer_id,))
            return True

    def handoff(self, request, reason):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            ensure_session(con,request['peer_id'],time.time())
            con.execute("UPDATE conversations SET human_mode=1,generation=generation+1,updated_at=? WHERE peer_id=?", (time.time(), request["peer_id"]))
            cancel_pending(con,request['peer_id'],'handoff')
            con.execute("INSERT OR IGNORE INTO tickets(peer_id,request_id,reason,created_at) VALUES (?,?,?,?)", (request["peer_id"], request["id"], reason, time.time()))
            self._refresh_ticket_topics(con,request['peer_id'])

    def _check_dialogue_revision(self, con, peer_id, expected_revision):
        if expected_revision is not None:
            from .operator import OperatorInbox
            if OperatorInbox(self).revision(peer_id,con)!=expected_revision:
                raise ValueError('Диалог изменился. Проверьте новые сообщения и повторите действие.')

    def take_dialogue(self, peer_id, name, expected_revision):
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            self._check_dialogue_revision(con,peer_id,expected_revision)
            latest=con.execute('SELECT id FROM requests WHERE peer_id=? ORDER BY id DESC LIMIT 1',(peer_id,)).fetchone()
            if not latest:
                raise ValueError('В диалоге пока нет сообщений.')
            now=time.time()
            ensure_session(con,peer_id,now)
            con.execute("INSERT OR IGNORE INTO tickets(peer_id,request_id,reason,status,created_at) VALUES (?,?,'staff_joined','open',?)",(peer_id,latest['id'],now))
            con.execute("UPDATE tickets SET assigned_to=?,taken_at=? WHERE peer_id=? AND status IN ('open','waiting_user')",(name,now,peer_id))
            con.execute('UPDATE conversations SET human_mode=1,generation=generation+1,updated_at=? WHERE peer_id=?',(now,peer_id))
            con.execute("UPDATE requests SET status='cancelled',reply=NULL,reason='staff_joined' WHERE peer_id=? AND status='ready' AND outcome IN ('answer','clarify','handoff','thanks','greeting','flight_info')",(peer_id,))
            cancel_pending(con,peer_id,'staff_joined')
            self._refresh_ticket_topics(con,peer_id)
            con.execute("INSERT INTO audit(action,target,created_at) VALUES ('take_dialogue',?,?)",(str(peer_id),now))

    def pause_bot(self, peer_id, paused, expected_revision):
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            self._check_dialogue_revision(con,peer_id,expected_revision)
            now=time.time()
            con.execute('UPDATE conversations SET bot_paused_until=?,generation=generation+1,updated_at=? WHERE peer_id=?',(now+86400 if paused else 0,now,peer_id))
            if paused:
                con.execute("UPDATE requests SET status='cancelled',reply=NULL,reason='bot_paused' WHERE peer_id=? AND status='ready' AND outcome NOT IN ('consent_revoked','consent_accepted','feedback')",(peer_id,))
            cancel_pending(con,peer_id,'bot_paused')
            con.execute('INSERT INTO audit(action,target,created_at) VALUES (?,?,?)',('pause_bot' if paused else 'unpause_bot',str(peer_id),now))

    def resume(self, peer_id, expected_revision=None):
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            self._check_dialogue_revision(con,peer_id,expected_revision)
            c=con.execute('SELECT bot_paused_until FROM conversations WHERE peer_id=?',(peer_id,)).fetchone()
            if c and c['bot_paused_until']>time.time():
                raise ValueError('Сначала снимите ограничение автоответов в разделе «Спам и ограничения».')
            if con.execute("SELECT 1 FROM operator_outbox WHERE peer_id=? AND status IN ('pending','sending','failed')",(peer_id,)).fetchone():
                raise ValueError('Сначала дождитесь отправки ответа или отмените неотправленный ответ.')
            self._refresh_ticket_topics(con,peer_id)
            con.execute("UPDATE conversations SET human_mode=0,generation=generation+1,clarification_count=0,updated_at=? WHERE peer_id=?", (time.time(), peer_id))
            con.execute("""UPDATE requests SET status='done',outcome='human_mode',reason='closed_by_staff',reply=NULL,updated_at=?
                WHERE peer_id=? AND status IN ('pending','processing')
                AND coalesce(json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END,'$.action'),'') NOT IN ('agree','revoke','feedback')""",(time.time(),peer_id))
            con.execute("UPDATE requests SET status='cancelled',reply=NULL,reason='closed_by_staff' WHERE peer_id=? AND status='ready' AND outcome IN ('answer','clarify','handoff','thanks','greeting','flight_info','rate_limited','input_rejected')",(peer_id,))
            cancel_pending(con,peer_id,'returned_to_bot')
            con.execute("UPDATE tickets SET status='closed',closed_at=?,closure_reason='manual' WHERE peer_id=? AND status IN ('open','waiting_user')", (time.time(), peer_id))
            close_session(con,peer_id,'manual',time.time())
            con.execute("INSERT INTO audit(action,target,created_at) VALUES ('resume_bot',?,?)", (str(peer_id), time.time()))

    def pause_auto_close(self, peer_id, ticket_id, paused):
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            changed=con.execute("UPDATE tickets SET auto_close_paused=? WHERE id=? AND peer_id=? AND status IN ('open','waiting_user')",(int(paused),ticket_id,peer_id)).rowcount
            if not changed:
                raise ValueError('Обращение уже завершено. Обновите страницу.')
            con.execute("INSERT INTO audit(action,target,created_at) VALUES (?,?,?)",('ticket_auto_close_paused' if paused else 'ticket_auto_close_enabled',str(ticket_id),time.time()))

    def reopen_waiting_ticket(self, ticket_id, last_staff_reply_at):
        with self.connect() as con:
            return con.execute("UPDATE tickets SET status='open' WHERE id=? AND status='waiting_user' AND last_staff_reply_at<=?",(ticket_id,last_staff_reply_at)).rowcount

    def expire_waiting_ticket(self, ticket_id, *, now=None):
        """Вызывать после успешной сверки с VK, без отправки сообщений пассажиру."""
        now=time.time() if now is None else now
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            hours=int(con.execute("SELECT value FROM settings WHERE key='waiting_user_timeout_hours'").fetchone()[0])
            if hours<=0:
                return False
            ticket=con.execute("""SELECT * FROM tickets t WHERE id=? AND status='waiting_user'
                AND auto_close_paused=0 AND last_staff_reply_at IS NOT NULL AND last_staff_reply_at<=?
                AND NOT EXISTS (SELECT 1 FROM conversations c WHERE c.peer_id=t.peer_id AND c.bot_paused_until>?)
                AND NOT EXISTS (SELECT 1 FROM requests r WHERE r.peer_id=t.peer_id AND r.status IN ('pending','processing','ready','sending'))
                AND NOT EXISTS (SELECT 1 FROM operator_outbox o WHERE o.peer_id=t.peer_id AND o.status IN ('pending','sending','failed'))""",(ticket_id,now-hours*3600,now)).fetchone()
            if not ticket:
                return False
            self._refresh_ticket_topics(con,ticket['peer_id'])
            con.execute("UPDATE tickets SET status='closed',closure_reason='inactivity',closed_at=? WHERE id=?",(now,ticket_id))
            close_session(con,ticket['peer_id'],'inactivity',now)
            con.execute("UPDATE conversations SET human_mode=0,generation=generation+1,clarification_count=0,updated_at=? WHERE peer_id=?",(now,ticket['peer_id']))
            con.execute("INSERT INTO audit(action,target,created_at) VALUES ('ticket_closed_inactive',?,?)",(str(ticket_id),now))
            return True

    def save_image_descriptions(self, request, generation, descriptions):
        with self.connect() as con:
            result=con.execute("""UPDATE requests SET image_descriptions=? WHERE id=? AND peer_id=?
                AND EXISTS (SELECT 1 FROM conversations c WHERE c.peer_id=requests.peer_id
                AND c.human_mode=0 AND c.consent_state='accepted' AND c.generation=?
                AND c.consent_version=(SELECT value FROM settings WHERE key='consent_version'))""",
                (json.dumps(descriptions,ensure_ascii=False),request['id'],request['peer_id'],generation))
            return bool(result.rowcount)

    def has_recent_reply(self, peer_id, request_id):
        # Social continuity survives thanks, restart, and a handover to/from staff.
        cutoff = time.time() - GREETING_IDLE_SECONDS
        return bool(self.one("""SELECT 1 FROM requests
            WHERE peer_id=? AND id<? AND status='sent' AND reply IS NOT NULL
                AND updated_at>=?
            UNION ALL SELECT 1 FROM staff_replies WHERE peer_id=? AND created_at>=?
            LIMIT 1""", (peer_id, request_id, cutoff, peer_id, cutoff)))

    def history(self, peer_id, limit=4):
        generation = self.conversation(peer_id)["generation"]
        rows=list(reversed(self.all("SELECT text,reply,image_descriptions FROM requests WHERE peer_id=? AND status='sent' AND ai_generation=? AND session_id=(SELECT id FROM dialogue_sessions WHERE peer_id=requests.peer_id AND closed_at IS NULL) AND outcome IN ('answer','clarify') AND flight_query IS NULL AND updated_at>=? AND id>coalesce((SELECT max(id) FROM requests WHERE peer_id=? AND outcome='thanks' AND status='sent'),0) ORDER BY id DESC LIMIT ?", (peer_id, generation,time.time()-1800, peer_id, limit))))
        for row in rows:
            images=json.loads(row.pop('image_descriptions'))
            if images:
                row['image_descriptions']=images
        return rows

    def purge(self):
        cutoff = time.time() - max(1, int(self.setting("retention_days"))) * 86400
        self.execute("UPDATE requests SET text='',attachments='[]',reply=NULL,image_descriptions='[]',flight_query=NULL,flight_valid_until=NULL WHERE created_at<? AND status NOT IN ('pending','processing','ready','sending')", (cutoff,))
        self.execute("UPDATE staff_replies SET text='',attachments='[]' WHERE created_at<?",(cutoff,))
        self.execute("UPDATE operator_outbox SET text='' WHERE created_at<? AND status IN ('sent','cancelled')",(cutoff,))
        self.execute('DELETE FROM protection_buckets WHERE updated_at<?',(cutoff,))
        self.execute('DELETE FROM protection_events WHERE last_at<?',(cutoff,))

    def backup(self, target):
        destination = sqlite3.connect(target)
        try:
            with self.connect() as con:
                con.backup(destination)
        finally:
            destination.close()
        Path(target).chmod(0o600)
