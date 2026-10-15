"""Tests for porter/switch.py — seamless harness switching core.

Covers:
- handoff section grouping (tags -> type -> keyword heuristics)
- empty/unknown project handling
- active-only visibility (pending + archived memories excluded)
- encrypted memories ride along as ciphertext, never decrypted
- briefing rendering (human copy, encrypted placeholder, blank slate)
- SwitchEvent audit persistence (action="harness_switch")
"""
from __future__ import annotations

import asyncio
import base64

import pytest

from porter import switch
from porter.embeddings import HashEmbedder
from porter.errors import InvalidInput
from porter.repositories import InMemoryMemoryRepository
from porter.triage import project_tag


def _repo() -> InMemoryMemoryRepository:
    return InMemoryMemoryRepository(HashEmbedder())


async def _add(repo, content, *, type="fact", tags=(), encrypted=False,
               key_id=None):
    """Write + approve (active) so handoff visibility rules apply."""
    if encrypted:
        rec, _ = await repo.create_pending(
            content=content, type=type, scope_tags=list(tags),
            confidence=0.9, source={}, encrypted=True, key_id=key_id,
        )
        return await repo.approve(rec.id)
    rec, _ = await repo.create_active(
        content=content, type=type, scope_tags=list(tags),
        confidence=0.9, source={},
    )
    return rec


async def _add_pending(repo, content, *, tags=()):
    rec, _ = await repo.create_pending(
        content=content, type="fact", scope_tags=list(tags),
        confidence=0.9, source={},
    )
    return rec


# ------------------------------------------------------------ grouping
def test_handoff_groups_into_sections():
    async def run():
        repo = _repo()
        d = await _add(repo, "We decided to use Postgres", tags=[project_tag("demo"), "decision"])
        p = await _add(repo, "Don't run migrations on Friday", tags=[project_tag("demo"), "pitfall"])
        c = await _add(repo, "Use tabs for indentation", type="skill_ref", tags=[project_tag("demo")])
        w = await _add(repo, "Refactoring auth module", tags=[project_tag("demo")])
        me = await _add(repo, "User prefers dark mode", type="personal")
        other = await _add(repo, "Other project memory", tags=[project_tag("other"), "decision"])

        h = await switch.build_handoff(
            repo, project="demo", from_harness="claude-code",
            to_harness="cursor",
        )
        assert h["project"] == "demo"
        assert h["from_harness"] == "claude-code"
        assert h["to_harness"] == "cursor"
        assert "at" in h
        secs = h["sections"]
        assert {e["id"] for e in secs["decisions"]} == {str(d.id)}
        assert {e["id"] for e in secs["pitfalls"]} == {str(p.id)}
        assert {e["id"] for e in secs["conventions"]} == {str(c.id)}
        # untagged project memory falls into the in_progress catch-all
        assert {e["id"] for e in secs["in_progress"]} == {str(w.id)}
        assert {e["id"] for e in secs["personal"]} == {str(me.id)}
        # nothing from project "other" leaks in
        all_ids = {e["id"] for s in secs.values() for e in s}
        assert str(other.id) not in all_ids
        return h

    h = asyncio.run(run())
    # every entry is MemoryOut-like
    for entries in h["sections"].values():
        for e in entries:
            assert e["status"] == "active"
            assert e["untrusted_data"] is True
            assert "id" in e and "content" in e


def test_handoff_type_and_keyword_fallbacks():
    async def run():
        repo = _repo()
        ep = await _add(repo, "Tried X then Y", type="episode",
                        tags=[project_tag("demo")])
        kw = await _add(repo, "We decided the API shape yesterday",
                        tags=[project_tag("demo")])
        h = await switch.build_handoff(
            repo, project="demo", from_harness="a", to_harness="b",
        )
        return h, ep, kw

    h, ep, kw = asyncio.run(run())
    assert {e["id"] for e in h["sections"]["in_progress"]} == {str(ep.id)}
    assert {e["id"] for e in h["sections"]["decisions"]} == {str(kw.id)}


def test_handoff_empty_project_name_raises():
    async def run():
        repo = _repo()
        with pytest.raises(InvalidInput):
            await switch.build_handoff(
                repo, project="  ", from_harness="a", to_harness="b",
            )
        with pytest.raises(InvalidInput):
            await switch.build_handoff(
                repo, project="demo", from_harness="", to_harness="b",
            )
        with pytest.raises(InvalidInput):
            await switch.build_handoff(
                repo, project="demo", from_harness="a", to_harness="b",
                top_k=0,
            )

    asyncio.run(run())


def test_handoff_unknown_project_returns_empty_sections():
    async def run():
        repo = _repo()
        me = await _add(repo, "User prefers dark mode", type="personal")
        h = await switch.build_handoff(
            repo, project="never-heard-of-it", from_harness="a",
            to_harness="b",
        )
        for name in ("in_progress", "decisions", "conventions", "pitfalls"):
            assert h["sections"][name] == []
        # personal luggage still follows the person
        assert {e["id"] for e in h["sections"]["personal"]} == {str(me.id)}

    asyncio.run(run())


# ------------------------------------------------------------ visibility
def test_handoff_active_only():
    async def run():
        repo = _repo()
        ok = await _add(repo, "Active memory", tags=[project_tag("demo")])
        pend = await _add_pending(repo, "Pending memory",
                                  tags=[project_tag("demo")])
        arch = await _add(repo, "Will be archived",
                          tags=[project_tag("demo")])
        await repo.archive(arch.id)
        h = await switch.build_handoff(
            repo, project="demo", from_harness="a", to_harness="b",
        )
        all_ids = {e["id"] for s in h["sections"].values() for e in s}
        assert str(ok.id) in all_ids
        assert str(pend.id) not in all_ids
        assert str(arch.id) not in all_ids

    asyncio.run(run())


