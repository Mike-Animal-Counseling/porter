"""Provenance, contradiction detection, semantic merge, conflict resolve.

Uses InMemoryMemoryRepository + HashEmbedder. HashEmbedder is coarse
(token n-gram overlap), so contradiction tests pin an explicit low
threshold; the 0.75 default in detect_contradictions targets production
embeddings (BGE-M3), where "use tabs" vs "use spaces" scores ~0.9+.
"""
import asyncio
from uuid import UUID, uuid4

import pytest

from porter.conflicts import (
    PROVENANCE_KEY,
    append_provenance,
    detect_contradictions,
    get_provenance,
    resolution_merged_into,
    resolution_superseded_by,
    resolve_conflict,
    semantic_merge,
)
from porter.errors import InvalidInput, NotFound
from porter.repositories import InMemoryMemoryRepository


def _run(coro):
    return asyncio.run(coro)


async def _mk_active(repo, content, **kw):
    rec, _dups = await repo.create_active(
        content=content,
        type=kw.get("type", "fact"),
        scope_tags=kw.get("scope_tags", ["proj"]),
        confidence=kw.get("confidence", 0.9),
        source=kw.get("source", {"harness": "test"}),
    )
    return rec


# ------------------------------------------------------------- provenance


def test_provenance_blank_for_plain_record(repo: InMemoryMemoryRepository):
    async def go():
        rec = await _mk_active(repo, "plain memory")
        return get_provenance(rec)

    prov = _run(go())
    assert prov == {"origin": None, "merges": [], "conflicts": []}


def test_append_provenance_roundtrip_and_pure(repo: InMemoryMemoryRepository):
    src = {"harness": "claude-code"}
    out = append_provenance(
        src, {"kind": "origin", "harness": "claude-code", "actor": "u1"}
    )
    # input untouched (pure function)
    assert src == {"harness": "claude-code"}
    assert PROVENANCE_KEY not in src

    out = append_provenance(
        out, {"kind": "merge", "from_id": uuid4(), "strategy": "newest_wins"}
    )
    out = append_provenance(
        out,
        {
            "kind": "conflict",
            "with_id": uuid4(),
            "resolution": "superseded_by:x",
        },
    )
    prov = out[PROVENANCE_KEY]
    assert prov["origin"]["harness"] == "claude-code"
    assert prov["origin"]["actor"] == "u1"
    assert prov["origin"]["at"]
    assert len(prov["merges"]) == 1
    assert prov["merges"][0]["strategy"] == "newest_wins"
    assert prov["merges"][0]["at"]
    assert len(prov["conflicts"]) == 1
    assert prov["conflicts"][0]["resolution"] == "superseded_by:x"


def test_append_provenance_rejects_bad_kind():
    with pytest.raises(InvalidInput):
        append_provenance({}, {"kind": "bogus"})
    with pytest.raises(InvalidInput):
        append_provenance({}, {"kind": "merge"})  # missing from_id
    with pytest.raises(InvalidInput):
        append_provenance({}, {"kind": "conflict"})  # missing with_id


def test_get_provenance_reads_record_source(repo: InMemoryMemoryRepository):
    async def go():
        src = append_provenance(
            {"harness": "codex"},
            {"kind": "origin", "harness": "codex", "actor": "u2"},
        )
        rec = await _mk_active(repo, "with provenance", source=src)
        fetched = await repo.get(rec.id)
        return get_provenance(fetched)

    prov = _run(go())
    assert prov["origin"]["harness"] == "codex"


# ------------------------------------------------- contradiction detection


def test_detect_antonym_swap_tabs_spaces(repo: InMemoryMemoryRepository):
    async def go():
        a = await _mk_active(repo, "项目使用 tabs 缩进")
        b = await _mk_active(repo, "项目使用 spaces 缩进")
        hits = await detect_contradictions(repo, a.id, threshold=0.7)
        return b.id, hits

    b_id, hits = _run(go())
    assert len(hits) == 1
    assert hits[0]["id"] == b_id
    assert "antonym_swap" in hits[0]["signals"]
    assert hits[0]["similarity"] >= 0.7
    assert "spaces" in hits[0]["excerpt"]


