"""Persistence layer.

Two implementations share one interface:
- PostgresMemoryRepository: production, asyncpg + pgvector (`<=>` cosine distance).
- InMemoryMemoryRepository: numpy cosine similarity. For tests and for running
  without Postgres/pgvector. NOT for production.

Both enforce the same domain rules (duplicate threshold, append-only audit).
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from collections.abc import Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from .config import settings
from .db import json_object
from .embeddings import Embedder
from .errors import InvalidInput

DUPLICATE_THRESHOLD = settings.duplicate_threshold  # PRD §8 F1; single source of truth


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class MemoryRecord:
    id: UUID
    content: str
    type: str
    scope_tags: list[str]
    source: dict[str, Any]
    confidence: float
    status: str  # pending | active | archived
    embedding: list[float] | None
    created_at: datetime
    updated_at: datetime
    superseded_by: UUID | None = None
    version: int = 1  # optimistic-concurrency counter (US7); +1 per revise
    # E2EE (P2, docs/DEVELOPER.md §6): when True, `content` is the client-side
    # XChaCha20-Poly1305 ciphertext (base64), embedding is always None, and
    # the server must never attempt decryption (it has no key).
    encrypted: bool = False
    key_id: str | None = None
    # Automation layer (migration 007):
    # - last_retrieved_at: idle clock for confidence decay (porter/decay.py).
    # - pinned: user-pinned memories never decay.
    # - triage_reasons: JSON-serializable list recorded at write time
    #   (drives inbox reason chips + digest grouping).
    last_retrieved_at: datetime = field(default_factory=_now)
    pinned: bool = False
    triage_reasons: list[str] = field(default_factory=list)


@dataclass
class AuditRecord:
    id: UUID
    client_id: UUID | None
    action: str
    memory_ids: list[UUID]
    scopes_used: list[str]
    meta: dict[str, Any]
    created_at: datetime


@dataclass
class ScoredMemory:
    record: MemoryRecord
    score: float  # cosine similarity, higher is better


class MemoryRepository(ABC):
    def __init__(self, embedder: Embedder) -> None:
        self.embedder = embedder

    @asynccontextmanager
    async def atomic(self, memory_ids):
        """Production backends override this for compound memory operations."""
        yield

    @abstractmethod
    async def create_pending(
        self,
        *,
        content: str,
        type: str,
        scope_tags: Sequence[str],
        confidence: float,
        source: dict[str, Any],
        actor_client_id: UUID | None = None,
        encrypted: bool = False,
        key_id: str | None = None,
        triage_reasons: Sequence[str] = (),
    ) -> tuple[MemoryRecord, list[ScoredMemory]]:
        """Insert as pending; returns (record, duplicate candidates > threshold).
        Logs a 'create' change (actor_client_id is the caller's clients.id,
        or None for owner/panel actions).

        E2EE (P2): when encrypted=True, `content` is ciphertext — it is
        stored as-is with embedding=None, duplicate detection is SKIPPED
        (ciphertext cannot be compared), and the record still lands in the
        pending inbox. The audit write is meta-only (never content)."""

    @abstractmethod
    async def get(self, memory_id: UUID) -> MemoryRecord | None: ...

    @abstractmethod
    async def list(
        self,
        *,
        status: str | None = None,
        tags: Sequence[str] = (),
        type: str | None = None,
        limit: int = 50,
        offset: int = 0,
        query: str | None = None,
        source_client: str | None = None,
    ) -> list[MemoryRecord]: ...

    @abstractmethod
    async def search(
        self,
        *,
        query: str,
        top_k: int = 5,
        tags: Sequence[str] = (),
        types: Sequence[str] = (),
        active_only: bool = True,
    ) -> list[ScoredMemory]: ...

    @abstractmethod
    async def approve(
        self, memory_id: UUID, *, actor_client_id: UUID | None = None
    ) -> MemoryRecord | None:
        """Move pending -> active. Logs an 'approve' change.
        Returns None if not found."""

    @abstractmethod
    async def revise(
        self,
        memory_id: UUID,
        *,
        content: str | None = None,
        type: str | None = None,
        scope_tags: Sequence[str] | None = None,
        confidence: float | None = None,
        actor_client_id: UUID | None = None,
    ) -> MemoryRecord | None:
        """Edit with history (PRD US2): create a NEW record carrying the edit,
        archive the old one with superseded_by -> new id. The old version
        stays in the chain for provenance. The new record's version is
        old.version + 1 (US7 optimistic concurrency). Logs a 'revise' change
        against the NEW record. Returns the new record, or None
        if the memory does not exist."""

    @abstractmethod
    async def delete(self, memory_id: UUID, *, actor_client_id: UUID | None = None) -> bool:
        """Hard delete: row AND vector gone (PRD US5). Logs a 'delete'
        change (a tombstone: the row no longer exists, so sync consumers
        only get the id + op)."""

    @abstractmethod
    async def find_similar_active(
        self, vec: Sequence[float], *, threshold: float = 0.70, limit: int = 5
    ) -> list[ScoredMemory]:
        """Similarity probe for triage: active, non-encrypted memories with
        score >= threshold, descending. Does NOT touch last_retrieved_at
        and never triggers decay (it is a probe, not a user retrieval)."""

    @abstractmethod
    async def create_active(
        self,
        *,
        content: str,
        type: str,
        scope_tags: Sequence[str],
        confidence: float,
        source: dict[str, Any],
        actor_client_id: UUID | None = None,
        triage_reasons: Sequence[str] = (),
    ) -> tuple[MemoryRecord, list[ScoredMemory]]:
        """Triage auto-approve path: like create_pending but status='active'
        immediately. Still returns duplicate candidates for the response.
        Logs a 'create' change; the caller audits 'auto_approve'."""

    @abstractmethod
    async def auto_merge(
        self,
        target_id: UUID,
        *,
        content: str,
        confidence: float,
        actor_client_id: UUID | None = None,
    ) -> MemoryRecord | None:
        """Fold a near-identical memory (>= 0.97) into an active target:
        confidence = max, updated_at bumped. Tags are deliberately NOT
        unioned (T1: tags drive scoped reads; silently expanding tag
        coverage on an automated path is an unneeded risk surface).
        Logs a 'revise' change; the caller audits 'auto_merge' with the
        incoming content hash. Returns the target, or None if missing."""

    @abstractmethod
    async def archive(
        self,
        memory_id: UUID,
        *,
        actor_client_id: UUID | None = None,
        superseded_by: UUID | None = None,
        source: dict | None = None,
    ) -> MemoryRecord | None:
        """Soft-archive: status -> 'archived' (restorable, never deleted).
        Logs an 'archive' change; the caller audits the reason
        (e.g. 'auto_archive' from decay)."""

    @abstractmethod
    async def touch_retrieved(
        self, memory_ids: Sequence[UUID], *, at: datetime | None = None
    ) -> None:
        """Reset the decay idle clock for retrieved memories. Metadata-only
        (no content/provenance change) — the narrow, documented exception
        to the no-in-place-update rule below."""

    @abstractmethod
    async def set_pinned(
        self, memory_id: UUID, pinned: bool, *, actor_client_id: UUID | None = None
    ) -> MemoryRecord | None:
        """Pin/unpin a memory. Pinned memories never decay. Metadata-only
        (same exception as touch_retrieved); logs a 'revise' change so sync
        consumers refresh, and the caller audits the action."""

    async def lifecycle_feedback(
        self, memory_id: UUID, *, boost: float = 0, multiplier: float = 1, at=None
    ) -> MemoryRecord | None:
        """Atomically adjust lifecycle metadata, preserving content and history."""
        raise NotImplementedError

    # NOTE: there is intentionally NO in-place update() on this interface.
    # All edits go through revise(), which archives the old record and links
    # it via superseded_by (PRD US2 history chain). An in-place update would
    # silently destroy provenance.

    async def resolve_head(self, memory_id: UUID) -> MemoryRecord | None:
        """Follow superseded_by to the current head of the revision chain.

        Clients doing optimistic concurrency (US7) hold the id they read;
        after another writer revises, that id points at an archived record.
        Resolving to the head lets the version check compare against the
        CURRENT version, so a stale id still produces a proper conflict
        instead of silently resurrecting an old revision. Cycle-safe.
        """
        seen: set[UUID] = set()
        cur = await self.get(memory_id)
        while cur is not None and cur.superseded_by is not None and cur.superseded_by not in seen:
            seen.add(cur.id)
            cur = await self.get(cur.superseded_by)
        return cur

    @abstractmethod
    async def update_with_version(
        self,
        memory_id: UUID,
        *,
        expected_version: int | None,
        content: str,
        actor_client_id: UUID | None = None,
    ) -> tuple[MemoryRecord | None, MemoryRecord | None]:
        """Optimistic-concurrency revise (US7).

        The addressed id is first resolved to the current head of the
        revision chain (resolve_head): a client holding a stale id still
        gets a correct version comparison instead of resurrecting an old
        revision.

        - expected_version None: plain revise() of the head, loser None.
        - matches the head's current version: plain revise(), loser None.
        - mismatch: last-writer-wins STILL applies (revise anyway), AND the
          overwritten version's content is copied into a NEW pending inbox
          record (source={"conflict_of": str(new_id), "client_id": ...},
          confidence preserved) so nothing is silently dropped.
        Returns (new_record_or_None, loser_pending_record_or_None)."""

    @abstractmethod
    async def log_change(
        self,
        *,
        memory_id: UUID,
        op: str,
        actor_client_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Append one entry to the change feed. op is one of
        create|approve|revise|delete. Returns the entry dict
        {seq, memory_id, op, actor_client_id, at}."""

    @abstractmethod
    async def get_changes(self, since: int, limit: int) -> tuple[list[dict[str, Any]], int]:
        """Changes with seq > since, oldest first, at most limit.
        Returns (changes, head_seq) where head_seq is the current feed head
        (0 when empty) regardless of limit."""

    @abstractmethod
    async def upsert_cursor(self, adapter_id: str, cursor: int) -> dict[str, Any]:
        """Set an adapter's sync cursor (explicit ack) and bump last_seen.
        Returns {adapter_id, cursor, last_seen}."""

    @abstractmethod
    async def list_cursors(self) -> list[dict[str, Any]]:
        """All sync cursors: [{adapter_id, cursor, last_seen}], ordered by
        adapter_id. Drives the panel /panel/sync adapter table."""

    @abstractmethod
    async def audit(
        self,
        *,
        action: str,
        memory_ids: Sequence[UUID] = (),
        client_id: UUID | None = None,
        scopes_used: Sequence[str] = (),
        meta: dict[str, Any] | None = None,
    ) -> AuditRecord: ...

    @abstractmethod
    async def audit_list(
        self,
        limit: int = 100,
        client_id: UUID | None = None,
        since: datetime | None = None,
    ) -> list[AuditRecord]: ...