def test_handoff_encrypted_memories_stay_ciphertext():
    async def run():
        repo = _repo()
        ct = base64.b64encode(b"fake-ciphertext-bytes").decode()
        enc = await _add(repo, ct, type="personal", encrypted=True,
                         key_id="k1")
        h = await switch.build_handoff(
            repo, project="demo", from_harness="a", to_harness="b",
        )
        (entry,) = h["sections"]["personal"]
        assert entry["id"] == str(enc.id)
        assert entry["encrypted"] is True
        assert entry["key_id"] == "k1"
        # ciphertext passes through opaquely — the server never decrypts
        assert entry["content"] == ct
        return h

    h = asyncio.run(run())
    # briefing must not dump ciphertext into prose
    brief = switch.render_briefing(h)
    assert base64.b64encode(b"fake-ciphertext-bytes").decode() not in brief
    assert "🔒" in brief


# ------------------------------------------------------------ briefing
def test_render_briefing_human_copy():
    handoff = {
        "project": "demo",
        "from_harness": "claude-code",
        "to_harness": "cursor",
        "at": "2026-10-14T00:00:00+00:00",
        "sections": {
            "in_progress": [{
                "id": "1", "content": "Refactoring auth module",
                "type": "fact", "scope_tags": [], "source": {},
                "confidence": 0.9, "status": "active",
                "created_at": "2026-10-14T00:00:00+00:00",
                "updated_at": "2026-10-14T00:00:00+00:00",
                "superseded_by": None, "version": 1,
                "encrypted": False, "key_id": None,
                "untrusted_data": True,
            }],
            "decisions": [],
            "conventions": [],
            "pitfalls": [{
                "id": "2", "content": "Don't deploy on Friday",
                "type": "fact", "scope_tags": [], "source": {},
                "confidence": 0.9, "status": "active",
                "created_at": "2026-10-14T00:00:00+00:00",
                "updated_at": "2026-10-14T00:00:00+00:00",
                "superseded_by": None, "version": 1,
                "encrypted": False, "key_id": None,
                "untrusted_data": True,
            }],
            "personal": [],
        },
    }
    brief = switch.render_briefing(handoff)
    assert "demo" in brief
    assert "claude-code" in brief and "cursor" in brief
    assert "Refactoring auth module" in brief
    assert "Don't deploy on Friday" in brief
    # empty sections are skipped, not rendered as empty headers
    assert "Decisions" not in brief
    assert "Conventions" not in brief
    # human touch: not a dry list (English default)
    assert "your bags have arrived" in brief
    assert brief.endswith("\n")


def test_render_briefing_chinese_opt_in():
    handoff = {
        "project": "demo",
        "from_harness": "claude-code",
        "to_harness": "cursor",
        "at": "2026-10-14T00:00:00+00:00",
        "sections": {s: [] for s in switch.SECTIONS},
    }
    brief = switch.render_briefing(handoff, lang="zh")
    assert "交接简报" in brief
    assert "行李已送达" in brief
    assert "白纸" in brief
    assert brief.endswith("\n")
    # unknown language falls back to English
    brief_xx = switch.render_briefing(handoff, lang="xx")
    assert "Handoff briefing" in brief_xx
    assert "交接简报" not in brief_xx


def test_render_briefing_blank_slate():
    handoff = {
        "project": "fresh",
        "from_harness": "a",
        "to_harness": "b",
        "at": "2026-10-14T00:00:00+00:00",
        "sections": {s: [] for s in switch.SECTIONS},
    }
    brief = switch.render_briefing(handoff)
    assert "blank slate" in brief


# ------------------------------------------------------------ audit
def test_switch_event_audit_record():
    async def run():
        repo = _repo()
        ev = switch.SwitchEvent(from_harness="claude-code",
                                to_harness="codex", project="demo",
                                actor="user-1")
        rec = await ev.record(repo)
        assert rec.action == "harness_switch"
        assert rec.meta["from_harness"] == "claude-code"
        assert rec.meta["to_harness"] == "codex"
        assert rec.meta["project"] == "demo"
        assert rec.meta["actor"] == "user-1"
        assert rec.memory_ids == []
        rows = await repo.audit_list()
        assert rows[0].id == rec.id

    asyncio.run(run())


def test_build_handoff_records_switch_event():
    async def run():
        repo = _repo()
        await _add(repo, "Something", tags=[project_tag("demo")])
        await switch.build_handoff(repo, project="demo",
                                   from_harness="a", to_harness="b")
        rows = await repo.audit_list()
        assert any(r.action == "harness_switch" and
                   r.meta["project"] == "demo" for r in rows)
        # ...but not twice when the caller opts out (it records itself)
        await switch.build_handoff(repo, project="demo",
                                   from_harness="a", to_harness="b",
                                   record_event=False)
        rows = await repo.audit_list()
        assert sum(1 for r in rows
                   if r.action == "harness_switch") == 1

    asyncio.run(run())


def test_switch_event_rejects_empty():
    with pytest.raises(InvalidInput):
        switch.SwitchEvent(from_harness="", to_harness="b", project="demo")
    with pytest.raises(InvalidInput):
        switch.SwitchEvent(from_harness="a", to_harness="b", project="  ")
