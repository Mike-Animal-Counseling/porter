"""Team workspaces: shared PROJECT-domain memory for teams.

The jump from personal tool to team product. One repo, several members,
each on their own harness — knowledge settles in one place instead of
being trapped in whoever's tool happened to learn it.

Design (deliberately zero-intrusion on the memory core):
- A workspace is a named container. Its memories are ORDINARY memories in
  the existing ``memories`` table, tagged with the workspace's project_tag
  (``workspace:<8 hex>``, derived deterministically from the workspace
  UUID). The sync-delta tag filter (sync.build_sync_delta) and the OAuth
  tag-scope machinery (oauth.effective_tag_filter) already filter per tag,
  so repositories.py and sync.py needed no changes at all.
- Membership and roles live in their own tables (migration 009_teams.sql).
  Roles: owner (everything), editor (read+write workspace project
  memories), viewer (read-only). See check_access().
- Joining mints a credential in the share-token scope language
  (``memory.read.workspace:<tag>`` / ``memory.write.workspace:<tag>``):
  viewers get a read-only share via oauth.mint_share_token (imported, not
  reimplemented); editors/owners get read+write via the same JWT/share
  shape, because mint_share_token by contract NEVER mints write scopes.
  Note: tag-scoped *write* enforcement is a teams-layer concern —
  check_access(need="write") — on top of the OAuth read scope; see the
  REST wiring spec below.
- RED LINE: the personal domain never enters a workspace. A memory of
  type "personal" carrying any ``workspace:<...>`` tag is rejected (422)
  by validate_workspace_memory at the create/revise boundary, regardless
  of who writes it — not even memory.admin.

Suggested REST wiring (not implemented here — app.py is untouched):
- POST /api/v1/workspaces
    body {name, project?} -> 201 workspace
    Caller becomes owner. Requires an authenticated OAuth client
    (a share token must never create a workspace).
- GET /api/v1/workspaces
    List workspaces the caller belongs to.
- GET /api/v1/workspaces/{id}
    Workspace detail + member list (any member may read).
- POST /api/v1/workspaces/{id}/members
    body {client_id, role} -> 201 member + member token (shown once).
    Owner only (teams.invite). Mints via mint_member_token.
- PATCH /api/v1/workspaces/{id}/members/{client_id}
    body {role} — owner only (store.set_role + audit).
- DELETE /api/v1/workspaces/{id}/members/{client_id}
    Owner only, or the member removing themselves.
- DELETE /api/v1/workspaces/{id}
    Owner only. Memories keep their tags (inaccessible once no token
    grants the scope — same semantics as revoking a share).
- Memory writes carrying a ``workspace:<...>`` tag must ALSO pass
  teams.check_access(need="write") for the caller's client_id, and
  validate_workspace_memory(type, tags) must run BEFORE triage — the
  personal-domain red line holds even for memory.admin callers.
"""

from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt

from .errors import Conflict, InsufficientScope, InvalidInput, NotFound
from .oauth import jwt_signing_key, mint_share_token
from .oauth_store import OAuthStore, ShareRecord
from .repositories import MemoryRepository

log = logging.getLogger("porter.teams")

# ---------------------------------------------------------------- constants
#: Valid workspace roles, weakest to strongest.
ROLES = ("owner", "editor", "viewer")
#: Valid `need` values for check_access().
NEEDS = ("read", "write", "admin")

# A workspace tag is `workspace:` + 8 lowercase hex chars, produced by
# workspace_tag(). Strict on purpose: a loose prefix match would also catch
# user tags like "workspace:notes", which are NOT workspace scopes.
_WORKSPACE_TAG_RE = re.compile(r"^workspace:[0-9a-f]{8}$")


# ---------------------------------------------------------------- pure helpers
def workspace_tag(workspace_id: UUID) -> str:
    """The scope/project tag for a workspace (pure function).

    Derived deterministically from the workspace UUID (first 8 hex chars),
    so any layer holding the workspace id can compute the tag without a DB
    round-trip — sync filters, OAuth scope checks, and memory writers all
    use this. The UNIQUE constraint on workspaces.project_tag guards the
    (astronomically unlikely) 8-hex prefix collision between two UUIDs.
    """
    return f"workspace:{workspace_id.hex[:8]}"


def is_workspace_tag(tag: str) -> bool:
    """True for tags shaped like ``workspace:<8 hex>`` (real workspace scopes)."""
    return bool(_WORKSPACE_TAG_RE.match(tag or ""))


