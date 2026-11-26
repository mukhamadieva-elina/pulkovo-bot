"""Persistent, optional feedback invitations after an hour of silence."""

import json
import logging
import secrets
import time
from .sessions import record_delivery, close_after_rating

DELAY = 60 * 60
COOLDOWN = 24 * 60 * 60
QUESTION = 'Как вы оцениваете помощь бота?\n\nВыберите от 1 до 5 звезд: 1 — совсем не помог, 5 — полностью помог.'


def star_rating(value):
    # Accept JSON numbers and the strings normalized by Engine.receive, but
    # never booleans, floats or loosely parsed values such as "1.0".
    if type(value) in (int, str) and str(value) in ('1', '2', '3', '4', '5'):
        return int(value)
    return None


def survey_options(survey):
    if survey['rating_scale'] == 'stars':
        return [(f'{n} ⭐', n, 'secondary') for n in range(1, 6)]
    return [('Хорошо', 'good', 'positive'), ('Плохо', 'bad', 'negative')]


def is_feedback_payload(payload):
    try:
        data = json.loads(payload) if isinstance(payload, str) else payload
        return isinstance(data, dict) and data.get('action') == 'feedback'
    except (TypeError, ValueError):
        return False


def message_random_id(con):
    while True:
        value = secrets.randbelow(2**31-1)+1
        if not con.execute('SELECT 1 FROM requests WHERE random_id=? UNION ALL SELECT 1 FROM operator_outbox WHERE random_id=? UNION ALL SELECT 1 FROM feedback_surveys WHERE random_id=?', (value,)*3).fetchone():
            return value


def cancel_pending(con, peer_id, reason):
    con.execute("UPDATE feedback_surveys SET status='cancelled',reason=?,updated_at=? WHERE peer_id=? AND status IN ('pending','sending')", (reason, time.time(), peer_id))


def record_activity(con, peer_id, request_id, now):
    # A request already in flight cannot safely be re-armed under the same ID.
    con.execute("UPDATE feedback_surveys SET status='cancelled',reason='new_activity',updated_at=? WHERE peer_id=? AND status='sending'", (now, peer_id))
    con.execute("UPDATE feedback_surveys SET activity_request_id=?,last_activity_at=?,due_at=?,retry_at=0,updated_at=? WHERE peer_id=? AND status='pending'", (request_id, now, now+DELAY, now, peer_id))


def survey_keyboard(survey):
    return json.dumps({'inline': True, 'buttons': [[
        {'action': {'type': 'text', 'label': label,
                    'payload': json.dumps({'action': 'feedback', 'token': survey['token'], 'rating': rating})},
         'color': color}
        for label, rating, color in survey_options(survey)
    ]]}, ensure_ascii=False)


