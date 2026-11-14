-- Remove the old import default only. Explicit changes of the card action,
-- manually created cards, internal contacts and publication states are kept.
CREATE TEMP TABLE complaint_answer_updates AS
SELECT c.id,c.version,v.snapshot FROM cards c
JOIN card_versions v ON v.card_id=c.id AND v.version=c.version
WHERE c.topic='Жалобы' AND c.visibility='public' AND c.action='handoff'
AND nullif(c.source_key,'') IS NOT NULL
AND NOT EXISTS (
 SELECT 1 FROM card_versions previous WHERE previous.card_id=c.id
 AND coalesce(json_extract(previous.snapshot,'$.action'),'')!='handoff'
);
UPDATE cards SET action='answer',version=version+1,updated_at=CAST(strftime('%s','now') AS REAL)
WHERE id IN (SELECT id FROM complaint_answer_updates);
INSERT INTO card_versions(card_id,version,snapshot,created_at)
SELECT c.id,c.version,json_set(u.snapshot,'$.action',c.action,'$.version',c.version,'$.updated_at',c.updated_at),c.updated_at
FROM cards c JOIN complaint_answer_updates u ON u.id=c.id;
INSERT INTO audit(action,target,created_at)
SELECT 'allow_imported_complaint_answer',CAST(id AS TEXT),CAST(strftime('%s','now') AS REAL)
FROM complaint_answer_updates;
DROP TABLE complaint_answer_updates;