def validate_workspace_memory(memory_type: str, tags) -> None:
    """Enforce the personal-domain red line (pure function).

    Raises ValueError (map to 422 at the REST boundary) when a memory of
    type "personal" carries any workspace tag. Personal memories —
    preferences, health, finance, PII — must NEVER enter a team
    workspace, no matter who writes them. Project memories and
    non-workspace tags are unaffected.
    """
    if memory_type == "personal" and any(is_workspace_tag(t) for t in (tags or [])):
        raise InvalidInput(
            "personal memories must not carry workspace tags: "
            "the personal domain never enters a team workspace"
        )


# ---------------------------------------------------------------- records
@dataclass
class WorkspaceRecord:
    id: UUID
    name: str
    owner_client_id: UUID
    project_tag: str  # e.g. workspace:a3f9k2x1; also the OAuth scope tag
    created_at: datetime


@dataclass
class WorkspaceMemberRecord:
    workspace_id: UUID
    client_id: UUID
    role: str  # one of ROLES
    joined_at: datetime


def _check_role(role: str) -> str:
    if role not in ROLES:
        raise InvalidInput(f"unknown role: {role!r} (expected one of {ROLES})")
    return role


# ---------------------------------------------------------------- store ABC
class TeamStore(ABC):
    """Persistence for workspaces + membership.

    Mirrors the OAuthStore/MemoryRepository split: InMemoryTeamStore for
    tests and dev, PostgresTeamStore for production (migration 009).
    The store is intentionally dumb about authorization — role semantics
    live in check_access() and the business functions below.
    """

    @abstractmethod
    async def create_workspace(self, *, name: str, owner_client_id: UUID) -> WorkspaceRecord:
        """Insert a workspace row; id and project_tag are generated here."""

    @abstractmethod
    async def get_workspace(self, workspace_id: UUID) -> WorkspaceRecord | None: ...

    @abstractmethod
    async def get_workspace_by_tag(self, project_tag: str) -> WorkspaceRecord | None:
        """Reverse lookup tag -> workspace (for the REST/memory-write path)."""

    @abstractmethod
    async def list_workspaces_for_client(self, client_id: UUID) -> list[WorkspaceRecord]: ...

    @abstractmethod
    async def add_member(
        self, *, workspace_id: UUID, client_id: UUID, role: str
    ) -> WorkspaceMemberRecord: ...

    @abstractmethod
    async def remove_member(self, *, workspace_id: UUID, client_id: UUID) -> bool:
        """Returns True when a membership row was actually removed."""

    @abstractmethod
    async def set_role(
        self, *, workspace_id: UUID, client_id: UUID, role: str
    ) -> WorkspaceMemberRecord:
        """Change a member's role. Raises KeyError for non-members."""

    @abstractmethod
    async def list_members(self, workspace_id: UUID) -> list[WorkspaceMemberRecord]: ...

    @abstractmethod
    async def get_member(
        self, workspace_id: UUID, client_id: UUID
    ) -> WorkspaceMemberRecord | None: ...

    @abstractmethod
    async def delete_workspace(self, workspace_id: UUID) -> bool:
        """Delete the workspace and (via CASCADE) its memberships. Memories
        keep their tags — they become unreadable once no token grants the
        scope, exactly like a revoked share."""


