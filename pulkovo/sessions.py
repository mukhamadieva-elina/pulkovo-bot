"""Consultations inside a single VK dialogue. All writes share the caller's transaction."""

import time

IDLE_SECONDS = 24 * 60 * 60
CLOSURE_LABELS = {
    'rating': 'После оценки',
    'inactivity': 'По неактивности',
    'manual': 'Завершена сотрудником',
}


def close_session(con, peer_id, reason, now):
    changed = con.execute("""UPDATE dialogue_sessions SET closed_at=?,closure_reason=?
        WHERE peer_id=? AND closed_at IS NULL""", (now, reason, peer_id)).rowcount
    if changed:
        con.execute('UPDATE conversations SET clarification_count=0 WHERE peer_id=?', (peer_id,))
        con.execute("""UPDATE feedback_surveys SET status='cancelled',reason='session_closed',updated_at=?
            WHERE peer_id=? AND status IN ('pending','sending')""", (now, peer_id))
    return changed


def expire_idle(con, now, peer_id=None):
    peers = con.execute("""SELECT s.peer_id FROM dialogue_sessions s
        JOIN conversations c ON c.peer_id=s.peer_id
        WHERE s.closed_at IS NULL AND s.last_activity_at<=? AND c.human_mode=0
        AND c.bot_paused_until<=? AND (? IS NULL OR s.peer_id=?)
        AND NOT EXISTS (SELECT 1 FROM tickets t WHERE t.peer_id=s.peer_id AND t.status IN ('open','waiting_user'))
        AND NOT EXISTS (SELECT 1 FROM requests r WHERE r.peer_id=s.peer_id AND r.status IN ('pending','processing','ready','sending'))
        AND NOT EXISTS (SELECT 1 FROM operator_outbox o WHERE o.peer_id=s.peer_id AND o.status IN ('pending','sending','failed'))""",
        (now-IDLE_SECONDS, now, peer_id, peer_id)).fetchall()
    for row in peers:
        # The deadline, rather than worker wake-up time, is the actual boundary.
        last = con.execute('SELECT last_activity_at FROM dialogue_sessions WHERE peer_id=? AND closed_at IS NULL', (row['peer_id'],)).fetchone()[0]
        close_session(con, row['peer_id'], 'inactivity', last+IDLE_SECONDS)
    return len(peers)


def ensure_session(con, peer_id, now):
    con.execute('INSERT OR IGNORE INTO dialogue_sessions(peer_id,started_at,last_activity_at) VALUES (?,?,?)', (peer_id, now, now))
    row = con.execute('SELECT id FROM dialogue_sessions WHERE peer_id=? AND closed_at IS NULL', (peer_id,)).fetchone()
    con.execute('UPDATE dialogue_sessions SET last_activity_at=max(last_activity_at,?) WHERE id=?', (now, row['id']))
    return row['id']


def record_delivery(con, request_id, now):
    con.execute("""UPDATE dialogue_sessions SET last_activity_at=max(last_activity_at,?)
        WHERE id=(SELECT session_id FROM requests WHERE id=?) AND closed_at IS NULL""", (now, request_id))


def close_after_rating(con, survey, now):
    basis = con.execute('SELECT session_id FROM requests WHERE id=?', (survey['request_id'],)).fetchone()
    if not basis or basis['session_id'] is None:
        return
    state = con.execute('SELECT * FROM conversations WHERE peer_id=?', (survey['peer_id'],)).fetchone()
    if state['human_mode'] or state['generation'] != survey['generation'] or state['bot_paused_until'] > now:
        return
    # A late vote for an earlier answer must not close newer work, even in the same session.
    newer = con.execute("""SELECT 1 FROM requests WHERE peer_id=? AND id>?
        AND coalesce(json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END,'$.action'),'')!='feedback'
        AND NOT (coalesce(outcome,'')='thanks' AND status='sent') LIMIT 1""",
        (survey['peer_id'], survey['activity_request_id'])).fetchone()
    if newer or con.execute("SELECT 1 FROM operator_outbox WHERE peer_id=? AND status IN ('pending','sending','failed')", (survey['peer_id'],)).fetchone():
        return
    if con.execute('SELECT 1 FROM dialogue_sessions WHERE id=? AND closed_at IS NULL', (basis['session_id'],)).fetchone():
        close_session(con, survey['peer_id'], 'rating', now)


def expire_sessions(db, *, now=None):
    with db.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        return expire_idle(con, time.time() if now is None else now)
