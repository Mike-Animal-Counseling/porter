-- Porter migration 005: P2 E2EE encrypted memories + memory.share tokens
--
-- 1. memories.encrypted / memories.key_id: E2EE writes store the client-side
--    XChaCha20-Poly1305 ciphertext in `content` as-is. The server never
--    computes an embedding for encrypted rows (embedding stays NULL), so
--    they are invisible to semantic search and duplicate detection by
--    construction. Tags/type/confidence/timestamps stay plaintext metadata.
-- 2. shares: owner-minted restricted share tokens (memory.share scope).
--    A share carries ONLY memory.read.<tag> scopes — never memory.write,
--    never memory.admin. The JWT itself is the credential; the table holds
--    only metadata + revocation state (the token string is never stored).
--    jti of the minted JWT == shares.id.

-- ---------------------------------------------------------------- E2EE columns
ALTER TABLE memories ADD COLUMN IF NOT EXISTS encrypted BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS key_id TEXT;

-- ---------------------------------------------------------------- shares
CREATE TABLE IF NOT EXISTS shares (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    scopes      TEXT[] NOT NULL,                       -- only memory.read.<tag>, nothing else
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked     BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS shares_expires_idx ON shares (expires_at);
