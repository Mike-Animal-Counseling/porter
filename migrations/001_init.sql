-- Porter migration 001: initial schema
-- Postgres 16 + pgvector. Run with: psql $DATABASE_URL -f migrations/001_init.sql
-- or via scripts/migrate.py

CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "vector";

-- ---------------------------------------------------------------- memories
CREATE TYPE memory_type AS ENUM ('preference', 'fact', 'episode', 'skill_ref');
CREATE TYPE memory_status AS ENUM ('pending', 'active', 'archived');

CREATE TABLE memories (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id      UUID,                                  -- reserved for multi-user (P2)
    content       TEXT NOT NULL,
    type          memory_type NOT NULL DEFAULT 'fact',
    scope_tags    TEXT[] NOT NULL DEFAULT '{}',
    source        JSONB NOT NULL DEFAULT '{}',          -- {client_id, created_at, ...}
    confidence    DOUBLE PRECISION NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
    status        memory_status NOT NULL DEFAULT 'pending',
    embedding     vector(1024),                         -- BGE-M3, local embedder
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_by UUID REFERENCES memories(id) ON DELETE SET NULL  -- US5: hard delete must never hit an FK wall
);
CREATE INDEX memories_status_idx ON memories (status);
CREATE INDEX memories_tags_idx ON memories USING GIN (scope_tags);
-- ANN index for semantic search (tune lists with scale: rows/1000)
CREATE INDEX memories_embedding_idx ON memories
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- ---------------------------------------------------------------- clients
CREATE TABLE clients (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          TEXT NOT NULL,
    client_id     TEXT NOT NULL UNIQUE,                 -- OAuth client_id
    secret_hash   TEXT NOT NULL,                        -- argon2
    redirect_uris TEXT[] NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- grants
CREATE TABLE grants (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id     UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    scopes        TEXT[] NOT NULL DEFAULT '{}',
    issued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ,
    revoked       BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX grants_client_idx ON grants (client_id);

-- ---------------------------------------------------------------- tokens
CREATE TABLE tokens (
    jti           UUID PRIMARY KEY DEFAULT gen_random_uuid(),  -- token id, for blacklist
    client_id     UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    scopes        TEXT[] NOT NULL DEFAULT '{}',
    issued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    revoked       BOOLEAN NOT NULL DEFAULT FALSE,
    refresh_of    UUID REFERENCES tokens(jti)
);
CREATE INDEX tokens_client_idx ON tokens (client_id);

-- ---------------------------------------------------------------- audit_log (append-only)
CREATE TYPE audit_action AS ENUM
    ('read', 'write', 'delete', 'export', 'snapshot_apply', 'revoke');

CREATE TABLE audit_log (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id     UUID REFERENCES clients(id) ON DELETE SET NULL,
    action        audit_action NOT NULL,
    memory_ids    UUID[] NOT NULL DEFAULT '{}',
    scopes_used   TEXT[] NOT NULL DEFAULT '{}',
    meta          JSONB NOT NULL DEFAULT '{}',          -- ids & counts only, never content
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_created_idx ON audit_log (created_at DESC);
CREATE INDEX audit_log_client_idx ON audit_log (client_id);

-- Defense in depth: even application bugs cannot UPDATE/DELETE audit rows.
CREATE OR REPLACE FUNCTION forbid_audit_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only (trigger forbid_audit_mutation)';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_no_update
    BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION forbid_audit_mutation();
CREATE TRIGGER audit_log_no_delete
    BEFORE DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION forbid_audit_mutation();

-- ---------------------------------------------------------------- snapshots
CREATE TABLE snapshots (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    manifest      JSONB NOT NULL,
    storage_path  TEXT NOT NULL,                       -- v1: local file path
    signature_ok  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- sync_cursors (P1 auto-sync stub, §9.5)
CREATE TABLE sync_cursors (
    device_id     TEXT PRIMARY KEY,                     -- adapter/device identifier
    cursor        TEXT NOT NULL DEFAULT '',             -- opaque incremental cursor
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