# ---------------------------------------------------------------- in-memory
class InMemoryTeamStore(TeamStore):
    """Test/dev implementation. Same rules, no persistence."""

    def __init__(self) -> None:
        self._workspaces: dict[UUID, WorkspaceRecord] = {}
        self._by_tag: dict[str, UUID] = {}
        # (workspace_id, client_id) -> member
        self._members: dict[tuple[UUID, UUID], WorkspaceMemberRecord] = {}

    def _now(self) -> datetime:
        return datetime.now(UTC)

    async def create_workspace(self, *, name, owner_client_id):
        ws_id = uuid4()
        tag = workspace_tag(ws_id)
        # Paranoia for the 8-hex prefix collision: regenerate until unique.
        while tag in self._by_tag:
            ws_id = uuid4()
            tag = workspace_tag(ws_id)
        rec = WorkspaceRecord(
            id=ws_id,
            name=name,
            owner_client_id=owner_client_id,
            project_tag=tag,
            created_at=self._now(),
        )
        self._workspaces[ws_id] = rec
        self._by_tag[tag] = ws_id
        return rec

    async def get_workspace(self, workspace_id):
        return self._workspaces.get(workspace_id)

    async def get_workspace_by_tag(self, project_tag):
        ws_id = self._by_tag.get(project_tag)
        return self._workspaces.get(ws_id) if ws_id else None

    async def list_workspaces_for_client(self, client_id):
        ws_ids = {wid for (wid, cid) in self._members if cid == client_id}
        return sorted(
            (self._workspaces[wid] for wid in ws_ids if wid in self._workspaces),
            key=lambda w: w.created_at,
        )

    async def add_member(self, *, workspace_id, client_id, role):
        _check_role(role)
        if workspace_id not in self._workspaces:
            raise KeyError(f"unknown workspace: {workspace_id}")
        key = (workspace_id, client_id)
        if key in self._members:
            raise Conflict(f"client {client_id} is already a member")
        rec = WorkspaceMemberRecord(
            workspace_id=workspace_id,
            client_id=client_id,
            role=role,
            joined_at=self._now(),
        )
        self._members[key] = rec
        return rec

    async def remove_member(self, *, workspace_id, client_id):
        return self._members.pop((workspace_id, client_id), None) is not None

    async def set_role(self, *, workspace_id, client_id, role):
        _check_role(role)
        rec = self._members.get((workspace_id, client_id))
        if rec is None:
            raise KeyError(f"client {client_id} is not a member")
        rec.role = role
        return rec

    async def list_members(self, workspace_id):
        return sorted(
            (m for (wid, _), m in self._members.items() if wid == workspace_id),
            key=lambda m: m.joined_at,
        )

    async def get_member(self, workspace_id, client_id):
        return self._members.get((workspace_id, client_id))

    async def delete_workspace(self, workspace_id):
        if self._workspaces.pop(workspace_id, None) is None:
            return False
        for key in [k for k in self._members if k[0] == workspace_id]:
            del self._members[key]
        self._by_tag = {t: wid for t, wid in self._by_tag.items() if wid != workspace_id}
        return True


# ---------------------------------------------------------------- postgres
class PostgresTeamStore(TeamStore):
    """Production implementation over asyncpg. Requires migration 009."""

    def __init__(self, pool) -> None:  # pool: asyncpg.Pool
        self.pool = pool

    @staticmethod
    def _ws_row(r) -> WorkspaceRecord:
        return WorkspaceRecord(
            id=r["id"],
            name=r["name"],
            owner_client_id=r["owner_client_id"],
            project_tag=r["project_tag"],
            created_at=r["created_at"],
        )

    @staticmethod
    def _mem_row(r) -> WorkspaceMemberRecord:
        return WorkspaceMemberRecord(
            workspace_id=r["workspace_id"],
            client_id=r["client_id"],
            role=r["role"],
            joined_at=r["joined_at"],
        )

    async def create_workspace(self, *, name, owner_client_id):
        # Retry loop for the 8-hex prefix collision (UNIQUE on project_tag).
        # In practice this never triggers; the loop is cheaper than a
        # pre-check and keeps the UNIQUE constraint as the arbiter.
        async with self.pool.acquire() as con:
            for _ in range(3):
                ws_id = uuid4()
                try:
                    row = await con.fetchrow(
                        """
                        INSERT INTO workspaces (id, name, owner_client_id, project_tag)
                        VALUES ($1, $2, $3, $4)
                        RETURNING id, name, owner_client_id, project_tag, created_at
                        """,
                        ws_id,
                        name,
                        owner_client_id,
                        workspace_tag(ws_id),
                    )
                    return self._ws_row(row)
                except Exception as e:
                    # asyncpg.UniqueViolationError — only retry tag collisions.
                    if "workspaces_project_tag_key" not in str(e):
                        raise
            raise RuntimeError("could not mint a unique workspace tag")

    async def get_workspace(self, workspace_id):
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT id, name, owner_client_id, project_tag, created_at"
                " FROM workspaces WHERE id = $1",
                workspace_id,
            )
        return self._ws_row(row) if row else None

    async def get_workspace_by_tag(self, project_tag):
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT id, name, owner_client_id, project_tag, created_at"
                " FROM workspaces WHERE project_tag = $1",
                project_tag,
            )
        return self._ws_row(row) if row else None

    async def list_workspaces_for_client(self, client_id):
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT w.id, w.name, w.owner_client_id, w.project_tag, w.created_at"
                " FROM workspaces w JOIN workspace_members m"
                " ON m.workspace_id = w.id"
                " WHERE m.client_id = $1 ORDER BY w.created_at",
                client_id,
            )
        return [self._ws_row(r) for r in rows]

    async def add_member(self, *, workspace_id, client_id, role):
        _check_role(role)
        async with self.pool.acquire() as con:
            try:
                row = await con.fetchrow(
                    """
                    INSERT INTO workspace_members (workspace_id, client_id, role)
                    VALUES ($1, $2, $3)
                    RETURNING workspace_id, client_id, role, joined_at
                    """,
                    workspace_id,
                    client_id,
                    role,
                )
            except Exception as e:
                msg = str(e)
                if "workspace_members_pkey" in msg:
                    raise Conflict(f"client {client_id} is already a member") from e
                if "workspace_members_workspace_id_fkey" in msg:
                    raise KeyError(f"unknown workspace: {workspace_id}") from e
                raise
        return self._mem_row(row)

    async def remove_member(self, *, workspace_id, client_id):
        async with self.pool.acquire() as con:
            res = await con.execute(
                "DELETE FROM workspace_members WHERE workspace_id = $1 AND client_id = $2",
                workspace_id,
                client_id,
            )
        return res == "DELETE 1"

    async def set_role(self, *, workspace_id, client_id, role):
        _check_role(role)
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                """
                UPDATE workspace_members SET role = $3
                WHERE workspace_id = $1 AND client_id = $2
                RETURNING workspace_id, client_id, role, joined_at
                """,
                workspace_id,
                client_id,
                role,
            )
        if row is None:
            raise KeyError(f"client {client_id} is not a member")
        return self._mem_row(row)

    async def list_members(self, workspace_id):
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT workspace_id, client_id, role, joined_at"
                " FROM workspace_members WHERE workspace_id = $1"
                " ORDER BY joined_at",
                workspace_id,
            )
        return [self._mem_row(r) for r in rows]

    async def get_member(self, workspace_id, client_id):
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT workspace_id, client_id, role, joined_at"
                " FROM workspace_members WHERE workspace_id = $1 AND client_id = $2",
                workspace_id,
                client_id,
            )
        return self._mem_row(row) if row else None

    async def delete_workspace(self, workspace_id):
        async with self.pool.acquire() as con:
            res = await con.execute("DELETE FROM workspaces WHERE id = $1", workspace_id)
        return res == "DELETE 1"


