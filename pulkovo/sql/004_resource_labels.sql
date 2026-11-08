ALTER TABLE resources ADD COLUMN label TEXT NOT NULL DEFAULT '';
UPDATE resources SET label=coalesce((SELECT c.title FROM cards c WHERE c.visibility='public' AND instr(c.answer,'{' || resources.key || '}')>0 ORDER BY c.id LIMIT 1),value) WHERE visibility='public';
