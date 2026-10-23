"""LLM Wiki layer (prototype): entity extraction, graph store, hybrid
search, and the memory_graph MCP tool. All tests use the deterministic
StubLLMExtractor + HashEmbedder so they run without a model on the box."""

import asyncio
import json

import pytest

from porter.embeddings import HashEmbedder, build_embedder
from porter.llm import EntityMention, StubLLMExtractor, build_extractor
from porter.mcp_server import MCPDeps, build_mcp_server
from porter.repositories import InMemoryMemoryRepository, ScoredMemory
from porter.wiki import (
    InMemoryWikiStore,
    WikiService,
    build_wiki_service,
)


def _svc(**kw):
    kw.setdefault("enabled", True)
    return WikiService(InMemoryWikiStore(), StubLLMExtractor(), **kw)


async def _approved_mem(repo, content, tags=("general",), **kw):
    rec, _ = await repo.create_pending(
        content=content,
        type="preference",
        scope_tags=list(tags),
        confidence=0.9,
        source={"via": "test"},
        **kw,
    )
    return await repo.approve(rec.id)


# ---------------------------------------------------------------- extractor


def test_stub_extractor_is_deterministic():
    ext = StubLLMExtractor()
    text = "User prefers Tailwind CSS for styling, lives in San Jose"
    a = [e.name for e in ext.extract_entities(text)]
    b = [e.name for e in ext.extract_entities(text)]
    assert a == b and len(a) > 0


def test_stub_extractor_finds_bilingual_entities():
    ext = StubLLMExtractor()
    names = {e.name for e in ext.extract_entities("用户偏好用 Tailwind 写样式")}
    assert "tailwind" in names
    assert any("\u4e00" <= ch <= "\u9fff" for n in names for ch in n)


def test_stub_extractor_builds_cooccurrence_relations():
    ext = StubLLMExtractor()
    mentions = ext.extract_entities("Tailwind and React for frontend work")
    by_name = {m.name: m for m in mentions}
    assert "tailwind" in by_name and "react" in by_name
    targets = {t for _, t in by_name["tailwind"].relations}
    assert "react" in targets


def test_build_extractor_unknown_kind_raises():
    with pytest.raises(ValueError, match="unknown LLM extractor kind"):
        build_extractor("gpt-99")


def test_entity_mention_defaults():
    m = EntityMention(name="x")
    assert m.kind == "topic" and m.relations == []


# ---------------------------------------------------------------- indexing


def test_on_approved_indexes_entities():
    async def _run():
        repo = InMemoryMemoryRepository(HashEmbedder())
        svc = _svc()
        rec = await _approved_mem(repo, "User prefers Tailwind CSS for styling")
        n = await svc.on_approved(rec)
        assert n > 0
        ents = await svc.store.entities_for_memory(rec.id)
        assert "tailwind" in {e.name for e in ents}
        mids = await svc.store.memory_ids_for_entity("tailwind")
        assert rec.id in mids

    asyncio.run(_run())


def test_on_approved_noop_when_disabled():
    async def _run():
        repo = InMemoryMemoryRepository(HashEmbedder())
        svc = _svc(enabled=False)
        rec = await _approved_mem(repo, "User prefers Tailwind CSS")
        assert await svc.on_approved(rec) == 0
        assert await svc.store.entities_for_memory(rec.id) == []

    asyncio.run(_run())


def test_on_approved_skips_encrypted():
    """E2EE: the server is ciphertext-blind; never index ciphertext."""

    async def _run():
        repo = InMemoryMemoryRepository(HashEmbedder())
        svc = _svc()
        rec = await _approved_mem(repo, "aGVsbG8gd29ybGQ=", encrypted=True, key_id="k1")
        assert rec.encrypted
        assert await svc.on_approved(rec) == 0
        assert await svc.store.entities_for_memory(rec.id) == []

    asyncio.run(_run())


def test_neighbors_follow_cooccurrence_edges():
    async def _run():
        repo = InMemoryMemoryRepository(HashEmbedder())
        svc = _svc()
        rec = await _approved_mem(repo, "Tailwind and React for frontend work")
        await svc.on_approved(rec)
        neigh = await svc.store.neighbors("tailwind")
        assert "react" in {n.entity.name for n in neigh}
        assert all(n.relation == "co_occurs" for n in neigh)

    asyncio.run(_run())


# ---------------------------------------------------------------- hybrid search


