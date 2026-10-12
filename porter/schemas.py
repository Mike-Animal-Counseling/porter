"""Pydantic schemas for the Porter v1 REST API (PRD §11)."""
from __future__ import annotations

import base64
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

MemoryType = Literal["preference", "fact", "episode", "skill_ref"]
MemoryStatus = Literal["pending", "active", "archived"]

MAX_CONTENT_LEN = 20_000  # DoS guard: embedding a novel is not a memory
MAX_QUERY_LEN = 2_000
MAX_TAGS = 20
MAX_TAG_LEN = 64
# E2EE (P2, docs/DEVELOPER.md §6): ciphertext is base64(nonce(24B) || ct), so
# the wire cap is larger than the plaintext cap — still bounded (DoS guard).
MAX_CIPHERTEXT_LEN = 40_000
MAX_KEY_ID_LEN = 64


def _validate_tags(v: list[str]) -> list[str]:
    if len(v) > MAX_TAGS:
        raise ValueError(f"at most {MAX_TAGS} scope_tags")
    for t in v:
        if not t or len(t) > MAX_TAG_LEN:
            raise ValueError(f"scope_tags must be 1..{MAX_TAG_LEN} chars")
    return v


# ------------------------------------------------------------------ requests
class EncryptionInfo(BaseModel):
    """E2EE envelope (P2, docs/DEVELOPER.md §6). `alg` is an exact literal —
    anything else is a 422 (the server cannot and must not guess)."""

    alg: Literal["xchacha20-poly1305"]
    key_id: str = Field(min_length=1, max_length=MAX_KEY_ID_LEN)


class MemoryCreate(BaseModel):
    """Exactly ONE of `content` (normal) or (`content_ciphertext` +
    `encryption`) (E2EE) — never both, never neither (422 otherwise)."""

    content: str | None = Field(default=None, min_length=1, max_length=MAX_CONTENT_LEN)
    content_ciphertext: str | None = Field(
        default=None, min_length=1, max_length=MAX_CIPHERTEXT_LEN
    )
    encryption: EncryptionInfo | None = None
    type: MemoryType = "fact"
    scope_tags: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source: dict[str, Any] = Field(default_factory=dict)

    @field_validator("scope_tags")
    @classmethod
    def _tags_ok(cls, v: list[str]) -> list[str]:
        return _validate_tags(v)

    @field_validator("content_ciphertext")
    @classmethod
    def _b64_ok(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            base64.b64decode(v, validate=True)
        except ValueError:
            raise ValueError("content_ciphertext must be valid base64")
        return v

    @model_validator(mode="after")
    def _exactly_one_payload(self):
        plain = self.content is not None
        cipher = self.content_ciphertext is not None or self.encryption is not None
        if plain and cipher:
            raise ValueError(
                "content and content_ciphertext/encryption are mutually exclusive"
            )
        if not plain and not (
            self.content_ciphertext is not None and self.encryption is not None
        ):
            raise ValueError(
                "either content or (content_ciphertext + encryption) is required"
            )
        return self


class MemorySearch(BaseModel):
    query: str = Field(min_length=1, max_length=MAX_QUERY_LEN)
    top_k: int = Field(default=5, ge=1, le=50)
    tags: list[str] = Field(default_factory=list)
    types: list[MemoryType] = Field(default_factory=list)

    @field_validator("tags")
    @classmethod
    def _tags_ok(cls, v: list[str]) -> list[str]:
        return _validate_tags(v)


class MemoryListQuery(BaseModel):
    status: MemoryStatus | None = None
    tags: list[str] = Field(default_factory=list)
    type: MemoryType | None = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


# ------------------------------------------------------------------ responses
class MemoryOut(BaseModel):
    id: UUID
    content: str
    type: MemoryType
    scope_tags: list[str]
    source: dict[str, Any]
    confidence: float
    status: MemoryStatus
    created_at: datetime
    updated_at: datetime
    superseded_by: UUID | None = None
    version: int = 1  # optimistic-concurrency counter (US7); +1 per revise
    # E2EE (P2): when True, `content` IS the base64 ciphertext (never
    # plaintext — the server never sees it) and `key_id` names the client
    # key. The server never attempts decryption.
    encrypted: bool = False
    key_id: str | None = None
    # PRD principle 5: retrieved memory is untrusted input — the sync
    # channel delivers content to downstream agents exactly like search,
    # so the marking rides along (T3: prompt-injection via sync).
    untrusted_data: bool = True


class DuplicateCandidate(BaseModel):
    id: UUID
    similarity: float


class MemoryCreateOut(BaseModel):
    id: UUID
    status: Literal["pending_review"]
    duplicate_candidates: list[DuplicateCandidate] = Field(default_factory=list)


class SearchResult(BaseModel):
    id: UUID
    content: str
    type: MemoryType
    scope_tags: list[str]
    confidence: float
    source: dict[str, Any]
    score: float
    untrusted_data: bool = True  # PRD principle 5: retrieved memory is untrusted input


class SearchOut(BaseModel):
    results: list[SearchResult]


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorOut(BaseModel):
    error: ErrorBody


class HealthOut(BaseModel):
    status: Literal["ok"]
    version: str = "0.1.0"


# ------------------------------------------------------------------ OAuth (PRD §10, §11)
class ClientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    redirect_uris: list[str] = Field(default_factory=list)

    @field_validator("redirect_uris")
    @classmethod
    def _uris_ok(cls, v: list[str]) -> list[str]:
        for u in v:
            if not (u.startswith("http://") or u.startswith("https://")):
                raise ValueError("redirect_uris must be http(s) URLs")
            if "#" in u:
                raise ValueError("redirect_uris must not contain a fragment")
        if len(v) > 10:
            raise ValueError("at most 10 redirect_uris")
        return v


class ClientOut(BaseModel):
    id: UUID
    client_id: str
    name: str
    redirect_uris: list[str]
    created_at: datetime


class ClientCreateOut(ClientOut):
    # The raw secret is returned exactly ONCE, at registration. It is never
    # stored in cleartext (argon2 hash only) and never returned again.
    client_secret: str


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    refresh_token: str
    scope: str  # space-separated granted scopes


class RevokeOut(BaseModel):
    revoked: bool


class MemoryRevise(BaseModel):
    """Edit a memory with history (PRD US2). All fields optional; at least
    one must be provided. Creates a NEW record; the old one is archived with
    superseded_by -> new id.

    Optional `expected_version` enables optimistic concurrency (US7): when
    set, the edit goes through update_with_version(). A version mismatch does
    NOT fail — last-writer-wins still applies, the overwritten content is
    preserved as a new pending inbox record, and the response reports
    conflict=true with loser_pending_id set. Note: the versioned path is
    content-only (type/scope_tags/confidence are ignored when
    expected_version is supplied)."""

    content: str | None = Field(default=None, min_length=1, max_length=MAX_CONTENT_LEN)
    type: MemoryType | None = None
    scope_tags: list[str] | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("scope_tags")
    @classmethod
    def _tags_ok(cls, v: list[str] | None) -> list[str] | None:
        return _validate_tags(v) if v is not None else v


class MemoryReviseOut(BaseModel):
    """PATCH /api/v1/memories/{id} response (PRD US7).

    Conflict is reported IN THE BODY (not a 409): a version mismatch is a
    normal, handled outcome — the write succeeded via last-writer-wins and
    the loser's content was preserved as a pending inbox record
    (loser_pending_id). conflict=false (the common case) keeps the field
    stable so clients can branch on it unconditionally."""

    memory: MemoryOut
    conflict: bool = False
    loser_pending_id: UUID | None = None


class CustomTagCreate(BaseModel):
    tag: str = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9_]{1,32}$")
    label: str = Field(min_length=1, max_length=64)


