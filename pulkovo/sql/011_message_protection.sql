ALTER TABLE conversations ADD COLUMN bot_paused_until REAL NOT NULL DEFAULT 0;
CREATE TABLE protection_buckets (
 bucket TEXT PRIMARY KEY, tokens REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE protection_events (
 peer_id INTEGER NOT NULL, reason TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 1,
 last_at REAL NOT NULL, notice_at REAL NOT NULL DEFAULT 0,
 PRIMARY KEY(peer_id,reason)
);