def test_detect_negation_flip(repo: InMemoryMemoryRepository):
    async def go():
        a = await _mk_active(repo, "用tabs")
        await _mk_active(repo, "不要用tabs")
        return await detect_contradictions(repo, a.id, threshold=0.8)

    hits = _run(go())
    assert len(hits) == 1
    assert "negation_flip" in hits[0]["signals"]


def test_detect_numeric_conflict(repo: InMemoryMemoryRepository):
    async def go():
        a = await _mk_active(repo, "项目要求 Python 3.11")
        await _mk_active(repo, "项目要求 Python 3.12")
        return await detect_contradictions(repo, a.id, threshold=0.85)

    hits = _run(go())
    assert len(hits) == 1
    assert "numeric_conflict" in hits[0]["signals"]


def test_detect_status_reversal(repo: InMemoryMemoryRepository):
    async def go():
        a = await _mk_active(repo, "已迁移到新集群")
        await _mk_active(repo, "未迁移到新集群")
        return await detect_contradictions(repo, a.id, threshold=0.8)

    hits = _run(go())
    assert len(hits) == 1
    assert "status_reversal" in hits[0]["signals"]


def test_detect_ignores_self_and_unrelated(repo: InMemoryMemoryRepository):
    async def go():
        a = await _mk_active(repo, "项目使用 tabs 缩进")
        await _mk_active(repo, "今天天气不错")
        # agreeing memory: similar but no contradiction signal
        await _mk_active(repo, "项目使用 tabs 缩进，保持一致")
        return await detect_contradictions(repo, a.id, threshold=0.5)

    hits = _run(go())
    assert hits == []


def test_detect_missing_memory_returns_empty(repo: InMemoryMemoryRepository):
    assert _run(detect_contradictions(repo, uuid4())) == []


# ------------------------------------------------------------------- merge


def test_semantic_merge_newest_wins_per_aspect(repo: InMemoryMemoryRepository):
    async def go():
        a = await _mk_active(
            repo, "old decision", scope_tags=["a"], confidence=0.5
        )
        b = await _mk_active(
            repo, "new decision", scope_tags=["b"], confidence=0.9
        )
        res = await semantic_merge(repo, [a.id, b.id])
        new = await repo.get(res["id"])
        old_a = await repo.get(a.id)
        old_b = await repo.get(b.id)
        return res, new, old_a, old_b

    res, new, old_a, old_b = _run(go())
    # default strategy: content+type from newest, tags unioned, conf max
    assert new.content == "new decision"
    assert sorted(new.scope_tags) == ["a", "b"]
    assert new.confidence == 0.9
    assert new.status == "active"
    assert res["strategy"] == "newest_wins_per_aspect"
    # sources archived with lineage back to the new record
    for old in (old_a, old_b):
        assert old.status == "archived"
        assert old.superseded_by == new.id
    # new record's provenance lists every source with the strategy
    prov = get_provenance(new)
    from_ids = {m["from_id"] for m in prov["merges"]}
    assert from_ids == {str(old_a.id), str(old_b.id)}
    assert all(m["strategy"] == "newest_wins_per_aspect" for m in prov["merges"])
    # archived sources carry a merged_into conflict entry
    for old in (old_a, old_b):
        conf = get_provenance(old)["conflicts"]
        assert len(conf) == 1
        assert conf[0]["resolution"] == resolution_merged_into(new.id)
        assert conf[0]["with_id"] == str(new.id)


def test_semantic_merge_newest_wins(repo: InMemoryMemoryRepository):
    async def go():
        a = await _mk_active(
            repo, "old", scope_tags=["a"], confidence=0.99, type="preference"
        )
        b = await _mk_active(
            repo, "new", scope_tags=["b"], confidence=0.5, type="fact"
        )
        res = await semantic_merge(repo, [a.id, b.id], strategy="newest_wins")
        return res, await repo.get(res["id"])

    res, new = _run(go())
    # whole newest record wins: tags/confidence NOT merged
    assert new.content == "new"
    assert new.scope_tags == ["b"]
    assert new.confidence == 0.5
    assert new.type == "fact"


def test_semantic_merge_manual_pick(repo: InMemoryMemoryRepository):
    async def go():
        a = await _mk_active(repo, "tabs forever", scope_tags=["x"])
        b = await _mk_active(repo, "spaces forever", scope_tags=["y"])
        res = await semantic_merge(
            repo, [a.id, b.id], strategy="manual_pick",
            content="use tabs for Go, spaces for Python",
        )
        return res, await repo.get(res["id"])

    res, new = _run(go())
    assert new.content == "use tabs for Go, spaces for Python"
    assert sorted(new.scope_tags) == ["x", "y"]


