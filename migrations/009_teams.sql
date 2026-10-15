-- Porter migration 009: team workspaces (shared project memory)
--
-- 1. workspaces: a team workspace is a named shared container for PROJECT
--    domain memories. Its `project_tag` (e.g. `workspace:a3f9k2x1`,
--    derived from the workspace UUID in porter/teams.py) doubles as the
--    OAuth scope tag — workspace memories are ORDINARY memories in the
--    existing `memories` table carrying that tag, so the memories schema,
--    the sync-delta tag filter, and OAuth tag scopes need zero changes.
-- 2. workspace_members: (workspace_id, client_id) -> role. Roles:
--    owner (everything), editor (read+write workspace project memories),
--    viewer (read-only). Enforced by the CHECK constraint plus
--    teams.check_access(); the DB layer is intentionally dumb about
--    semantics.
-- 3. The personal-domain red line (no personal memory may carry a workspace
--    tag) is enforced in application code (teams.validate_workspace_memory),
--    because it depends on the memory `type`, not just the tag — a CHECK
--    constraint on these tables could never see it.
-- 4. Membership rows die with their workspace (ON DELETE CASCADE).
--    Deleting a workspace does NOT delete the memories: they keep their
--    tags and become inaccessible once no token grants the scope — the
--    same semantics as revoking a share (005).

-- ---------------------------------------------------------------- workspaces
CREATE TABLE IF NOT EXISTS workspaces (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    owner_client_id UUID NOT NULL,
    project_tag     TEXT NOT NULL UNIQUE,   -- e.g. workspace:a3f9k2x1; also the OAuth scope tag
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS workspaces_owner_idx ON workspaces (owner_client_id);
CREATE INDEX IF NOT EXISTS workspaces_tag_idx ON workspaces (project_tag);

-- ---------------------------------------------------------------- members
CREATE TABLE IF NOT EXISTS workspace_members (
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    client_id    UUID NOT NULL,
    role         TEXT NOT NULL CHECK (role IN ('owner','editor','viewer')),
    joined_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, client_id)
);
CREATE INDEX IF NOT EXISTS workspace_members_client_idx ON workspace_members (client_id);
