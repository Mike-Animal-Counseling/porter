"""OAuth 2.0 authorization-code flow for Porter (PRD §10).

Design decisions (documented, production-grade):
- Access tokens are **JWTs (HS256)**: stateless verification, 1h TTL. Revocation
  is enforced via a jti blacklist (DB `tokens.revoked` + 30s in-process cache),
  so a revoked token dies within ~30s, inside the PRD's 60s budget (T2/US3).
- Refresh tokens and authorization codes are **opaque random strings**; only
  sha256(...) is persisted. Presenting the raw value is the only way to use it.
- Client secrets are **argon2** hashes; the raw secret is shown exactly once,
  at registration.
- Signing key: PORTER_JWT_SECRET if set, else a per-process random key with a
  loud warning (tokens do not survive restarts). Never hardcoded.
"""

from __future__ import annotations

import hashlib
import html
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from .config import settings
from .errors import InvalidGrant, InvalidScope, TokenRevoked
from .oauth_store import OAuthStore, ShareRecord

log = logging.getLogger("porter.oauth")

# ------------------------------------------------------------------ scopes
# PRD §10.3: 10 preset scope_tags (user-customizable in the panel later).
PRESET_SCOPE_TAGS: dict[str, str] = {
    "dietary": "饮食",
    "coding_style": "代码风格",
    "workflow": "工作流偏好",
    "work": "工作信息",
    "travel": "旅行",
    "health": "健康",
    "finance": "财务",
    "social": "社交关系",
    "device": "设备环境",
    "general": "通用",
}

# base scope -> (short Chinese label, longer explanation for the consent screen)
BASE_SCOPES: dict[str, tuple[str, str]] = {
    "memory.read": (
        "读取全部记忆",
        "允许检索并查看你的所有记忆条目（不分标签）。",
    ),
    "memory.write": (
        "写入记忆",
        "允许提交新记忆。低风险内容可自动生效；敏感、冲突或低置信度内容须经你确认。严格模式下所有写入均须确认。",
    ),
    "memory.admin": (
        "管理记忆",
        "允许批准 / 删除记忆、查看审计日志、管理接入方。⚠️ 最高权限，请谨慎授予。",
    ),
    "state.export": (
        "导出状态快照",
        "允许把记忆与 agent 状态打包成快照文件导出。",
    ),
    "state.import": (
        "导入状态快照",
        "允许把快照文件导入，恢复记忆与 agent 状态。",
    ),
}

ALL_KNOWN_SCOPES = list(BASE_SCOPES)  # + memory.read.<tag> (dynamic)


def describe_scope(scope: str, custom_tags: dict[str, str] | None = None) -> tuple[str, str] | None:
    """(label, explanation) in Chinese, or None if unknown.

    custom_tags: {tag: label} from the store — makes memory.read.<custom>
    scopes describable/validatable (PRD §10.3, panel settings).
    """
    if scope in BASE_SCOPES:
        return BASE_SCOPES[scope]
    if scope.startswith("memory.read."):
        tag = scope[len("memory.read.") :]
        cn = PRESET_SCOPE_TAGS.get(tag) or (custom_tags or {}).get(tag)
        if cn:
            return (
                f"读取「{cn}」类记忆",
                f"允许检索并查看标签为「{tag}」的记忆条目，其他标签不可见。",
            )
    return None


def validate_scopes(scopes: list[str], custom_tags: dict[str, str] | None = None) -> None:
    """Raise InvalidScope (400, PRD §11) on any unknown scope."""
    for s in scopes:
        if describe_scope(s, custom_tags) is None:
            raise InvalidScope(f"unknown scope: {s}")


def scope_implies(granted: list[str], required: str) -> bool:
    """Does the granted set satisfy a required scope?

    - memory.admin implies everything (PRD §10).
    - memory.read is satisfied by memory.read itself or any memory.read.<tag>.
    """
    g = set(granted)
    if "memory.admin" in g:
        return True
    if required == "memory.read":
        return "memory.read" in g or any(s.startswith("memory.read.") for s in g)
    return required in g


def effective_tag_filter(granted: list[str]) -> list[str] | None:
    """Server-side tag restriction derived from scopes (PRD §13.3).

    Returns None for unrestricted (memory.read / memory.admin), otherwise the
    sorted list of tags the token may see. Never trust client-supplied tags.
    """
    if "memory.admin" in granted or "memory.read" in granted:
        return None
    tags = {s.split(".", 2)[2] for s in granted if s.startswith("memory.read.")}
    return sorted(tags)


