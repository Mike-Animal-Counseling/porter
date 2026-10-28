"""Authentication + authorization middleware (W2, PRD §10).

Flow per request (non-public paths):
1. Resolve auth (optional): parse `Authorization: Bearer <JWT>`, verify
   signature + expiry, then check the jti blacklist (T2). Revoked -> 401
   `token_revoked`. Owner-minted share tokens (sub == "share", P2
   memory.share) resolve against the `shares` table instead: accepted iff
   found AND not revoked AND not expired -> AuthContext(client_pk=None,
   client_id="share:<id>", scopes=[memory.read.<tag>...]). Done FIRST so the
   rate-limit key uses the VERIFIED identity — keying on unverified JWT
   claims would let an attacker mint fresh buckets with forged `sub`
   values (T5 bypass). Shares get per-token buckets (never "client:None").
2. Rate limit (T5): key = verified client_pk when authenticated, else
   client IP. 429 `rate_limited` when the bucket is empty.
3. `require_scopes(...)` dependencies enforce scopes server-side (T1).

Public paths (no bearer needed, still rate-limited by IP):
  /healthz, /docs, /openapi.json, /redoc, /oauth/authorize, /oauth/token.
POST /api/v1/clients is auth-optional: first registration requires the
one-time setup token (P0); afterwards it requires `memory.admin`.
The panel will own client management (documented in README).

When AUTH_ENABLED=0 the middleware installs an owner context with every
scope (W1 behavior, local dev only).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import jwt
from fastapi import Request
from fastapi.responses import JSONResponse

from . import oauth as oauth_logic
from .errors import (
    InsufficientScope,
    PorterError,
    RateLimited,
    TokenRevoked,
    Unauthorized,
)
from .oauth import scope_implies
from .schemas import ErrorOut

ALL_KNOWN_SCOPES = list(oauth_logic.BASE_SCOPES)

PUBLIC_PATHS = {
    "/healthz",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/oauth/authorize",
    "/oauth/token",
}


def _is_mcp_path(path: str) -> bool:
    """The mounted MCP sub-app bypasses FastAPI dependencies, so the
    middleware enforces auth for it directly (T1): no anonymous MCP access,
    not even initialize/tools-list. Tool calls re-verify independently
    (defense in depth; stdio has no middleware at all)."""
    return path == "/mcp" or path.startswith("/mcp/")


# Path prefixes reachable without a bearer token. The panel (/panel) enforces
# its own owner-session/loopback gate (porter/origin.py): it is the owner
# interface, and main.py binds 127.0.0.1 by default. Host validation runs
# outside this middleware before public-path or owner decisions.
PUBLIC_PATH_PREFIXES = ("/panel", "/owner")


def _is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    return any(path == p or path.startswith(p + "/") for p in PUBLIC_PATH_PREFIXES)


# (method, path) pairs where auth is resolved but not required; the endpoint
# decides (used by POST /clients bootstrap).
AUTH_OPTIONAL = {("POST", "/api/v1/clients")}


@dataclass
class AuthContext:
    client_pk: UUID | None  # clients.id; None in owner context (auth disabled)
    client_id: str  # public client_id string, or "owner"
    scopes: list[str]


class RevocationCache:
    """jti -> revoked, 30s TTL. Revoked tokens die well inside the 60s
    budget (T2/US3); explicit invalidation on local revoke paths."""

    def __init__(self, ttl_seconds: int = 30) -> None:
        self.ttl = ttl_seconds
        self._cache: dict[str, tuple[bool, float]] = {}

    def get(self, jti: str) -> bool | None:
        hit = self._cache.get(str(jti))
        if hit is None:
            return None
        revoked, ts = hit
        if time.monotonic() - ts > self.ttl:
            self._cache.pop(jti, None)
            return None
        return revoked

    def set(self, jti: str, revoked: bool) -> None:
        self._cache[str(jti)] = (revoked, time.monotonic())

    def invalidate(self, jti: str | None = None) -> None:
        if jti is None:
            self._cache.clear()
        else:
            self._cache.pop(str(jti), None)


async def _resolve_for_path(request: Request) -> AuthContext | None:
    """Auth resolution per path class. Raises PorterError on bad credentials;
    returns None for public paths and for auth-optional paths without a
    usable bearer token (the endpoint decides)."""
    if _is_public_path(request.url.path):
        return None
    if not request.app.state.auth_enabled:
        return AuthContext(client_pk=None, client_id="owner", scopes=list(ALL_KNOWN_SCOPES))
    return await _resolve_auth(request)  # may be None on AUTH_OPTIONAL


async def is_jti_revoked(oauth_store, revocation_cache: RevocationCache, jti: str) -> bool:
    """Fail-closed revocation check, transport-agnostic (HTTP + MCP)."""
    hit = revocation_cache.get(jti)
    if hit is not None:
        return hit
    row = await oauth_store.get_token(UUID(jti))
    revoked = row is None or bool(row.revoked)  # unknown jti: fail closed
    revocation_cache.set(jti, revoked)
    return revoked


async def verify_bearer_token(
    token: str, oauth_store, revocation_cache: RevocationCache
) -> AuthContext:
    """Standalone Bearer verification for non-HTTP transports (MCP stdio/HTTP).

    Mirrors the HTTP middleware's checks (signature, expiry, revocation) so a
    token rejected over REST is rejected identically over MCP (PRD §8 F3).
    Raises PorterError (Unauthorized / TokenRevoked) on failure.
    """
    try:
        claims = jwt.decode(
            token,
            oauth_logic.jwt_signing_key(),
            algorithms=["HS256"],
            options={"require": ["exp", "jti", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise Unauthorized("access token expired")
    except jwt.InvalidTokenError:
        raise Unauthorized("invalid access token")
    return await _resolve_token_identity(oauth_store, revocation_cache, claims)


async def _resolve_share_identity(
    oauth_store, revocation_cache: RevocationCache, jti: str
) -> AuthContext:
    """Accept an owner-minted share token (P2, memory.share).

    sub == "share" marks share JWTs; the jti IS the share id. Accepted iff
    the share exists AND is not revoked AND is not expired. DELETE
    /api/v1/shares/{id} invalidates the cache entry, so a revoked share
    dies on the very next request (well inside the 60s budget, T2). Unknown
    or forged share jtis fail closed as TokenRevoked.
    """
    # NOTE: is_jti_revoked() is NOT consulted here — it fail-closes on
    # tokens-table misses, and share jtis deliberately live in `shares`,
    # not `tokens`.
    if revocation_cache.get(jti) is True:
        raise TokenRevoked("share token has been revoked")
    try:
        share = await oauth_store.get_share(UUID(jti))
    except (ValueError, AttributeError, TypeError):
        share = None
    now = datetime.now(UTC)
    if share is None or share.revoked or share.expires_at <= now:
        raise TokenRevoked("share token has been revoked")
    return AuthContext(
        client_pk=None,
        client_id=f"share:{share.id}",
        scopes=list(share.scopes),
    )


async def _resolve_token_identity(
    oauth_store, revocation_cache: RevocationCache, claims: dict
) -> AuthContext:
    """Single identity-resolution helper behind verify_bearer_token (MCP) and
    _resolve_auth (HTTP) — one code path, no divergence (P2 memory.share).

    Regular client tokens: tokens-table jti lookup + revocation blacklist
    (T2). Share tokens: sub == "share" -> _resolve_share_identity."""
    jti = str(claims["jti"])
    if str(claims.get("sub")) == "share":
        return await _resolve_share_identity(oauth_store, revocation_cache, jti)
    if await is_jti_revoked(oauth_store, revocation_cache, jti):
        raise TokenRevoked("access token has been revoked")
    return AuthContext(
        client_pk=UUID(str(claims["sub"])),
        client_id=str(claims.get("cid", claims["sub"])),
        scopes=list(claims.get("scopes", [])),
    )


async def _resolve_auth(request: Request) -> AuthContext | None:
    """Return AuthContext or None (caller decides 401 vs optional)."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    token = header[7:].strip()
    try:
        claims = jwt.decode(
            token,
            oauth_logic.jwt_signing_key(),
            algorithms=["HS256"],
            options={"require": ["exp", "jti", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise Unauthorized("access token expired")
    except jwt.InvalidTokenError:
        raise Unauthorized("invalid access token")
    return await _resolve_token_identity(
        request.app.state.oauth_store, request.app.state.revocation_cache, claims
    )


async def auth_middleware(request: Request, call_next):
    """HTTP middleware: resolve auth -> rate limit -> enforce.

    PorterError raised here is converted to the unified {error:{...}} shape
    directly, because pure-ASGI middleware exceptions bypass FastAPI's
    exception handlers.
    """
    try:
        # Resolve auth FIRST so the rate-limit key is the VERIFIED identity.
        # Keying on unverified JWT claims would let an attacker mint fresh
        # buckets with forged `sub` values (T5 bypass). An invalid token still
        # counts against the caller's IP bucket.
        try:
            resolved = await _resolve_for_path(request)
            auth_error: PorterError | None = None
        except PorterError as exc:
            resolved, auth_error = None, exc
        ip = request.client.host if request.client else "unknown"
        # T5: the rate-limit key is the VERIFIED identity. client_pk is None
        # for the owner context (auth disabled) AND for share tokens
        # (memory.share, P2) — keying both on "client:None" would let one
        # share burn the bucket for every other share (DoS vector), so
        # shares get their own per-token bucket on the verified client_id.
        if resolved is None:
            rate_key = f"ip:{ip}"
        elif resolved.client_pk is not None:
            rate_key = f"client:{resolved.client_pk}"
        else:
            rate_key = f"share:{resolved.client_id}"
        if not request.app.state.rate_limiter.allow(rate_key):
            raise RateLimited("rate limit exceeded: 60 requests/minute per client")
        if auth_error is not None:
            raise auth_error
        if resolved is None and request.app.state.auth_enabled and _is_mcp_path(request.url.path):
            # /mcp is strict: the sub-app has no FastAPI dependencies, so a
            # missing token must 401 here rather than sailing through to an
            # anonymous initialize/tools-list.
            raise Unauthorized("missing or invalid bearer token")
        request.state.auth = resolved
    except PorterError as exc:
        if (
            request.url.path == "/owner/login"
            and "text/html" in request.headers.get("accept", "")
            and isinstance(exc, RateLimited)
        ):
            from .owner_login import login_language, render_login

            return render_login(login_language(request), error="rate", status_code=429)
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorOut(error={"code": exc.code, "message": exc.message}).model_dump(),
        )
    return await call_next(request)


def current_auth(request: Request) -> AuthContext | None:
    """Dependency: the resolved auth context (None when auth optional+absent)."""
    return getattr(request.state, "auth", None)


def require_scopes(*scopes: str):
    """Dependency factory. Enforces scopes server-side (T1, PRD §13.3)."""

    async def _dep(request: Request) -> list[str]:
        if not request.app.state.auth_enabled:
            return sorted(ALL_KNOWN_SCOPES)  # owner context (W1 behavior)
        ctx: AuthContext | None = getattr(request.state, "auth", None)
        if ctx is None:
            raise Unauthorized("missing or invalid bearer token")
        for required in scopes:
            if not scope_implies(ctx.scopes, required):
                raise InsufficientScope(f"missing required scope: {required}")
        if "memory.admin" in ctx.scopes:
            return sorted(ALL_KNOWN_SCOPES)
        return sorted(ctx.scopes)

    return _dep


# Read/write/admin aliases used by the routers.
read_scopes = require_scopes("memory.read")
write_scopes = require_scopes("memory.write")
admin_scopes = require_scopes("memory.admin")
owner = admin_scopes