# ---------------------------------------------------------------- access control
async def check_access(store: TeamStore, *, workspace_id: UUID, client_id: UUID, need: str) -> bool:
    """Role-based access check for a workspace (pure-ish: reads the store).

    Permission semantics:
    - need="read":  any member (owner, editor, viewer)
    - need="write": owner or editor — may read AND write the workspace's
      project memories
    - need="admin": owner only — invite/remove members, change roles,
      delete the workspace
    Non-members get False for every need. Unknown `need` -> ValueError
    (fail loudly, never silently grant).
    """
    if need not in NEEDS:
        raise InvalidInput(f"unknown need: {need!r} (expected one of {NEEDS})")
    member = await store.get_member(workspace_id, client_id)
    if member is None:
        return False
    if need == "read":
        return True
    if need == "write":
        return member.role in ("owner", "editor")
    return member.role == "owner"  # admin


# ---------------------------------------------------------------- business functions
async def require_memory_write(store, *, client_id, memory_type, tags, is_admin=False):
    """Enforce the personal boundary and current workspace role at writes."""
    validate_workspace_memory(memory_type, tags)
    for tag in {tag for tag in tags if is_workspace_tag(tag)}:
        if is_admin:
            continue
        workspace = await store.get_workspace_by_tag(tag) if store is not None else None
        if (
            workspace is None
            or client_id is None
            or not await check_access(
                store, workspace_id=workspace.id, client_id=client_id, need="write"
            )
        ):
            raise InsufficientScope("workspace editor membership required")


def _ws_dict(ws: WorkspaceRecord) -> dict:
    return {
        "id": str(ws.id),
        "name": ws.name,
        "owner_client_id": str(ws.owner_client_id),
        "project_tag": ws.project_tag,
        "created_at": ws.created_at.isoformat(),
    }


def _member_dict(m: WorkspaceMemberRecord) -> dict:
    return {
        "workspace_id": str(m.workspace_id),
        "client_id": str(m.client_id),
        "role": m.role,
        "joined_at": m.joined_at.isoformat(),
    }


