ALTER TABLE requests ADD COLUMN vk_cmid INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requests ADD COLUMN vk_date INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tickets ADD COLUMN last_staff_reply_at REAL;
DROP INDEX ticket_open_peer;
CREATE UNIQUE INDEX ticket_active_peer ON tickets(peer_id) WHERE status IN ('open','waiting_user');
CREATE TABLE staff_replies(id INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE, peer_id INTEGER NOT NULL, vk_cmid INTEGER NOT NULL DEFAULT 0, admin_id INTEGER NOT NULL, created_at REAL NOT NULL);
CREATE INDEX staff_replies_peer ON staff_replies(peer_id,created_at);
