
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resources(key TEXT PRIMARY KEY, value TEXT NOT NULL, visibility TEXT NOT NULL DEFAULT 'public');
CREATE TABLE IF NOT EXISTS cards(
 id INTEGER PRIMARY KEY, source_key TEXT UNIQUE, topic TEXT NOT NULL, title TEXT NOT NULL,
 question_variants TEXT NOT NULL DEFAULT '[]', answer TEXT NOT NULL,
 conditions TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT 'any' CHECK(mode IN ('any','normal','cover')),
 action TEXT NOT NULL DEFAULT 'answer' CHECK(action IN ('answer','handoff')),
 visibility TEXT NOT NULL DEFAULT 'public' CHECK(visibility IN ('public','internal')),
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','disabled')),
 issues TEXT NOT NULL DEFAULT '[]', reviewed INTEGER NOT NULL DEFAULT 0,
 version INTEGER NOT NULL DEFAULT 1, source TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS card_versions(
 id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL REFERENCES cards(id), version INTEGER NOT NULL,
 snapshot TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(card_id,version)
);
CREATE TABLE IF NOT EXISTS import_batches(id INTEGER PRIMARY KEY, digest TEXT UNIQUE NOT NULL, filename TEXT NOT NULL, created_at REAL NOT NULL, report TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversations(
 peer_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, human_mode INTEGER NOT NULL DEFAULT 0,
 consent_version TEXT, consent_state TEXT NOT NULL DEFAULT 'unknown', generation INTEGER NOT NULL DEFAULT 0,
 clarification_count INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS consent_events(
 id INTEGER PRIMARY KEY, peer_id INTEGER NOT NULL, user_id INTEGER NOT NULL, action TEXT NOT NULL,
 version TEXT NOT NULL, document_url TEXT NOT NULL, text_snapshot TEXT NOT NULL,
 vk_event_key TEXT NOT NULL UNIQUE, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS requests(
 id INTEGER PRIMARY KEY, event_key TEXT UNIQUE NOT NULL, peer_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
 text TEXT NOT NULL DEFAULT '', attachments TEXT NOT NULL DEFAULT '[]', input_type TEXT NOT NULL,
 payload TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
 outcome TEXT, reason TEXT, topic TEXT, intent TEXT, tone TEXT, card_id INTEGER, card_version INTEGER,
 reply TEXT, keyboard TEXT, random_id INTEGER NOT NULL, ai_generation INTEGER,
 prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
 duration_ms INTEGER, send_attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS requests_peer ON requests(peer_id,id);
CREATE INDEX IF NOT EXISTS requests_status ON requests(status,id);
CREATE INDEX IF NOT EXISTS requests_created ON requests(created_at);
CREATE TABLE IF NOT EXISTS tickets(
 id INTEGER PRIMARY KEY, peer_id INTEGER NOT NULL, request_id INTEGER NOT NULL REFERENCES requests(id),
 reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', created_at REAL NOT NULL, closed_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ticket_open_peer ON tickets(peer_id) WHERE status='open';
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, action TEXT NOT NULL, target TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS embeddings(card_id INTEGER NOT NULL, card_version INTEGER NOT NULL, model TEXT NOT NULL, vector TEXT NOT NULL, PRIMARY KEY(card_id,model));
