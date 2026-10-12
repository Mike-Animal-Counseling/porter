"""Importer tests for ChatGPT / Claude official-export importers (PRD §8 F7).

Covers: conversations.json parsing for both export shapes, the
durable-statement heuristic, dry-run output, pending-inbox import with
source tags + confidence 0.5, malformed inputs (never crash), the zip
path (in-memory only, zip-slip, zip-bomb guard), and read-only proof
(source files byte-identical after import).
"""
from __future__ import annotations

import asyncio
import json
import struct
import zipfile
from pathlib import Path

import pytest

from porter.embeddings import HashEmbedder
from porter.import_cli import main as import_main
from porter.importers import (
    IMPORTERS,
    ChatGPTExportImporter,
    ClaudeExportImporter,
    _looks_durable,
    import_dedupe_key,
    run_import,
)
from porter.repositories import InMemoryMemoryRepository


# ------------------------------------------------------------ fixtures
def _chatgpt_mapping(msgs: list[tuple[str, str, float]]) -> dict:
    """Build a ChatGPT-style mapping from (role, text, create_time) triples."""
    mapping: dict = {
        "root": {"id": "root", "message": None,
                 "parent": None, "children": ["m0"] if msgs else []}
    }
    prev = "root"
    for i, (role, text, ts) in enumerate(msgs):
        mid = f"m{i}"
        mapping[mid] = {
            "id": mid,
            "message": {
                "id": mid,
                "author": {"role": role},
                "content": {"content_type": "text", "parts": [text]},
                "create_time": ts,
            },
            "parent": prev,
            "children": [],
        }
        mapping[prev]["children"].append(mid)
        prev = mid
    return mapping


@pytest.fixture()
def chatgpt_export(tmp_path):
    convs = [
        {
            "id": "conv-a",
            "title": "Sourdough starter",
            "create_time": 1728000000.0,
            "mapping": _chatgpt_mapping([
                ("user",
                 "My name is Alex. I prefer concise answers with no fluff. "
                 "(my test key sk-abcdefghijklmnopqrst)",
                 1728000000.0),
                ("assistant", "Sure thing, Alex.", 1728000060.0),
            ]),
        },
        {
            "id": "conv-b",
            "title": "Python question",
            "create_time": 1728000000.0,
            "mapping": _chatgpt_mapping([
                ("user", "How do I sort a list in Python?", 1728000000.0),
                ("assistant", "Use sorted().", 1728090000.0),  # next day
            ]),
        },
        {
            "id": "conv-c",
            "title": "Empty chat",
            "create_time": 1728000000.0,
            "mapping": {"root": {"id": "root", "message": None,
                                 "parent": None, "children": []}},
        },
        "not-a-conversation",
        42,
    ]
    p = tmp_path / "conversations.json"
    p.write_text(json.dumps(convs), encoding="utf-8")
    return p


@pytest.fixture()
def claude_export(tmp_path):
    convs = [
        {
            "uuid": "claude-1",
            "name": "Trip planning",
            "created_at": "2024-10-05T10:00:00Z",
            "chat_messages": [
                {"uuid": "mm1",
                 "text": "I love hiking in the Alps, remember that I "
                         "prefer morning flights.",
                 "sender": "human",
                 "created_at": "2024-10-05T10:00:00Z"},
                {"uuid": "mm2", "text": "Noted!",
                 "sender": "assistant",
                 "created_at": "2024-10-05T10:01:00Z"},
            ],
        },
        {
            "uuid": "claude-2",
            "name": "Debug help",
            "created_at": "2024-10-06T10:00:00Z",
            "chat_messages": [
                {"uuid": "mm3", "text": "why is this segfaulting",
                 "sender": "human",
                 "created_at": "2024-10-06T10:00:00Z"},
            ],
        },
        {"uuid": "claude-3", "name": "Empty", "chat_messages": []},
        None,
    ]
    p = tmp_path / "conversations.json"
    p.write_text(json.dumps(convs), encoding="utf-8")
    return p


@pytest.fixture()
def chatgpt_zip(tmp_path, chatgpt_export):
    zp = tmp_path / "export.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.write(chatgpt_export, "ChatGPT Data/conversations.json")
    return zp


# ------------------------------------------------------------ parsing
def test_chatgpt_scan(chatgpt_export):
    candidates, note = ChatGPTExportImporter(chatgpt_export).scan()
    # conv-a: episode + fact (durable first message); conv-b: episode only;
    # conv-c empty; 2 malformed entries.
    assert len(candidates) == 3, note
    assert "2 malformed" in note and "1 empty" in note

    episodes = [c for c in candidates if c.type == "episode"]
    facts = [c for c in candidates if c.type == "fact"]
    assert len(episodes) == 2 and len(facts) == 1

    ep_a = next(c for c in episodes
                if c.source["conversation_id"] == "conv-a")
    assert ep_a.content == (
        "ChatGPT conversation 'Sourdough starter' (2 messages, 2024-10-04)"
    )
    assert ep_a.source["client_id"] == "chatgpt"
    assert ep_a.source["imported_from"] == str(chatgpt_export)
    assert ep_a.confidence == 0.5

    ep_b = next(c for c in episodes
                if c.source["conversation_id"] == "conv-b")
    assert "(2 messages, 2024-10-04 → 2024-10-05)" in ep_b.content

    fact = facts[0]
    assert fact.source["conversation_id"] == "conv-a"
    assert "My name is Alex" in fact.content
    assert len(fact.content) <= 2000
    assert fact.confidence == 0.5