class Feedback:
    def __init__(self, db):
        self.db = db
        self.vote_checks = {}

    def record_bot_delivery(self, request, *, now=None):
        """Commit delivery and arm/cancel the invitation in one transaction."""
        now = time.time() if now is None else now
        peer = request['peer_id']
        with self.db.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            con.execute("UPDATE requests SET status='sent',send_attempts=send_attempts+1,updated_at=? WHERE id=?", (now, request['id']))
            if not request['outcome'].startswith('feedback'):
                record_delivery(con,request['id'],now)
            if request['outcome'] in ('thanks', 'farewell'):
                con.execute("UPDATE feedback_surveys SET activity_request_id=?,last_activity_at=?,due_at=?,updated_at=? WHERE peer_id=? AND generation=? AND status='pending'", (request['id'], now, now+DELAY, now, peer, request['ai_generation']))
                return
            if request['outcome'].startswith('feedback'):
                return
            if request['outcome'] != 'answer':
                cancel_pending(con, peer, 'not_a_final_answer')
                return
            cancel_pending(con, peer, 'new_answer')
            state = con.execute('SELECT * FROM conversations WHERE peer_id=?', (peer,)).fetchone()
            enabled = con.execute("SELECT value FROM settings WHERE key='feedback_enabled'").fetchone()
            if not enabled or enabled[0] != 'true' or state['human_mode'] or state['bot_paused_until']>now or state['generation'] != request['ai_generation']:
                return
            random_id = message_random_id(con)
            latest = con.execute("""SELECT max(id) FROM requests WHERE peer_id=? AND coalesce(json_extract(
                CASE WHEN json_valid(payload) THEN payload ELSE '{}' END,'$.action'),'')!='feedback'""", (peer,)).fetchone()[0]
            con.execute("""INSERT OR IGNORE INTO feedback_surveys
                (peer_id,request_id,generation,activity_request_id,last_activity_at,due_at,token,random_id,text,created_at,updated_at,rating_scale)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,'stars')""", (peer, request['id'], state['generation'], latest, now, now+DELAY, secrets.token_urlsafe(24), random_id, QUESTION, now, now))

    def vote(self, peer_id, payload, *, now=None):
        now = time.time() if now is None else now
        with self.db.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            return self._vote(con, peer_id, payload, now)

    def _vote(self, con, peer_id, payload, now, *, close=True):
        token, rating = payload.get('token'), payload.get('rating')
        stars = star_rating(rating)
        if not isinstance(token, str) or len(token) > 80 or (stars is None and rating not in ('good', 'bad')):
            return 'invalid'
        # A click with the unguessable token also confirms receipt after a
        # lost send response, including while a retry is pending/cancelled.
        row = con.execute("SELECT * FROM feedback_surveys WHERE peer_id=? AND token=? AND (sent_at IS NOT NULL OR attempted_at IS NOT NULL)", (peer_id, token)).fetchone()
        if not row:
            return 'invalid'
        if (row['rating_scale'] == 'stars' and stars is None) or (row['rating_scale'] == 'binary' and stars is not None):
            return 'invalid'
        if row['rating'] is not None or row['rating_stars'] is not None:
            return 'duplicate'
        con.execute("UPDATE feedback_surveys SET rating=?,rating_stars=?,rated_at=?,status='sent',sent_at=coalesce(sent_at,attempted_at,?),updated_at=? WHERE id=?", (rating if stars is None else None, stars, now, now, now, row['id']))
        if close:
            close_after_rating(con,row,now)
        return 'accepted'

    def sync_votes(self, api, engine, *, now=None, stop=None):
        """Recover missed button clicks without replaying questions or sending replies."""
        now = time.time() if now is None else now
        days = min(30, max(1, int(self.db.setting('retention_days'))))
        surveys = self.db.all("""SELECT * FROM feedback_surveys WHERE rating IS NULL
            AND rating_stars IS NULL AND coalesce(sent_at,attempted_at)>=?""", (now-days*86400,))
        by_peer = {}
        for survey in surveys:
            by_peer.setdefault(survey['peer_id'], []).append(survey)
        self.vote_checks = {peer:at for peer,at in self.vote_checks.items() if peer in by_peer}
        due = sorted((peer for peer in by_peer if now-self.vote_checks.get(peer, 0)>=60),
                     key=lambda peer:self.vote_checks.get(peer, 0))[:10]
        recovered = 0
        for peer in due:
            if stop is not None and stop.is_set():
                break
            self.vote_checks[peer] = now
            try:
                items = api.messages.getHistory(peer_id=peer, count=100).get('items', [])
                items = [m for m in items if m.get('peer_id')==peer and type(m.get('date')) is int
                         and 0<m['date']<=now+60 and type(m.get('conversation_message_id') or m.get('id')) is int
                         and (m.get('conversation_message_id') or m['id'])>0]
                items.sort(key=lambda m:(m['date'], m.get('conversation_message_id') or m['id']))
                tokens = {s['token']:s for s in by_peer[peer]}
                for item in items:
                    if item.get('from_id')!=peer or not is_feedback_payload(item.get('payload')):
                        continue
                    data = item['payload']
                    data = json.loads(data) if isinstance(data, str) else data
                    token = data.get('token')
                    if not isinstance(token, str) or token not in tokens:
                        continue
                    survey = tokens[token]
                    if item['date']<int(survey['attempted_at'] or survey['sent_at']):
                        continue
                    key = f"{engine.config.group_id}:{peer}:{item.get('conversation_message_id') or item['id']}"
                    # Do not close work when history contains activity which
                    # Long Poll has not yet delivered to the bot.
                    close = True
                    for other in items:
                        if other['date']<int(survey['last_activity_at']):
                            continue
                        if other.get('from_id')==peer and not is_feedback_payload(other.get('payload')):
                            other_key=f"{engine.config.group_id}:{peer}:{other.get('conversation_message_id') or other['id']}"
                            if not self.db.one('SELECT id FROM requests WHERE event_key=?',(other_key,)):
                                close=False
                        elif other.get('from_id')==-engine.config.group_id and not engine.is_own_reply(other):
                            close=False
                    payload = {'action':'feedback','token':token,'rating':data.get('rating')}
                    with self.db.connect() as con:
                        con.execute('BEGIN IMMEDIATE')
                        if con.execute('SELECT 1 FROM requests WHERE event_key=?',(key,)).fetchone():
                            continue
                        result = self._vote(con, peer, payload, item['date'], close=close)
                        if result=='invalid':
                            continue
                        basis=con.execute('SELECT session_id FROM requests WHERE id=?',(survey['request_id'],)).fetchone()
                        con.execute("""INSERT INTO requests
                            (event_key,peer_id,user_id,input_type,payload,status,outcome,reason,random_id,
                             created_at,updated_at,vk_cmid,vk_date,session_id)
                            VALUES (?,?,?,'text',?,'done',?,'feedback_recovered',?,?,?,?,?,?)""",
                            (key,peer,peer,json.dumps(payload),'feedback' if result=='accepted' else 'feedback_duplicate',
                             message_random_id(con),item['date'],now,item.get('conversation_message_id') or 0,item['date'],basis['session_id']))
                        recovered += 1
            except Exception:
                logging.warning('Не удалось сверить оценки с VK; повтор в следующем цикле.')
        return recovered

    def _eligible(self, con, survey, now):
        state = con.execute('SELECT * FROM conversations WHERE peer_id=?', (survey['peer_id'],)).fetchone()
        settings = dict(con.execute('SELECT key,value FROM settings'))
        if (settings.get('feedback_enabled') != 'true' or settings.get('consent_ready') != 'true' or not state or state['human_mode'] or state['bot_paused_until']>now
                or state['generation'] != survey['generation'] or state['clarification_count']
                or state['consent_state'] != 'accepted' or state['consent_version'] != settings['consent_version']):
            return 'dialogue_changed'
        if now-survey['last_activity_at'] >= COOLDOWN:
            return 'stale_invitation'
        latest = con.execute("""SELECT * FROM requests WHERE peer_id=? AND coalesce(json_extract(
            CASE WHEN json_valid(payload) THEN payload ELSE '{}' END,'$.action'),'')!='feedback' ORDER BY id DESC LIMIT 1""", (survey['peer_id'],)).fetchone()
        if not latest or latest['id'] != survey['activity_request_id']:
            return 'new_activity'
        if latest['status'] in ('pending', 'processing', 'ready', 'sending'):
            return 'wait'
        if latest['status'] != 'sent' or latest['outcome'] not in ('answer', 'thanks', 'farewell'):
            return 'not_a_final_answer'
        if con.execute("SELECT 1 FROM requests WHERE peer_id=? AND status IN ('pending','processing','ready','sending')", (survey['peer_id'],)).fetchone():
            return 'wait'
        if con.execute("SELECT 1 FROM operator_outbox WHERE peer_id=? AND status IN ('pending','sending','failed')", (survey['peer_id'],)).fetchone():
            return 'dialogue_changed'
        if con.execute("SELECT 1 FROM feedback_surveys WHERE peer_id=? AND id!=? AND coalesce(sent_at,attempted_at)>?", (survey['peer_id'], survey['id'], now-COOLDOWN)).fetchone():
            return 'daily_limit'
        return ''

    def _claim(self, now):
        with self.db.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            # Recover a worker interrupted after claiming, without losing the
            # stable random_id. VK calls have a much shorter network timeout.
            con.execute("UPDATE feedback_surveys SET status='pending' WHERE status='sending' AND updated_at<?", (now-300,))
            rows = con.execute("SELECT * FROM feedback_surveys WHERE status='pending' AND due_at<=? AND retry_at<=? ORDER BY due_at,id LIMIT 100", (now, now)).fetchall()
            for row in rows:
                reason = self._eligible(con, row, now)
                if reason:
                    con.execute('UPDATE feedback_surveys SET status=?,reason=?,retry_at=?,updated_at=? WHERE id=?', ('pending' if reason == 'wait' else 'cancelled', reason, now+60, now, row['id']))
                    continue
                con.execute("UPDATE feedback_surveys SET status='sending',updated_at=? WHERE id=?", (now, row['id']))
                return dict(row)
            return None

    def _defer(self, ident, reason, now):
        self.db.execute("UPDATE feedback_surveys SET status='pending',retry_at=?,reason=?,updated_at=? WHERE id=? AND status='sending'", (now+60, reason, now, ident))

    def _history(self, survey, api, engine):
        """Check VK before sending, including activity not yet seen by Long Poll."""
        items = api.messages.getHistory(peer_id=survey['peer_id'], count=100).get('items', [])
        items = [item for item in items if item.get('peer_id') == survey['peer_id']]
        if not items:
            return 'wait', None
        for item in items:
            try:
                payload = item.get('payload') or '{}'
                payload = json.loads(payload) if isinstance(payload, str) else payload
                marker = payload.get('pulkovo_bot') if isinstance(payload, dict) else None
            except (TypeError, ValueError):
                marker = None
            if item.get('from_id') == -engine.config.group_id and (item.get('random_id') == survey['random_id'] or marker == survey['random_id']):
                return 'already_sent', item
        latest = self.db.one('SELECT max(vk_cmid) cmid,max(coalesce(nullif(vk_date,0),created_at)) date FROM requests WHERE peer_id=?', (survey['peer_id'],))
        basis = self.db.one('SELECT created_at FROM requests WHERE id=?', (survey['request_id'],))
        for item in items:
            if item.get('from_id') == survey['peer_id']:
                if (item.get('conversation_message_id', 0) > latest['cmid']
                        or item.get('date', 0) > int(latest['date'] or 0)):
                    return 'wait', None
            elif item.get('from_id') == -engine.config.group_id and not engine.is_own_reply(item):
                if item.get('admin_author_id') and item.get('date', 0) >= int(basis['created_at']):
                    return 'staff_activity', None
                if item.get('date', 0) > int(survey['last_activity_at']):
                    return 'unknown_outgoing', None
        return '', None

    def deliver(self, transport, engine, *, now=None):
        now = time.time() if now is None else now
        survey = self._claim(now)
        if not survey:
            return False
        try:
            reason, existing = self._history(survey, transport.api, engine)
        except Exception:
            self._defer(survey['id'], 'history_unavailable', now)
            return False
        if reason == 'already_sent':
            sent_at = existing.get('date') or survey['attempted_at'] or now
            self.db.execute("UPDATE feedback_surveys SET status='sent',sent_at=coalesce(sent_at,?),vk_message_id=?,updated_at=? WHERE id=?", (sent_at, existing.get('id', 0), now, survey['id']))
            return True
        if reason == 'wait':
            self._defer(survey['id'], 'unseen_activity', now)
            return False
        if reason:
            self.db.execute("UPDATE feedback_surveys SET status='cancelled',reason=?,updated_at=? WHERE id=?", (reason, now, survey['id']))
            return False
        with self.db.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            current = con.execute('SELECT * FROM feedback_surveys WHERE id=?', (survey['id'],)).fetchone()
            if current['status'] != 'sending':
                return False
            reason = self._eligible(con, current, now)
            if reason or current['due_at'] > now:
                con.execute("UPDATE feedback_surveys SET status='cancelled',reason=?,updated_at=? WHERE id=?", (reason or 'new_activity', now, survey['id']))
                return False
            if current['attempts'] >= 3:
                con.execute("UPDATE feedback_surveys SET status='failed',reason='delivery_error',updated_at=? WHERE id=?", (now, survey['id']))
                return False
            con.execute('UPDATE feedback_surveys SET attempts=attempts+1,attempted_at=coalesce(attempted_at,?),updated_at=? WHERE id=?', (now, now, survey['id']))
        try:
            message_id = transport.send(survey['peer_id'], survey['text'], survey_keyboard(survey), survey['random_id'])
        except Exception:
            with self.db.connect() as con:
                con.execute("UPDATE feedback_surveys SET status=CASE WHEN attempts>=3 THEN 'failed' ELSE 'pending' END,retry_at=?,reason='delivery_error',updated_at=? WHERE id=? AND status='sending'", (now+60, now, survey['id']))
            return False
        self.db.execute("UPDATE feedback_surveys SET status='sent',sent_at=coalesce(sent_at,?),vk_message_id=?,reason='',updated_at=? WHERE id=?", (now, message_id if type(message_id) is int else 0, now, survey['id']))
        return True
