ALTER TABLE staff_replies ADD COLUMN text TEXT NOT NULL DEFAULT '';
ALTER TABLE staff_replies ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]';
ALTER TABLE staff_replies ADD COLUMN source TEXT NOT NULL DEFAULT 'vk';
ALTER TABLE staff_replies ADD COLUMN staff_name TEXT NOT NULL DEFAULT '';
ALTER TABLE staff_replies ADD COLUMN updated_at REAL NOT NULL DEFAULT 0;
CREATE TABLE operator_outbox(
 id INTEGER PRIMARY KEY, client_key TEXT NOT NULL UNIQUE,
 peer_id INTEGER NOT NULL REFERENCES conversations(peer_id),
 ticket_id INTEGER NOT NULL REFERENCES tickets(id), basis_request_id INTEGER NOT NULL,
 text TEXT NOT NULL, staff_name TEXT NOT NULL,
 random_id INTEGER NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
 vk_message_id INTEGER NOT NULL DEFAULT 0, vk_cmid INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX operator_outbox_status ON operator_outbox(status,id);
CREATE INDEX operator_outbox_peer ON operator_outbox(peer_id,id);