def test_claude_scan(claude_export):
    candidates, note = ClaudeExportImporter(claude_export).scan()
    assert len(candidates) == 3, note  # episode+fact, episode, (empty+None)
    episodes = [c for c in candidates if c.type == "episode"]
    facts = [c for c in candidates if c.type == "fact"]
    assert len(episodes) == 2 and len(facts) == 1
    assert episodes[0].content.startswith("Claude conversation 'Trip planning'")
    assert episodes[0].source["client_id"] == "claude"
    assert facts[0].source["conversation_id"] == "claude-1"
    assert "I love hiking" in facts[0].content


def test_shapes_parsed_defensively_either_way(chatgpt_export, claude_export):
    # A mislabeled file still parses: shapes are tried independently of
    # which importer is running.
    c1, _ = ChatGPTExportImporter(claude_export).scan()
    assert len(c1) == 3
    c2, _ = ClaudeExportImporter(chatgpt_export).scan()
    assert len(c2) == 3


def test_unknown_shape_skipped_with_warning(tmp_path):
    p = tmp_path / "conversations.json"
    p.write_text(json.dumps({"conversations": []}), encoding="utf-8")
    candidates, note = ChatGPTExportImporter(p).scan()
    assert candidates == []
    assert "neither the ChatGPT nor the Claude" in note


def test_heuristic_rules():
    assert _looks_durable("My name is Alex")
    assert _looks_durable("I'm a backend engineer")
    assert _looks_durable("I live in San Jose")
    assert _looks_durable("I work at a startup")
    assert _looks_durable("my favorite editor is vim")
    assert _looks_durable("I prefer concise answers")
    assert _looks_durable("I don't like meetings before 10")
    assert _looks_durable("remember that I hate cilantro")
    assert _looks_durable("from now on always use type hints")
    assert not _looks_durable("How do I sort a list in Python?")
    assert not _looks_durable("why is this segfaulting")
    assert not _looks_durable("")


# ------------------------------------------------------------ malformed input: never crash
def test_malformed_json_skipped(tmp_path):
    p = tmp_path / "conversations.json"
    p.write_bytes(b"{not json at all,,,\x00\xff")
    candidates, note = ChatGPTExportImporter(p).scan()
    assert candidates == []
    assert "not parseable as JSON" in note


def test_deeply_nested_json_skipped(tmp_path):
    p = tmp_path / "conversations.json"
    p.write_text("[" * 100_000, encoding="utf-8")  # RecursionError in json
    candidates, note = ChatGPTExportImporter(p).scan()
    assert candidates == []
    assert "skipped" in note


def test_null_bytes_and_non_utf8_survive(tmp_path):
    # Invalid bytes live INSIDE string values (not trailing garbage, which
    # is correctly rejected as invalid JSON): null bytes are stripped,
    # non-UTF8 bytes become U+FFFD, parsing continues.
    doc = [{
        "id": "x", "title": "T",
        "mapping": _chatgpt_mapping(
            [("user", "I like coffee PLACEHOLDER", 1728000000.0)]),
    }]
    raw = json.dumps(doc, ensure_ascii=True).encode("ascii")
    raw = raw.replace(b"PLACEHOLDER", b"\x00 and \xff\xfe bytes")
    p = tmp_path / "conversations.json"
    p.write_bytes(raw)
    candidates, note = ChatGPTExportImporter(p).scan()
    assert len(candidates) == 2, note  # episode + durable fact
    assert "\x00" not in candidates[0].content
    fact = next(c for c in candidates if c.type == "fact")
    assert "I like coffee" in fact.content


def test_missing_file_and_wrong_suffix(tmp_path):
    cands, note = ChatGPTExportImporter(tmp_path / "nope.json").scan()
    assert cands == [] and "skipped" in note
    other = tmp_path / "export.txt"
    other.write_text("[]")
    cands, note = ChatGPTExportImporter(other).scan()
    assert cands == [] and ".json" in note


# ------------------------------------------------------------ zip handling
def test_zip_import_in_memory(chatgpt_zip):
    candidates, note = ChatGPTExportImporter(chatgpt_zip).scan()
    assert len(candidates) == 3, note
    assert all(c.source["imported_from"] == str(chatgpt_zip)
               for c in candidates)


def test_zip_without_conversations_json(tmp_path):
    zp = tmp_path / "empty.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("notes.txt", "hello")
    candidates, note = ChatGPTExportImporter(zp).scan()
    assert candidates == []
    assert "no conversations.json" in note


