-- porter:no-transaction
-- Porter migration 002: OAuth support (PRD §10, §12)
--
-- Runs WITHOUT an explicit transaction block because
-- ALTER TYPE ... ADD VALUE is forbidden inside one (each statement runs in
-- its own implicit transaction via the migration runner).

-- ---------------------------------------------------------------- auth codes
-- One-time authorization codes (10 min TTL). Only sha256(code) is stored.
CREATE TABLE auth_codes (
    code_hash    TEXT PRIMARY KEY,                       -- sha256(code)
    client_id    UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    scopes       TEXT[] NOT NULL DEFAULT '{}',
    redirect_uri TEXT NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,
    used         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX auth_codes_client_idx ON auth_codes (client_id);

-- ---------------------------------------------------------------- tokens: refresh token hashes
-- Access tokens are JWTs (only jti rows are stored). Refresh tokens are
-- opaque; only sha256(refresh_token) is stored here.
ALTER TABLE tokens ADD COLUMN token_hash TEXT;
CREATE INDEX tokens_hash_idx ON tokens (token_hash);

-- ---------------------------------------------------------------- audit actions for OAuth lifecycle
ALTER TYPE audit_action ADD VALUE 'grant';      -- auth code exchanged / tokens issued
ALTER TYPE audit_action ADD VALUE 'refresh';   -- refresh token rotation
ALTER TYPE audit_action ADD VALUE 'security';  -- e.g. refresh-token reuse detected (T2)
