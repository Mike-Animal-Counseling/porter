"""LLM Wiki layer: entity-graph organization on top of Porter memories.

Prototype (flag-gated, default OFF via PORTER_WIKI_ENABLED=1).

When a memory is approved, an LLM extractor (porter/llm.py) pulls out
entities/topics/relations. They are stored in a lightweight graph:

    wiki_entities          (id, name UNIQUE, kind, created_at)
    wiki_memory_entities   (memory_id, entity_id)      -- mention edges
    wiki_edges             (src, dst, relation, memory_id) -- co-occurrence

Retrieval boost: hybrid_search() runs the normal embedding recall first,
then expands one hop over shared entities (entity -> other memories that
mention it), merges and re-ranks. Pure additive: with the flag off, or
with no wiki service attached, search behavior is byte-identical to v1.

Security notes (prototype inherits the repo's guarantees):
- Encrypted (E2EE) memories are NEVER indexed: the server is
  ciphertext-blind, so there is nothing meaningful to extract.
- Graph expansion respects the same tag/active filters as the base search.
- The graph stores entity *names* derived from memory text; names are
  metadata, not secrets, but they are still only readable through the
  memory.read-scoped MCP tool below.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID, uuid4

from .llm import BaseLLMExtractor
from .repositories import MemoryRecord, ScoredMemory


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class WikiEntity:
    id: UUID
    name: str
    kind: str
    memories: list[UUID] = field(default_factory=list)  # memory ids mentioning it


@dataclass
class WikiNeighbor:
    entity: WikiEntity
    relation: str
    via_memory_id: UUID | None = None


# ---------------------------------------------------------------------------
# Store abstraction: the graph tables (migration 006_wiki.sql).
# ---------------------------------------------------------------------------


class WikiStore(ABC):
    """Persistence for the entity graph."""

    @abstractmethod
    async def upsert_entity(self, name: str, kind: str) -> UUID:
        """Insert the entity if missing (by name); return its id."""

    @abstractmethod
    async def link_memory(self, memory_id: UUID, entity_id: UUID) -> None:
        """Record that a memory mentions an entity (idempotent)."""

    @abstractmethod
    async def add_edge(
        self, src_id: UUID, dst_id: UUID, relation: str,
        memory_id: UUID | None,
    ) -> None:
        """Record a directed relation edge (deduped per memory)."""

    @abstractmethod
    async def memory_ids_for_entity(self, name: str) -> list[UUID]:
        """All memory ids mentioning the entity (exact name match)."""

    @abstractmethod
    async def entities_for_memory(self, memory_id: UUID) -> list[WikiEntity]:
        """Entities mentioned by one memory."""

    @abstractmethod
    async def neighbors(self, name: str) -> list[WikiNeighbor]:
        """1-hop neighbors of an entity via wiki_edges."""


class InMemoryWikiStore(WikiStore):
    """Test/dev store. Mirrors the SQL schema in dicts."""

    def __init__(self) -> None:
        self._entities: dict[str, WikiEntity] = {}          # name -> entity
        self._by_id: dict[UUID, WikiEntity] = {}
        self._mem_entities: dict[UUID, set[UUID]] = {}      # memory_id -> entity ids
        self._edges: set[tuple[UUID, UUID, str, str]] = set()  # (src,dst,rel,mem)

    async def upsert_entity(self, name: str, kind: str) -> UUID:
        ent = self._entities.get(name)
        if ent is None:
            ent = WikiEntity(id=uuid4(), name=name, kind=kind)
            self._entities[name] = ent
            self._by_id[ent.id] = ent
        return ent.id

    async def link_memory(self, memory_id: UUID, entity_id: UUID) -> None:
        self._mem_entities.setdefault(memory_id, set()).add(entity_id)
        ent = self._by_id.get(entity_id)
        if ent is not None and memory_id not in ent.memories:
            ent.memories.append(memory_id)

    async def add_edge(self, src_id, dst_id, relation, memory_id) -> None:
        self._edges.add((src_id, dst_id, relation, str(memory_id or "")))

    async def memory_ids_for_entity(self, name: str) -> list[UUID]:
        ent = self._entities.get(name)
        return list(ent.memories) if ent else []

    async def entities_for_memory(self, memory_id: UUID) -> list[WikiEntity]:
        ids = self._mem_entities.get(memory_id, set())
        return [self._by_id[i] for i in ids if i in self._by_id]

    async def neighbors(self, name: str) -> list[WikiNeighbor]:
        ent = self._entities.get(name)
        if ent is None:
            return []
        out: list[WikiNeighbor] = []
        for src, dst, rel, mem in self._edges:
            if src == ent.id and dst in self._by_id:
                out.append(WikiNeighbor(
                    entity=self._by_id[dst], relation=rel,
                    via_memory_id=UUID(mem) if mem else None,
                ))
        return out


class PostgresWikiStore(WikiStore):
    """Postgres backend (tables from migrations/006_wiki.sql)."""

    def __init__(self, pool) -> None:  # asyncpg pool; untyped to avoid import
        self._pool = pool

    async def upsert_entity(self, name: str, kind: str) -> UUID:
        async with self._pool.acquire() as con:
            row = await con.fetchrow(
                """
                INSERT INTO wiki_entities (name, kind)
                VALUES ($1, $2)
                ON CONFLICT (name) DO UPDATE SET kind = EXCLUDED.kind
                RETURNING id
                """,
                name, kind,
            )
            return row["id"]

    async def link_memory(self, memory_id: UUID, entity_id: UUID) -> None:
        async with self._pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO wiki_memory_entities (memory_id, entity_id)
                VALUES ($1, $2) ON CONFLICT DO NOTHING
                """,
                memory_id, entity_id,
            )

    async def add_edge(self, src_id, dst_id, relation, memory_id) -> None:
        async with self._pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO wiki_edges
                    (src_entity_id, dst_entity_id, relation, memory_id)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT DO NOTHING
                """,
                src_id, dst_id, relation, memory_id,
            )

    async def memory_ids_for_entity(self, name: str) -> list[UUID]:
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT wme.memory_id
                FROM wiki_memory_entities wme
                JOIN wiki_entities we ON we.id = wme.entity_id
                WHERE we.name = $1
                """,
                name,
            )
            return [r["memory_id"] for r in rows]

    async def entities_for_memory(self, memory_id: UUID) -> list[WikiEntity]:
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT we.id, we.name, we.kind
                FROM wiki_memory_entities wme
                JOIN wiki_entities we ON we.id = wme.entity_id
                WHERE wme.memory_id = $1
                """,
                memory_id,
            )
            out = []
            for r in rows:
                mem_rows = await con.fetch(
                    "SELECT memory_id FROM wiki_memory_entities WHERE entity_id = $1",
                    r["id"],
                )
                out.append(WikiEntity(
                    id=r["id"], name=r["name"], kind=r["kind"],
                    memories=[m["memory_id"] for m in mem_rows],
                ))
            return out

    async def neighbors(self, name: str) -> list[WikiNeighbor]:
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT we2.id, we2.name, we2.kind, e.relation, e.memory_id
                FROM wiki_entities we1
                JOIN wiki_edges e ON e.src_entity_id = we1.id
                JOIN wiki_entities we2 ON we2.id = e.dst_entity_id
                WHERE we1.name = $1
                """,
                name,
            )
            out = []
            for r in rows:
                mem_rows = await con.fetch(
                    "SELECT memory_id FROM wiki_memory_entities WHERE entity_id = $1",
                    r["id"],
                )
                out.append(WikiNeighbor(
                    entity=WikiEntity(
                        id=r["id"], name=r["name"], kind=r["kind"],
                        memories=[m["memory_id"] for m in mem_rows],
                    ),
                    relation=r["relation"],
                    via_memory_id=r["memory_id"],
                ))
            return out