def apply_tag_filter(requested: list[str], allowed: list[str] | None) -> list[str]:
    """Intersect client-hint tags with the server-side allowlist.

    Returns None-meaning-unrestricted as the caller's `allowed`; otherwise the
    narrowed list. NOTE: an empty result here means "nothing the client asked
    for is granted" — the caller must fail closed (return no rows), NOT treat
    it as "no filter". See search_memories / list_memories.
    """
    if allowed is None:
        return requested
    if not requested:
        return allowed
    return [t for t in requested if t in allowed]


# ------------------------------------------------------------------ secrets
_ph = PasswordHasher()  # argon2id defaults

_dummy_hash_cache: str | None = None


def _dummy_hash() -> str:
    """Pre-computed argon2 hash used when the client_id is unknown, so
    _authenticate_client always costs one verify (no timing oracle)."""
    global _dummy_hash_cache
    if _dummy_hash_cache is None:
        _dummy_hash_cache = _ph.hash("porter-dummy-secret-never-issued")
    return _dummy_hash_cache


def hash_secret(secret: str) -> str:
    return _ph.hash(secret)


def verify_secret(secret_hash: str, secret: str) -> bool:
    try:
        return _ph.verify(secret_hash, secret)
    except VerifyMismatchError:
        return False
    except Exception:  # malformed hash -> treat as mismatch, never crash
        log.warning("secret hash verification failed: malformed hash")
        return False


def new_client_id() -> str:
    return "porter_" + secrets.token_urlsafe(12)


def new_client_secret() -> str:
    return secrets.token_urlsafe(32)


def new_auth_code() -> str:
    return secrets.token_urlsafe(32)


def new_refresh_token() -> str:
    return "porter_rt_" + secrets.token_urlsafe(32)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ------------------------------------------------------------------ JWT (access tokens)
_jwt_key: bytes | None = None


def jwt_signing_key() -> bytes:
    global _jwt_key
    if _jwt_key is None:
        configured = settings.jwt_secret
        if configured:
            _jwt_key = configured.encode()
        else:
            _jwt_key = secrets.token_bytes(32)
            log.warning(
                "PORTER_JWT_SECRET is not set: generated an ephemeral signing key. "
                "Access tokens will NOT survive a restart. Set PORTER_JWT_SECRET "
                "in production."
            )
    return _jwt_key


def issue_access_token(*, jti: UUID, client_pk: UUID, client_id: str, scopes: list[str]) -> str:
    now = int(time.time())
    payload = {
        "jti": str(jti),
        "sub": str(client_pk),  # clients.id
        "cid": client_id,  # public client_id string
        "scopes": sorted(scopes),
        "iat": now,
        "exp": now + settings.access_token_ttl_seconds,
    }
    return jwt.encode(payload, jwt_signing_key(), algorithm="HS256")


# ------------------------------------------------------------------ shares (P2, memory.share)
async def mint_share_token(
    store: OAuthStore,
    *,
    name: str,
    tags: list[str],
    expires_in_hours: int,
) -> tuple[ShareRecord, str]:
    """Mint an owner-restricted share token (P2).

    Security contract (enforced here, in one place — both the REST endpoint
    and the panel call this, so the two surfaces can never diverge):
    - every tag must be a real preset or custom tag; anything else
      (``memory.admin``, ``../``, empty, duplicates, unknown) -> 400
      ``invalid_scope``. Scope smuggling is impossible because scopes are
      DERIVED from validated tags, never taken from caller input.
    - minted scopes are ALWAYS exactly [f"memory.read.{t}" for t in tags] —
      NOTHING else, ever. No memory.write, no memory.admin, no state.*.
    - the JWT (jti = share id, sub = "share") is returned to the caller only;
      it is never stored — only the share metadata row persists.

    Returns (ShareRecord, token). The caller must show the token exactly
    once and never log it.
    """
    custom = {t["tag"] for t in await store.list_custom_tags()}
    valid = set(PRESET_SCOPE_TAGS) | custom
    if not tags:
        raise InvalidScope("at least one tag is required for a share")
    seen: set[str] = set()
    for tag in tags:
        if not tag or tag in seen or tag not in valid:
            raise InvalidScope(f"invalid share tag: {tag!r}")
        seen.add(tag)
    scopes = [f"memory.read.{t}" for t in tags]  # the ONLY scopes a share ever gets
    expires_at = datetime.now(UTC) + timedelta(hours=expires_in_hours)
    share = await store.create_share(name=name, scopes=scopes, expires_at=expires_at)
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


# ------------------------------------------------------------------ flow logic
@dataclass
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str  # space-separated


async def _authenticate_client(store: OAuthStore, client_id: str, client_secret: str):
    """Return the ClientRecord or raise InvalidGrant (uniform, no enumeration)."""
    client = await store.get_client_by_client_id(client_id)
    # Always run one argon2 verify so response timing does not reveal whether
    # the client_id exists (enumeration via timing side-channel).
    candidate_hash = client.secret_hash if client is not None else _dummy_hash()
    ok = verify_secret(candidate_hash, client_secret)
    if client is None or not ok:
        raise InvalidGrant("invalid client credentials")
    return client


