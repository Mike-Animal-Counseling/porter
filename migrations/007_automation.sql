-- Porter migration 007: automation layer columns.
--
-- 1. last_retrieved_at: idle clock for confidence decay (porter/decay.py).
--    Backfilled from updated_at so pre-existing memories don't all decay
--    at once on upgrade.
-- 2. pinned: user-pinned memories never decay (set via revise).
-- 3. triage_reasons: JSON list of triage decision reasons recorded at
--    write time (drives inbox reason chips + /api/v1/digest grouping).

ALTER TABLE memories ADD COLUMN IF NOT EXISTS last_retrieved_at TIMESTAMPTZ;
UPDATE memories
SET last_retrieved_at = updated_at
WHERE last_retrieved_at IS NULL;
ALTER TABLE memories ALTER COLUMN last_retrieved_at SET NOT NULL;
ALTER TABLE memories ALTER COLUMN last_retrieved_at SET DEFAULT now();

ALTER TABLE memories ADD COLUMN IF NOT EXISTS pinned BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE memories ADD COLUMN IF NOT EXISTS triage_reasons TEXT NOT NULL DEFAULT '[]';