class _NarrowRepo:
    """Simulates an embedding recall that MISSED mem_b (the feature's
    raison d'etre): search only ever returns mem_a."""

    def __init__(self, mem_a, mem_b):
        self._mems = {mem_a.id: mem_a, mem_b.id: mem_b}
        self._a = mem_a

    async def search(self, *, query, top_k=5, tags=(), types=(), active_only=True):
        return [ScoredMemory(self._a, 0.9)]

    async def get(self, mid):
        return self._mems.get(mid)


def test_hybrid_search_expands_missed_memory_via_shared_entity():
    async def _run():
        repo = InMemoryMemoryRepository(HashEmbedder())
        svc = _svc()
        mem_a = await _approved_mem(repo, "User prefers Tailwind CSS")
        mem_b = await _approved_mem(repo, "Team standardized on Tailwind")
        await svc.on_approved(mem_a)
        await svc.on_approved(mem_b)

        narrow = _NarrowRepo(mem_a, mem_b)
        out = await svc.hybrid_search(narrow, "tailwind adoption", top_k=5)
        ids = [s.record.id for s in out]
        assert mem_a.id in ids and mem_b.id in ids
        by_id = {s.record.id: s for s in out}
        # base hit keeps its embedding score; expansion gets the floor score
        assert by_id[mem_a.id].score == pytest.approx(0.9)
        assert by_id[mem_b.id].score >= svc.EXPANSION_SCORE

    asyncio.run(_run())


def test_hybrid_search_disabled_matches_base():
    async def _run():
        repo = InMemoryMemoryRepository(HashEmbedder())
        svc = _svc(enabled=False)
        mem_a = await _approved_mem(repo, "User prefers Tailwind CSS")
        out = await svc.hybrid_search(repo, "tailwind", top_k=5)
        base = await repo.search(query="tailwind", top_k=5)
        assert [s.record.id for s in out] == [s.record.id for s in base]
        assert mem_a.id in [s.record.id for s in out]

    asyncio.run(_run())


def test_hybrid_search_respects_tag_filter_on_expansion():
    async def _run():
        repo = InMemoryMemoryRepository(HashEmbedder())
        svc = _svc()
        mem_a = await _approved_mem(repo, "User prefers Tailwind CSS", tags=("coding_style",))
        mem_b = await _approved_mem(repo, "Team standardized on Tailwind", tags=("work",))
        await svc.on_approved(mem_a)
        await svc.on_approved(mem_b)
        narrow = _NarrowRepo(mem_a, mem_b)
        out = await svc.hybrid_search(narrow, "tailwind adoption", top_k=5, tags=("coding_style",))
        ids = [s.record.id for s in out]
        assert mem_a.id in ids and mem_b.id not in ids

    asyncio.run(_run())


def test_hybrid_search_skips_pending_and_encrypted_on_expansion():
    async def _run():
        repo = InMemoryMemoryRepository(HashEmbedder())
        svc = _svc()
        mem_a = await _approved_mem(repo, "User prefers Tailwind CSS")
        # pending (never approved) but manually indexed — must not surface
        pend, _ = await repo.create_pending(
            content="Tailwind is great",
            type="fact",
            scope_tags=["general"],
            confidence=0.9,
            source={},
        )
        await svc.on_approved(pend)
        narrow = _NarrowRepo(mem_a, pend)
        out = await svc.hybrid_search(narrow, "tailwind", top_k=5)
        assert [s.record.id for s in out] == [mem_a.id]

    asyncio.run(_run())


def test_build_wiki_service_factory():
    svc = build_wiki_service("memory", enabled=True)
    assert isinstance(svc, WikiService) and svc.enabled
    assert isinstance(svc.store, InMemoryWikiStore)
    off = build_wiki_service("memory")
    assert not off.enabled


# ---------------------------------------------------------------- embedder registry


def test_qwen3_kind_registered_but_lazy():
    """qwen3 must be a KNOWN kind (no ValueError); the model import itself
    is lazy, so on a box without sentence-transformers this raises
    ImportError — the eval script treats that as SKIP, not failure."""
    try:
        build_embedder("qwen3")
    except ImportError:
        pass  # expected here: no torch/sentence-transformers on this VM
    except ValueError:
        pytest.fail("qwen3 kind not registered")
    with pytest.raises(ValueError, match="unknown embedder kind"):
        build_embedder("nope")


def test_bge_m3_alias_registered():
    try:
        build_embedder("bge-m3")
    except ImportError:
        pass
    except ValueError:
        pytest.fail("bge-m3 alias not registered")