# ================================================================ in-memory
class InMemoryMemoryRepository(MemoryRepository):
    """Test/dev implementation. Same rules, numpy similarity, no persistence."""

    def __init__(self, embedder: Embedder) -> None:
        super().__init__(embedder)
        self._memories: dict[UUID, MemoryRecord] = {}
        self._audit: list[AuditRecord] = []
        self._audit_locked: bool = False  # simulates the DB trigger
        # P1 sync feed (PRD §9.5): append-only change list + cursor table.
        self._changes: list[dict[str, Any]] = []
        self._seq: int = 0
        self._cursors: dict[str, dict[str, Any]] = {}
        # Serializes update_with_version: without it, two coroutines racing
        # on the same record interleave between resolve_head and revise and
        # branch the history chain (two active heads). Single-threaded
        # asyncio still interleaves at awaits.
        self._uv_lock = asyncio.Lock()

    def _similar(
        self, vec: Sequence[float], candidates: list[MemoryRecord], top_k: int
    ) -> list[ScoredMemory]:
        scored = [
            ScoredMemory(m, self.embedder.cosine(vec, m.embedding or []))
            for m in candidates
            if m.embedding
        ]
        scored.sort(key=lambda s: s.score, reverse=True)
        return scored[:top_k]

    def _filter(
        self,
        memories: list[MemoryRecord],
        tags: Sequence[str],
        types: Sequence[str],
        active_only: bool,
    ) -> list[MemoryRecord]:
        out = []
        for m in memories:
            if active_only and m.status != "active":
                continue
            if tags and not set(tags).intersection(m.scope_tags):
                continue
            if types and m.type not in types:
                continue
            out.append(m)
        return out

    async def create_pending(
        self,
        *,
        content,
        type,
        scope_tags,
        confidence,
        source,
        actor_client_id=None,
        encrypted=False,
        key_id=None,
        triage_reasons=(),
    ):
        # E2EE (P2): ciphertext is stored as-is, embedding=None (the server
        # must not see plaintext, so there is nothing to embed), and
        # duplicate detection is skipped (ciphertext is not comparable —
        # even identical plaintexts encrypt differently under random nonces).
        from .teams import validate_workspace_memory

        validate_workspace_memory(type, scope_tags)
        vec = None if encrypted else await self.embedder.aembed_one(content)
        dups: list[ScoredMemory] = []
        if not encrypted:
            dups = [
                s
                for s in self._similar(vec, list(self._memories.values()), 5)
                if s.score > DUPLICATE_THRESHOLD
            ]
        rec = MemoryRecord(
            id=uuid4(),
            content=content,
            type=type,
            scope_tags=list(scope_tags),
            source=dict(source),
            confidence=confidence,
            status="pending",
            embedding=vec,
            created_at=_now(),
            updated_at=_now(),
            encrypted=bool(encrypted),
            key_id=key_id,
            triage_reasons=list(triage_reasons),
        )
        self._memories[rec.id] = rec
        await self.log_change(memory_id=rec.id, op="create", actor_client_id=actor_client_id)
        return rec, dups

    async def find_similar_active(self, vec, *, threshold=0.70, limit=5):
        cands = [
            m
            for m in self._memories.values()
            if m.status == "active" and not m.encrypted and m.embedding
        ]
        return [s for s in self._similar(vec, cands, limit) if s.score >= threshold]

    async def create_active(
        self,
        *,
        content,
        type,
        scope_tags,
        confidence,
        source,
        actor_client_id=None,
        triage_reasons=(),
    ):
        from .teams import validate_workspace_memory

        validate_workspace_memory(type, scope_tags)
        vec = await self.embedder.aembed_one(content)
        dups: list[ScoredMemory] = [
            s
            for s in self._similar(vec, list(self._memories.values()), 5)
            if s.score > DUPLICATE_THRESHOLD
        ]
        rec = MemoryRecord(
            id=uuid4(),
            content=content,
            type=type,
            scope_tags=list(scope_tags),
            source=dict(source),
            confidence=confidence,
            status="active",
            embedding=vec,
            created_at=_now(),
            updated_at=_now(),
            triage_reasons=list(triage_reasons),
        )
        self._memories[rec.id] = rec
        await self.log_change(memory_id=rec.id, op="create", actor_client_id=actor_client_id)
        return rec, dups

    async def auto_merge(self, target_id, *, content, confidence, actor_client_id=None):
        # See the abstract docstring: tags are deliberately NOT unioned (T1).
        rec = self._memories.get(target_id)
        if rec is None or rec.status != "active" or rec.encrypted:
            return None
        rec.confidence = max(rec.confidence, confidence)
        rec.updated_at = _now()
        rec.last_retrieved_at = rec.updated_at
        await self.log_change(memory_id=rec.id, op="revise", actor_client_id=actor_client_id)
        return rec

    async def archive(self, memory_id, *, actor_client_id=None, superseded_by=None, source=None):
        rec = self._memories.get(memory_id)
        if rec is None or rec.status == "archived":
            return rec
        rec.status = "archived"
        if superseded_by is not None:
            rec.superseded_by = superseded_by
        if source is not None:
            rec.source = dict(source)
        rec.updated_at = _now()
        await self.log_change(memory_id=rec.id, op="archive", actor_client_id=actor_client_id)
        return rec

    async def touch_retrieved(self, memory_ids, *, at=None):
        stamp = at or _now()
        for mid in memory_ids:
            rec = self._memories.get(mid)
            if rec is not None:
                rec.last_retrieved_at = stamp

    async def lifecycle_feedback(self, memory_id, *, boost=0, multiplier=1, at=None):
        if not (0 <= boost <= 1 and 0 <= multiplier <= 1):
            raise InvalidInput("lifecycle feedback factors must be between 0 and 1")
        rec = self._memories.get(memory_id)
        if rec is None or rec.status != "active" or rec.encrypted or rec.pinned:
            return None
        rec.confidence = min(1.0, rec.confidence * multiplier + boost * (1 - rec.confidence))
        if at is not None:
            rec.last_retrieved_at = at
        await self.log_change(memory_id=rec.id, op="revise")
        return rec

    async def set_pinned(self, memory_id, pinned, *, actor_client_id=None):
        rec = self._memories.get(memory_id)
        if rec is None:
            return None
        rec.pinned = bool(pinned)
        rec.updated_at = _now()
        await self.log_change(memory_id=rec.id, op="revise", actor_client_id=actor_client_id)
        return rec

    async def get(self, memory_id):
        from .decay import apply_on_read

        rec = self._memories.get(memory_id)
        if rec is None:
            return None
        alive = await apply_on_read(self, [rec])
        return alive[0] if alive else rec

    async def list(
        self, *, status=None, tags=(), type=None, limit=50, offset=0, query=None, source_client=None
    ):
        rows = list(self._memories.values())
        if status:
            rows = [m for m in rows if m.status == status]
        if tags:
            rows = [m for m in rows if set(tags).intersection(m.scope_tags)]
        if type:
            rows = [m for m in rows if m.type == type]
        if query:
            q = query.lower()
            rows = [m for m in rows if q in m.content.lower()]
        if source_client:
            rows = [m for m in rows if m.source.get("client_id") == source_client]
        rows.sort(key=lambda m: m.created_at, reverse=True)
        return rows[offset : offset + limit]

    async def search(self, *, query, top_k=5, tags=(), types=(), active_only=True):
        # E2EE (P2): encrypted memories are EXCLUDED explicitly. They carry
        # no embedding (the server never sees plaintext, so there is nothing
        # to embed or compare), so semantic search can never match them.
        # The explicit filter — not just the `embedding is None` guard in
        # _similar — keeps this true even if a future change ever backfills
        # embeddings, and documents the guarantee at the decision point.
        vec = await self.embedder.aembed_one(query)
        cands = [
            m
            for m in self._filter(list(self._memories.values()), tags, types, active_only)
            if not m.encrypted
        ]
        scored = self._similar(vec, cands, top_k)
        # Decay (automation layer): idle memories lose confidence; those
        # below the archive threshold are archived here, audited, and
        # excluded from results. Touches last_retrieved_at for survivors.
        from .decay import apply_on_read

        alive = await apply_on_read(self, [s.record for s in scored])
        alive_ids = {m.id for m in alive}
        return [s for s in scored if s.record.id in alive_ids]

    async def approve(self, memory_id, *, actor_client_id=None):
        rec = self._memories.get(memory_id)
        if rec is None:
            return None
        rec.status = "active"
        rec.updated_at = _now()
        await self.log_change(memory_id=rec.id, op="approve", actor_client_id=actor_client_id)
        return rec

    async def revise(
        self,
        memory_id,
        *,
        content=None,
        type=None,
        scope_tags=None,
        confidence=None,
        actor_client_id=None,
    ):
        old = self._memories.get(memory_id)
        if old is None:
            return None
        new_content = content if content is not None else old.content
        # E2EE (P2): encryption is a property of the record, carried through
        # the history chain. The server is ciphertext-blind: new_content is
        # stored as-is (the panel shows ciphertext for encrypted records, so
        # an edit pastes new ciphertext), never re-embedded.
        vec = None if old.encrypted else await self.embedder.aembed_one(new_content)
        new = MemoryRecord(
            id=uuid4(),
            content=new_content,
            type=type if type is not None else old.type,
            scope_tags=list(scope_tags) if scope_tags is not None else list(old.scope_tags),
            source=dict(old.source),
            confidence=confidence if confidence is not None else old.confidence,
            status=old.status,
            embedding=vec,
            created_at=_now(),
            updated_at=_now(),
            version=old.version + 1,  # US7 optimistic-concurrency counter
            encrypted=old.encrypted,
            key_id=old.key_id,
            pinned=old.pinned,
            triage_reasons=list(old.triage_reasons),
        )
        new.source["revised_from"] = str(old.id)
        old.status = "archived"
        old.superseded_by = new.id
        old.updated_at = _now()
        self._memories[new.id] = new
        await self.log_change(memory_id=old.id, op="archive", actor_client_id=actor_client_id)
        # The change subject is the NEW record (the old one is archived).
        await self.log_change(memory_id=new.id, op="revise", actor_client_id=actor_client_id)
        return new

    async def delete(self, memory_id, *, actor_client_id=None):
        if memory_id not in self._memories:
            return False
        await self.log_change(memory_id=memory_id, op="delete", actor_client_id=actor_client_id)
        self._memories.pop(memory_id)
        return True

    async def update_with_version(
        self, memory_id, *, expected_version, content, actor_client_id=None
    ):
        # Serialized: resolve_head + revise + loser-preservation must be
        # atomic w.r.t. other writers, or the history chain branches (two
        # active heads). See the _uv_lock note in __init__.
        async with self._uv_lock:
            return await self._update_with_version_inner(
                memory_id,
                expected_version=expected_version,
                content=content,
                actor_client_id=actor_client_id,
            )

    async def _update_with_version_inner(
        self, memory_id, *, expected_version, content, actor_client_id=None
    ):
        head = await self.resolve_head(memory_id)
        if head is None:
            return None, None
        old = self._memories.get(head.id)
        if old is None:
            return None, None
        conflict = expected_version is not None and expected_version != old.version
        new = await self.revise(head.id, content=content, actor_client_id=actor_client_id)
        loser = None
        if conflict and new is not None:
            # Last-writer-wins still applied above; preserve the overwritten
            # content as a new pending inbox record so nothing is lost (US7).
            # E2EE: the loser's encryption rides along (ciphertext-blind copy).
            loser, _dups = await self.create_pending(
                content=old.content,
                type=old.type,
                scope_tags=list(old.scope_tags),
                confidence=old.confidence,  # preserved
                source={
                    "conflict_of": str(new.id),
                    "client_id": (str(actor_client_id) if actor_client_id else None),
                },
                actor_client_id=actor_client_id,
                encrypted=old.encrypted,
                key_id=old.key_id,
            )
        return new, loser

    async def log_change(self, *, memory_id, op, actor_client_id=None):
        self._seq += 1
        entry = {
            "seq": self._seq,
            "memory_id": memory_id,
            "op": op,
            "actor_client_id": actor_client_id,
            "at": _now(),
            "scope_tags": list(self._memories[memory_id].scope_tags)
            if memory_id in self._memories
            else None,
        }
        self._changes.append(entry)
        return entry

    async def get_changes(self, since, limit):
        rows = [c for c in self._changes if c["seq"] > since][:limit]
        return [dict(c) for c in rows], self._seq

    async def upsert_cursor(self, adapter_id, cursor):
        self._cursors[adapter_id] = {
            "adapter_id": adapter_id,
            "cursor": int(cursor),
            "last_seen": _now(),
        }
        return dict(self._cursors[adapter_id])

    async def list_cursors(self):
        return [dict(self._cursors[k]) for k in sorted(self._cursors)]

    async def audit(self, *, action, memory_ids=(), client_id=None, scopes_used=(), meta=None):
        if self._audit_locked:
            raise RuntimeError("audit log is append-only")
        rec = AuditRecord(
            id=uuid4(),
            client_id=client_id,
            action=action,
            memory_ids=list(memory_ids),
            scopes_used=list(scopes_used),
            meta=dict(meta or {}),
            created_at=_now(),
        )
        self._audit.append(rec)
        return rec

    async def audit_list(
        self,
        limit: int = 100,
        client_id: UUID | None = None,
        since: datetime | None = None,
    ):
        rows = list(reversed(self._audit))
        if client_id is not None:
            rows = [r for r in rows if r.client_id == client_id]
        if since is not None:
            rows = [r for r in rows if r.created_at >= since]
        return rows[:limit]

    # test helper: prove append-only by attempting mutation
    def lock_audit(self) -> None:
        self._audit_locked = True


