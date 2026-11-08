ALTER TABLE tickets ADD COLUMN closure_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE tickets ADD COLUMN auto_close_paused INTEGER NOT NULL DEFAULT 0;
UPDATE tickets SET closure_reason='manual' WHERE status='closed';
CREATE INDEX tickets_waiting_timeout ON tickets(last_staff_reply_at) WHERE status='waiting_user';