def test_harrier_kind_registered_but_lazy():
    """harrier must be a KNOWN kind (no ValueError); the model import
    itself is lazy, so on a box without sentence-transformers this raises
    ImportError — the eval script treats that as SKIP, not failure."""
    try:
        build_embedder("harrier")
    except ImportError:
        pass  # expected here: no torch/sentence-transformers on this VM
    except ValueError:
        pytest.fail("harrier kind not registered")


def test_embeddinggemma_kind_registered_but_lazy():
    """Same lazy-registration contract as harrier/qwen3."""
    try:
        build_embedder("embeddinggemma")
    except ImportError:
        pass  # expected here: no torch/sentence-transformers on this VM
    except ValueError:
        pytest.fail("embeddinggemma kind not registered")


# ---------------------------------------------------------------- MCP: memory_graph


def _mcp_server_with_wiki(repo, wiki):
    from porter.app import create_app
    from porter.oauth_store import InMemoryOAuthStore

    app = create_app(repo, InMemoryOAuthStore(), auth_enabled=False, wiki_service=wiki)
    return build_mcp_server(
        MCPDeps(
            repo_fn=lambda: repo,
            oauth_store_fn=lambda: app.state.oauth_store,
            revocation_cache_fn=lambda: app.state.revocation_cache,
            rate_limiter_fn=lambda: app.state.rate_limiter,
            auth_enabled_fn=lambda: False,
            wiki_fn=lambda: wiki,
        )
    )


def test_mcp_memory_graph_entity_mode():
    async def _run():
        repo = InMemoryMemoryRepository(HashEmbedder())
        svc = _svc()
        rec = await _approved_mem(repo, "Tailwind and React for frontend work")
        await svc.on_approved(rec)
        server = _mcp_server_with_wiki(repo, svc)

        result = await server.call_tool("memory_graph", {"entity": "Tailwind"})
        assert not result.is_error
        payload = json.loads(result.content[0].text)
        assert payload["entity"] == "tailwind"  # normalized
        assert "react" in {n["name"] for n in payload["neighbors"]}
        assert str(rec.id) in {m["id"] for m in payload["memories"]}
        assert payload["untrusted_data"] is True

    asyncio.run(_run())


def test_mcp_memory_graph_memory_mode():
    async def _run():
        repo = InMemoryMemoryRepository(HashEmbedder())
        svc = _svc()
        rec = await _approved_mem(repo, "User prefers Tailwind CSS")
        await svc.on_approved(rec)
        server = _mcp_server_with_wiki(repo, svc)

        result = await server.call_tool("memory_graph", {"memory_id": str(rec.id)})
        assert not result.is_error
        payload = json.loads(result.content[0].text)
        assert "tailwind" in {e["name"] for e in payload["entities"]}

    asyncio.run(_run())


def test_mcp_memory_graph_requires_exactly_one_arg():
    from mcp.server.mcpserver.exceptions import ToolError

    async def _run():
        repo = InMemoryMemoryRepository(HashEmbedder())
        server = _mcp_server_with_wiki(repo, _svc())
        with pytest.raises(ToolError, match="exactly one of"):
            await server.call_tool("memory_graph", {})
        with pytest.raises(ToolError, match="exactly one of"):
            await server.call_tool("memory_graph", {"entity": "x", "memory_id": "y"})

    asyncio.run(_run())


def test_mcp_memory_graph_disabled_returns_clear_error():
    from mcp.server.mcpserver.exceptions import ToolError

    async def _run():
        repo = InMemoryMemoryRepository(HashEmbedder())
        # wiki service present but DISABLED: tool is registered (it must be
        # — postgres mode attaches the service after build_mcp_server runs)
        # but calls fail closed with wiki_disabled.
        server = _mcp_server_with_wiki(repo, _svc(enabled=False))
        tools = await server.list_tools()
        assert "memory_graph" in {t.name for t in tools}
        with pytest.raises(ToolError, match="wiki_disabled"):
            await server.call_tool("memory_graph", {"entity": "tailwind"})
        # wiki_boost with no wiki service is a silent no-op, not an error
        result = await server.call_tool("memory_search", {"query": "x", "wiki_boost": True})
        assert not result.is_error

    asyncio.run(_run())


def test_mcp_memory_search_wiki_boost_flag():
    async def _run():
        repo = InMemoryMemoryRepository(HashEmbedder())
        svc = _svc()
        await svc.on_approved(await _approved_mem(repo, "User prefers Tailwind CSS"))
        server = _mcp_server_with_wiki(repo, svc)
        result = await server.call_tool("memory_search", {"query": "tailwind", "wiki_boost": True})
        assert not result.is_error
        payload = json.loads(result.content[0].text)
        assert len(payload["results"]) >= 1

    asyncio.run(_run())