# ================================================================ postgres
# E2EE (P2): `encrypted`/`key_id` ride along on every select/RETURNING so the
# two impls can never drift apart. Search excludes encrypted rows explicitly
# (see PostgresMemoryRepository.search).
_PG_COLS = (
    "id, content, type, scope_tags, source, confidence, status,"
    " embedding::text, created_at, updated_at, superseded_by, version,"
    " encrypted, key_id, last_retrieved_at, pinned, triage_reasons"
)
_PG_LIST_SQL = f"""
SELECT {_PG_COLS}
FROM memories
"""
_PG_RETURNING = f"""
RETURNING {_PG_COLS}
"""


def _row_to_record(row: Any) -> MemoryRecord:
    emb = row["embedding"]
    return MemoryRecord(
        id=row["id"],
        content=row["content"],
        type=row["type"],
        scope_tags=list(row["scope_tags"] or []),
        source=json_object(row["source"]),
        confidence=float(row["confidence"]),
        status=row["status"],
        embedding=_parse_vector(emb),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        superseded_by=row["superseded_by"],
        version=int(row["version"]) if row["version"] is not None else 1,
        encrypted=bool(row["encrypted"]) if row["encrypted"] is not None else False,
        key_id=row["key_id"],
        last_retrieved_at=row["last_retrieved_at"] or row["updated_at"],
        pinned=bool(row["pinned"]) if row["pinned"] is not None else False,
        triage_reasons=list(json.loads(row["triage_reasons"] or "[]")),
    )


