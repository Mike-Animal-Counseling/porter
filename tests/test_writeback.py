"""Write-back tests (porter/writeback.py): Porter -> harness-native files.

Covers: marker-zone create/replace (user content outside markers untouched),
pre-write backups, dry_run writing nothing, unknown-harness fallback,
sensitive-personal exclusion, active-only gating, idempotent re-writes,
symlink refusal, and per-writer target paths — all against fake harness
dirs under tmp_path (never the real home directory).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from porter.writeback import (
    MARK_END,
    MARK_START,
    WRITERS,
    ClaudeCodeWriter,
    ClineWriter,
    CodexWriter,
    CursorWriter,
    GenericAgentsMdWriter,
    RooWriter,
    WindsurfWriter,
    get_writer,
)


def _mem(
    id="m1",
    content="Prefer tabs for indentation.",
    type="project",
    status="active",
    tags=None,
    triage_reasons=None,
    encrypted=False,
):
    return {
        "id": id,
        "content": content,
        "type": type,
        "scope_tags": tags or ["project:demo"],
        "status": status,
        "triage_reasons": triage_reasons or [],
        "source": {},
        "encrypted": encrypted,
    }


def _run(writer, memories, **kw):
    return asyncio.run(writer.write(memories, **kw))


def test_writeback_preserves_crlf_outside_zone_and_literal_backslashes(tmp_path):
    target = tmp_path / "AGENTS.md"
    target.write_bytes(
        b"Human header\r\n<!-- porter-sync:start -->old<!-- porter-sync:end -->\r\nHuman footer\r\n"
    )
    content = r"Use C:\projects\notes\1 as data"
    result = _run(GenericAgentsMdWriter(), [_mem(content=content)], project=str(tmp_path))
    assert result.written_files
    updated = target.read_bytes()
    assert updated.startswith(b"Human header\r\n")
    assert updated.endswith(b"\r\nHuman footer\r\n")
    assert content.encode() in updated


def test_writeback_rejects_non_utf8_without_replacing_user_bytes(tmp_path):
    target = tmp_path / "AGENTS.md"
    original = b"User data\xff\xfe\r\n"
    target.write_bytes(original)
    result = _run(GenericAgentsMdWriter(), [_mem()], project=str(tmp_path))
    assert result.skipped and not result.written_files
    assert target.read_bytes() == original


def test_writeback_replace_failure_keeps_original_and_removes_temp(tmp_path, monkeypatch):
    from porter import writeback

    target = tmp_path / "AGENTS.md"
    original = b"Human instructions\r\n"
    target.write_bytes(original)

    def fail(*_args):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(writeback.os, "replace", fail)
    result = _run(GenericAgentsMdWriter(), [_mem()], project=str(tmp_path))
    assert result.skipped and not result.written_files
    assert target.read_bytes() == original
    assert not list(tmp_path.glob(".porter-write-*"))


# ---------------------------------------------------------------------------
# Registry / fallback
# ---------------------------------------------------------------------------
def test_registry_has_expected_writers():
    assert set(WRITERS) == {
        "claude-code",
        "codex",
        "cursor",
        "windsurf",
        "cline",
        "roo",
    }
    assert isinstance(get_writer("codex"), CodexWriter)
    assert isinstance(get_writer("Claude-Code"), ClaudeCodeWriter)  # case-insensitive


def test_unknown_harness_falls_back_to_agents_md(tmp_path):
    w = get_writer("some-future-harness")
    assert isinstance(w, GenericAgentsMdWriter)
    res = _run(w, [_mem()], project=str(tmp_path / "proj"))
    target = tmp_path / "proj" / "AGENTS.md"
    assert str(target) in res.written_files
    text = target.read_text(encoding="utf-8")
    assert MARK_START in text and MARK_END in text
    assert "Prefer tabs" in text


# ---------------------------------------------------------------------------
# Marker zones: user content outside is never touched
# ---------------------------------------------------------------------------
def test_cursorrules_zone_appended_user_content_preserved(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    rules = proj / ".cursorrules"
    user_text = "# My rules\n\nAlways use tabs.\n"
    rules.write_text(user_text, encoding="utf-8")

    res = _run(CursorWriter(), [_mem()], project=str(proj))

    assert str(rules) in res.written_files
    text = rules.read_text(encoding="utf-8")
    # User content verbatim and BEFORE the marker zone.
    assert text.startswith(user_text)
    assert MARK_START in text and MARK_END in text
    assert "Prefer tabs for indentation." in text
    # Exactly one zone.
    assert text.count(MARK_START) == 1


def test_existing_zone_replaced_user_content_untouched(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    agents = proj / "AGENTS.md"
    agents.write_text(
        "# Project notes\n\nHand-written rule: no emojis.\n\n"
        f"{MARK_START}\nSTALE porter content\n{MARK_END}\n",
        encoding="utf-8",
    )

    res = _run(GenericAgentsMdWriter(), [_mem(content="New synced fact.")], project=str(proj))

    assert str(agents) in res.written_files
    text = agents.read_text(encoding="utf-8")
    assert "Hand-written rule: no emojis." in text  # untouched
    assert "STALE porter content" not in text  # replaced
    assert "New synced fact." in text
    assert text.count(MARK_START) == 1 and text.count(MARK_END) == 1


def test_codex_porter_md_created_under_fake_home(tmp_path):
    w = CodexWriter(home=tmp_path)
    res = _run(w, [_mem(), _mem(id="m2", content="Second fact.")])
    target = tmp_path / ".codex" / "memories" / "porter.md"
    assert str(target) in res.written_files
    text = target.read_text(encoding="utf-8")
    assert MARK_START in text and "Second fact." in text
    # New file: nothing to back up.
    assert res.backed_up == []


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------
def test_backup_captured_before_overwrite(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    rules = proj / ".cursorrules"
    original = "# user rules\n"
    rules.write_text(original, encoding="utf-8")

    res = _run(CursorWriter(), [_mem()], project=str(proj))

    assert len(res.backed_up) == 1
    backup = Path(res.backed_up[0])
    assert backup.read_text(encoding="utf-8") == original
    # Backup lives next to the target, with a .bak suffix so importer
    # *.md scans never mistake it for a memory.
    assert backup.parent.parent.parent == rules.parent
    assert backup.name.endswith(".bak")
    assert ".porter-backup" in backup.parts


def test_second_identical_write_is_noop(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    w = CursorWriter()
    first = _run(w, [_mem()], project=str(proj))
    assert first.written_files
    second = _run(w, [_mem()], project=str(proj))
    assert second.written_files == []
    assert any("already up to date" in s for s in second.skipped)
    assert second.backed_up == []  # no churn when nothing changed


# ---------------------------------------------------------------------------
# dry_run
# ---------------------------------------------------------------------------
def test_dry_run_writes_nothing(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    rules = proj / ".cursorrules"
    rules.write_text("# user\n", encoding="utf-8")

    res = _run(CursorWriter(), [_mem()], project=str(proj), dry_run=True)

    assert str(rules) in res.written_files  # reported...
    assert rules.read_text(encoding="utf-8") == "# user\n"  # ...but untouched
    assert res.backed_up == []
    assert "dry run" in res.note
    assert not (proj / ".porter-backup").exists()


# ---------------------------------------------------------------------------
# Eligibility: status, encryption, sensitivity
# ---------------------------------------------------------------------------
def test_only_active_memories_written(tmp_path):
    w = CodexWriter(home=tmp_path)
    res = _run(
        w,
        [
            _mem(id="a", status="active"),
            _mem(id="p", status="pending", content="Pending one."),
            _mem(id="x", status="archived", content="Archived one."),
        ],
    )
    text = (tmp_path / ".codex" / "memories" / "porter.md").read_text(encoding="utf-8")
    assert "Prefer tabs" in text
    assert "Pending one." not in text and "Archived one." not in text
    assert any(s.startswith("p:") for s in res.skipped)
    assert any(s.startswith("x:") for s in res.skipped)


def test_sensitive_personal_skipped_by_default(tmp_path):
    w = CodexWriter(home=tmp_path)
    sensitive = _mem(
        id="s1",
        type="personal",
        content="My SSN is 123-45-6789.",
        triage_reasons=["sensitive_content"],
    )
    plain_personal = _mem(id="s2", type="personal", content="I like dark mode.")
    res = _run(w, [sensitive, plain_personal])
    text = (tmp_path / ".codex" / "memories" / "porter.md").read_text(encoding="utf-8")
    assert "123-45-6789" not in text
    assert "dark mode" in text  # non-sensitive personal is fine
    assert any("s1" in s and "sensitive" in s for s in res.skipped)


def test_sensitive_personal_included_with_opt_in(tmp_path):
    w = CodexWriter(home=tmp_path)
    sensitive = _mem(
        id="s1",
        type="personal",
        content="Allergy: peanuts.",
        triage_reasons=["ml_sensitive:health"],
    )
    res = _run(w, [sensitive], include_sensitive=True)
    text = (tmp_path / ".codex" / "memories" / "porter.md").read_text(encoding="utf-8")
    assert "peanuts" in text
    assert res.written_files


def test_encrypted_memory_never_rendered(tmp_path):
    w = CodexWriter(home=tmp_path)
    res = _run(w, [_mem(id="e1", content="aGVsbG8=", encrypted=True)])
    assert not res.written_files
    assert any("e1" in s and "encrypted" in s for s in res.skipped)


# ---------------------------------------------------------------------------
# Symlink guard
# ---------------------------------------------------------------------------
def test_symlink_target_refused(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    outside = tmp_path / "real.md"
    outside.write_text("# real\n", encoding="utf-8")
    link = proj / ".cursorrules"
    link.symlink_to(outside)

    res = _run(CursorWriter(), [_mem()], project=str(proj))

    assert not res.written_files
    assert any("symlink" in s for s in res.skipped)
    assert outside.read_text(encoding="utf-8") == "# real\n"  # untouched


# ---------------------------------------------------------------------------
# Per-writer targets
# ---------------------------------------------------------------------------
def test_claude_code_matches_project_dir(tmp_path):
    home = tmp_path / "home"
    projdir = home / ".claude" / "projects" / "-tmp-proj"
    (projdir / "memory").mkdir(parents=True)

    w = ClaudeCodeWriter(home=home)
    res = _run(w, [_mem()], project="/tmp/proj")

    target = projdir / "memory" / "porter.md"
    assert str(target) in res.written_files
    assert "matched Claude Code project dir" in res.note


def test_claude_code_falls_back_to_global_claude_md(tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    w = ClaudeCodeWriter(home=home)
    res = _run(w, [_mem()], project="/tmp/unknown-proj")

    target = home / ".claude" / "CLAUDE.md"
    assert str(target) in res.written_files
    assert "falling back to global CLAUDE.md" in res.note


def test_windsurf_global_memories_without_project(tmp_path):
    w = WindsurfWriter(home=tmp_path)
    res = _run(w, [_mem()])
    target = tmp_path / ".codeium" / "windsurf" / "memories" / "porter.md"
    assert str(target) in res.written_files


def test_windsurf_project_writes_windsurfrules(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    res = _run(WindsurfWriter(home=tmp_path), [_mem()], project=str(proj))
    assert str(proj / ".windsurfrules") in res.written_files


def test_cline_and_roo_memory_bank(tmp_path):
    for cls in (ClineWriter, RooWriter):
        proj = tmp_path / f"p-{cls.harness}"
        proj.mkdir()
        res = _run(cls(), [_mem()], project=str(proj))
        target = proj / "memory-bank" / "porter.md"
        assert str(target) in res.written_files, cls.harness


def test_cursor_requires_project_root():
    res = _run(CursorWriter(), [_mem()])
    assert not res.written_files
    assert "project root" in res.note


def test_write_result_to_dict_roundtrip(tmp_path):
    w = CodexWriter(home=tmp_path)
    res = _run(w, [_mem()], dry_run=True)
    d = res.to_dict()
    assert set(d) == {"written_files", "skipped", "backed_up", "note"}
    assert d["written_files"] == res.written_files


def test_zone_regex_replaces_only_first_zone(tmp_path):
    # Two zones (shouldn't happen, but must not corrupt): only the first
    # is replaced, the second left alone.
    proj = tmp_path / "proj"
    proj.mkdir()
    agents = proj / "AGENTS.md"
    agents.write_text(
        f"{MARK_START}\nold1\n{MARK_END}\nmid\n{MARK_START}\nold2\n{MARK_END}\n",
        encoding="utf-8",
    )
    _run(GenericAgentsMdWriter(), [_mem(content="fresh")], project=str(proj))
    text = agents.read_text(encoding="utf-8")
    assert "old1" not in text and "fresh" in text
    assert "old2" in text and text.count(MARK_START) == 2