class CustomTagOut(BaseModel):
    tag: str
    label: str


class ScopeTagsOut(BaseModel):
    preset: dict[str, str]
    custom: list[CustomTagOut]


class SnapshotCreate(BaseModel):
    tags: list[str] = Field(default_factory=list)
    password: str | None = Field(default=None, min_length=1, max_length=128)


class SnapshotOut(BaseModel):
    id: UUID
    manifest: dict[str, Any]
    storage_path: str
    signature_ok: bool
    created_at: datetime


# ------------------------------------------------------------------ sync (P1, PRD §9.5)
class SyncChange(BaseModel):
    """One entry of the change feed.

    - memory is null for delete tombstones and for create changes whose
      record is still pending when the caller lacks memory.admin (inbox
      rule: unapproved content never leaks to adapters).
    - status is null for delete tombstones (the row no longer exists)."""

    seq: int
    memory_id: UUID
    op: Literal["create", "approve", "revise", "delete", "archive"]
    status: MemoryStatus | None = None
    at: datetime
    actor_client_id: UUID | None = None
    memory: MemoryOut | None = None


class SyncDeltaOut(BaseModel):
    """GET /api/v1/sync/delta response. `cursor` is the current feed head
    (use it as the next `since` after persisting what you consumed)."""

    changes: list[SyncChange]
    cursor: int


class CursorIn(BaseModel):
    """Explicit cursor ack. The adapter is identified by its own token —
    there is deliberately no adapter_id field (client-supplied identity
    would be a spoofing vector)."""

    cursor: int = Field(ge=0)


class CursorOut(BaseModel):
    adapter_id: str
    cursor: int
    last_seen: datetime


# ------------------------------------------------------------------ shares (P2, memory.share)
class ShareCreate(BaseModel):
    """Owner-minted restricted share token. Minted scopes are ALWAYS exactly
    [f"memory.read.{t}" for t in tags] — nothing else, ever (enforced in
    oauth.mint_share_token, not here). Tag validity (preset + custom) is
    checked at mint time and raises 400 (invalid_scope); length/range
    violations on this body are 422."""

    name: str = Field(min_length=1, max_length=64)
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    expires_in_hours: int = Field(ge=1, le=2160)


class ShareOut(BaseModel):
    """A share WITHOUT its token (list/revoke views). The token is shown
    exactly once, in the mint response, and never stored or logged."""

    id: UUID
    name: str
    scopes: list[str]
    created_at: datetime
    expires_at: datetime
    revoked: bool


class ShareCreateOut(ShareOut):
    # The raw token is returned exactly ONCE, at mint time. It is never
    # stored in cleartext (only the share metadata lives in `shares`) and
    # never returned again.
    token: str