def _parse_vector(text: str | None) -> list[float] | None:
    if text is None:
        return None
    return [float(x) for x in text.strip("[]").split(",") if x]


class _TransactionalPool:
    """Route compound repository operations through their current connection."""

    def __init__(self, pool):
        self.raw = pool
        self.connection = ContextVar("memory_transaction_connection", default=None)

    @asynccontextmanager
    async def acquire(self):
        current = self.connection.get()
        if current is not None:
            yield current
        else:
            async with self.raw.acquire() as connection:
                yield connection

    def __getattr__(self, name):
        current = self.connection.get()
        return getattr(current if current is not None else self.raw, name)


class PostgresMemoryRepository(MemoryRepository):
    """Production implementation over asyncpg + pgvector.

    Requires a pool created by porter.db.create_pool() with migrations applied.
    """

    def __init__(self, embedder: Embedder, pool) -> None:  # pool: asyncpg.Pool
        super().__init__(embedder)
        self.pool = _TransactionalPool(pool)

    @asynccontextmanager
    async def atomic(self, memory_ids):
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                token = self.pool.connection.set(connection)
                try:
                    await connection.fetch(
                        "SELECT id FROM memories WHERE id = ANY($1::uuid[]) ORDER BY id FOR UPDATE",
                        list(set(memory_ids)),
                    )
                    yield
                finally:
                    self.pool.connection.reset(token)

    @staticmethod
    def _vec_literal(vec: Sequence[float]) -> str:
        return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"

    async def create_pending(
        self,
        *,
        content,
        type,
        scope_tags,
        confidence,
        source,
        actor_client_id=None,
        encrypted=False,
        key_id=None,
        triage_reasons=(),
    ):
        # E2EE (P2): ciphertext stored as-is, embedding NULL (the server must
        # not see plaintext), duplicate detection skipped (ciphertext is not
        # comparable — identical plaintexts differ under random nonces).
        from .teams import validate_workspace_memory

        validate_workspace_memory(type, scope_tags)
        vec = None if encrypted else await self.embedder.aembed_one(content)
        async with self.pool.acquire() as con:
            async with con.transaction():
                row = await con.fetchrow(
                    f"""
                    INSERT INTO memories (content, type, scope_tags, confidence, source, embedding,
                                          encrypted, key_id, triage_reasons)
                    VALUES ($1, $2, $3, $4, $5, $6::vector, $7, $8, $9)
                    {_PG_RETURNING}
                    """,
                    content,
                    type,
                    list(scope_tags),
                    confidence,
                    _json(source),
                    self._vec_literal(vec) if vec is not None else None,
                    bool(encrypted),
                    key_id,
                    json.dumps(list(triage_reasons)),
                )
                await con.execute(
                    "INSERT INTO change_log (memory_id, op, actor_client_id)"
                    " VALUES ($1, 'create', $2)",
                    row["id"],
                    actor_client_id,
                )
            dup_rows = []
            if not encrypted:
                dup_rows = await con.fetch(
                    """
                    SELECT id, 1 - (embedding <=> $1::vector) AS score
                    FROM memories
                    WHERE id != $2 AND embedding IS NOT NULL
                      AND 1 - (embedding <=> $1::vector) > $3
                    ORDER BY embedding <=> $1::vector
                    LIMIT 5
                    """,
                    self._vec_literal(vec),
                    row["id"],
                    DUPLICATE_THRESHOLD,
                )
        rec = _row_to_record(row)
        dups = [
            ScoredMemory(
                MemoryRecord(
                    id=r["id"],
                    content="",
                    type="",
                    scope_tags=[],
                    source={},
                    confidence=0.0,
                    status="",
                    embedding=None,
                    created_at=_now(),
                    updated_at=_now(),
                ),
                float(r["score"]),
            )
            for r in dup_rows
        ]
        return rec, dups

    async def find_similar_active(self, vec, *, threshold=0.70, limit=5):
        # Probe for triage: no last_retrieved_at touch, no decay (see
        # abstract docstring). Return full records just like the memory
        # backend: triage needs project tags and the inbox needs labels.
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                f"""
                SELECT {_PG_COLS}, 1 - (embedding <=> $1::vector) AS score
                FROM memories
                WHERE status = 'active' AND encrypted = FALSE
                  AND embedding IS NOT NULL
                  AND 1 - (embedding <=> $1::vector) >= $2
                ORDER BY embedding <=> $1::vector
                LIMIT $3
                """,
                self._vec_literal(vec),
                threshold,
                limit,
            )
        return [
            ScoredMemory(
                _row_to_record(r),
                float(r["score"]),
            )
            for r in rows
        ]

    async def create_active(
        self,
        *,
        content,
        type,
        scope_tags,
        confidence,
        source,
        actor_client_id=None,
        triage_reasons=(),
    ):
        from .teams import validate_workspace_memory

        validate_workspace_memory(type, scope_tags)
        vec = await self.embedder.aembed_one(content)
        async with self.pool.acquire() as con:
            async with con.transaction():
                row = await con.fetchrow(
                    f"""
                    INSERT INTO memories (content, type, scope_tags, confidence, source,
                                          embedding, status, triage_reasons)
                    VALUES ($1, $2, $3, $4, $5, $6::vector, 'active', $7)
                    {_PG_RETURNING}
                    """,
                    content,
                    type,
                    list(scope_tags),
                    confidence,
                    _json(source),
                    self._vec_literal(vec),
                    json.dumps(list(triage_reasons)),
                )
                await con.execute(
                    "INSERT INTO change_log (memory_id, op, actor_client_id)"
                    " VALUES ($1, 'create', $2)",
                    row["id"],
                    actor_client_id,
                )
            dup_rows = await con.fetch(
                """
                SELECT id, 1 - (embedding <=> $1::vector) AS score
                FROM memories
                WHERE id != $2 AND embedding IS NOT NULL
                  AND 1 - (embedding <=> $1::vector) > $3
                ORDER BY embedding <=> $1::vector
                LIMIT 5
                """,
                self._vec_literal(vec),
                row["id"],
                DUPLICATE_THRESHOLD,
            )
        rec = _row_to_record(row)
        dups = [
            ScoredMemory(
                MemoryRecord(
                    id=r["id"],
                    content="",
                    type="",
                    scope_tags=[],
                    source={},
                    confidence=0.0,
                    status="",
                    embedding=None,
                    created_at=_now(),
                    updated_at=_now(),
                ),
                float(r["score"]),
            )
            for r in dup_rows
        ]
        return rec, dups

    async def auto_merge(self, target_id, *, content, confidence, actor_client_id=None):
        # Tags deliberately NOT unioned (T1) — see abstract docstring.
        async with self.pool.acquire() as con:
            async with con.transaction():
                row = await con.fetchrow(
                    f"""
                    UPDATE memories
                    SET confidence = GREATEST(confidence, $2),
                        updated_at = now(), last_retrieved_at = now()
                    WHERE id = $1 AND status = 'active' AND encrypted = FALSE
                    {_PG_RETURNING}
                    """,
                    target_id,
                    confidence,
                )
                if row is None:
                    return None
                await con.execute(
                    "INSERT INTO change_log (memory_id, op, actor_client_id)"
                    " VALUES ($1, 'revise', $2)",
                    row["id"],
                    actor_client_id,
                )
        return _row_to_record(row)

    async def archive(self, memory_id, *, actor_client_id=None, superseded_by=None, source=None):
        async with self.pool.acquire() as con:
            async with con.transaction():
                row = await con.fetchrow(
                    f"""
                    UPDATE memories
                    SET status = 'archived', updated_at = now(),
                        superseded_by = COALESCE($2, superseded_by),
                        source = COALESCE($3::jsonb, source)
                    WHERE id = $1 AND status != 'archived'
                    {_PG_RETURNING}
                    """,
                    memory_id,
                    superseded_by,
                    _json(source) if source is not None else None,
                )
                if row is None:
                    # Already archived or missing: return current state.
                    row = await con.fetchrow(_PG_LIST_SQL + "WHERE id = $1", memory_id)
                    return _row_to_record(row) if row else None
                await con.execute(
                    "INSERT INTO change_log (memory_id, op, actor_client_id)"
                    " VALUES ($1, 'archive', $2)",
                    row["id"],
                    actor_client_id,
                )
        return _row_to_record(row)

    async def touch_retrieved(self, memory_ids, *, at=None):
        ids = list(memory_ids)
        if not ids:
            return
        stamp = at or _now()
        async with self.pool.acquire() as con:
            await con.execute(
                "UPDATE memories SET last_retrieved_at = $1 WHERE id = ANY($2)",
                stamp,
                ids,
            )

    async def lifecycle_feedback(self, memory_id, *, boost=0, multiplier=1, at=None):
        if not (0 <= boost <= 1 and 0 <= multiplier <= 1):
            raise InvalidInput("lifecycle feedback factors must be between 0 and 1")
        async with self.pool.acquire() as con:
            async with con.transaction():
                row = await con.fetchrow(
                    f"""UPDATE memories
                    SET confidence = LEAST(1.0, confidence * $2 + $3 * (1 - confidence)),
                        last_retrieved_at = COALESCE($4, last_retrieved_at)
                    WHERE id = $1 AND status = 'active' AND NOT encrypted AND NOT pinned
                    {_PG_RETURNING}""",
                    memory_id,
                    float(multiplier),
                    float(boost),
                    at,
                )
                if row is None:
                    return None
                await con.execute(
                    "INSERT INTO change_log (memory_id, op) VALUES ($1, 'revise')", memory_id
                )
        return _row_to_record(row)

    async def set_pinned(self, memory_id, pinned, *, actor_client_id=None):
        async with self.pool.acquire() as con:
            async with con.transaction():
                row = await con.fetchrow(
                    f"""
                    UPDATE memories SET pinned = $2, updated_at = now()
                    WHERE id = $1
                    {_PG_RETURNING}
                    """,
                    memory_id,
                    bool(pinned),
                )
                if row is None:
                    return None
                await con.execute(
                    "INSERT INTO change_log (memory_id, op, actor_client_id)"
                    " VALUES ($1, 'revise', $2)",
                    row["id"],
                    actor_client_id,
                )
        return _row_to_record(row)

    async def get(self, memory_id):
        from .decay import apply_on_read

        async with self.pool.acquire() as con:
            row = await con.fetchrow(_PG_LIST_SQL + "WHERE id = $1", memory_id)
        if not row:
            return None
        rec = _row_to_record(row)
        alive = await apply_on_read(self, [rec])
        if not alive:
            # apply_on_read archived it (decay) — re-fetch so the caller
            # sees the current status, not the pre-archive snapshot.
            async with self.pool.acquire() as con:
                row = await con.fetchrow(_PG_LIST_SQL + "WHERE id = $1", memory_id)
            return _row_to_record(row) if row else None
        return alive[0]

    async def list(
        self, *, status=None, tags=(), type=None, limit=50, offset=0, query=None, source_client=None
    ):
        clauses, args = [], []
        if status:
            clauses.append(f"status = ${len(args) + 1}")
            args.append(status)
        if tags:
            clauses.append(f"scope_tags && ${len(args) + 1}")
            args.append(list(tags))
        if type:
            clauses.append(f"type = ${len(args) + 1}")
            args.append(type)
        if query:
            # position() avoids LIKE-escape pitfalls entirely.
            clauses.append(f"position(lower(${len(args) + 1}) in lower(content)) > 0")
            args.append(query)
        if source_client:
            clauses.append(f"source->>'client_id' = ${len(args) + 1}")
            args.append(source_client)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                _PG_LIST_SQL + f"{where} ORDER BY created_at DESC "
                f"LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}",
                *args,
                limit,
                offset,
            )
        return [_row_to_record(r) for r in rows]

    async def search(self, *, query, top_k=5, tags=(), types=(), active_only=True):
        vec = await self.embedder.aembed_one(query)
        # E2EE (P2): encrypted rows are excluded EXPLICITLY. They carry no
        # embedding (the server never sees plaintext), so `embedding IS NOT
        # NULL` would already skip them — but the explicit predicate keeps
        # the guarantee at the decision point even if a future migration
        # ever backfills embeddings, and documents it for reviewers.
        clauses, args = (
            [
                "embedding IS NOT NULL",
                "encrypted = FALSE",
            ],
            [self._vec_literal(vec)],
        )
        if active_only:
            clauses.append("status = 'active'")
        if tags:
            clauses.append(f"scope_tags && ${len(args) + 1}")
            args.append(list(tags))
        if types:
            clauses.append(f"type = ANY(${len(args) + 1})")
            args.append(list(types))
        where = "WHERE " + " AND ".join(clauses)
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                _PG_LIST_SQL.replace(
                    "embedding::text,",
                    "embedding::text, 1 - (embedding <=> $1::vector) AS score,",
                )
                + f"{where} ORDER BY embedding <=> $1::vector LIMIT ${len(args) + 1}",
                *args,
                top_k,
            )
        scored = [ScoredMemory(_row_to_record(r), float(r["score"])) for r in rows]
        # Decay (automation layer): see InMemoryMemoryRepository.search.
        from .decay import apply_on_read

        alive = await apply_on_read(self, [s.record for s in scored])
        alive_ids = {m.id for m in alive}
        return [s for s in scored if s.record.id in alive_ids]

    async def approve(self, memory_id, *, actor_client_id=None):
        # Single UPDATE...RETURNING: no TOCTOU window between check and write.
        async with self.pool.acquire() as con:
            async with con.transaction():
                row = await con.fetchrow(
                    f"""
                    UPDATE memories SET status = 'active', updated_at = now()
                    WHERE id = $1
                    {_PG_RETURNING}
                    """,
                    memory_id,
                )
                if row is not None:
                    await con.execute(
                        "INSERT INTO change_log (memory_id, op, actor_client_id)"
                        " VALUES ($1, 'approve', $2)",
                        row["id"],
                        actor_client_id,
                    )
        return _row_to_record(row) if row else None

    async def _revise_tx(
        self,
        con,
        old: MemoryRecord,
        *,
        content: str | None = None,
        type: str | None = None,
        scope_tags: Sequence[str] | None = None,
        confidence: float | None = None,
        actor_client_id: UUID | None = None,
    ) -> Any:
        """History-preserving edit inside an open transaction. Returns the
        new row. The new record's version is old.version + 1 (US7)."""
        new_content = content if content is not None else old.content
        new_type = type if type is not None else old.type
        new_tags = list(scope_tags) if scope_tags is not None else old.scope_tags
        new_conf = confidence if confidence is not None else old.confidence
        new_source = dict(old.source)
        new_source["revised_from"] = str(old.id)
        # E2EE (P2): encryption rides the history chain; the server stays
        # ciphertext-blind (no re-embed — the new content is opaque to us).
        vec = None if old.encrypted else await self.embedder.aembed_one(new_content)
        new_row = await con.fetchrow(
            f"""
            INSERT INTO memories
                (content, type, scope_tags, confidence, source, status,
                 embedding, version, encrypted, key_id, pinned, triage_reasons)
            VALUES ($1, $2, $3, $4, $5, $6, $7::vector, $8, $9, $10, $11, $12)
            {_PG_RETURNING}
            """,
            new_content,
            new_type,
            new_tags,
            new_conf,
            _json(new_source),
            old.status,
            self._vec_literal(vec) if vec is not None else None,
            old.version + 1,
            old.encrypted,
            old.key_id,
            old.pinned,
            _json(old.triage_reasons),
        )
        await con.execute(
            "UPDATE memories SET status = 'archived',"
            " superseded_by = $2, updated_at = now() WHERE id = $1",
            old.id,
            new_row["id"],
        )
        # Retire the old ID in adapters before publishing its replacement.
        await con.execute(
            "INSERT INTO change_log (memory_id, op, actor_client_id) VALUES ($1, 'archive', $2)",
            old.id,
            actor_client_id,
        )
        await con.execute(
            "INSERT INTO change_log (memory_id, op, actor_client_id) VALUES ($1, 'revise', $2)",
            new_row["id"],
            actor_client_id,
        )
        return new_row

    async def revise(
        self,
        memory_id,
        *,
        content=None,
        type=None,
        scope_tags=None,
        confidence=None,
        actor_client_id=None,
    ):
        # Edit with history (PRD US2): new row carries the edit; the old row
        # is archived with superseded_by -> new id. One transaction so the
        # chain can never half-exist.
        async with self.pool.acquire() as con:
            async with con.transaction():
                old_row = await con.fetchrow(_PG_LIST_SQL + "WHERE id = $1 FOR UPDATE", memory_id)
                if old_row is None:
                    return None
                new_row = await self._revise_tx(
                    con,
                    _row_to_record(old_row),
                    content=content,
                    type=type,
                    scope_tags=scope_tags,
                    confidence=confidence,
                    actor_client_id=actor_client_id,
                )
        return _row_to_record(new_row)

    async def update_with_version(
        self, memory_id, *, expected_version, content, actor_client_id=None
    ):
        async with self.pool.acquire() as con:
            async with con.transaction():
                # Resolve the revision chain INSIDE the transaction, holding
                # a FOR UPDATE lock on each link. Resolving before the lock
                # (as before) lets two concurrent writers both lock the
                # same head and branch the history chain; locks are taken
                # in forward chain order, so no deadlock cycle is possible.
                seen: set[UUID] = set()
                cur_id = memory_id
                old_row = None
                while True:
                    row = await con.fetchrow(_PG_LIST_SQL + "WHERE id = $1 FOR UPDATE", cur_id)
                    if row is None:
                        return None, None
                    sb = row["superseded_by"]
                    if sb is None or sb in seen:
                        old_row = row
                        break
                    seen.add(cur_id)
                    cur_id = sb
                old = _row_to_record(old_row)
                conflict = expected_version is not None and expected_version != old.version
                new_row = await self._revise_tx(
                    con,
                    old,
                    content=content,
                    actor_client_id=actor_client_id,
                )
                new_id = new_row["id"]
                loser = None
                if conflict:
                    # Last-writer-wins still applied above; preserve the
                    # overwritten content as a new pending inbox record so
                    # nothing is silently dropped (US7). E2EE: the loser's
                    # encryption rides along (ciphertext-blind copy).
                    vec = None if old.encrypted else await self.embedder.aembed_one(old.content)
                    loser_row = await con.fetchrow(
                        f"""
                        INSERT INTO memories
                            (content, type, scope_tags, confidence, source,
                             status, embedding, encrypted, key_id)
                        VALUES ($1, $2, $3, $4, $5, 'pending', $6::vector, $7, $8)
                        {_PG_RETURNING}
                        """,
                        old.content,
                        old.type,
                        old.scope_tags,
                        old.confidence,
                        _json(
                            {
                                "conflict_of": str(new_id),
                                "client_id": (str(actor_client_id) if actor_client_id else None),
                            }
                        ),
                        self._vec_literal(vec) if vec is not None else None,
                        old.encrypted,
                        old.key_id,
                    )
                    await con.execute(
                        "INSERT INTO change_log (memory_id, op, actor_client_id)"
                        " VALUES ($1, 'create', $2)",
                        loser_row["id"],
                        actor_client_id,
                    )
                    loser = _row_to_record(loser_row)
        return _row_to_record(new_row), loser

    async def delete(self, memory_id, *, actor_client_id=None):
        async with self.pool.acquire() as con:
            async with con.transaction():
                row = await con.fetchrow(
                    "DELETE FROM memories WHERE id = $1 RETURNING scope_tags", memory_id
                )
                if row is None:
                    return False
                await con.execute(
                    "INSERT INTO change_log (memory_id, op, actor_client_id, scope_tags)"
                    " VALUES ($1, 'delete', $2, $3)",
                    memory_id,
                    actor_client_id,
                    list(row["scope_tags"]),
                )
        return True

    async def log_change(self, *, memory_id, op, actor_client_id=None):
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                "INSERT INTO change_log (memory_id, op, actor_client_id)"
                " VALUES ($1, $2, $3)"
                " RETURNING seq, memory_id, op, actor_client_id, at",
                memory_id,
                op,
                actor_client_id,
            )
        return {
            "seq": row["seq"],
            "memory_id": row["memory_id"],
            "op": row["op"],
            "actor_client_id": row["actor_client_id"],
            "at": row["at"],
        }

    async def get_changes(self, since, limit):
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT seq, memory_id, op, actor_client_id, at, scope_tags"
                " FROM change_log WHERE seq > $1"
                " ORDER BY seq ASC LIMIT $2",
                since,
                limit,
            )
            head = await con.fetchval("SELECT COALESCE(MAX(seq), 0) FROM change_log")
        changes = [
            {
                "seq": r["seq"],
                "memory_id": r["memory_id"],
                "op": r["op"],
                "actor_client_id": r["actor_client_id"],
                "at": r["at"],
                "scope_tags": list(r["scope_tags"]) if r["scope_tags"] is not None else None,
            }
            for r in rows
        ]
        return changes, int(head)

    async def upsert_cursor(self, adapter_id, cursor):
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                """
                INSERT INTO sync_cursors (adapter_id, cursor, last_seen)
                VALUES ($1, $2, now())
                ON CONFLICT (adapter_id)
                DO UPDATE SET cursor = EXCLUDED.cursor, last_seen = now()
                RETURNING adapter_id, cursor, last_seen
                """,
                adapter_id,
                int(cursor),
            )
        return {
            "adapter_id": row["adapter_id"],
            "cursor": int(row["cursor"]),
            "last_seen": row["last_seen"],
        }

    async def list_cursors(self):
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT adapter_id, cursor, last_seen FROM sync_cursors ORDER BY adapter_id"
            )
        return [
            {
                "adapter_id": r["adapter_id"],
                "cursor": int(r["cursor"]),
                "last_seen": r["last_seen"],
            }
            for r in rows
        ]

    async def audit(self, *, action, memory_ids=(), client_id=None, scopes_used=(), meta=None):
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                """
                INSERT INTO audit_log (client_id, action, memory_ids, scopes_used, meta)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, client_id, action, memory_ids, scopes_used, meta, created_at
                """,
                client_id,
                action,
                list(memory_ids),
                list(scopes_used),
                _json(meta or {}),
            )
        return AuditRecord(
            id=row["id"],
            client_id=row["client_id"],
            action=row["action"],
            memory_ids=list(row["memory_ids"]),
            scopes_used=list(row["scopes_used"]),
            meta=json_object(row["meta"]),
            created_at=row["created_at"],
        )

    async def audit_list(
        self,
        limit: int = 100,
        client_id: UUID | None = None,
        since: datetime | None = None,
    ):
        clauses, args = [], []
        if client_id is not None:
            clauses.append(f"client_id = ${len(args) + 1}")
            args.append(client_id)
        if since is not None:
            clauses.append(f"created_at >= ${len(args) + 1}")
            args.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT id, client_id, action, memory_ids, scopes_used, meta, created_at"
                f" FROM audit_log {where} ORDER BY created_at DESC LIMIT ${len(args) + 1}",
                *args,
                limit,
            )
        return [
            AuditRecord(
                id=r["id"],
                client_id=r["client_id"],
                action=r["action"],
                memory_ids=list(r["memory_ids"]),
                scopes_used=list(r["scopes_used"]),
                meta=json_object(r["meta"]),
                created_at=r["created_at"],
            )
            for r in rows
        ]


def _json(obj: dict) -> str:
    import json

    return json.dumps(obj)
