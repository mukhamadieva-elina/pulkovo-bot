"""Ответы сотрудника: очередь отправки, защита от повторов и общая переписка."""

import hashlib
import json
import re
import time
from .feedback import cancel_pending, message_random_id, star_rating, survey_options
from .sessions import ensure_session, CLOSURE_LABELS


def control_label(payload):
    try:
        data=json.loads(payload or '{}')
    except (TypeError, ValueError):
        return ''
    if not isinstance(data,dict) or not isinstance(data.get('action'),str):
        return ''
    if data['action']=='feedback':
        stars=star_rating(data.get('rating'))
        if stars is not None:
            return f'Оценка: {stars} из 5 ⭐'
        return 'Оценка «Хорошо»' if data.get('rating')=='good' else 'Оценка «Плохо»' if data.get('rating')=='bad' else 'Оценка помощи бота'
    if data['action']=='agree' and data.get('source')=='text':
        return 'Согласие отправлено текстом'
    return {
        'agree':'Выбрано «Согласен»',
        'decline':'Выбрано общение с сотрудником',
        'operator':'Запрос помощи сотрудника',
        'revoke':'Запрос на отзыв согласия',
    }.get(data['action'],'')


class OperatorInbox:
    def __init__(self, db):
        self.db = db

    def revision(self, peer_id, con=None):
        if con is None:
            with self.db.connect() as connection:
                return self.revision(peer_id,connection)
        parts=[]
        for query in (
            "SELECT human_mode,generation,bot_paused_until,bot_paused_until>CAST(strftime('%s','now') AS REAL) FROM conversations WHERE peer_id=?",
            'SELECT reason,count,last_at FROM protection_events WHERE peer_id=? ORDER BY reason',
            'SELECT id,status,note,topic_version,auto_close_paused,closure_reason,last_staff_reply_at FROM tickets WHERE peer_id=? ORDER BY id',
            'SELECT p.updated_at FROM conversations c LEFT JOIN vk_profiles p ON p.user_id=c.user_id WHERE c.peer_id=?',
            'SELECT id,status,updated_at FROM requests WHERE peer_id=? ORDER BY id DESC LIMIT 100',
            'SELECT id,updated_at FROM staff_replies WHERE peer_id=? ORDER BY id DESC LIMIT 100',
            'SELECT id,status,updated_at FROM operator_outbox WHERE peer_id=? ORDER BY id DESC LIMIT 100',
            'SELECT id,status,rating,rating_stars,updated_at FROM feedback_surveys WHERE peer_id=? ORDER BY id DESC LIMIT 100',
            'SELECT id,last_activity_at,closed_at,closure_reason FROM dialogue_sessions WHERE peer_id=? ORDER BY id DESC LIMIT 100',
        ):
            parts.append([tuple(row) for row in con.execute(query,(peer_id,))])
        return hashlib.sha256(json.dumps(parts).encode()).hexdigest()

    def queue(self, peer_id, text, name, client_key, expected_revision):
        text=text.strip();name=name.strip()[:80] or 'Сотрудник'
        if not text or len(text)>3500:
            raise ValueError('Введите ответ длиной от 1 до 3500 символов.')
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,100}',client_key):
            raise ValueError('Обновите страницу перед отправкой.')
        with self.db.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            old=con.execute('SELECT * FROM operator_outbox WHERE client_key=?',(client_key,)).fetchone()
            if old:
                if old['peer_id']!=peer_id or old['text']!=text:
                    raise ValueError('Эта форма уже отправлена. Обновите страницу.')
                return old['id']
            if self.revision(peer_id,con)!=expected_revision:
                raise ValueError('Переписка изменилась. Проверьте новые сообщения перед отправкой; ваш текст сохранён в поле ответа.')
            latest=con.execute("""SELECT * FROM requests WHERE peer_id=? AND coalesce(json_extract(
                CASE WHEN json_valid(payload) THEN payload ELSE '{}' END,'$.action'),'')!='feedback' ORDER BY id DESC LIMIT 1""",(peer_id,)).fetchone()
            if not latest or not 0<peer_id<2_000_000_000:
                raise ValueError('Нет личного диалога для отправки ответа.')
            if con.execute("SELECT 1 FROM operator_outbox WHERE peer_id=? AND status IN ('pending','sending','failed')",(peer_id,)).fetchone():
                raise ValueError('Предыдущий ответ ещё не отправлен. Дождитесь отправки или отмените его.')
            now=time.time()
            ensure_session(con,peer_id,now)
            con.execute("INSERT OR IGNORE INTO tickets(peer_id,request_id,reason,status,created_at) VALUES (?,?,'staff_joined','open',?)",(peer_id,latest['id'],now))
            ticket=con.execute("SELECT * FROM tickets WHERE peer_id=? AND status IN ('open','waiting_user')",(peer_id,)).fetchone()
            self.db._refresh_ticket_topics(con,peer_id)
            con.execute('UPDATE conversations SET human_mode=1,generation=generation+1,updated_at=? WHERE peer_id=?',(now,peer_id))
            cancel_pending(con,peer_id,'staff_joined')
            con.execute("UPDATE requests SET status='cancelled',reason='staff_joined',reply=NULL WHERE peer_id=? AND status='ready' AND outcome IN ('answer','clarify','handoff','thanks','greeting','farewell','flight_info')",(peer_id,))
            random_id=message_random_id(con)
            row=con.execute('INSERT INTO operator_outbox(client_key,peer_id,ticket_id,basis_request_id,text,staff_name,random_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',(client_key,peer_id,ticket['id'],latest['id'],text,name,random_id,now,now))
            con.execute("INSERT INTO audit(action,target,created_at) VALUES ('operator_reply_queued',?,?)",(str(row.lastrowid),now))
            return row.lastrowid

    def match(self, message):
        # Проверяется только исходящее сообщение сообщества (в вызывающем коде).
        peer=message.get('peer_id')
        if message.get('random_id'):
            row=self.db.one('SELECT * FROM operator_outbox WHERE peer_id=? AND random_id=?',(peer,message['random_id']))
            if row:
                return row
        try:
            payload=message.get('payload') or {}
            payload=json.loads(payload) if isinstance(payload,str) else payload
            ident=payload.get('pulkovo_staff') if isinstance(payload,dict) else None
        except (ValueError,TypeError):
            ident=None
        return self.db.one('SELECT * FROM operator_outbox WHERE peer_id=? AND id=?',(peer,ident)) if isinstance(ident,int) else None

    def confirm(self, row, message):
        cmid=int(message.get('conversation_message_id') or 0)
        sent_at=int(message.get('date') or time.time())
        self.db.staff_replied(row['peer_id'],f"operator:{row['id']}",0,cmid,sent_at,text=row['text'],source='admin',staff_name=row['staff_name'],basis_request_id=row['basis_request_id'])
        message_id=int(message.get('id') or 0)
        self.db.execute("UPDATE operator_outbox SET status='sent',vk_message_id=max(vk_message_id,?),vk_cmid=max(vk_cmid,?),error='',updated_at=? WHERE id=? AND (status!='sent' OR vk_message_id<? OR vk_cmid<?)",(message_id,cmid,time.time(),row['id'],message_id,cmid))

    def deliver(self, api):
        with self.db.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row=con.execute("SELECT * FROM operator_outbox WHERE (status='pending' AND (attempts=0 OR updated_at<?)) OR (status='sending' AND updated_at<?) ORDER BY id LIMIT 1",(time.time()-10,time.time()-90)).fetchone()
            if not row:
                return False
            row=dict(row)
            con.execute("UPDATE operator_outbox SET status='sending',attempts=attempts+1,updated_at=? WHERE id=?",(time.time(),row['id']))
        try:
            result=api.messages.send(peer_id=row['peer_id'],message=row['text'],random_id=row['random_id'],payload=json.dumps({'pulkovo_staff':row['id']}),keyboard=json.dumps({'one_time':True,'buttons':[]}))
        except Exception:
            self.db.execute("UPDATE operator_outbox SET status=?,error='Не удалось подтвердить отправку в VK.',updated_at=? WHERE id=? AND status='sending'",('failed' if row['attempts']+1>=3 else 'pending',time.time(),row['id']))
            return True
        message={'id':result,'date':int(time.time())}
        try:
            response=api.messages.getById(message_ids=result)
            found=next((m for m in response.get('items',[]) if m.get('peer_id')==row['peer_id'] and m.get('id')==result),None)
            if found:
                message=found
        except Exception:
            # Успешный messages.send уже подтверждает отправку. Детали подтянет Long Poll.
            pass
        self.confirm(row,message)
        return True

    def control(self, peer_id, ident, action):
        with self.db.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row=con.execute('SELECT * FROM operator_outbox WHERE id=? AND peer_id=?',(ident,peer_id)).fetchone()
            if not row or row['status'] not in ('failed','pending'):
                raise ValueError('Ответ уже отправлен или отправляется. Обновите страницу.')
            if action=='retry':
                if row['status']!='failed':
                    raise ValueError('Ответ уже ожидает отправки.')
                status='pending'
            elif action=='cancel':
                status='cancelled'
            else:
                raise ValueError('Неизвестное действие.')
            # При повторе random_id прежний: VK не создаст второе сообщение.
            con.execute('UPDATE operator_outbox SET status=?,attempts=0,error=\'\',updated_at=? WHERE id=?',(status,time.time(),ident))

    def timeline(self, peer_id, ticket_id=None, *, limit=150, include_sessions=False):
        result=[]
        bounds=''
        params=(peer_id,)
        end=1e30
        if ticket_id is not None:
            ticket=self.db.ticket_details(peer_id,ticket_id)
            if not ticket:
                raise ValueError('Обращение не найдено в этом диалоге.')
            end=ticket['closed_at'] if ticket['closed_at'] is not None else end
            bounds=' AND created_at>? AND created_at<=?'
            params+=(ticket['period_start'],end)
        rows=self.db.all('SELECT * FROM requests WHERE peer_id=?'+bounds+' ORDER BY id DESC LIMIT 100',params)
        for row in rows:
            attachments=json.loads(row['attachments'])
            action=control_label(row['payload'])
            result.append(dict(id=f"request-{row['id']}",author='Пассажир',kind='user',text=row['text'] or action or ('' if attachments else 'Текст не сохранён'),control=bool(action and not row['text']),attachments=attachments,created_at=row['vk_date'] or row['created_at'],status='',cmid=row['vk_cmid']))
            if row['reply'] and row['status']=='sent' and row['updated_at']<=end:
                result.append(dict(id=f"bot-{row['id']}",author='Бот',kind='bot',text=row['reply'],attachments=[],created_at=row['updated_at'],status='sent',cmid=0))
        for row in self.db.all('SELECT * FROM staff_replies WHERE peer_id=?'+bounds+' ORDER BY created_at DESC LIMIT 100',params):
            attachments=json.loads(row['attachments'])
            result.append(dict(id=f"staff-{row['id']}",author=row['staff_name'] or 'Сотрудник в VK',kind='staff',text=row['text'] or ('' if attachments else 'Ответ сотрудника — содержание доступно в VK'),attachments=attachments,created_at=row['created_at'],status='sent',cmid=row['vk_cmid']))
        outbox_filter=' AND ticket_id=?' if ticket_id is not None else ''
        outbox_params=(peer_id,ticket_id) if ticket_id is not None else (peer_id,)
        for row in self.db.all("SELECT * FROM operator_outbox WHERE peer_id=? AND status!='sent'"+outbox_filter+' ORDER BY id DESC LIMIT 30',outbox_params):
            result.append(dict(id=f"outbox-{row['id']}",author=row['staff_name'],kind='staff',text=row['text'],attachments=[],created_at=row['created_at'],status=row['status'],outbox_id=row['id'],error=row['error'],cmid=0))
        survey_bounds=' AND sent_at>? AND sent_at<=?' if ticket_id is not None else ''
        for row in self.db.all("SELECT * FROM feedback_surveys WHERE peer_id=? AND sent_at IS NOT NULL"+survey_bounds+' ORDER BY sent_at DESC LIMIT 30',params):
            buttons=' · '.join(label for label, _, _ in survey_options(row))
            result.append(dict(id=f"survey-{row['id']}",author='Бот',kind='bot',text=row['text']+'\n'+buttons,attachments=[],created_at=row['sent_at'],status='sent',cmid=0))
        visible=sorted(result,key=lambda m:(m['created_at'],m['cmid']))[-limit:]
        if not visible or ticket_id is not None or not include_sessions:
            return visible
        first=visible[0]['created_at']
        for session in self.db.all('SELECT * FROM dialogue_sessions WHERE peer_id=? AND (closed_at IS NULL OR closed_at>=?) ORDER BY id DESC LIMIT 100',(peer_id,first)):
            # Keep the heading when the start of a long session is outside the last 30 messages.
            # VK timestamps are rounded down to seconds; put the boundary before the first input.
            start=int(session['started_at'])
            visible.append(dict(id=f"session-start-{session['id']}",kind='boundary',created_at=max(start,first),cmid=-1,
                text=f"Сессия № {session['id']}"+(' · продолжение' if session['started_at']<first else ' · начало'),boundary_at=session['started_at']))
            if session['closed_at'] is not None:
                visible.append(dict(id=f"session-end-{session['id']}",kind='boundary',created_at=session['closed_at'],cmid=10**12,
                    text=f"Сессия № {session['id']} завершена · {CLOSURE_LABELS[session['closure_reason']]}",boundary_at=session['closed_at']))
        return sorted(visible,key=lambda m:(m['created_at'],m['cmid']))
