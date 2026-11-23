CREATE TABLE dialogue_sessions(
 id INTEGER PRIMARY KEY,
 peer_id INTEGER NOT NULL REFERENCES conversations(peer_id),
 started_at REAL NOT NULL,
 last_activity_at REAL NOT NULL,
 closed_at REAL,
 closure_reason TEXT CHECK(closure_reason IN ('rating','inactivity','manual')),
 CHECK((closed_at IS NULL AND closure_reason IS NULL) OR (closed_at IS NOT NULL AND closure_reason IS NOT NULL))
);
CREATE UNIQUE INDEX session_active_peer ON dialogue_sessions(peer_id) WHERE closed_at IS NULL;
CREATE INDEX session_closed ON dialogue_sessions(closed_at);
ALTER TABLE requests ADD COLUMN session_id INTEGER REFERENCES dialogue_sessions(id);
ALTER TABLE requests ADD COLUMN consent_prompt_version TEXT;
ALTER TABLE requests ADD COLUMN consent_prompt_generation INTEGER;
CREATE INDEX requests_session ON requests(session_id,id);