async def _mint_pair(
    store: OAuthStore, client, scopes: list[str], refresh_of: UUID | None = None
) -> TokenPair:
    now = datetime.now(UTC)
    access_jti = uuid4()
    refresh_jti = uuid4()
    refresh_value = new_refresh_token()
    access_exp = now + timedelta(seconds=settings.access_token_ttl_seconds)
    refresh_exp = now + timedelta(days=settings.refresh_token_ttl_days)

    await store.create_token(
        jti=access_jti, client_id=client.id, scopes=scopes, expires_at=access_exp
    )
    await store.create_token(
        jti=refresh_jti,
        client_id=client.id,
        scopes=scopes,
        expires_at=refresh_exp,
        token_hash=sha256_hex(refresh_value),
        refresh_of=refresh_of,
    )
    access = issue_access_token(
        jti=access_jti, client_pk=client.id, client_id=client.client_id, scopes=scopes
    )
    return TokenPair(
        access_token=access,
        refresh_token=refresh_value,
        expires_in=settings.access_token_ttl_seconds,
        scope=" ".join(sorted(scopes)),
    )


async def exchange_code(
    store: OAuthStore,
    audit,
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
) -> TokenPair:
    """authorization_code grant: one-time code -> token pair."""
    client = await _authenticate_client(store, client_id, client_secret)
    rec = await store.consume_auth_code(sha256_hex(code))
    if rec is None:
        raise InvalidGrant("invalid, expired, or already-used authorization code")
    if rec.client_id != client.id:
        raise InvalidGrant("authorization code was not issued to this client")
    if rec.redirect_uri != redirect_uri:
        raise InvalidGrant("redirect_uri mismatch")
    await store.create_grant(client_id=client.id, scopes=rec.scopes)
    await audit(
        action="grant",
        client_id=client.id,
        scopes_used=rec.scopes,
        meta={"via": "authorization_code"},
    )
    return await _mint_pair(store, client, rec.scopes)


async def refresh_token_grant(
    store: OAuthStore,
    audit,
    *,
    refresh_token: str,
    client_id: str,
    client_secret: str,
) -> TokenPair:
    """refresh_token grant with rotation + reuse detection (T2, PRD §10.4)."""
    client = await _authenticate_client(store, client_id, client_secret)
    row = await store.get_token_by_refresh_hash(sha256_hex(refresh_token))
    if row is None or row.client_id != client.id:
        raise InvalidGrant("invalid refresh token")
    now = datetime.now(UTC)

    if row.revoked:
        successor = await store.get_refresh_successor(row.jti)
        if successor is not None and not successor.revoked:
            age = (now - successor.issued_at).total_seconds()
            if age <= settings.refresh_reuse_grace_seconds:
                # Likely a benign race (client retried during rotation):
                # fail closed without nuking the client. The client already
                # holds the fresh pair from its successful rotation.
                # Audited anyway: a burst of these is a theft signal (T2).
                await audit(
                    action="security",
                    client_id=client.id,
                    scopes_used=row.scopes,
                    meta={"reason": "refresh_token_reuse_within_grace", "jti": str(row.jti)},
                )
                raise InvalidGrant("refresh token already rotated")
        # Real reuse: treat as theft -> revoke EVERYTHING for this client (T2).
        n = await store.revoke_all_for_client(client.id)
        await audit(
            action="security",
            client_id=client.id,
            scopes_used=row.scopes,
            meta={"reason": "refresh_token_reuse", "jti": str(row.jti), "tokens_revoked": n},
        )
        raise TokenRevoked("refresh token reuse detected: all tokens for this client revoked")

    if row.expires_at <= now:
        raise InvalidGrant("refresh token expired")

    # Rotation: kill the old refresh token, mint a fresh pair chained to it.
    await store.revoke_token(row.jti)
    await audit(
        action="refresh",
        client_id=client.id,
        scopes_used=row.scopes,
        meta={"rotated_jti": str(row.jti)},
    )
    return await _mint_pair(store, client, row.scopes, refresh_of=row.jti)