# ---------------------------------------------------------------------------
# Service: indexing hook + hybrid retrieval.
# ---------------------------------------------------------------------------


class WikiService:
    """Glue between the memory repository, the extractor, and the store.

    enabled=False (default) makes every method a documented no-op, so the
    flag-off path cannot change v1 behavior.
    """

    # Score floor for graph-expanded hits that the embedding recall missed.
    # Below a decent embedding hit (~0.5+) but above noise: expansion is a
    # recall net, not a precision claim.
    EXPANSION_SCORE = 0.45
    EXPANSION_BONUS_PER_ENTITY = 0.05

    def __init__(
        self,
        store: WikiStore,
        extractor: BaseLLMExtractor,
        *,
        enabled: bool = False,
    ) -> None:
        self.store = store
        self.extractor = extractor
        self.enabled = enabled

    async def on_approved(self, record: MemoryRecord) -> int:
        """Index a freshly approved memory. Returns entities linked.

        No-op when disabled. Encrypted memories are skipped (ciphertext-
        blind server: there is nothing meaningful to extract).
        """
        if not self.enabled or record.encrypted:
            return 0
        mentions = self.extractor.extract_entities(record.content or "")
        if not mentions:
            return 0
        name_to_id: dict[str, UUID] = {}
        for m in mentions:
            eid = await self.store.upsert_entity(m.name, m.kind)
            name_to_id[m.name] = eid
            await self.store.link_memory(record.id, eid)
        for m in mentions:
            src = name_to_id[m.name]
            for rel, target in m.relations:
                dst = name_to_id.get(target)
                if dst is not None and dst != src:
                    await self.store.add_edge(src, dst, rel, record.id)
        return len(mentions)

    async def hybrid_search(
        self,
        repo,  # MemoryRepository; untyped to avoid a circular import
        query: str,
        *,
        top_k: int = 5,
        tags=(),
        types=(),
        active_only: bool = True,
    ) -> list[ScoredMemory]:
        """Embedding recall, then 1-hop graph expansion, merged + ranked.

        1. base = repo.search(query, top_k*3) — the v1 embedding path.
        2. Extract entities from the *query*; for each, pull memory ids that
           mention it (1 hop: entity -> memories).
        3. Fetch those memories (respecting active/tags filters), score them
           EXPANSION_SCORE + bonus per shared entity, merge with base by
           max score, dedupe, truncate to top_k.
        """
        base = await repo.search(
            query=query, top_k=top_k * 3, tags=tags, types=types,
            active_only=active_only,
        )
        if not self.enabled:
            return base[:top_k]
        q_entities = {
            e.name for e in self.extractor.extract_entities(query)
        }
        if not q_entities:
            return base[:top_k]

        tag_set = set(tags)
        merged: dict[UUID, ScoredMemory] = {s.record.id: s for s in base}
        for name in q_entities:
            for mid in await self.store.memory_ids_for_entity(name):
                if mid in merged:
                    continue
                rec = await repo.get(mid)
                if rec is None:
                    continue
                if active_only and rec.status != "active":
                    continue
                if rec.encrypted:
                    continue  # E2EE: never surface via graph expansion
                if tag_set and not (tag_set & set(rec.scope_tags)):
                    continue
                shared = await self._shared_entity_count(mid, q_entities)
                merged[mid] = ScoredMemory(
                    rec,
                    self.EXPANSION_SCORE
                    + self.EXPANSION_BONUS_PER_ENTITY * shared,
                )
        ranked = sorted(merged.values(), key=lambda s: s.score, reverse=True)
        return ranked[:top_k]

    async def _shared_entity_count(
        self, memory_id: UUID, names: set[str]
    ) -> int:
        ents = await self.store.entities_for_memory(memory_id)
        have = {e.name for e in ents}
        return len(have & names)


def build_wiki_service(
    repo_kind: str,
    pool=None,
    *,
    enabled: bool = False,
    extractor_kind: str = "stub",
) -> WikiService:
    """Factory. repo_kind: "postgres" | "memory". The extractor defaults to
    the deterministic stub; pass extractor_kind="ollama" etc. once wired."""
    from .llm import build_extractor

    store: WikiStore = (
        PostgresWikiStore(pool) if repo_kind == "postgres" else InMemoryWikiStore()
    )
    return WikiService(
        store, build_extractor(extractor_kind), enabled=enabled,
    )
