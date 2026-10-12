-- Porter migration 004: P1 auto-sync (PRD §9.5 / F9, US7)
--
-- 1. change_log: append-only feed of every memory mutation. Adapters pull
--    increments with GET /api/v1/sync/delta?since=<seq>.
-- 2. memories.version: optimistic-concurrency counter for US7
--    last-writer-wins conflict handling (increments on every revise).
-- 3. sync_cursors: (device_id TEXT PK, cursor TEXT) -> (adapter_id TEXT PK,
--    cursor BIGINT NOT NULL DEFAULT 0, last_seen TIMESTAMPTZ).
--    The TEXT -> BIGINT conversion is guarded: non-numeric legacy cursor
--    values become 0 instead of aborting the migration.
--
-- NOTE: audit_log's audit_action enum is intentionally NOT extended (the
-- sync fetch audit reuses action='read' with meta={"via":"sync"}); ALTER
-- TYPE ... ADD VALUE inside a transaction block is a known PG hazard and
-- db.run_migrations runs plain .sql files in one transaction.

-- ---------------------------------------------------------------- change_log
CREATE TABLE IF NOT EXISTS change_log (
    seq             BIGSERIAL PRIMARY KEY,
    memory_id       UUID NOT NULL,
    op              TEXT NOT NULL,                 -- create|approve|revise|delete
    actor_client_id UUID,                          -- clients.id; NULL = owner/panel
    at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS change_log_seq_idx ON change_log (seq);
CREATE INDEX IF NOT EXISTS change_log_memory_idx ON change_log (memory_id);

-- ---------------------------------------------------------------- version
ALTER TABLE memories ADD COLUMN IF NOT EXISTS version INT NOT NULL DEFAULT 1;

-- ---------------------------------------------------------------- sync_cursors
ALTER TABLE sync_cursors RENAME COLUMN device_id TO adapter_id;
ALTER TABLE sync_cursors
    ALTER COLUMN cursor TYPE BIGINT
    USING (CASE WHEN cursor ~ '^[0-9]+$' THEN cursor::BIGINT ELSE 0 END);
ALTER TABLE sync_cursors ALTER COLUMN cursor SET DEFAULT 0;
ALTER TABLE sync_cursors ALTER COLUMN cursor SET NOT NULL;
ALTER TABLE sync_cursors
    ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ NOT NULL DEFAULT now();
