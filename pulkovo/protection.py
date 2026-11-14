"""Local resource limits. User text is never executed or used as an instruction."""

import re
import time
import unicodedata


LIMIT_TEXT = 'Вы отправили много сообщений подряд. Подождите немного и повторите вопрос. Если нужна помощь сотрудника, напишите /оператор.'
AI_LIMIT_TEXT = 'Автоматический помощник временно достиг лимита обработки. Попробуйте позже или напишите /оператор, чтобы обратиться к сотруднику.'
UNSAFE_TEXT = 'Я помогаю по вопросам аэропорта и не выполняю команды по изменению своих правил. Напишите, что вы хотите узнать об аэропорте.'
TOO_LONG_TEXT = 'Сообщение слишком длинное. Разделите его на несколько коротких вопросов и отправляйте по одному.'


def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold() if unicodedata.category(c) != 'Cf')


def instruction_attack(text):
    """A narrow first filter, not a complete prompt-injection detector."""
    value = normalized(text)
    return bool(re.search(
        r'(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior|system)\s+(?:instructions?|prompts?|rules?)'
        r'|(?:игнорируй|забудь|отмени)\s+(?:все\s+)?(?:предыдущие|системные|прежние)\s+(?:инструкции|правила|указания)'
        r'|(?:покажи|раскрой|выведи|напечатай|reveal|print|show)\s+(?:свой\s+|the\s+|your\s+)?(?:системный\s+промпт|system\s+prompt|api[_ -]?key|ключ\s+api|access[_ -]?token)'
        r'|<\|(?:system|im_start|im_end)\|>', value))


def take_token(con, bucket, capacity, seconds, now):
    row = con.execute('SELECT * FROM protection_buckets WHERE bucket=?', (bucket,)).fetchone()
    tokens = capacity if row is None else min(capacity, row['tokens'] + max(0, now-row['updated_at'])*capacity/seconds)
    allowed = tokens >= 1
    con.execute('INSERT INTO protection_buckets VALUES (?,?,?) ON CONFLICT(bucket) DO UPDATE SET tokens=excluded.tokens,updated_at=excluded.updated_at',
                (bucket, tokens-1 if allowed else tokens, now))
    return allowed


def record_limit(con, peer, reason, now):
    con.execute('INSERT INTO protection_events(peer_id,reason,last_at) VALUES (?,?,?) ON CONFLICT(peer_id,reason) DO UPDATE SET count=count+1,last_at=excluded.last_at', (peer, reason, now))
    last = con.execute('SELECT max(notice_at) FROM protection_events WHERE peer_id=?', (peer,)).fetchone()[0]
    if last and now-last < 60:
        return False
    con.execute('UPDATE protection_events SET notice_at=? WHERE peer_id=? AND reason=?', (now, peer, reason))
    return True


def admit(con, peer, control, human_mode, now):
    # Consent withdrawal is applied by ingress even if a repeated command is
    # suppressed here. Service commands have their own small reserve.
    service = control.get('action') in ('agree', 'decline', 'revoke', 'operator', 'feedback')
    key = f'control:{peer}' if service else f'incoming:{peer}'
    capacity = 6 if service else 12
    reason = ''
    if not take_token(con, key, capacity, 60, now):
        reason = 'message_rate'
    elif not service:
        pending = con.execute("SELECT count(*) FROM requests WHERE status IN ('pending','processing','ready','sending') AND peer_id=?", (peer,)).fetchone()[0]
        if pending >= 5:
            reason = 'dialogue_backlog'
    total = con.execute("SELECT count(*) FROM requests WHERE status IN ('pending','processing','ready','sending')").fetchone()[0]
    if total >= 200:
        reason = 'queue_capacity'
    if not reason:
        return '', False
    notice = record_limit(con, peer, reason, now)
    return reason, notice and not human_mode and not service and total < 200


class Protection:
    def __init__(self, db):
        self.db = db

    def reserve_ai(self, peer):
        now = time.time()
        with self.db.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            if not take_token(con, f'ai:{peer}', 30, 3600, now):
                return 'user_ai_budget'
            if not take_token(con, 'ai:global', 300, 3600, now):
                return 'global_ai_budget'
        return ''

    def notice(self, peer, reason):
        with self.db.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            return record_limit(con, peer, reason, time.time())

    def state(self, peer):
        return self.db.all('SELECT reason,count,last_at FROM protection_events WHERE peer_id=? ORDER BY last_at DESC', (peer,))