async def revoke_token_value(
    store: OAuthStore,
    audit,
    invalidate_cache,
    *,
    token: str,
    client_id: str,
    client_secret: str,
    revoke_all: bool = False,
) -> bool:
    """Revoke one token (access JWT or refresh opaque) or all for the client.

    Returns True if something was revoked. Idempotent per RFC 7009.
    """
    client = await _authenticate_client(store, client_id, client_secret)
    if revoke_all:
        n = await store.revoke_all_for_client(client.id)
        invalidate_cache()
        await audit(
            action="revoke",
            client_id=client.id,
            scopes_used=[],
            meta={"all": True, "tokens_revoked": n},
        )
        return True

    # Try as access-token JWT (ignore expiry: a dead token needs no kill, but
    # marking it revoked is harmless hygiene). Narrow except: DB errors must
    # propagate, not silently fall through to the refresh-token attempt.
    try:
        claims = jwt.decode(
            token,
            jwt_signing_key(),
            algorithms=["HS256"],
            options={"verify_exp": False},
        )
        jti = UUID(claims["jti"])
    except (jwt.InvalidTokenError, KeyError, ValueError, AttributeError):
        claims = None  # type: ignore[assignment]
        jti = None  # type: ignore[assignment]
    if jti is not None:
        row = await store.get_token(jti)
        if row is not None and row.client_id == client.id and not row.revoked:
            await store.revoke_token(jti)
            invalidate_cache(str(jti))  # cache is keyed by str(jti)
            await audit(
                action="revoke",
                client_id=client.id,
                scopes_used=row.scopes,
                meta={"jti": str(jti), "kind": "access"},
            )
            return True
        return False

    # Try as refresh token.
    row = await store.get_token_by_refresh_hash(sha256_hex(token))
    if row is not None and row.client_id == client.id and not row.revoked:
        await store.revoke_token(row.jti)
        await audit(
            action="revoke",
            client_id=client.id,
            scopes_used=row.scopes,
            meta={"jti": str(row.jti), "kind": "refresh"},
        )
        return True
    return False


# ------------------------------------------------------------------ consent screen
CONSENT_CSS = """
body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
background:#f6f7f9;color:#1a1a1a;margin:0;padding:32px}
.card{max-width:560px;margin:0 auto;background:#fff;border-radius:12px;
padding:32px;box-shadow:0 2px 12px rgba(0,0,0,.08)}
h1{font-size:20px;margin:0 0 8px}.sub{color:#666;font-size:14px;margin-bottom:20px}
.scope{border:1px solid #e5e7eb;border-radius:8px;padding:12px 14px;margin:10px 0;
display:flex;gap:12px;align-items:flex-start}
.scope input{margin-top:4px;transform:scale(1.2)}
.scope b{display:block;font-size:15px}.scope span{color:#666;font-size:13px}
.warn{border-color:#f59e0b;background:#fffbeb}
.actions{display:flex;gap:12px;margin-top:24px}
button{flex:1;padding:12px;border-radius:8px;font-size:16px;cursor:pointer;border:0}
.approve{background:#111827;color:#fff}.deny{background:#f3f4f6;color:#111827}
.note{font-size:12px;color:#999;margin-top:16px}
"""


def render_consent_screen(
    *,
    client_name: str,
    scopes: list[str],
    client_id: str,
    redirect_uri: str,
    state: str,
    requested: list[str],
    custom_tags: dict[str, str] | None = None,
) -> str:
    """Consent screen (PRD §10.2): per-scope Chinese explanation + checkboxes."""
    rows = []
    for s in scopes:
        desc = describe_scope(s, custom_tags)
        if desc is None:  # validated upstream; defensive
            continue
        label, expl = desc
        warn = " warn" if s == "memory.admin" else ""
        rows.append(
            f'<label class="scope{warn}">'
            f'<input type="checkbox" name="granted_scopes" value="{html.escape(s)}" checked>'
            f"<div><b>{html.escape(label)}</b>"
            f"<span><code>{html.escape(s)}</code> — {html.escape(expl)}</span></div>"
            "</label>"
        )
    hidden = (
        f'<input type="hidden" name="client_id" value="{html.escape(client_id)}">'
        f'<input type="hidden" name="redirect_uri" value="{html.escape(redirect_uri)}">'
        f'<input type="hidden" name="state" value="{html.escape(state)}">'
        + "".join(
            f'<input type="hidden" name="requested_scopes" value="{html.escape(s)}">'
            for s in requested
        )
    )
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>授权请求 — Porter</title><style>{CONSENT_CSS}</style></head>
<body><div class="card">
<h1>「{html.escape(client_name)}」请求访问你的 Porter 记忆</h1>
<div class="sub">勾选你愿意授予的权限，拒绝后该应用将无法访问。授权后可随时在面板撤销。</div>
<form method="post" action="/oauth/authorize">
{hidden}
{"".join(rows)}
<div class="actions">
<button class="approve" type="submit" name="decision" value="approve">批准所选权限</button>
<button class="deny" type="submit" name="decision" value="deny">拒绝</button>
</div></form>
<div class="note">Porter · 记忆归你所有，应用只是经授权的客人</div>
</div></body></html>"""
