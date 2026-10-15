"""Memory lifecycle engine: half-life tiers, decay formula, retrieval
boost, background sweeper, contradiction flagging.

All tests use InMemoryMemoryRepository + HashEmbedder (no model needed).
Long-idle records are built by backdating ``updated_at`` /
``last_retrieved_at`` directly on the stored objects.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from porter.embeddings import HashEmbedder
from porter.lifecycle import (
    CONTRADICTION_PENALTY,
    DEFAULT_HALF_LIFE_DAYS,
    HALF_LIVES,
    RETRIEVAL_BOOST,
    effective_confidence,
    flag_stale_on_contradiction,
    half_life_days,
    record_retrieval,
    sweep,
)
from porter.repositories import InMemoryMemoryRepository, MemoryRecord
from porter.triage import ARCHIVE_CONFIDENCE


def _repo():
    return InMemoryMemoryRepository(HashEmbedder())


def _run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc)


def _bare_record(**kw):
    """Minimal MemoryRecord for pure half-life tier tests (no repo)."""
    base = dict(
        id=uuid4(),
        content="c",
        type="fact",
        scope_tags=[],
        source={},
        confidence=0.9,
        status="active",
        embedding=None,
        created_at=_now(),
        updated_at=_now(),
    )
    base.update(kw)
    return MemoryRecord(**base)


async def _active(repo, content="c", **kw):
    kw.setdefault("type", "fact")
    kw.setdefault("scope_tags", [])
    kw.setdefault("confidence", 0.9)
    kw.setdefault("source", {"via": "test"})
    rec, _ = await repo.create_active(content=content, **kw)
    return rec


def _backdate(rec, days):
    past = _now() - timedelta(days=days)
    rec.updated_at = past
    rec.last_retrieved_at = past
    return rec


# ------------------------------------------------------- half-life tiers


def test_half_life_tiers():
    assert HALF_LIVES[("project", "task_state")] == 14.0
    assert HALF_LIVES[("project", "convention")] == 180.0
    assert HALF_LIVES[("project", "decision")] == 180.0
    assert HALF_LIVES[("personal", "preference")] == 365.0

    # project task state: short half-life via kind tag
    r = _bare_record(scope_tags=["project:myapp", "task"])
    assert half_life_days(r) == 14.0
    # project conventions / decisions: long
    assert half_life_days(_bare_record(scope_tags=["project:myapp", "convention"])) == 180.0
    assert half_life_days(_bare_record(scope_tags=["project:myapp", "decision"])) == 180.0
    assert half_life_days(_bare_record(scope_tags=["Project:MyApp", "ADR"])) == 180.0
    # personal preference: very long
    assert half_life_days(_bare_record(type="preference")) == 365.0
    assert half_life_days(_bare_record(type="personal")) == 365.0
    # personal fact is a defined tier (180d); the 90d default is the
    # safety net for (domain, kind) pairs added in the future.
    assert half_life_days(_bare_record()) == 180.0
    assert HALF_LIVES.get(("personal", "future_kind"),
                          DEFAULT_HALF_LIFE_DAYS) == 90.0
    assert DEFAULT_HALF_LIFE_DAYS == 90.0


def test_half_life_decay_formula():
    # Exactly one half-life of idleness halves the effective confidence.
    rec = _bare_record(
        confidence=0.8,
        type="fact",
        scope_tags=["project:myapp", "task"],  # half-life 14d
    )
    _backdate(rec, 14)
    assert effective_confidence(rec) == pytest.approx(0.4)
    # Two half-lives -> quarter.
    _backdate(rec, 28)
    assert effective_confidence(rec) == pytest.approx(0.2)
    # No idleness -> stored confidence (up to float noise).
    fresh = _bare_record(confidence=0.8, scope_tags=["project:myapp", "task"])
    assert effective_confidence(fresh) == pytest.approx(0.8)


def test_effective_confidence_skips_encrypted_and_pinned():
    rec = _bare_record(confidence=0.8, encrypted=True)
    _backdate(rec, 365)
    assert effective_confidence(rec) == 0.8
    pinned = _bare_record(confidence=0.8, pinned=True)
    _backdate(pinned, 365)
    assert effective_confidence(pinned) == 0.8


# ------------------------------------------------------- retrieval boost


def test_retrieval_boost_and_idle_reset():
    async def _run2():
        repo = _repo()
        rec = await _active(repo, "editor theme", confidence=0.8)
        _backdate(rec, 10)
        before = rec.last_retrieved_at
        await record_retrieval(repo, [rec.id])
        # 0.8 + 0.05 * (1 - 0.8) = 0.81
        assert rec.confidence == pytest.approx(0.8 + RETRIEVAL_BOOST * 0.2)
        assert rec.last_retrieved_at > before
    _run(_run2())


def test_retrieval_boost_capped_at_one():
    async def _run2():
        repo = _repo()
        rec = await _active(repo, "pinned fact", confidence=1.0)
        await record_retrieval(repo, [rec.id])
        assert rec.confidence == 1.0
        # near-1.0 converges but never exceeds 1.0
        rec2 = await _active(repo, "almost", confidence=0.999)
        await record_retrieval(repo, [rec2.id])
        assert rec2.confidence <= 1.0
        assert rec2.confidence > 0.999
    _run(_run2())


def test_retrieval_skips_missing_and_inactive():
    async def _run2():
        repo = _repo()
        rec = await _active(repo, "gone soon", confidence=0.5)
        await repo.archive(rec.id)
        # unknown id + archived id: no crash, no-op
        await record_retrieval(repo, [uuid4(), rec.id])
    _run(_run2())


# ------------------------------------------------------- sweeper


def test_sweeper_archives_decayed_and_is_idempotent():
    async def _run2():
        repo = _repo()
        # Decayed: project task_state (14d half-life), idle 60d ->
        # 0.5 * 0.5**(60/14) ~= 0.026 < 0.2 -> archive.
        old = await _active(
            repo, "old wip", confidence=0.5,
            scope_tags=["project:myapp", "task"],
        )
        _backdate(old, 60)
        # Fresh: same kind, idle 1d -> stays.
        fresh = await _active(
            repo, "fresh wip", confidence=0.9,
            scope_tags=["project:myapp", "task"],
        )
        # Pinned + ancient: never archived.
        pinned = await _active(repo, "pinned lore", confidence=0.5)
        pinned.pinned = True
        _backdate(pinned, 600)

        res = await sweep(repo)
        assert res["scanned"] == 3
        assert res["archived"] == 1
        assert res["archived_ids"] == [old.id]
        assert (await repo.get(old.id)).status == "archived"
        assert (await repo.get(fresh.id)).status == "active"
        assert (await repo.get(pinned.id)).status == "active"

        audits = await repo.audit_list()
        entry = next(
            a for a in audits
            if a.action == "lifecycle_archive" and old.id in a.memory_ids
        )
        assert entry.meta["reason"] == "half_life_decay"
        assert entry.meta["half_life_days"] == 14.0
        assert entry.meta["effective_confidence"] < ARCHIVE_CONFIDENCE

        # Second run: idempotent, archives nothing new.
        res2 = await sweep(repo)
        assert res2["archived"] == 0
        assert res2["archived_ids"] == []
        assert res2["scanned"] == 2
    _run(_run2())


def test_sweeper_pagination():
    async def _run2():
        repo = _repo()
        doomed = []
        for i in range(5):
            rec = await _active(
                repo, f"stale {i}", confidence=0.5,
                scope_tags=["project:myapp", "task"],
            )
            _backdate(rec, 60)
            doomed.append(rec.id)
        keeper = await _active(repo, "keeper", confidence=0.95)
        res = await sweep(repo, batch_size=2)
        assert res["scanned"] == 6
        assert res["archived"] == 5
        assert set(res["archived_ids"]) == set(doomed)
        assert (await repo.get(keeper.id)).status == "active"
    _run(_run2())


# ------------------------------------------------------- contradiction flag


def test_contradiction_flag_penalizes_and_audits():
    async def _run2():
        repo = _repo()
        rec = await _active(repo, "tabs not spaces", confidence=0.9)
        other = await _active(repo, "spaces not tabs", confidence=0.9)
        await flag_stale_on_contradiction(repo, rec.id, other.id)
        assert rec.confidence == 0.9 * CONTRADICTION_PENALTY
        audits = await repo.audit_list()
        entry = next(a for a in audits if a.action == "contradiction_flag")
        assert set(entry.memory_ids) == {rec.id, other.id}
        assert entry.meta["old_confidence"] == 0.9
        assert entry.meta["new_confidence"] == round(0.9 * 0.7, 4)
    _run(_run2())


def test_contradiction_flag_noop_on_missing_or_archived():
    async def _run2():
        repo = _repo()
        other = await _active(repo, "other", confidence=0.9)
        await flag_stale_on_contradiction(repo, uuid4(), other.id)  # no crash
        rec = await _active(repo, "archived one", confidence=0.9)
        await repo.archive(rec.id)
        await flag_stale_on_contradiction(repo, rec.id, other.id)
        assert rec.confidence == 0.9  # untouched
        audits = await repo.audit_list()
        assert not [a for a in audits if a.action == "contradiction_flag"]
    _run(_run2())
