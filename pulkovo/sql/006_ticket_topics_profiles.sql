ALTER TABLE tickets ADD COLUMN topic TEXT NOT NULL DEFAULT '';
ALTER TABLE tickets ADD COLUMN topic_source TEXT NOT NULL DEFAULT 'auto';
ALTER TABLE tickets ADD COLUMN topic_request_id INTEGER REFERENCES requests(id);
ALTER TABLE tickets ADD COLUMN topic_version INTEGER NOT NULL DEFAULT 1;
UPDATE tickets SET topic_request_id=(SELECT r.id FROM requests r WHERE r.peer_id=tickets.peer_id AND nullif(trim(r.topic),'') IS NOT NULL AND r.created_at>coalesce((SELECT max(previous.closed_at) FROM tickets previous WHERE previous.peer_id=tickets.peer_id AND previous.id<tickets.id),0) AND r.created_at<=coalesce(tickets.closed_at,1e30) ORDER BY r.id DESC LIMIT 1);
UPDATE tickets SET topic=coalesce((SELECT r.topic FROM requests r WHERE r.id=tickets.topic_request_id),'');
CREATE TABLE vk_profiles(
 user_id INTEGER PRIMARY KEY,
 first_name TEXT NOT NULL DEFAULT '', last_name TEXT NOT NULL DEFAULT '',
 updated_at REAL NOT NULL DEFAULT 0, last_checked_at REAL NOT NULL DEFAULT 0
);