def test_semantic_merge_rejects_bad_input(repo: InMemoryMemoryRepository):
    async def go():
        a = await _mk_active(repo, "one")
        b = await _mk_active(repo, "two")
        with pytest.raises(InvalidInput):
            await semantic_merge(repo, [a.id])  # fewer than two
        with pytest.raises(InvalidInput):
            await semantic_merge(repo, [a.id, b.id], strategy="nope")
        with pytest.raises(InvalidInput):
            await semantic_merge(repo, [a.id, b.id], strategy="manual_pick")
        with pytest.raises(NotFound):
            await semantic_merge(repo, [a.id, uuid4()])  # missing

    _run(go())


# ------------------------------------------------------------------ resolve


def test_resolve_conflict_archives_losers(repo: InMemoryMemoryRepository):
    async def go():
        winner = await _mk_active(repo, "项目使用 tabs 缩进")
        loser1 = await _mk_active(repo, "项目使用 spaces 缩进")
        loser2 = await _mk_active(repo, "不要用tabs")
        res = await resolve_conflict(
            repo,
            winner.id,
            [loser1.id, loser2.id],
            resolution=resolution_superseded_by(winner.id),
        )
        return res, winner.id, loser1.id, loser2.id

    res, wid, l1, l2 = _run(go())
    assert res["winner_id"] == wid
    assert set(res["archived_ids"]) == {l1, l2}
    assert res["skipped_ids"] == []
    assert res["resolution"] == f"superseded_by:{wid}"

    async def check():
        w = await repo.get(wid)
        a1 = await repo.get(l1)
        a2 = await repo.get(l2)
        return w, a1, a2

    w, a1, a2 = _run(check())
    assert w.status == "active"  # winner untouched
    assert w.superseded_by is None
    for arch in (a1, a2):
        assert arch.status == "archived"
        assert arch.superseded_by == wid
        conf = get_provenance(arch)["conflicts"]
        assert len(conf) == 1
        assert conf[0]["with_id"] == str(wid)
        assert conf[0]["resolution"] == f"superseded_by:{wid}"


def test_resolve_conflict_idempotent(repo: InMemoryMemoryRepository):
    async def go():
        winner = await _mk_active(repo, "keep me")
        loser = await _mk_active(repo, "drop me")
        r1 = await resolve_conflict(
            repo, winner.id, [loser.id],
            resolution=resolution_superseded_by(winner.id),
        )
        r2 = await resolve_conflict(
            repo, winner.id, [loser.id, uuid4()],
            resolution=resolution_superseded_by(winner.id),
        )
        return r1, r2

    r1, r2 = _run(go())
    assert len(r1["archived_ids"]) == 1
    assert r2["archived_ids"] == []
    assert len(r2["skipped_ids"]) == 2  # already-archived + missing


def test_resolve_conflict_rejects_missing_winner(repo: InMemoryMemoryRepository):
    async def go():
        loser = await _mk_active(repo, "drop me")
        with pytest.raises(NotFound):
            await resolve_conflict(
                repo, uuid4(), [loser.id], resolution="x"
            )

    _run(go())


def test_merge_then_detect_chain(repo: InMemoryMemoryRepository):
    """End to end: contradicting pair -> merge -> lineage walkable."""

    async def go():
        a = await _mk_active(repo, "项目使用 tabs 缩进")
        b = await _mk_active(repo, "项目使用 spaces 缩进")
        hits = await detect_contradictions(repo, a.id, threshold=0.7)
        assert hits and "antonym_swap" in hits[0]["signals"]
        res = await semantic_merge(
            repo, [a.id, b.id], strategy="manual_pick",
            content="项目使用 tabs 缩进（团队决议 2026-10）",
        )
        new = await repo.get(res["id"])
        return new

    new = _run(go())
    prov = get_provenance(new)
    assert len(prov["merges"]) == 2
    # every merge entry points at a real archived source
    async def check():
        return [await repo.get(UUID(m["from_id"])) for m in prov["merges"]]

    sources = _run(check())
    assert all(s.status == "archived" for s in sources)
    assert all(s.superseded_by == new.id for s in sources)
