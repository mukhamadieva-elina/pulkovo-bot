CREATE TABLE feedback_surveys(
 id INTEGER PRIMARY KEY,
 peer_id INTEGER NOT NULL REFERENCES conversations(peer_id),
 request_id INTEGER NOT NULL UNIQUE REFERENCES requests(id),
 generation INTEGER NOT NULL,
 activity_request_id INTEGER NOT NULL REFERENCES requests(id),
 last_activity_at REAL NOT NULL, due_at REAL NOT NULL,
 token TEXT NOT NULL UNIQUE, random_id INTEGER NOT NULL UNIQUE,
 text TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sending','sent','cancelled','failed')),
 attempts INTEGER NOT NULL DEFAULT 0, attempted_at REAL,
 retry_at REAL NOT NULL DEFAULT 0,
 sent_at REAL, vk_message_id INTEGER NOT NULL DEFAULT 0,
 rating TEXT CHECK(rating IN ('good','bad')), rated_at REAL,
 reason TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX feedback_active_peer ON feedback_surveys(peer_id) WHERE status IN ('pending','sending');
CREATE INDEX feedback_due ON feedback_surveys(status,due_at,retry_at);
CREATE INDEX feedback_peer ON feedback_surveys(peer_id,id);
CREATE INDEX feedback_sent ON feedback_surveys(sent_at);
CREATE INDEX requests_random_id ON requests(random_id);
