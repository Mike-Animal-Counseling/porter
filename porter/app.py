"""FastAPI application factory. Routers per PRD §11.

W2: OAuth + scope enforcement live here. Auth state per app:
  app.state.auth_enabled     bool (default True; AUTH_ENABLED=0 for dev)
  app.state.oauth_store      OAuthStore | None (None until lifespan wires PG)
  app.state.rate_limiter     TokenBucket (T5)
  app.state.revocation_cache RevocationCache (T2: revoked jtis die < 60s)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from hmac import compare_digest
from urllib.parse import urlencode
from uuid import UUID

from fastapi import Depends, FastAPI, Form, Query, Request, UploadFile, File
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from . import auth, oauth as oauth_logic
from .auth import RevocationCache
from .config import settings
from .errors import (
    InsufficientScope,
    InvalidGrant,
    InvalidInput,
    InvalidScope,
    NotFound,
    PorterError,
    ServiceNotReady,
    Unauthorized,
    porter_error_handler,
)
from .oauth_store import OAuthStore
from .origin import check_same_origin
from .ratelimit import TokenBucket
from .repositories import MemoryRepository
from . import conflicts as conflicts_logic
from . import lifecycle as lifecycle_logic
from . import switch as switch_logic
from . import teams as teams_logic
from . import writeback as writeback_logic
from .e2ee_hub import EnvelopeStore, InMemoryEnvelopeStore
from .schemas import (
    ClientCreate,
    ClientCreateOut,
    ClientOut,
    ConflictResolveIn,
    CursorIn,
    CursorOut,
    CustomTagCreate,
    CustomTagOut,
    DeviceRegisterIn,
    DuplicateCandidate,
    EnvelopePostIn,
    ErrorOut,
    HealthOut,
    MemoryCreate,
    MemoryCreateOut,
    MemoryMergeIn,
    MemoryOut,
    MemoryPin,
    MemoryRevise,
    MemoryReviseOut,
    MemorySearch,
    RevokeOut,
    ScopeTagsOut,
    SearchOut,
    SearchResult,
    ShareCreate,
    ShareCreateOut,
    ShareOut,
    SnapshotCreate,
    _validate_tags as _validate_tag_list,
    SnapshotOut,
    SwitchHandoffIn,
    SyncChange,
    SyncDeltaOut,
    WorkspaceCreateIn,
    WorkspaceInviteIn,
    WorkspaceOut,
    WritebackIn,
    WritebackOut,
    TokenOut,
)
from . import snapshots as snapshots_logic
from . import sync as sync_logic
from .snapshots import SnapshotStore, InMemorySnapshotStore
from .exporter import build_export_payload

API_PREFIX = "/api/v1"

log = logging.getLogger("porter.app")


def _to_out(rec) -> MemoryOut:
    return MemoryOut(
        id=rec.id, content=rec.content, type=rec.type,
        scope_tags=rec.scope_tags, source=rec.source,
        confidence=rec.confidence, status=rec.status,
        created_at=rec.created_at, updated_at=rec.updated_at,
        superseded_by=rec.superseded_by,
        version=rec.version,
        # E2EE (P2): content IS the ciphertext here; the server never decrypts.
        encrypted=rec.encrypted,
        key_id=rec.key_id,
        # Automation layer.
        pinned=rec.pinned,
        triage_reasons=list(rec.triage_reasons or []),
        # PRD principle 5 (T3): retrieved memory is untrusted input.
        untrusted_data=True,
    )


def _validation_error_detail(exc: RequestValidationError) -> str:
    parts = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e.get("loc", ()))
        parts.append(f"{loc}: {e.get('msg', 'invalid')}")
    return "; ".join(parts)[:1000]


def _redirect_with_params(base: str, params: dict) -> str:
    sep = "&" if "?" in base else "?"
    return base + sep + urlencode(params)


def create_app(
    repo: MemoryRepository | None = None,
    oauth_store: OAuthStore | None = None,
    auth_enabled: bool | None = None,
    rate_limit_per_minute: int | None = None,
    lifespan=None,
    snapshot_store: SnapshotStore | None = None,
    enable_mcp: bool = True,
    wiki_service=None,  # porter.wiki.WikiService | None; None = wiki layer off
    envelope_store: EnvelopeStore | None = None,
    team_store=None,  # porter.teams.TeamStore | None
) -> FastAPI:
    app = FastAPI(
        title="Porter",
        description="Personal AI memory + state layer (PRD v2.0). W2: OAuth enforced.",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.wiki_service = wiki_service
    app.middleware("http")(auth.auth_middleware)
    app.add_exception_handler(PorterError, porter_error_handler)

    @app.exception_handler(RequestValidationError)
    async def _request_validation_handler(request, exc: RequestValidationError):
        # PRD §11: every error response is {error: {code, message}} — including
        # 422s from request validation (FastAPI's default {"detail": [...]} is
        # replaced here).
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_input",
                    "message": _validation_error_detail(exc),
                }
            },
        )

    # NOTE: deliberately NO permissive CORS. Porter is a security-sensitive
    # local service; v1 has no legitimate cross-origin browser client
    # (panel is same-origin, MCP clients are native CLIs). Browsers must
    # not be able to call the API cross-origin — this is a layer of the
    # CSRF defense alongside porter/origin.py. If a browser-based client
    # ever needs access, add an explicit allow-list (never "*").
    app.state.repo = repo
    app.state.oauth_store = oauth_store
    app.state.snapshot_store = (
        snapshot_store if snapshot_store is not None else InMemorySnapshotStore()
    )
    # New subsystems (seamless-switching release): blind-hub key envelopes
    # and team workspaces. In-memory by default; postgres variants are wired
    # in main.lifespan when PORTER_REPO=postgres.
    app.state.envelope_store = (
        envelope_store if envelope_store is not None else InMemoryEnvelopeStore()
    )
    app.state.team_store = (
        team_store if team_store is not None else teams_logic.InMemoryTeamStore()
    )
    app.state.auth_enabled = (
        settings.auth_enabled if auth_enabled is None else auth_enabled
    )
    app.state.rate_limiter = TokenBucket(
        rate_limit_per_minute
        if rate_limit_per_minute is not None
        else settings.rate_limit_per_minute
    )
    app.state.revocation_cache = RevocationCache(ttl_seconds=30)
    if app.state.auth_enabled and not settings.setup_token_is_env:
        # P0: the one-time bootstrap token is the only thing standing between
        # a network neighbor and self-registered memory.admin during setup.
        # Log it loudly; set PORTER_SETUP_TOKEN to use a stable value.
        log.warning(
            "PORTER_SETUP_TOKEN is not set: generated one-time setup token %s. "
            "Pass it as the X-Porter-Setup-Token header for the FIRST client "
            "registration only. Do not expose port 8000 beyond localhost.",
            settings.setup_token,
        )

    def get_repo() -> MemoryRepository:
        r = app.state.repo
        if r is None:
            raise ServiceNotReady("repository is still starting up")
        return r

    def get_oauth_store() -> OAuthStore:
        s = app.state.oauth_store
        if s is None:
            raise ServiceNotReady("oauth store is still starting up")
        return s

    def get_snapshot_store() -> SnapshotStore:
        return app.state.snapshot_store

    def get_envelope_store() -> EnvelopeStore:
        return app.state.envelope_store

    def get_team_store():
        return app.state.team_store

    async def _custom_tag_map(store: OAuthStore) -> dict[str, str]:
        return {t["tag"]: t["label"] for t in await store.list_custom_tags()}

    def _client_pk(request: Request) -> UUID | None:
        ctx = auth.current_auth(request)
        return ctx.client_pk if ctx else None

    def _adapter_id(request: Request) -> str:
        """Adapter identity for sync: the token's public client_id, or
        "owner" when auth is disabled (owner context)."""
        ctx = auth.current_auth(request)
        return ctx.client_id if ctx else "owner"

    def _require_adapter_id(request: Request) -> str:
        """POST /sync/cursor auth: any valid bearer, no special scope. The
        adapter identifies itself by its token (no client-supplied id)."""
        ctx = auth.current_auth(request)
        if ctx is None:
            if request.app.state.auth_enabled:
                raise Unauthorized("missing or invalid bearer token")
            return "owner"
        return ctx.client_id

    # ------------------------------------------------------------- memories
    @app.post(
        f"{API_PREFIX}/memories",
        response_model=MemoryCreateOut,
        status_code=202,
        responses={422: {"model": ErrorOut}},
        summary="Write a memory (goes to pending review queue)",
    )
    async def create_memory(
        body: MemoryCreate,
        request: Request,
        request_repo: MemoryRepository = Depends(get_repo),
        granted: list[str] = Depends(auth.require_scopes("memory.write")),
    ):
        # E2EE (P2, docs/DEVELOPER.md §6): exactly one of `content` /
        # (`content_ciphertext` + `encryption`) — enforced by the schema's
        # _exactly_one_payload validator (422); the branches below are
        # unreachable in the other case, guarded explicitly (no asserts —
        # they vanish under `python -O`).
        #
        # Automation layer: writes go through triage (porter/triage.py).
        # Low-risk memories auto-approve; sensitive/conflict/low-confidence
        # queue for humans; near-identical (>= 0.97) auto-merge. E2EE always
        # queues (ciphertext-blind). PORTER_APPROVAL_MODE=strict restores the
        # v1 "everything queues" behavior.
        from .triage import create_with_triage, project_tag

        # `project` convenience -> normalized `project:<name>` scope tag
        # (SPECS §7.1.1). Re-validated: one more tag can breach MAX_TAGS.
        tags = list(body.scope_tags)
        if body.project:
            try:
                ptag = project_tag(body.project)
            except ValueError as exc:
                raise InvalidInput(f"invalid project: {exc}")
            if ptag not in tags:
                tags.append(ptag)
            try:
                tags = _validate_tag_list(tags)
            except ValueError as exc:
                raise InvalidInput(str(exc))

        if body.content_ciphertext is not None:
            if body.encryption is None:
                raise InvalidInput(
                    "encryption is required with content_ciphertext"
                )
            rec, decision = await create_with_triage(
                request_repo,
                content=body.content_ciphertext,
                type=body.type,
                scope_tags=tags,
                confidence=body.confidence,
                source=body.source,
                actor_client_id=_client_pk(request),
                encrypted=True,
                key_id=body.encryption.key_id,
                via="api",
                scopes_used=granted,
            )
        else:
            if body.content is None:
                raise InvalidInput("content is required")
            rec, decision = await create_with_triage(
                request_repo,
                content=body.content,
                type=body.type,
                scope_tags=tags,
                confidence=body.confidence,
                source=body.source,
                actor_client_id=_client_pk(request),
                via="api",
                scopes_used=granted,
            )
        if decision.action == "auto_merge":
            return MemoryCreateOut(
                id=decision.merge_target_id,  # type: ignore[arg-type]
                status="merged",
                triage_reasons=decision.reasons,
                merged_into=decision.merge_target_id,
            )
        # create_with_triage already audited ("write" / "auto_approve");
        # the legacy "write" audit below is subsumed — keep one audit per
        # write to avoid double-counting.
        status = "active" if decision.action == "auto_approve" else "pending_review"
        return MemoryCreateOut(
            id=rec.id,
            status=status,  # type: ignore[arg-type]
            duplicate_candidates=[
                DuplicateCandidate(id=mid, similarity=round(score, 4))
                for mid, score in decision.duplicate_scores
            ],
            triage_reasons=decision.reasons,
        )

    @app.get(
        f"{API_PREFIX}/memories",
        response_model=list[MemoryOut],
        summary="List memories with filters",
    )
    async def list_memories(
        request: Request,
        status: str | None = Query(default=None, pattern="^(pending|active|archived)$"),
        tag: list[str] = Query(default=[]),
        type: str | None = Query(default=None, pattern="^(preference|fact|episode|skill_ref)$"),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        request_repo: MemoryRepository = Depends(get_repo),
        granted: list[str] = Depends(auth.require_scopes("memory.read")),
    ):
        # Server-side scope enforcement (T1, PRD §13.3): non-admin tokens see
        # only active memories, restricted to their granted tags.
        allowed = oauth_logic.effective_tag_filter(granted)
        if "memory.admin" not in granted:
            status = "active"
        tags = oauth_logic.apply_tag_filter(tag, allowed)
        if allowed is not None and not tags:
            return []  # fail closed: asked only for tags outside the grant
        rows = await request_repo.list(
            status=status, tags=tags, type=type, limit=limit, offset=offset
        )
        return [_to_out(r) for r in rows]

    @app.post(
        f"{API_PREFIX}/memories/search",
        response_model=SearchOut,
        summary="Semantic search over active memories",
    )
    async def search_memories(
        body: MemorySearch,
        request: Request,
        request_repo: MemoryRepository = Depends(get_repo),
        granted: list[str] = Depends(auth.require_scopes("memory.read")),
    ):
        # T1: the tag filter comes from the token's scopes, never from the
        # client's request alone (client tags are hints, intersected here).
        allowed = oauth_logic.effective_tag_filter(granted)
        tags = oauth_logic.apply_tag_filter(body.tags, allowed)
        if allowed is not None and not tags:
            # Fail closed: the client asked only for tags outside its grant.
            # An empty intersection must NOT become "no filter".
            await request_repo.audit(
                action="read", memory_ids=[], scopes_used=granted,
                client_id=_client_pk(request),
                meta={"top_k": body.top_k, "hits": 0, "note": "tag_filter_empty"},
            )
            return SearchOut(results=[])
        scored = await request_repo.search(
            query=body.query, top_k=body.top_k, tags=tags, types=body.types
        )
        await request_repo.audit(
            action="read",
            memory_ids=[s.record.id for s in scored],
            scopes_used=granted,
            client_id=_client_pk(request),
            meta={"top_k": body.top_k, "hits": len(scored)},
        )
        return SearchOut(
            results=[
                SearchResult(
                    id=s.record.id, content=s.record.content, type=s.record.type,
                    scope_tags=s.record.scope_tags, confidence=s.record.confidence,
                    source=s.record.source, score=round(s.score, 4),
                    untrusted_data=True,
                )
                for s in scored
            ]
        )

    @app.post(
        f"{API_PREFIX}/memories/{{memory_id}}/approve",
        response_model=MemoryOut,
        responses={404: {"model": ErrorOut}},
        summary="Approve a pending memory (owner/panel only)",
    )
    async def approve_memory(
        memory_id: UUID,
        request: Request,
        request_repo: MemoryRepository = Depends(get_repo),
        granted: list[str] = Depends(auth.require_scopes("memory.admin")),
    ):
        rec = await request_repo.approve(
            memory_id, actor_client_id=_client_pk(request)
        )
        if rec is None:
            raise NotFound(f"memory {memory_id} not found")
        # LLM Wiki layer (prototype, flag-gated): index entities on approve.
        # Best-effort and non-blocking for the approve itself — a wiki
        # failure must never fail the user's approval.
        wiki = getattr(request.app.state, "wiki_service", None)
        if wiki is not None and getattr(wiki, "enabled", False):
            try:
                await wiki.on_approved(rec)
            except Exception:  # noqa: BLE001 - prototype best-effort hook
                log.exception("wiki on_approved failed for %s", rec.id)
        # Visibility change is security-relevant: audit it (threat T7).
        await request_repo.audit(
            action="write",
            memory_ids=[rec.id],
            scopes_used=granted,
            client_id=_client_pk(request),
            meta={"transition": "pending->active"},
        )
        return _to_out(rec)

    @app.post(
        f"{API_PREFIX}/memories/{{memory_id}}/pin",
        response_model=MemoryOut,
        responses={404: {"model": ErrorOut}},
        summary="Pin/unpin a memory (pinned memories never decay)",
    )
    async def pin_memory(
        memory_id: UUID,
        body: MemoryPin,
        request: Request,
        request_repo: MemoryRepository = Depends(get_repo),
        granted: list[str] = Depends(auth.require_scopes("memory.admin")),
    ):
        rec = await request_repo.set_pinned(
            memory_id, body.pinned, actor_client_id=_client_pk(request)
        )
        if rec is None:
            raise NotFound(f"memory {memory_id} not found")
        await request_repo.audit(
            action="pin" if body.pinned else "unpin",
            memory_ids=[rec.id],
            scopes_used=granted,
            client_id=_client_pk(request),
        )
        return _to_out(rec)

    @app.delete(
        f"{API_PREFIX}/memories/{{memory_id}}",
        status_code=204,
        responses={404: {"model": ErrorOut}},
        summary="Hard delete a memory (row + vector gone, PRD US5)",
    )
    async def delete_memory(
        memory_id: UUID,
        request: Request,
        request_repo: MemoryRepository = Depends(get_repo),
        granted: list[str] = Depends(auth.require_scopes("memory.admin")),
    ):
        ok = await request_repo.delete(
            memory_id, actor_client_id=_client_pk(request)
        )
        if not ok:
            raise NotFound(f"memory {memory_id} not found")
        await request_repo.audit(
            action="delete", memory_ids=[memory_id], scopes_used=granted,
            client_id=_client_pk(request),
        )
        return None

    @app.patch(
        f"{API_PREFIX}/memories/{{memory_id}}",
        response_model=MemoryReviseOut,
        responses={404: {"model": ErrorOut}, 422: {"model": ErrorOut}},
        summary="Edit a memory with history (PRD US2; US7 optimistic concurrency)",
    )
    async def revise_memory(
        memory_id: UUID,
        body: MemoryRevise,
        request: Request,
        request_repo: MemoryRepository = Depends(get_repo),
        granted: list[str] = Depends(auth.require_scopes("memory.admin")),
    ):
        # US7 optimistic concurrency: expected_version routes through
        # update_with_version() (content-only). A version mismatch is a
        # handled outcome — 200 with conflict=true + loser_pending_id —
        # never a 409 (last-writer-wins already applied the write, and the
        # loser's content was preserved in the pending inbox).
        if body.expected_version is not None:
            if body.content is None:
                raise InvalidInput(
                    "expected_version requires a new content value"
                )
            new, loser = await request_repo.update_with_version(
                memory_id,
                expected_version=body.expected_version,
                content=body.content,
                actor_client_id=_client_pk(request),
            )
            if new is None:
                raise NotFound(f"memory {memory_id} not found")
            await request_repo.audit(
                action="write", memory_ids=[new.id], scopes_used=granted,
                client_id=_client_pk(request),
                meta={
                    "revised_from": str(memory_id),
                    "expected_version": body.expected_version,
                    "conflict": loser is not None,
                },
            )
            return MemoryReviseOut(
                memory=_to_out(new),
                conflict=loser is not None,
                loser_pending_id=loser.id if loser is not None else None,
            )
        if all(
            v is None
            for v in (body.content, body.type, body.scope_tags, body.confidence)
        ):
            raise InvalidInput("nothing to revise")
        rec = await request_repo.revise(
            memory_id,
            content=body.content,
            type=body.type,
            scope_tags=body.scope_tags,
            confidence=body.confidence,
            actor_client_id=_client_pk(request),
        )
        if rec is None:
            raise NotFound(f"memory {memory_id} not found")
        await request_repo.audit(
            action="write", memory_ids=[rec.id], scopes_used=granted,
            client_id=_client_pk(request),
            meta={"revised_from": str(memory_id)},
        )
        return MemoryReviseOut(memory=_to_out(rec), conflict=False)

    # ------------------------------------------------------------- clients
    @app.post(
        f"{API_PREFIX}/clients",
        response_model=ClientCreateOut,
        status_code=201,
        summary="Register an OAuth client (secret shown ONCE)",
    )
    async def register_client(
        body: ClientCreate,
        request: Request,
        store: OAuthStore = Depends(get_oauth_store),
    ):
        # Bootstrap: the very first registration requires the one-time setup
        # token (P0). An open bootstrap combined with a consent screen that
        # has no owner session would otherwise let any network neighbor
        # self-register and self-approve memory.admin during setup.
        # Afterwards, client management requires memory.admin — the panel
        # will own this (documented in README).
        client_count = await store.count_clients()
        if client_count == 0 and request.app.state.auth_enabled:
            provided = request.headers.get("x-porter-setup-token", "")
            if not compare_digest(provided, settings.setup_token):
                raise Unauthorized("invalid or missing setup token")
        elif request.app.state.auth_enabled and client_count > 0:
            ctx = auth.current_auth(request)
            if ctx is None:
                raise Unauthorized("missing or invalid bearer token")
            if "memory.admin" not in ctx.scopes:
                raise InsufficientScope(
                    "client registration requires memory.admin"
                )
        secret = oauth_logic.new_client_secret()
        rec = await store.create_client(
            name=body.name,
            client_id=oauth_logic.new_client_id(),
            secret_hash=oauth_logic.hash_secret(secret),
            redirect_uris=body.redirect_uris,
        )
        return ClientCreateOut(
            id=rec.id, client_id=rec.client_id, name=rec.name,
            redirect_uris=rec.redirect_uris, created_at=rec.created_at,
            client_secret=secret,
        )

    @app.get(
        f"{API_PREFIX}/clients",
        response_model=list[ClientOut],
        summary="List OAuth clients (never returns secrets)",
    )
    async def list_clients(
        store: OAuthStore = Depends(get_oauth_store),
        granted: list[str] = Depends(auth.require_scopes("memory.admin")),
    ):
        return [
            ClientOut(
                id=c.id, client_id=c.client_id, name=c.name,
                redirect_uris=c.redirect_uris, created_at=c.created_at,
            )
            for c in await store.list_clients()
        ]

    # ------------------------------------------------------------- shares (P2, memory.share)
    @app.post(
        f"{API_PREFIX}/shares",
        response_model=ShareCreateOut,
        status_code=201,
        responses={400: {"model": ErrorOut}, 422: {"model": ErrorOut}},
        summary="Mint a restricted share token (owner only)",
    )
    async def create_share_ep(
        body: ShareCreate,
        request: Request,
        store: OAuthStore = Depends(get_oauth_store),
        request_repo: MemoryRepository = Depends(get_repo),
        granted: list[str] = Depends(auth.require_scopes("memory.admin")),
    ):
        # Tag validity (preset + custom tags) is enforced inside
        # mint_share_token -> 400 invalid_scope on smuggling attempts
        # ("memory.admin", "../", "", duplicates, unknown tags). Minted
        # scopes are ALWAYS exactly [f"memory.read.{t}" for t in tags] —
        # nothing else, ever. The token is returned exactly once here; it
        # is never stored and never logged.
        name = body.name.strip()
        if not name:
            raise InvalidInput("name must not be blank")
        share, token = await oauth_logic.mint_share_token(
            store,
            name=name,
            tags=list(body.tags),
            expires_in_hours=body.expires_in_hours,
        )
        await request_repo.audit(
            action="grant",
            scopes_used=granted,
            client_id=_client_pk(request),
            meta={
                "via": "shares_api",
                "share_id": str(share.id),
                "share_name": share.name,
                "scopes": share.scopes,
                "expires_at": share.expires_at.isoformat(),
            },
        )
        return ShareCreateOut(
            id=share.id, name=share.name, scopes=share.scopes,
            created_at=share.created_at, expires_at=share.expires_at,
            revoked=share.revoked, token=token,
        )

    @app.get(
        f"{API_PREFIX}/shares",
        response_model=list[ShareOut],
        summary="List share tokens (tokens are never returned)",
    )
    async def list_shares_ep(
        store: OAuthStore = Depends(get_oauth_store),
        granted: list[str] = Depends(auth.require_scopes("memory.admin")),
    ):
        return [
            ShareOut(
                id=s.id, name=s.name, scopes=s.scopes,
                created_at=s.created_at, expires_at=s.expires_at,
                revoked=s.revoked,
            )
            for s in await store.list_shares()
        ]

    @app.delete(
        f"{API_PREFIX}/shares/{{share_id}}",
        status_code=204,
        responses={404: {"model": ErrorOut}},
        summary="Revoke a share token (dies on its next request, < 60s)",
    )
    async def revoke_share_ep(
        share_id: UUID,
        request: Request,
        store: OAuthStore = Depends(get_oauth_store),
        request_repo: MemoryRepository = Depends(get_repo),
        granted: list[str] = Depends(auth.require_scopes("memory.admin")),
    ):
        share = await store.get_share(share_id)
        if share is None:
            raise NotFound(f"share {share_id} not found")
        await store.revoke_share(share_id)
        # Invalidate the revocation-cache entry so the token dies on its
        # very next request (T2: < 60s budget); the fail-closed store read
        # in auth._resolve_share_identity is the backstop.
        request.app.state.revocation_cache.invalidate(str(share_id))
        await request_repo.audit(
            action="revoke", scopes_used=granted,
            client_id=_client_pk(request),
            meta={
                "via": "shares_api",
                "share_id": str(share_id),
                "share_name": share.name,
            },
        )
        return None

    # ------------------------------------------------------------- scope tags
    @app.get(
        f"{API_PREFIX}/scope-tags",
        response_model=ScopeTagsOut,
        summary="Preset + custom scope tags (PRD §10.3)",
    )
    async def get_scope_tags(
        store: OAuthStore = Depends(get_oauth_store),
        granted: list[str] = Depends(auth.require_scopes("memory.admin")),
    ):
        return ScopeTagsOut(
            preset=dict(oauth_logic.PRESET_SCOPE_TAGS),
            custom=[
                CustomTagOut(tag=t["tag"], label=t["label"])
                for t in await store.list_custom_tags()
            ],
        )

    @app.post(
        f"{API_PREFIX}/scope-tags",
        response_model=CustomTagOut,
        status_code=201,
        summary="Add a custom scope tag",
    )
    async def add_scope_tag(
        body: CustomTagCreate,
        store: OAuthStore = Depends(get_oauth_store),
        granted: list[str] = Depends(auth.require_scopes("memory.admin")),
    ):
        if body.tag in oauth_logic.PRESET_SCOPE_TAGS:
            raise InvalidInput(f"'{body.tag}' is a preset tag and cannot be redefined")
        await store.add_custom_tag(tag=body.tag, label=body.label)
        return CustomTagOut(tag=body.tag, label=body.label)

    @app.delete(
        f"{API_PREFIX}/scope-tags/{{tag}}",
        status_code=204,
        responses={404: {"model": ErrorOut}},
        summary="Remove a custom scope tag (presets are protected)",
    )
    async def remove_scope_tag(
        tag: str,
        store: OAuthStore = Depends(get_oauth_store),
        granted: list[str] = Depends(auth.require_scopes("memory.admin")),
    ):
        if tag in oauth_logic.PRESET_SCOPE_TAGS:
            raise InvalidInput(f"'{tag}' is a preset tag and cannot be removed")
        ok = await store.remove_custom_tag(tag)
        if not ok:
            raise NotFound(f"custom tag '{tag}' not found")
        return None

    # ------------------------------------------------------------- export
    @app.get(
        f"{API_PREFIX}/export",
        summary="Export everything (porter-export/v1, PRD §15)",
    )
    async def export_all(
        request: Request,
        request_repo: MemoryRepository = Depends(get_repo),
        granted: list[str] = Depends(auth.require_scopes("memory.admin")),
    ):
        import json as _json

        payload = await build_export_payload(request_repo)
        body = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
        await request_repo.audit(
            action="export", memory_ids=[], scopes_used=granted,
            client_id=_client_pk(request),
            meta={"format": "porter-export/v1",
                  "memory_count": payload["memory_count"]},
        )
        return JSONResponse(
            content=_json.loads(body.decode("utf-8")),
            headers={
                "Content-Disposition":
                    "attachment; filename=porter-export-v1.json"
            },
        )

    # ------------------------------------------------------------- snapshots
    @app.post(
        f"{API_PREFIX}/snapshots",
        response_model=SnapshotOut,
        status_code=201,
        summary="Create a state snapshot (PRD §9)",
    )
    async def create_snapshot_ep(
        body: SnapshotCreate,
        request_repo: MemoryRepository = Depends(get_repo),
        snap_store: SnapshotStore = Depends(get_snapshot_store),
        granted: list[str] = Depends(auth.require_scopes("state.export")),
    ):
        rec, _path = await snapshots_logic.create_snapshot(
            request_repo, snap_store,
            tags=body.tags, password=body.password,
            storage_dir=settings.snapshot_dir,
        )
        return SnapshotOut(
            id=rec.id, manifest=rec.manifest, storage_path=rec.storage_path,
            signature_ok=rec.signature_ok, created_at=rec.created_at,
        )

    @app.get(
        f"{API_PREFIX}/snapshots",
        response_model=list[SnapshotOut],
        summary="List snapshot history",
    )
    async def list_snapshots_ep(
        limit: int = Query(default=50, ge=1, le=200),
        snap_store: SnapshotStore = Depends(get_snapshot_store),
        granted: list[str] = Depends(auth.require_scopes("state.export")),
    ):
        return [
            SnapshotOut(
                id=r.id, manifest=r.manifest, storage_path=r.storage_path,
                signature_ok=r.signature_ok, created_at=r.created_at,
            )
            for r in await snap_store.list(limit=limit)
        ]

    @app.get(
        f"{API_PREFIX}/snapshots/{{snapshot_id}}/download",
        summary="Download a snapshot file",
    )
    async def download_snapshot_ep(
        snapshot_id: UUID,
        snap_store: SnapshotStore = Depends(get_snapshot_store),
        granted: list[str] = Depends(auth.require_scopes("state.export")),
    ):
        rec = await snap_store.get(snapshot_id)
        if rec is None or not os.path.isfile(rec.storage_path):
            raise NotFound(f"snapshot {snapshot_id} not found")
        return FileResponse(
            rec.storage_path,
            filename=os.path.basename(rec.storage_path),
            media_type="application/octet-stream",
        )

    @app.post(
        f"{API_PREFIX}/snapshots/apply",
        summary="Apply a snapshot file (dry-run or import to pending inbox, PRD §9.2)",
    )
    async def apply_snapshot_ep(
        request: Request,
        file: UploadFile = File(...),
        password: str | None = Form(default=None),
        dry_run: bool = Form(default=True),
        request_repo: MemoryRepository = Depends(get_repo),
        granted: list[str] = Depends(auth.require_scopes("state.import")),
    ):
        import tempfile

        # Spill to disk (never trust in-RAM size for user uploads).
        suffix = os.path.basename(file.filename or "upload.porter")[-64:]
        with tempfile.NamedTemporaryFile(
            prefix="porter-apply-", suffix="-" + suffix, delete=False
        ) as tmp:
            while True:
                chunk = await file.read(1 << 20)
                if not chunk:
                    break
                tmp.write(chunk)
            tmp_path = tmp.name
        try:
            if dry_run:
                try:
                    summary = snapshots_logic.inspect_snapshot_file(tmp_path, password)
                except ValueError as exc:
                    raise InvalidInput(str(exc))
                return {"dry_run": True, **summary}
            try:
                report = await snapshots_logic.apply_snapshot_file(
                    request_repo, request_repo.audit,
                    path=tmp_path, password=password,
                )
            except ValueError as exc:
                raise InvalidInput(str(exc))
            return {"dry_run": False, **report}
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ------------------------------------------------------------- OAuth
    @app.get(
        "/oauth/authorize",
        response_class=HTMLResponse,
        summary="OAuth consent screen (PRD §10.2)",
    )
    async def oauth_authorize_get(
        client_id: str = Query(...),
        redirect_uri: str = Query(...),
        scope: str = Query(default=""),
        state: str = Query(default=""),
        response_type: str = Query(default="code"),
        store: OAuthStore = Depends(get_oauth_store),
    ):
        if response_type != "code":
            raise InvalidGrant("only response_type=code is supported")
        client = await store.get_client_by_client_id(client_id)
        if client is None:
            # Cannot redirect safely: the client is unknown.
            raise InvalidGrant("unknown client_id")
        if redirect_uri not in client.redirect_uris:
            raise InvalidGrant("redirect_uri is not registered for this client")
        scopes = scope.split()
        if not scopes:
            raise InvalidScope("no scope requested")
        custom = await _custom_tag_map(store)
        try:
            oauth_logic.validate_scopes(scopes, custom)
        except InvalidScope:
            # redirect_uri is trusted now: redirect with the OAuth2 error.
            return RedirectResponse(
                _redirect_with_params(
                    redirect_uri, {"error": "invalid_scope", "state": state}
                ),
                status_code=302,
            )
        return HTMLResponse(
            oauth_logic.render_consent_screen(
                client_name=client.name, scopes=scopes, client_id=client_id,
                redirect_uri=redirect_uri, state=state, requested=scopes,
                custom_tags=custom,
            )
        )

    @app.post("/oauth/authorize", summary="Consent decision (approve/deny)")
    async def oauth_authorize_post(
        request: Request,
        client_id: str = Form(...),
        redirect_uri: str = Form(...),
        state: str = Form(default=""),
        decision: str = Form(...),
        granted_scopes: list[str] = Form(default=[]),
        requested_scopes: list[str] = Form(default=[]),
        store: OAuthStore = Depends(get_oauth_store),
        request_repo: MemoryRepository = Depends(get_repo),
    ):
        # CSRF guard (T1): without this, a malicious website could forge a
        # cross-origin form POST that approves scopes for an attacker's
        # registered client while the owner simply has the page open.
        # Same rule as the owner panel (porter/origin.py).
        check_same_origin(request)
        client = await store.get_client_by_client_id(client_id)
        if client is None or redirect_uri not in client.redirect_uris:
            raise InvalidGrant("invalid client or redirect_uri")
        if decision != "approve":
            return RedirectResponse(
                _redirect_with_params(
                    redirect_uri, {"error": "access_denied", "state": state}
                ),
                status_code=302,
            )
        # Only scopes that were actually requested can be granted; the owner
        # may grant a subset via the checkboxes.
        granted = [s for s in granted_scopes if s in requested_scopes]
        if not granted:
            return RedirectResponse(
                _redirect_with_params(
                    redirect_uri, {"error": "invalid_scope", "state": state}
                ),
                status_code=302,
            )
        oauth_logic.validate_scopes(granted, await _custom_tag_map(store))  # 400 on tampered form values
        code = oauth_logic.new_auth_code()
        await store.create_auth_code(
            code_hash=oauth_logic.sha256_hex(code),
            client_id=client.id,
            scopes=granted,
            redirect_uri=redirect_uri,
            expires_at=datetime.now(timezone.utc)
            + timedelta(minutes=settings.auth_code_ttl_minutes),
        )
        await store.create_grant(client_id=client.id, scopes=granted)
        await request_repo.audit(
            action="grant", client_id=client.id, scopes_used=granted,
            meta={"via": "consent_screen"},
        )
        return RedirectResponse(
            _redirect_with_params(redirect_uri, {"code": code, "state": state}),
            status_code=302,
        )

    @app.post(
        "/oauth/token",
        response_model=TokenOut,
        summary="Token endpoint: authorization_code / refresh_token",
    )
    async def oauth_token(
        grant_type: str = Form(...),
        code: str = Form(default=""),
        redirect_uri: str = Form(default=""),
        refresh_token: str = Form(default=""),
        client_id: str = Form(...),
        client_secret: str = Form(...),
        store: OAuthStore = Depends(get_oauth_store),
        request_repo: MemoryRepository = Depends(get_repo),
    ):
        if grant_type == "authorization_code":
            if not code or not redirect_uri:
                raise InvalidGrant("code and redirect_uri are required")
            pair = await oauth_logic.exchange_code(
                store, request_repo.audit, code=code, redirect_uri=redirect_uri,
                client_id=client_id, client_secret=client_secret,
            )
        elif grant_type == "refresh_token":
            if not refresh_token:
                raise InvalidGrant("refresh_token is required")
            pair = await oauth_logic.refresh_token_grant(
                store, request_repo.audit, refresh_token=refresh_token,
                client_id=client_id, client_secret=client_secret,
            )
        else:
            raise InvalidGrant(f"unsupported grant_type: {grant_type}")
        return TokenOut(
            access_token=pair.access_token,
            expires_in=pair.expires_in,
            refresh_token=pair.refresh_token,
            scope=pair.scope,
        )

    @app.post(
        "/oauth/revoke",
        response_model=RevokeOut,
        summary="Revoke one token or all tokens for a client (T2)",
    )
    async def oauth_revoke(
        request: Request,
        token: str = Form(...),
        client_id: str = Form(...),
        client_secret: str = Form(...),
        revoke_all: bool = Form(default=False),
        store: OAuthStore = Depends(get_oauth_store),
        request_repo: MemoryRepository = Depends(get_repo),
    ):
        revoked = await oauth_logic.revoke_token_value(
            store,
            request_repo.audit,
            request.app.state.revocation_cache.invalidate,
            token=token,
            client_id=client_id,
            client_secret=client_secret,
            revoke_all=revoke_all,
        )
        return RevokeOut(revoked=revoked)

    # ------------------------------------------------------------- audit
    @app.get(
        f"{API_PREFIX}/audit-log",
        summary="Audit log (append-only)",
    )
    async def get_audit_log(
        limit: int = Query(default=100, ge=1, le=500),
        client_id: UUID | None = Query(default=None),
        since: datetime | None = Query(default=None),
        request_repo: MemoryRepository = Depends(get_repo),
        granted: list[str] = Depends(auth.require_scopes("memory.admin")),
    ):
        rows = await request_repo.audit_list(
            limit=limit, client_id=client_id, since=since
        )
        return [
            {
                "id": str(r.id), "client_id": str(r.client_id) if r.client_id else None,
                "action": r.action, "memory_ids": [str(m) for m in r.memory_ids],
                "scopes_used": r.scopes_used, "meta": r.meta,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]

    # ------------------------------------------------------------- digest (automation)
    @app.get(
        f"{API_PREFIX}/digest",
        summary="Automation digest: pending queue, auto-approvals, conflicts",
    )
    async def get_digest(
        request: Request,
        request_repo: MemoryRepository = Depends(get_repo),
        granted: list[str] = Depends(auth.require_scopes("memory.read")),
    ):
        """Fail-closed visibility: pending COUNTS are safe for any reader,
        but pending CONTENTS never leak to non-admin callers (inbox rule —
        unapproved content stays invisible, same as the sync feed)."""
        is_admin = "memory.admin" in granted
        allowed = oauth_logic.effective_tag_filter(granted)

        def _visible(m) -> bool:
            if allowed is None:
                return True
            return bool(set(m.scope_tags or []).intersection(allowed))

        pendings = await request_repo.list(status="pending", limit=1000)
        by_reason: dict[str, int] = {}
        for m in pendings:
            if not _visible(m):
                continue
            for r in (m.triage_reasons or ["unknown"]):
                by_reason[r] = by_reason.get(r, 0) + 1
        visible_pending = sum(1 for m in pendings if _visible(m))

        conflicts = []
        if is_admin:
            for m in pendings:
                if "possible_conflict" in (m.triage_reasons or []):
                    conflicts.append({
                        "id": str(m.id),
                        "content_preview": m.content[:160],
                        "created_at": m.created_at.isoformat(),
                    })
                    if len(conflicts) >= 20:
                        break

        recent_auto = []
        for a in await request_repo.audit_list(limit=300):
            if a.action != "auto_approve" or len(recent_auto) >= 10:
                continue
            for mid in a.memory_ids:
                m = await request_repo.get(mid)
                if m is None or m.status != "active" or not _visible(m):
                    continue
                # get() applied decay above; skip if it just got archived.
                if m.status != "active":
                    continue
                recent_auto.append({
                    "id": str(m.id),
                    "content_preview": m.content[:160],
                    "created_at": m.created_at.isoformat(),
                })
                if len(recent_auto) >= 10:
                    break

        return {
            "pending_count": visible_pending,
            "pending_by_reason": by_reason,
            "conflicts": conflicts,
            "recent_auto_approved": recent_auto,
        }

    # ------------------------------------------------------------- sync (P1, PRD §9.5)
    @app.get(
        f"{API_PREFIX}/sync/delta",
        response_model=SyncDeltaOut,
        responses={422: {"model": ErrorOut}},
        summary="Incremental sync delta (P1 auto-sync)",
    )
    async def sync_delta(
        request: Request,
        since: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
        # Project filter (SPECS §7.1.1): only this project's working memory
        # plus untagged/global memories. Omit for the full feed.
        project: str | None = Query(default=None, max_length=128),
        request_repo: MemoryRepository = Depends(get_repo),
        granted: list[str] = Depends(auth.require_scopes("memory.read")),
    ):
        # Visibility rules (tag-scope filtering, inbox rule, non-admin
        # active-only) live in porter/sync.py, shared with the MCP tool.
        payload = await sync_logic.build_sync_delta(
            request_repo, since=since, limit=limit, granted=granted,
            project=project,
        )
        # Online-status bookkeeping: refresh last_seen, but NEVER advance
        # the cursor on a fetch — the adapter acks explicitly via POST.
        await sync_logic.touch_cursor(
            request_repo, _adapter_id(request)
        )
        await request_repo.audit(
            action="read",
            memory_ids=[UUID(c["memory_id"]) for c in payload["changes"]],
            scopes_used=granted,
            client_id=_client_pk(request),
            # T7: record WHO pulled (adapter identity: token client_id, or
            # "owner"); ids/counts only, never content.
            meta={"via": "sync", "adapter": _adapter_id(request)},
        )
        return SyncDeltaOut(
            changes=[SyncChange(**c) for c in payload["changes"]],
            cursor=payload["cursor"],
        )

    @app.post(
        f"{API_PREFIX}/sync/cursor",
        response_model=CursorOut,
        responses={422: {"model": ErrorOut}},
        summary="Ack a sync cursor (explicit adapter checkpoint)",
    )
    async def sync_cursor(
        body: CursorIn,
        request: Request,
        request_repo: MemoryRepository = Depends(get_repo),
    ):
        # Auth: any valid bearer, no special scope. The adapter is
        # identified by its own token (never a client-supplied id).
        adapter_id = _require_adapter_id(request)
        row = await request_repo.upsert_cursor(adapter_id, body.cursor)
        return CursorOut(
            adapter_id=row["adapter_id"],
            cursor=row["cursor"],
            last_seen=row["last_seen"],
        )

    # ------------------------------------------- seamless switching (v0.2.0)
    @app.post(
        f"{API_PREFIX}/switch/handoff",
        responses={422: {"model": ErrorOut}},
        summary="Build a handoff payload for switching harnesses",
    )
    async def switch_handoff(
        body: SwitchHandoffIn,
        request: Request,
        request_repo: MemoryRepository = Depends(get_repo),
        granted: list[str] = Depends(auth.require_scopes("memory.read")),
    ):
        # build_handoff records the harness_switch audit event itself —
        # the endpoint MUST NOT record it again (see switch.py docstring).
        payload = await switch_logic.build_handoff(
            request_repo,
            project=body.project,
            from_harness=body.from_harness,
            to_harness=body.to_harness,
            top_k=body.top_k,
            actor_client_id=_client_pk(request),
        )
        payload["briefing"] = switch_logic.render_briefing(payload)
        return payload

    @app.post(
        f"{API_PREFIX}/writeback/sync",
        response_model=WritebackOut,
        responses={422: {"model": ErrorOut}},
        summary="Write Porter memories back to a harness's native format",
    )
    async def writeback_sync(
        body: WritebackIn,
        request: Request,
        request_repo: MemoryRepository = Depends(get_repo),
        granted: list[str] = Depends(auth.require_scopes("memory.read")),
    ):
        # Gather active memories visible to the caller, then render them
        # into the harness's native format (porter-sync marked zones only).
        records = await request_repo.list(status="active", limit=200)
        memories = [
            {
                "id": str(r.id),
                "content": r.content,
                "type": r.type,
                "status": r.status,
                "scope_tags": list(r.scope_tags or ()),
                "confidence": r.confidence,
                "source": dict(r.source or {}),
                "encrypted": bool(r.encrypted),
                "triage_reasons": list(getattr(r, "triage_reasons", None) or ()),
            }
            for r in records
        ]
        writer = writeback_logic.get_writer(body.harness)
        result = await writer.write(
            memories,
            project=body.project,
            dry_run=body.dry_run,
            include_sensitive=body.include_sensitive,
        )
        await request_repo.audit(
            action="writeback",
            memory_ids=[r.id for r in records[:50]],
            client_id=_client_pk(request),
            scopes_used=granted,
            meta={
                "harness": body.harness,
                "project": body.project,
                "dry_run": body.dry_run,
                "written": len(result.written_files),
            },
        )
        return WritebackOut(**result.to_dict())

    # ------------------------------------------------------- conflicts (v0.2.0)
    @app.get(
        f"{API_PREFIX}/memories/{{memory_id}}/contradictions",
        responses={422: {"model": ErrorOut}},
        summary="Detect memories contradicting the given one",
    )
    async def memory_contradictions(
        memory_id: UUID,
        request: Request,
        request_repo: MemoryRepository = Depends(get_repo),
        granted: list[str] = Depends(auth.require_scopes("memory.read")),
    ):
        rec = await request_repo.get(memory_id)
        if rec is None:
            raise NotFound("memory not found")
        return {
            "memory_id": str(memory_id),
            "contradictions": await conflicts_logic.detect_contradictions(
                request_repo, memory_id
            ),
        }

    @app.post(
        f"{API_PREFIX}/memories/merge",
        responses={422: {"model": ErrorOut}},
        summary="Semantically merge memories with provenance",
    )
    async def memory_merge(
        body: MemoryMergeIn,
        request: Request,
        request_repo: MemoryRepository = Depends(get_repo),
        granted: list[str] = Depends(auth.require_scopes("memory.write")),
    ):
        merged = await conflicts_logic.semantic_merge(
            request_repo,
            body.memory_ids,
            strategy=body.strategy,
            content=body.content,
            actor_client_id=_client_pk(request),
        )
        return merged

    @app.post(
        f"{API_PREFIX}/memories/resolve",
        responses={422: {"model": ErrorOut}},
        summary="Resolve a conflict: winner stays, losers archived",
    )
    async def memory_resolve(
        body: ConflictResolveIn,
        request: Request,
        request_repo: MemoryRepository = Depends(get_repo),
        granted: list[str] = Depends(auth.require_scopes("memory.write")),
    ):
        result = await conflicts_logic.resolve_conflict(
            request_repo,
            body.winner_id,
            body.loser_ids,
            resolution=body.resolution,
            actor_client_id=_client_pk(request),
        )
        return result

    # ------------------------------------------------------- e2ee hub (v0.2.0)
    @app.post(
        f"{API_PREFIX}/e2ee/devices",
        responses={422: {"model": ErrorOut}},
        summary="Register a device's X25519 public key (blind hub)",
    )
    async def e2ee_register_device(
        body: DeviceRegisterIn,
        request: Request,
        store: EnvelopeStore = Depends(get_envelope_store),
        granted: list[str] = Depends(auth.require_scopes("memory.read")),
    ):
        # Anyone with a valid token may register a device; the public key
        # alone is useless without the private key, which never leaves the
        # device. The hub stays blind by construction.
        rec = await store.register_device(body.device_id, body.public_key)
        return {
            "device_id": rec.device_id,
            "public_key": rec.public_key,
            "created_at": rec.created_at.isoformat(),
        }

    @app.post(
        f"{API_PREFIX}/e2ee/envelopes",
        responses={422: {"model": ErrorOut}},
        summary="Post a sealed key envelope for another device",
    )
    async def e2ee_post_envelope(
        body: EnvelopePostIn,
        request: Request,
        store: EnvelopeStore = Depends(get_envelope_store),
        granted: list[str] = Depends(auth.require_scopes("memory.read")),
    ):
        env = await store.post_envelope(
            body.to_device, body.from_device, body.sealed
        )
        return {
            "id": str(env.id),
            "to_device": env.to_device,
            "from_device": env.from_device,
            "created_at": env.created_at.isoformat(),
        }

    @app.get(
        f"{API_PREFIX}/e2ee/envelopes",
        responses={422: {"model": ErrorOut}},
        summary="Claim pending sealed envelopes for a device",
    )
    async def e2ee_claim_envelopes(
        request: Request,
        device_id: str = Query(min_length=1, max_length=64),
        store: EnvelopeStore = Depends(get_envelope_store),
        granted: list[str] = Depends(auth.require_scopes("memory.read")),
    ):
        # Claiming is idempotent: envelopes are marked claimed atomically on
        # first claim; repeats return [].
        envs = await store.claim_envelopes(device_id)
        return {
            "device_id": device_id,
            "envelopes": [
                {
                    "id": str(e.id),
                    "from_device": e.from_device,
                    "sealed": e.sealed,
                    "created_at": e.created_at.isoformat(),
                }
                for e in envs
            ],
        }

    # ---------------------------------------------------------- teams (v0.2.0)
    @app.post(
        f"{API_PREFIX}/workspaces",
        response_model=WorkspaceOut,
        responses={422: {"model": ErrorOut}},
        summary="Create a shared project workspace",
    )
    async def workspace_create(
        body: WorkspaceCreateIn,
        request: Request,
        request_repo: MemoryRepository = Depends(get_repo),
        store=Depends(get_team_store),
        granted: list[str] = Depends(auth.require_scopes("memory.write")),
    ):
        ws = await teams_logic.create_workspace(
            store,
            request_repo,
            name=body.name,
            owner_client_id=_client_pk(request),
            project=body.project,
        )
        return WorkspaceOut(
            id=ws["id"], name=ws["name"], project_tag=ws["project_tag"],
            created_at=ws["created_at"],
        )

    @app.get(
        f"{API_PREFIX}/workspaces",
        responses={422: {"model": ErrorOut}},
        summary="List my workspaces",
    )
    async def workspace_list(
        request: Request,
        store=Depends(get_team_store),
        granted: list[str] = Depends(auth.require_scopes("memory.read")),
    ):
        client_id = _client_pk(request)
        items = await store.list_workspaces_for_client(client_id)
        return {
            "workspaces": [
                {
                    "id": str(w.id),
                    "name": w.name,
                    "project_tag": w.project_tag,
                    "created_at": w.created_at.isoformat(),
                }
                for w in items
            ]
        }

    @app.post(
        f"{API_PREFIX}/workspaces/{{workspace_id}}/members",
        responses={422: {"model": ErrorOut}},
        summary="Invite a member to a workspace (owner only)",
    )
    async def workspace_invite(
        workspace_id: UUID,
        body: WorkspaceInviteIn,
        request: Request,
        request_repo: MemoryRepository = Depends(get_repo),
        store=Depends(get_team_store),
        granted: list[str] = Depends(auth.require_scopes("memory.write")),
    ):
        member = await teams_logic.invite(
            store,
            request_repo,
            workspace_id=workspace_id,
            client_id=body.client_id,
            role=body.role,
            actor_client_id=_client_pk(request),
        )
        return member

    # ------------------------------------------------------- lifecycle (v0.2.0)
    @app.post(
        f"{API_PREFIX}/admin/lifecycle/sweep",
        responses={422: {"model": ErrorOut}},
        summary="Run the memory lifecycle sweeper (admin)",
    )
    async def lifecycle_sweep(
        request: Request,
        request_repo: MemoryRepository = Depends(get_repo),
        granted: list[str] = Depends(auth.require_scopes("memory.admin")),
        batch_size: int = Query(default=500, ge=1, le=2000),
    ):
        # Archives active memories whose half-life-decayed confidence fell
        # below ARCHIVE_CONFIDENCE. Idempotent: re-runs archive nothing new.
        result = await lifecycle_logic.sweep(
            request_repo, batch_size=batch_size, actor="lifecycle_sweeper"
        )
        result["archived_ids"] = [str(i) for i in result["archived_ids"]]
        await request_repo.audit(
            action="lifecycle_sweep",
            memory_ids=[],
            client_id=_client_pk(request),
            scopes_used=granted,
            meta={
                "scanned": result["scanned"],
                "archived": result["archived"],
            },
        )
        return result

    # ------------------------------------------------------------- health
    @app.get("/healthz", response_model=HealthOut)
    async def healthz():
        from . import __version__

        return HealthOut(status="ok", version=__version__)

    # ------------------------------------------------------------- panel (PRD §8 F4)
    # Owner dashboard: localhost-only gate enforced inside porter/panel.py.
    import pathlib as _pathlib

    from .panel import router as panel_router

    _static_dir = _pathlib.Path(__file__).parent / "static"
    if _static_dir.is_dir():
        app.mount(
            "/panel/static", StaticFiles(directory=str(_static_dir)),
            name="panel-static",
        )
    app.include_router(panel_router)


    # ------------------------------------------------------------- MCP (PRD §8 F3)
    # Mounted LAST: mounts are matched in registration order, so /api/v1/*
    # and /oauth/* keep precedence over the /mcp sub-app.
    if enable_mcp:
        from .mcp_server import MCPDeps, mount_mcp

        mount_mcp(
            app,
            MCPDeps(
                repo_fn=lambda: app.state.repo,
                oauth_store_fn=lambda: app.state.oauth_store,
                revocation_cache_fn=lambda: app.state.revocation_cache,
                rate_limiter_fn=lambda: app.state.rate_limiter,
                auth_enabled_fn=lambda: app.state.auth_enabled,
                wiki_fn=lambda: getattr(app.state, "wiki_service", None),
                llm_extractor_kind_fn=lambda: settings.llm_extractor_kind,
            ),
            path="/mcp",
        )

    return app

