"""OAuth persistence: clients, authorization codes, tokens, grants.

Two implementations share one interface:
- PostgresOAuthStore: production, asyncpg. Tables from migrations 001/002.
- InMemoryOAuthStore: for tests and for running without Postgres.

Token model (PRD §10.2, §12):
- Access tokens are JWTs (HS256). Only their jti + metadata live in `tokens`;
  the JWT string itself is never stored.
- Refresh tokens are opaque random strings; only sha256(token) is stored in
  `tokens.token_hash`. Presenting the raw value is the only way to use it.
- Authorization codes are opaque; only sha256(code) is stored in `auth_codes`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence
from uuid import UUID, uuid4


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ClientRecord:
    id: UUID
    name: str
    client_id: str          # public OAuth client_id, e.g. "porter_..."
    secret_hash: str        # argon2 hash; raw secret never persisted
    redirect_uris: list[str]
    created_at: datetime


@dataclass
class AuthCodeRecord:
    code_hash: str          # sha256(code)
    client_id: UUID         # FK -> clients.id
    scopes: list[str]
    redirect_uri: str
    expires_at: datetime
    used: bool
    created_at: datetime


@dataclass
class TokenRecord:
    jti: UUID
    client_id: UUID         # FK -> clients.id
    scopes: list[str]
    issued_at: datetime
    expires_at: datetime
    revoked: bool
    token_hash: str | None = None   # sha256(refresh token), NULL for access rows
    refresh_of: UUID | None = None  # chain link for rotation / reuse detection


@dataclass
class ShareRecord:
    """Owner-minted restricted share token metadata (P2, memory.share).

    The JWT itself is the credential and is NEVER stored — only this
    metadata + revocation state live here. The JWT's jti == this id,
    sub == "share", and scopes are ALWAYS exactly
    [f"memory.read.{t}" for t in granted tags] (minted in
    oauth.mint_share_token; nothing else, ever)."""

    id: UUID
    name: str
    scopes: list[str]
    created_at: datetime
    expires_at: datetime
    revoked: bool


class OAuthStore(ABC):
    # ------------------------------------------------------------ clients
    @abstractmethod
    async def create_client(
        self, *, name: str, client_id: str, secret_hash: str,
        redirect_uris: Sequence[str],
    ) -> ClientRecord: ...

    @abstractmethod
    async def get_client_by_client_id(self, client_id: str) -> ClientRecord | None: ...

    @abstractmethod
    async def get_client_by_id(self, id: UUID) -> ClientRecord | None: ...

    @abstractmethod
    async def count_clients(self) -> int: ...

    @abstractmethod
    async def list_clients(self) -> list[ClientRecord]: ...

    # ------------------------------------------------------------ auth codes
    @abstractmethod
    async def create_auth_code(
        self, *, code_hash: str, client_id: UUID, scopes: Sequence[str],
        redirect_uri: str, expires_at: datetime,
    ) -> None: ...

    @abstractmethod
    async def consume_auth_code(self, code_hash: str) -> AuthCodeRecord | None:
        """Atomically mark used and return the record; None if missing/used/expired."""

    # ------------------------------------------------------------ tokens
    @abstractmethod
    async def create_token(
        self, *, jti: UUID, client_id: UUID, scopes: Sequence[str],
        expires_at: datetime, token_hash: str | None = None,
        refresh_of: UUID | None = None,
    ) -> None: ...

    @abstractmethod
    async def get_token(self, jti: UUID) -> TokenRecord | None: ...

    @abstractmethod
    async def get_token_by_refresh_hash(self, token_hash: str) -> TokenRecord | None: ...

    @abstractmethod
    async def get_refresh_successor(self, jti: UUID) -> TokenRecord | None:
        """Newest token whose refresh_of == jti (rotation chain)."""

    @abstractmethod
    async def revoke_token(self, jti: UUID) -> None: ...

    @abstractmethod
    async def revoke_all_for_client(self, client_id: UUID) -> int:
        """Mark every token row for the client revoked. Returns count."""

    @abstractmethod
    async def list_revoked_jtis(self, since: datetime) -> list[UUID]:
        """jtis revoked since `since` — for blacklist cache warmup."""

    # ------------------------------------------------------------ shares (P2, memory.share)
    @abstractmethod
    async def create_share(
        self, *, name: str, scopes: Sequence[str], expires_at: datetime,
    ) -> ShareRecord:
        """Mint share metadata. The JWT is minted by the caller
        (oauth.mint_share_token) and is never stored."""

    @abstractmethod
    async def get_share(self, jti: UUID) -> ShareRecord | None:
        """Share by id (= JWT jti). None if unknown."""

    @abstractmethod
    async def list_shares(self) -> list[ShareRecord]:
        """All shares, newest first. Never includes tokens (none are stored)."""

    @abstractmethod
    async def revoke_share(self, jti: UUID) -> None:
        """Mark a share revoked. Idempotent."""

    # ------------------------------------------------------------ grants
    @abstractmethod
    async def create_grant(
        self, *, client_id: UUID, scopes: Sequence[str],
        expires_at: datetime | None = None,
    ) -> None: ...

    # ------------------------------------------------------------ custom scope_tags (PRD §10.3)
    @abstractmethod
    async def list_custom_tags(self) -> list[dict[str, Any]]:
        """Custom tags as [{tag, label}], sorted by tag."""

    @abstractmethod
    async def add_custom_tag(self, *, tag: str, label: str) -> None:
        """Insert or update a custom tag label."""

    @abstractmethod
    async def remove_custom_tag(self, tag: str) -> bool:
        """Delete a custom tag. Returns True if it existed."""


# ================================================================ in-memory
class InMemoryOAuthStore(OAuthStore):
    def __init__(self) -> None:
        self._clients: dict[str, ClientRecord] = {}          # by client_id
        self._clients_by_id: dict[UUID, ClientRecord] = {}
        self._codes: dict[str, AuthCodeRecord] = {}           # by code_hash
        self._tokens: dict[UUID, TokenRecord] = {}            # by jti
        self._tokens_by_hash: dict[str, TokenRecord] = {}    # by token_hash
        self._grants: list[dict[str, Any]] = []
        self._custom_tags: dict[str, str] = {}                # tag -> label
        self._shares: dict[UUID, ShareRecord] = {}            # by id (= JWT jti)

    async def create_client(self, *, name, client_id, secret_hash, redirect_uris):
        if client_id in self._clients:
            raise ValueError(f"client_id {client_id} already exists")
        rec = ClientRecord(
            id=uuid4(), name=name, client_id=client_id, secret_hash=secret_hash,
            redirect_uris=list(redirect_uris), created_at=_now(),
        )
        self._clients[client_id] = rec
        self._clients_by_id[rec.id] = rec
        return rec

    async def get_client_by_client_id(self, client_id):
        return self._clients.get(client_id)

    async def get_client_by_id(self, id):
        return self._clients_by_id.get(id)

    async def count_clients(self):
        return len(self._clients)

    async def list_clients(self):
        return sorted(self._clients.values(), key=lambda c: c.created_at)

    async def create_auth_code(self, *, code_hash, client_id, scopes, redirect_uri, expires_at):
        self._codes[code_hash] = AuthCodeRecord(
            code_hash=code_hash, client_id=client_id, scopes=list(scopes),
            redirect_uri=redirect_uri, expires_at=expires_at, used=False,
            created_at=_now(),
        )

    async def consume_auth_code(self, code_hash):
        rec = self._codes.get(code_hash)
        if rec is None or rec.used or rec.expires_at <= _now():
            return None
        rec.used = True  # single-threaded test impl; Postgres uses UPDATE...RETURNING
        return rec

    async def create_token(self, *, jti, client_id, scopes, expires_at,
                           token_hash=None, refresh_of=None):
        rec = TokenRecord(
            jti=jti, client_id=client_id, scopes=list(scopes),
            issued_at=_now(), expires_at=expires_at, revoked=False,
            token_hash=token_hash, refresh_of=refresh_of,
        )
        self._tokens[jti] = rec
        if token_hash:
            self._tokens_by_hash[token_hash] = rec

    async def get_token(self, jti):
        return self._tokens.get(jti)

    async def get_token_by_refresh_hash(self, token_hash):
        return self._tokens_by_hash.get(token_hash)

    async def get_refresh_successor(self, jti):
        cands = [t for t in self._tokens.values() if t.refresh_of == jti]
        if not cands:
            return None
        return max(cands, key=lambda t: t.issued_at)

    async def revoke_token(self, jti):
        rec = self._tokens.get(jti)
        if rec:
            rec.revoked = True

    async def revoke_all_for_client(self, client_id):
        n = 0
        for t in self._tokens.values():
            if t.client_id == client_id and not t.revoked:
                t.revoked = True
                n += 1
        return n

    async def list_revoked_jtis(self, since):
        return [
            t.jti for t in self._tokens.values()
            if t.revoked and t.issued_at >= since
        ]

    async def create_grant(self, *, client_id, scopes, expires_at=None):
        self._grants.append({
            "client_id": client_id, "scopes": list(scopes),
            "expires_at": expires_at, "issued_at": _now(), "revoked": False,
        })

    async def list_custom_tags(self):
        return [
            {"tag": t, "label": self._custom_tags[t]}
            for t in sorted(self._custom_tags)
        ]

    async def add_custom_tag(self, *, tag, label):
        self._custom_tags[tag] = label

    async def remove_custom_tag(self, tag):
        return self._custom_tags.pop(tag, None) is not None

    async def create_share(self, *, name, scopes, expires_at):
        rec = ShareRecord(
            id=uuid4(), name=name, scopes=list(scopes),
            created_at=_now(), expires_at=expires_at, revoked=False,
        )
        self._shares[rec.id] = rec
        return rec

    async def get_share(self, jti):
        return self._shares.get(jti)

    async def list_shares(self):
        return sorted(
            self._shares.values(), key=lambda s: s.created_at, reverse=True
        )

    async def revoke_share(self, jti):
        rec = self._shares.get(jti)
        if rec:
            rec.revoked = True


# ================================================================ postgres
def _token_row(r: Any) -> TokenRecord:
    return TokenRecord(
        jti=r["jti"], client_id=r["client_id"], scopes=list(r["scopes"] or []),
        issued_at=r["issued_at"], expires_at=r["expires_at"],
        revoked=bool(r["revoked"]), token_hash=r["token_hash"],
        refresh_of=r["refresh_of"],
    )


class PostgresOAuthStore(OAuthStore):
    """Production implementation over asyncpg. Requires migrations 001+002."""

    def __init__(self, pool) -> None:  # pool: asyncpg.Pool
        self.pool = pool

    async def create_client(self, *, name, client_id, secret_hash, redirect_uris):
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                """
                INSERT INTO clients (name, client_id, secret_hash, redirect_uris)
                VALUES ($1, $2, $3, $4)
                RETURNING id, name, client_id, secret_hash, redirect_uris, created_at
                """,
                name, client_id, secret_hash, list(redirect_uris),
            )
        return ClientRecord(
            id=row["id"], name=row["name"], client_id=row["client_id"],
            secret_hash=row["secret_hash"], redirect_uris=list(row["redirect_uris"] or []),
            created_at=row["created_at"],
        )

    async def get_client_by_client_id(self, client_id):
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT id, name, client_id, secret_hash, redirect_uris, created_at"
                " FROM clients WHERE client_id = $1",
                client_id,
            )
        return (
            ClientRecord(
                id=row["id"], name=row["name"], client_id=row["client_id"],
                secret_hash=row["secret_hash"],
                redirect_uris=list(row["redirect_uris"] or []),
                created_at=row["created_at"],
            )
            if row else None
        )

    async def get_client_by_id(self, id):
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT id, name, client_id, secret_hash, redirect_uris, created_at"
                " FROM clients WHERE id = $1",
                id,
            )
        return (
            ClientRecord(
                id=row["id"], name=row["name"], client_id=row["client_id"],
                secret_hash=row["secret_hash"],
                redirect_uris=list(row["redirect_uris"] or []),
                created_at=row["created_at"],
            )
            if row else None
        )

    async def count_clients(self):
        async with self.pool.acquire() as con:
            return await con.fetchval("SELECT COUNT(*) FROM clients")

    async def list_clients(self):
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT id, name, client_id, secret_hash, redirect_uris, created_at"
                " FROM clients ORDER BY created_at"
            )
        return [
            ClientRecord(
                id=r["id"], name=r["name"], client_id=r["client_id"],
                secret_hash=r["secret_hash"],
                redirect_uris=list(r["redirect_uris"] or []),
                created_at=r["created_at"],
            )
            for r in rows
        ]

    async def create_auth_code(self, *, code_hash, client_id, scopes, redirect_uri, expires_at):
        async with self.pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO auth_codes (code_hash, client_id, scopes, redirect_uri, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                """,
                code_hash, client_id, list(scopes), redirect_uri, expires_at,
            )

    async def consume_auth_code(self, code_hash):
        # Single UPDATE...RETURNING: atomic mark-used, no TOCTOU on reuse.
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                """
                UPDATE auth_codes SET used = TRUE
                WHERE code_hash = $1 AND used = FALSE AND expires_at > now()
                RETURNING code_hash, client_id, scopes, redirect_uri, expires_at,
                          used, created_at
                """,
                code_hash,
            )
        if row is None:
            return None
        return AuthCodeRecord(
            code_hash=row["code_hash"], client_id=row["client_id"],
            scopes=list(row["scopes"] or []), redirect_uri=row["redirect_uri"],
            expires_at=row["expires_at"], used=True, created_at=row["created_at"],
        )

    async def create_token(self, *, jti, client_id, scopes, expires_at,
                           token_hash=None, refresh_of=None):
        async with self.pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO tokens (jti, client_id, scopes, expires_at, token_hash, refresh_of)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                jti, client_id, list(scopes), expires_at, token_hash, refresh_of,
            )

    async def get_token(self, jti):
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT jti, client_id, scopes, issued_at, expires_at, revoked,"
                " token_hash, refresh_of FROM tokens WHERE jti = $1",
                jti,
            )
        return _token_row(row) if row else None

    async def get_token_by_refresh_hash(self, token_hash):
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT jti, client_id, scopes, issued_at, expires_at, revoked,"
                " token_hash, refresh_of FROM tokens WHERE token_hash = $1",
                token_hash,
            )
        return _token_row(row) if row else None

    async def get_refresh_successor(self, jti):
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT jti, client_id, scopes, issued_at, expires_at, revoked,"
                " token_hash, refresh_of FROM tokens"
                " WHERE refresh_of = $1 ORDER BY issued_at DESC LIMIT 1",
                jti,
            )
        return _token_row(row) if row else None

    async def revoke_token(self, jti):
        async with self.pool.acquire() as con:
            await con.execute("UPDATE tokens SET revoked = TRUE WHERE jti = $1", jti)

    async def revoke_all_for_client(self, client_id):
        async with self.pool.acquire() as con:
            res = await con.execute(
                "UPDATE tokens SET revoked = TRUE WHERE client_id = $1 AND revoked = FALSE",
                client_id,
            )
        # asyncpg returns "UPDATE <n>"
        try:
            return int(res.split()[-1])
        except (ValueError, IndexError):
            return 0

    async def list_revoked_jtis(self, since):
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT jti FROM tokens WHERE revoked = TRUE AND issued_at >= $1",
                since,
            )
        return [r["jti"] for r in rows]

    async def create_grant(self, *, client_id, scopes, expires_at=None):
        async with self.pool.acquire() as con:
            await con.execute(
                "INSERT INTO grants (client_id, scopes, expires_at) VALUES ($1, $2, $3)",
                client_id, list(scopes), expires_at,
            )

    async def list_custom_tags(self):
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT tag, label FROM custom_tags ORDER BY tag"
            )
        return [{"tag": r["tag"], "label": r["label"]} for r in rows]

    async def add_custom_tag(self, *, tag, label):
        async with self.pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO custom_tags (tag, label) VALUES ($1, $2)
                ON CONFLICT (tag) DO UPDATE SET label = EXCLUDED.label
                """,
                tag, label,
            )

    async def remove_custom_tag(self, tag):
        async with self.pool.acquire() as con:
            res = await con.execute("DELETE FROM custom_tags WHERE tag = $1", tag)
        return res == "DELETE 1"

    # ------------------------------------------------------------ shares (P2, memory.share)
    @staticmethod
    def _share_row(r: Any) -> ShareRecord:
        return ShareRecord(
            id=r["id"], name=r["name"], scopes=list(r["scopes"] or []),
            created_at=r["created_at"], expires_at=r["expires_at"],
            revoked=bool(r["revoked"]),
        )

    async def create_share(self, *, name, scopes, expires_at):
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                """
                INSERT INTO shares (name, scopes, expires_at)
                VALUES ($1, $2, $3)
                RETURNING id, name, scopes, created_at, expires_at, revoked
                """,
                name, list(scopes), expires_at,
            )
        return self._share_row(row)

    async def get_share(self, jti):
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT id, name, scopes, created_at, expires_at, revoked"
                " FROM shares WHERE id = $1",
                jti,
            )
        return self._share_row(row) if row else None

    async def list_shares(self):
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT id, name, scopes, created_at, expires_at, revoked"
                " FROM shares ORDER BY created_at DESC"
            )
        return [self._share_row(r) for r in rows]

    async def revoke_share(self, jti):
        async with self.pool.acquire() as con:
            await con.execute(
                "UPDATE shares SET revoked = TRUE WHERE id = $1", jti
            )