def test_zip_slip_never_writes(tmp_path, chatgpt_export):
    zp = tmp_path / "evil.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("../../evil.txt", "pwned")
        zf.writestr("conversations.json",
                    chatgpt_export.read_bytes())
    before = set(tmp_path.rglob("*"))
    candidates, _ = ChatGPTExportImporter(zp).scan()
    assert len(candidates) == 3
    assert set(tmp_path.rglob("*")) == before  # nothing extracted
    assert not (tmp_path.parent / "evil.txt").exists()


def _make_lying_zip(path: Path, claimed_size: int):
    """Craft a zip whose central directory claims a huge uncompressed size."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("conversations.json", b"[]")
    data = bytearray(path.read_bytes())
    patched = 0
    i = 0
    while True:
        i = data.find(b"PK\x01\x02", i)  # central-directory signature
        if i == -1:
            break
        struct.pack_into("<I", data, i + 24, claimed_size)
        patched += 1
        i += 4
    assert patched >= 1
    path.write_bytes(bytes(data))


def test_zip_bomb_guard_rejects_lying_header(tmp_path):
    zp = tmp_path / "bomb.zip"
    _make_lying_zip(zp, 300 * 1024 * 1024)
    candidates, note = ChatGPTExportImporter(zp).scan()
    assert candidates == []
    assert "zip-bomb guard" in note


# ------------------------------------------------------------ CLI
def test_cli_dry_run_chatgpt(chatgpt_export, capsys):
    rc = import_main(["--harness", "chatgpt", "--dry-run",
                      "--file", str(chatgpt_export)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "would be imported" in out
    assert "Sourdough starter" in out
    # dry-run previews are scrubbed: the pasted test key never appears raw
    assert "sk-abcdefghijklmnopqrst" not in out
    assert "[REDACTED]" in out


def test_cli_dry_run_claude_zip(chatgpt_zip, capsys):
    rc = import_main(["--harness", "claude", "--dry-run",
                      "--file", str(chatgpt_zip)])
    assert rc == 0
    assert "would be imported" in capsys.readouterr().out


def test_cli_export_requires_file(capsys):
    rc = import_main(["--harness", "chatgpt", "--dry-run"])
    assert rc == 1
    assert "--file" in capsys.readouterr().err
    rc = import_main(["--harness", "claude", "--dry-run"])
    assert rc == 1


# ------------------------------------------------------------ pending inbox + dedupe + read-only
def test_run_import_pending_inbox(chatgpt_export):
    async def _run():
        repo = InMemoryMemoryRepository(HashEmbedder())
        imp = ChatGPTExportImporter(chatgpt_export)
        candidates, _ = imp.scan()
        summary = await run_import(repo, candidates)
        assert summary["imported"] == 3
        assert summary["redactions"] >= 1  # the pasted test key got scrubbed

        pending = await repo.list(status="pending", limit=100)
        assert len(pending) == 3
        for rec in pending:
            assert rec.confidence == 0.5
            assert rec.source["client_id"] == "chatgpt"
            assert rec.source["imported_from"] == str(chatgpt_export)
            assert "conversation_id" in rec.source
        fact = next(r for r in pending if r.type == "fact")
        assert "[REDACTED]" in fact.content
        assert "sk-abcdefghijklmnopqrst" not in fact.content
        # Nothing became searchable without approval.
        assert await repo.search(query="sourdough", top_k=5) == []

        # Re-import: per-conversation dedupe keys make it a clean no-op.
        s2 = await run_import(repo, candidates)
        assert s2["imported"] == 0
        assert s2["skipped_already_imported"] == 3
        assert len(await repo.list(status="pending", limit=100)) == 3
    asyncio.run(_run())


def test_dedupe_key_granularity():
    base = {"imported_from": "/x/conversations.json"}
    assert import_dedupe_key(base) == "/x/conversations.json"
    ep = import_dedupe_key({**base, "conversation_id": "a",
                            "kind": "episode"})
    fact = import_dedupe_key({**base, "conversation_id": "a",
                              "kind": "fact"})
    other = import_dedupe_key({**base, "conversation_id": "b",
                               "kind": "episode"})
    assert len({ep, fact, other}) == 3  # episode+fact+other conv distinct


def test_export_importers_registered():
    assert IMPORTERS["chatgpt"] is ChatGPTExportImporter
    assert IMPORTERS["claude"] is ClaudeExportImporter


def test_source_files_byte_identical(chatgpt_export, chatgpt_zip):
    """READ-ONLY: export files unchanged after scan + import."""
    async def _run():
        paths = [chatgpt_export, chatgpt_zip]
        before = {p: p.read_bytes() for p in paths}
        repo = InMemoryMemoryRepository(HashEmbedder())
        for cls, path in ((ChatGPTExportImporter, chatgpt_export),
                          (ClaudeExportImporter, chatgpt_zip)):
            candidates, _ = cls(path).scan()
            await run_import(repo, candidates)
        after = {p: p.read_bytes() for p in paths}
        assert before == after
    asyncio.run(_run())