async def create_workspace(
    store: TeamStore,
    repo: MemoryRepository,
    *,
    name: str,
    owner_client_id: UUID,
    project: str | None = None,
) -> dict:
    """Create a workspace, seed the owner membership, and audit it.

    `project` is an optional human label (e.g. the repo this workspace
    mirrors); it is recorded in the audit trail only — the workspace's
    identity for memories and scopes is always project_tag.
    Returns a JSON-able workspace dict.
    """
    if not name or not name.strip():
        raise InvalidInput("workspace name must not be empty")
    ws = await store.create_workspace(name=name.strip(), owner_client_id=owner_client_id)
    await store.add_member(workspace_id=ws.id, client_id=owner_client_id, role="owner")
    await repo.audit(
        action="workspace_create",
        client_id=owner_client_id,
        meta={
            "workspace_id": str(ws.id),
            "name": ws.name,
            "project_tag": ws.project_tag,
            "project": project,
        },
    )
    log.info("workspace created: %s (%s)", ws.name, ws.project_tag)
    return _ws_dict(ws)


async def invite(
    store: TeamStore,
    repo: MemoryRepository,
    *,
    workspace_id: UUID,
    client_id: UUID,
    role: str,
    actor_client_id: UUID,
) -> dict:
    """Add a member to a workspace. Owner-only; audited.

    Raises NotFound for unknown workspaces, PermissionError when the actor
    lacks admin, ValueError for bad roles or duplicate membership.
    The member's credential is minted separately via mint_member_token
    (the token is shown exactly once, so it must not be buried inside
    this call's return value).
    Returns a JSON-able member dict.
    """
    ws = await store.get_workspace(workspace_id)
    if ws is None:
        raise NotFound(f"unknown workspace: {workspace_id}")
    if not await check_access(
        store, workspace_id=workspace_id, client_id=actor_client_id, need="admin"
    ):
        raise PermissionError(
            f"client {actor_client_id} is not an owner of workspace {workspace_id}"
        )
    member = await store.add_member(
        workspace_id=workspace_id, client_id=client_id, role=_check_role(role)
    )
    await repo.audit(
        action="workspace_invite",
        client_id=actor_client_id,
        meta={
            "workspace_id": str(workspace_id),
            "invited_client_id": str(client_id),
            "role": role,
        },
    )
    log.info(
        "workspace invite: %s -> %s as %s (by %s)",
        workspace_id,
        client_id,
        role,
        actor_client_id,
    )
    return _member_dict(member)


# ---------------------------------------------------------------- member tokens
async def mint_member_token(
    oauth_store: OAuthStore,
    *,
    workspace: WorkspaceRecord,
    role: str,
    name: str | None = None,
    expires_in_hours: int = 24 * 30,
) -> tuple[ShareRecord, str]:
    """Mint the credential a workspace member presents to the API.

    Scope language is the share-token language (oauth.mint_share_token):
    - viewer -> [``memory.read.workspace:<tag>``] — minted by DELEGATING to
      oauth.mint_share_token (imported, not reimplemented): exactly the
      owner-restricted read-only share contract.
    - editor/owner -> [``memory.read.workspace:<tag>``,
      ``memory.write.workspace:<tag>``] — the same JWT/share shape
      (jti == share id, sub == "share"), minted here because
      mint_share_token by contract NEVER mints write scopes.

    The workspace tag is registered as a custom tag first, so
    mint_share_token's tag validation (preset + custom) accepts it — this
    also makes the scope describable on the OAuth consent screen.
    Tag-scoped *write* enforcement additionally requires
    check_access(need="write") at the memory-write boundary (see module
    docstring); the token alone is the read pass.

    Returns (ShareRecord, token). Show the token exactly once; never log it.
    """
    _check_role(role)
    tag = workspace.project_tag
    if not is_workspace_tag(tag):
        raise InvalidInput(f"not a workspace tag: {tag!r}")
    await oauth_store.add_custom_tag(tag=tag, label=f"Team workspace: {workspace.name}")
    token_name = name or f"workspace {workspace.name} ({role})"
    if role == "viewer":
        return await mint_share_token(
            oauth_store,
            name=token_name,
            tags=[tag],
            expires_in_hours=expires_in_hours,
        )
    # editor/owner: read+write. Same share-record + JWT shape as
    # oauth.mint_share_token (jti == share.id, sub == "share"); only the
    # scope list differs, which mint_share_token cannot express.
    scopes = [f"memory.read.{tag}", f"memory.write.{tag}"]
    expires_at = datetime.now(UTC) + timedelta(hours=expires_in_hours)
    share = await oauth_store.create_share(name=token_name, scopes=scopes, expires_at=expires_at)
    now = int(time.time())
    payload = {
        "jti": str(share.id),
        "sub": "share",  # marks share tokens (not a clients.id UUID)
        "cid": f"share:{share.id}",  # public identity, used for rate-limit buckets
        "scopes": sorted(scopes),
        "iat": now,
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, jwt_signing_key(), algorithm="HS256")
    return share, token
