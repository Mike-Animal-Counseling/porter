"""Importer tests for the 12-harness extension (PRD §8 F7).

Covers: claude-code, windsurf, cline, roo, gemini, qwen importers +
NO_NATIVE_STORE honesty + clients/ config snippet validation +
adversarial parser cases (huge files, weird frontmatter, symlinks,
secret-scrubber bypass attempts).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml  # type: ignore

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11 fallback
    import tomli as tomllib  # type: ignore

from porter.embeddings import HashEmbedder
from porter.import_cli import main as import_main
from porter.importers import (
    IMPORTERS,
    NO_NATIVE_STORE,
    AiderImporter,
    ClaudeCodeImporter,
    ClineImporter,
    CursorImporter,
    DevinImporter,
    DroidImporter,
    GeminiImporter,
    JunieImporter,
    OpenHandsImporter,
    QwenImporter,
    RooImporter,
    TabnineImporter,
    WarpImporter,
    WindsurfImporter,
    _decode_claude_project_dir,
    run_import,
    scrub_secrets,
)
from porter.repositories import InMemoryMemoryRepository

CLIENTS_DIR = Path(__file__).resolve().parent.parent / "clients"


# ------------------------------------------------------------ fixtures
@pytest.fixture()
def claude_home(tmp_path):
    home = tmp_path / ".claude"
    proj = home / "projects" / "-Users-test-proj"
    mem = proj / "memory"
    mem.mkdir(parents=True)
    (mem / "notes.md").write_text(
        "---\ntitle: Project notes\ntags: workflow\n---\n\nAlways run tests before committing.\n"
    )
    (mem / "plain.md").write_text("No frontmatter here, just text.\n")
    (proj / "MEMORY.md").write_text("# Proj memory\n\nUses pnpm.\n")
    (home / "CLAUDE.md").write_text("# Instructions\n\nBe concise. Prefer ripgrep.\n")
    (home / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": str(tmp_path / "auto-mem")})
    )
    auto = tmp_path / "auto-mem"
    auto.mkdir()
    (auto / "extra.md").write_text("From the override dir.\n")
    return home


@pytest.fixture()
def windsurf_home(tmp_path):
    mem = tmp_path / ".codeium" / "windsurf" / "memories"
    mem.mkdir(parents=True)
    (mem / "m1.md").write_text("# Windsurf note\n\nSome memory.\n")
    return tmp_path / ".codeium" / "windsurf" / "memories"


@pytest.fixture()
def cline_proj(tmp_path):
    mb = tmp_path / "proj" / "memory-bank"
    mb.mkdir(parents=True)
    (mb / "brief.md").write_text("---\ntitle: Brief\ntype: episode\n---\n\nDid the thing.\n")
    return tmp_path / "proj"


@pytest.fixture()
def roo_proj(tmp_path):
    mb = tmp_path / "proj" / "memory-bank"
    mb.mkdir(parents=True)
    (mb / "brief.md").write_text("Roo memory bank note.\n")
    rules = tmp_path / "proj" / ".roo" / "rules"
    rules.mkdir(parents=True)
    (rules / "style.md").write_text("# Style rule\n\nUse tabs.\n")
    return tmp_path / "proj"


@pytest.fixture()
def gemini_home(tmp_path):
    g = tmp_path / ".gemini"
    g.mkdir()
    (g / "GEMINI.md").write_text("# Gemini\n\nBe helpful.\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "GEMINI.md").write_text("# Project\n\nUse bun.\n")
    return tmp_path


# ------------------------------------------------------------ claude-code
def test_decode_claude_project_dir_best_effort():
    assert _decode_claude_project_dir("-Users-test-proj") == "/Users/test/proj"
    # no leading dash -> still handled
    assert _decode_claude_project_dir("Users-test") == "/Users/test"


def test_claude_code_scan(claude_home):
    imp = ClaudeCodeImporter(claude_home)
    candidates, note = imp.scan()
    # 2 memory/*.md + MEMORY.md + auto-mem extra.md + CLAUDE.md seed = 5
    assert len(candidates) == 5, note
    by_src = {Path(c.imported_from).name: c for c in candidates}
    notes = by_src["notes.md"]
    assert notes.source["client_id"] == "claude-code"
    assert notes.source["project"] == "/Users/test/proj"
    assert notes.source["project_dir"] == "-Users-test-proj"
    assert notes.scope_tags == ["workflow"]
    auto = by_src["extra.md"]
    assert auto.source.get("auto_memory_dir") is True
    seed = by_src["CLAUDE.md"]
    assert seed.type == "project"
    assert seed.confidence == 0.3
    assert "seed" in seed.scope_tags
    assert seed.source.get("seed") is True


def test_claude_code_missing_home(tmp_path):
    candidates, note = ClaudeCodeImporter(tmp_path / "nope").scan()
    assert candidates == []
    assert "none found" in note


def test_claude_code_bad_settings_json(tmp_path):
    home = tmp_path / ".claude"
    home.mkdir()
    (home / "settings.json").write_text("{not json")
    # must not raise; default locations still scanned (empty here)
    candidates, _ = ClaudeCodeImporter(home).scan()
    assert candidates == []


# ------------------------------------------------------------ windsurf
def test_windsurf_scan(windsurf_home):
    imp = WindsurfImporter(windsurf_home)
    candidates, note = imp.scan()
    assert len(candidates) == 1
    assert candidates[0].source["client_id"] == "windsurf"


def test_windsurf_absent_is_normal(tmp_path):
    candidates, note = WindsurfImporter(tmp_path / "nope").scan()
    assert candidates == []
    assert "normal" in note


# ------------------------------------------------------------ cline / roo
def test_cline_scan(cline_proj):
    imp = ClineImporter([cline_proj])
    candidates, note = imp.scan()
    assert len(candidates) == 1
    c = candidates[0]
    assert c.source["client_id"] == "cline"
    assert c.type == "episode"  # frontmatter honored
    assert c.source["project_root"] == str(cline_proj)


def test_cline_no_roots():
    candidates, note = ClineImporter([]).scan()
    assert candidates == []
    assert "no project roots" in note


def test_roo_scan_rules_as_skill_ref(roo_proj):
    imp = RooImporter([roo_proj])
    candidates, note = imp.scan()
    assert len(candidates) == 2, note
    rule = next(c for c in candidates if c.source.get("kind") == "rule")
    assert rule.type == "skill_ref"
    assert "rule" in rule.scope_tags
    assert rule.source["client_id"] == "roo"
    mem = next(c for c in candidates if c.source.get("kind") != "rule")
    assert mem.type == "fact"


# ------------------------------------------------------------ gemini / qwen
def test_gemini_scan(gemini_home):
    imp = GeminiImporter(gemini_home / ".gemini", [gemini_home / "proj"])
    candidates, note = imp.scan()
    assert len(candidates) == 2, note
    assert all(c.type == "project" for c in candidates)
    assert all(c.confidence == 0.4 for c in candidates)
    assert all("seed" in c.scope_tags for c in candidates)
    assert all(c.source["client_id"] == "gemini" for c in candidates)
    proj = next(c for c in candidates if "project_root" in c.source)
    assert proj.source["project_root"] == str(gemini_home / "proj")


def test_qwen_scan(tmp_path):
    q = tmp_path / ".qwen"
    q.mkdir()
    (q / "QWEN.md").write_text("# Qwen\n\nBe fast.\n")
    candidates, _ = QwenImporter(q).scan()
    assert len(candidates) == 1
    assert candidates[0].source["client_id"] == "qwen"
    assert candidates[0].confidence == 0.4


def test_warp_scan(tmp_path):
    # global ~/.warp/WARP.md + <project>/WARP.md -> project-domain seeds
    warp_home = tmp_path / ".warp"
    warp_home.mkdir()
    (warp_home / "WARP.md").write_text("# Warp\n\nAlways use pnpm.\n")
    proj = tmp_path / "myrepo"
    proj.mkdir()
    (proj / "WARP.md").write_text("# Project\n\nUse ruff.\n")
    candidates, note = WarpImporter(warp_home, [proj]).scan()
    assert len(candidates) == 2, note
    assert all(c.type == "project" for c in candidates)
    assert all("seed" in c.scope_tags for c in candidates)
    tagged = next(c for c in candidates if "project:myrepo" in c.scope_tags)
    assert tagged.source["project_root"] == str(proj)
    assert "warp" in IMPORTERS


def test_cursor_rules_scan(tmp_path):
    # .cursorrules + .cursor/rules/*.mdc -> project-domain seeds with the
    # project tag from the directory name
    proj = tmp_path / "myrepo"
    (proj / ".cursor" / "rules").mkdir(parents=True)
    (proj / ".cursorrules").write_text("Always run tests.\n")
    (proj / ".cursor" / "rules" / "style.mdc").write_text("Use tabs.\n")
    candidates, note = CursorImporter([proj]).scan()
    assert len(candidates) == 2, note
    assert all(c.type == "project" for c in candidates)
    assert all("project:myrepo" in c.scope_tags for c in candidates)
    assert all(c.source["client_id"] == "cursor" for c in candidates)
    names = sorted(Path(c.imported_from).name for c in candidates)
    assert names == [".cursorrules", "style.mdc"]


def test_aider_chat_history_scan(tmp_path):
    # <project>/.aider.chat.history.md -> per-session project-domain seeds
    proj = tmp_path / "myrepo"
    proj.mkdir()
    (proj / ".aider.chat.history.md").write_text(
        "# aider chat started at 2026-10-01\n\n"
        "User asked to refactor the auth module. We decided to use JWT.\n"
        "This is a fairly long session body so it passes the stub filter. "
        "More context about the codebase structure and decisions made.\n\n"
        "# aider chat started at 2026-10-02\n\n"
        "Fixed the login bug. The pitfall was the token refresh race. "
        "Added a regression test and documented the workaround in detail.\n"
    )
    candidates, note = AiderImporter([proj]).scan()
    assert len(candidates) == 2, note
    assert all(c.type == "project" for c in candidates)
    assert all("project:myrepo" in c.scope_tags for c in candidates)
    assert all(c.source["client_id"] == "aider" for c in candidates)
    assert "aider" in IMPORTERS

    # missing file -> empty, honest note (no crash)
    candidates, note = AiderImporter([tmp_path / "empty"]).scan()
    assert candidates == []


# ------------------------------------------------------------ honesty registry
def test_no_native_store_registry():
    assert set(NO_NATIVE_STORE) == {
        "opencode",
        "amp",
        "kilo",
        "kimi",
        "copilot-cli",
        "aider",
        # batch 2 (2026-10-12): MCP implant only
        "continue",
        "zencoder",
        "neovim",
        "emacs",
        "antigravity",
        "kiro",
        "augment",
    }
    for name, reason in NO_NATIVE_STORE.items():
        assert "MCP" in reason  # every reason points at the MCP path


def test_importers_registry_complete():
    assert set(IMPORTERS) >= {
        "codex",
        "cursor",
        "zed",
        "claude-code",
        "windsurf",
        "cline",
        "roo",
        "gemini",
        "qwen",
        # batch 2 (2026-10-12)
        "junie",
        "droid",
        "tabnine",
        "devin",
        "openhands",
    }


# ------------------------------------------------------------ read-only
def test_new_importers_never_write_source_files(claude_home, windsurf_home):
    """READ-ONLY: source trees byte-identical after scan+import."""

    async def _run():
        targets = [claude_home, windsurf_home.parent]
        before = {p: p.read_bytes() for t in targets for p in t.rglob("*.md") if p.is_file()}
        repo = InMemoryMemoryRepository(HashEmbedder())
        for imp in (ClaudeCodeImporter(claude_home), WindsurfImporter(windsurf_home)):
            candidates, _ = imp.scan()
            await run_import(repo, candidates)
        after = {p: p.read_bytes() for t in targets for p in t.rglob("*.md") if p.is_file()}
        assert before == after

    asyncio.run(_run())


# ------------------------------------------------------------ CLI dry-run
def test_cli_dry_run_new_harnesses(claude_home, capsys):
    rc = import_main(["--harness", "claude-code", "--dry-run", "--claude-home", str(claude_home)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "claude-code" in out
    assert "would be imported" in out


def test_cli_all_includes_new(claude_home, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(claude_home.parent))
    rc = import_main(["--harness", "all", "--dry-run", "--claude-home", str(claude_home)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[claude-code]" in out
    assert "[windsurf]" in out  # absent dir -> honest note, no crash


# ------------------------------------------------------------ clients/ configs
def _all_configs():
    return sorted(CLIENTS_DIR.iterdir())


def test_clients_dir_has_all_harnesses():
    names = {p.name for p in _all_configs()}
    for expected in [
        "codex-config.toml",
        "codex-http-config.toml",
        "cursor-mcp.json",
        "zed-settings.json",
        "claude-code-mcp.json",
        "opencode.json",
        "opencode-v2-http.json",
        "gemini-settings.json",
        "qwen-settings.json",
        "amp-settings.json",
        "windsurf-mcp_config.json",
        "roo-mcp.json",
        "cline-mcp.json",
        "kilocode-mcp.json",
        "kimi-mcp.json",
        "copilot-cli-mcp.json",
        "aider-conf.yml",
        # batch 2 (2026-10-12)
        "devin-mcp_config.json",
        "continue-config.json",
        "junie-mcp.json",
        "droid-mcp.json",
        "openhands-mcp.json",
        "tabnine-settings.json",
        "zencoder-settings.json",
        "neovim-mcphub-servers.json",
        "emacs-gptel-mcp.el",
        "antigravity-mcp_config.json",
        "kiro-mcp.json",
        "augment-mcp.json",
    ]:
        assert expected in names, f"missing {expected}"


def _load_config(path: Path):
    text = path.read_text(encoding="utf-8")
    placeholder = (
        "{env:PORTER_TOKEN}" if path.name == "opencode-v2-http.json" else "{{PORTER_TOKEN}}"
    )
    assert placeholder in text, f"{path.name}: token placeholder missing"
    # no real-looking secrets
    assert "sk-" not in text.replace(placeholder, ""), f"{path.name}: suspicious secret-like value"
    if path.suffix == ".json":
        return json.loads(text)
    if path.suffix == ".toml":
        return tomllib.loads(text)
    if path.suffix in (".yml", ".yaml"):
        return yaml.safe_load(text)
    raise AssertionError(f"unknown suffix {path.suffix}")


def _find_porter_server(cfg, path: Path):
    """Locate the porter server entry across the vendor-specific key shapes."""
    if path.name == "opencode.json":
        return cfg["mcp"]["porter"]
    if path.name == "opencode-v2-http.json":
        return cfg["mcp"]["servers"]["porter"]
    if path.name == "amp-settings.json":
        return cfg["amp.mcpServers"]["porter"]
    if path.name == "aider-conf.yml":
        return cfg["mcp-servers"]["porter"]
    if path.name == "zed-settings.json":
        return cfg["context_servers"]["porter"]  # pre-existing zed shape
    if path.name == "zencoder-settings.json":
        return cfg["zencoder.mcpServers"]["porter"]
    if path.suffix == ".toml":  # codex: [mcp_servers.porter]
        return cfg["mcp_servers"]["porter"]
    return cfg["mcpServers"]["porter"]


def _is_http_only(path: Path) -> bool:
    return path.name in {
        "antigravity-mcp_config.json",
        "codex-http-config.toml",
        "opencode-v2-http.json",
    }


def test_all_client_configs_parse_and_reference_porter():
    for path in _all_configs():
        if path.suffix not in (".json", ".toml", ".yml", ".yaml"):
            continue
        cfg = _load_config(path)
        entry = _find_porter_server(cfg, path)
        blob = json.dumps(entry)
        if _is_http_only(path):
            assert entry.get("serverUrl") or entry.get("url"), path.name
            assert "127.0.0.1:8000/mcp" in blob, path.name
        else:
            # stdio shape sanity: some command/args invoking porter.mcp_stdio
            assert "porter.mcp_stdio" in blob, path.name
        assert "{{PORTER_TOKEN}}" in blob or _is_http_only(path), path.name


def test_opencode_uses_mcp_key_not_mcpservers():
    cfg = _load_config(CLIENTS_DIR / "opencode.json")
    assert "mcp" in cfg and "mcpServers" not in cfg
    entry = cfg["mcp"]["porter"]
    assert entry["type"] == "local"
    assert "env" not in entry
    assert entry["environment"]["PORTER_REPO"] == "postgres"
    assert entry["environment"]["PORTER_TOKEN"] == "{{PORTER_TOKEN}}"


def test_copilot_cli_has_stdio_type_and_caveat():
    cfg = _load_config(CLIENTS_DIR / "copilot-cli-mcp.json")
    assert cfg["mcpServers"]["porter"]["type"] == "stdio"
    assert "UNVERIFIED" in cfg["_comment"] or "verify" in cfg["_comment"].lower()


# ------------------------------------------------------------ adversarial
def test_huge_file_skipped(tmp_path):
    big = tmp_path / ".codeium" / "windsurf" / "memories"
    big.mkdir(parents=True)
    p = big / "huge.md"
    # sparse write: 9MB without filling RAM
    with p.open("wb") as f:
        f.truncate(9 * 1024 * 1024)
    candidates, _ = WindsurfImporter(big).scan()
    assert candidates == []  # skipped, not OOM'd


def test_weird_frontmatter_no_crash(tmp_path):
    d = tmp_path / ".codeium" / "windsurf" / "memories"
    d.mkdir(parents=True)
    (d / "a.md").write_text("---\nno closing fence\njust text\n")
    (d / "b.md").write_text("---\ntitle: x\n: weird\n:::\n---\nbody\n")
    (d / "c.md").write_text("\x00\x01 binary \xff\xfe stuff\n", encoding="utf-8")
    (d / "empty.md").write_text("   \n  \n")
    candidates, _ = WindsurfImporter(d).scan()
    # a/b/c parse (best-effort), empty skipped — key point: no crash
    assert len(candidates) == 3


def test_symlink_outside_tree_skipped(tmp_path):
    mem = tmp_path / ".codeium" / "windsurf" / "memories"
    mem.mkdir(parents=True)
    outside = tmp_path / "secret.md"
    outside.write_text("TOP SECRET DO NOT IMPORT\n")
    (mem / "evil.md").symlink_to(outside)
    (mem / "real.md").write_text("legit\n")
    candidates, _ = WindsurfImporter(mem).scan()
    assert len(candidates) == 1
    assert "TOP SECRET" not in " ".join(c.content for c in candidates)


def test_title_and_tag_caps(tmp_path):
    d = tmp_path / ".codeium" / "windsurf" / "memories"
    d.mkdir(parents=True)
    tags = ", ".join(f"t{i}" for i in range(30))
    (d / "x.md").write_text(f"---\ntitle: {'T' * 500}\ntags: {tags}\n---\n\nbody\n")
    (candidates, _) = WindsurfImporter(d).scan()
    assert len(candidates[0].title) <= 200
    assert len(candidates[0].scope_tags) <= 20


@pytest.mark.parametrize(
    "text",
    [
        "API_KEY: sk-abcdef1234567890abcdef",
        'api-key = "sk-abcdef1234567890abcdef"',
        "password: hunter2hunter",
        "client_secret=hunter2hunter",
        "auth_token: abcdef1234567890",
    ],
)
def test_scrubber_kv_variants(text):
    scrubbed, n = scrub_secrets(text)
    assert n >= 1, text
    assert "[REDACTED]" in scrubbed
    # the secret VALUE must be gone (key name may survive by design)
    for token in ("sk-abcdef1234567890abcdef", "hunter2hunter", "abcdef1234567890"):
        assert token not in scrubbed, text


# ------------------------------------------------------------ adversarial
# regression (found in the 12-harness self-review)
def test_symlinked_project_memory_md_refused(tmp_path):
    """_parse_markdown_file is the choke point: symlinks refused even for
    direct paths like per-project MEMORY.md."""
    from porter.importers import _parse_markdown_file

    outside = tmp_path / "evil.md"
    outside.write_text("EVIL\n")
    link = tmp_path / "MEMORY.md"
    link.symlink_to(outside)
    assert _parse_markdown_file(link) is None


def test_symlinked_claude_project_memory_md_refused(tmp_path):
    home = tmp_path / ".claude"
    proj = home / "projects" / "-Users-x"
    (proj / "memory").mkdir(parents=True)
    outside = tmp_path / "evil.md"
    outside.write_text("EVIL MEMORY CONTENT\n")
    (proj / "MEMORY.md").symlink_to(outside)
    candidates, _ = ClaudeCodeImporter(home).scan()
    assert all("EVIL" not in c.content for c in candidates)


def test_dry_run_never_prints_raw_secrets(tmp_path, capsys):
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "s.md").write_text("note: api_key = hunter2hunter2 do not share\n")
    rc = import_main(["--harness", "windsurf", "--dry-run", "--windsurf-dir", str(mem)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "hunter2hunter2" not in out
    assert "[REDACTED]" in out


# ------------------------------------------------------------ batch 2 (2026-10-12):
# junie / droid / tabnine / devin / openhands seed importers
@pytest.fixture()
def junie_home(tmp_path):
    j = tmp_path / ".junie"
    j.mkdir()
    (j / "guidelines.md").write_text("# Guidelines\n\nAlways use tabs.\n")
    proj = tmp_path / "proj"
    (proj / ".junie").mkdir(parents=True)
    (proj / ".junie" / "guidelines.md").write_text(
        "# Project guidelines\n\nNo emojis in commit messages.\n"
    )
    return tmp_path


@pytest.fixture()
def droid_home(tmp_path):
    f = tmp_path / ".factory"
    f.mkdir()
    (f / "AGENTS.md").write_text("# Agents\n\nBe concise.\n")
    proj = tmp_path / "droid-proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("# Project agents\n\nUse ripgrep.\n")
    return tmp_path


@pytest.fixture()
def batch2_proj(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".devin").mkdir(parents=True)
    (proj / ".devin" / "rules.md").write_text("# Rules\n\nShip small diffs.\n")
    (proj / "TABNINE.md").write_text("# Tabnine\n\nUse spaces.\n")
    skills = proj / ".agents" / "skills" / "deploy"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# Deploy\n\nRun ./deploy.sh\n")
    return proj


def test_junie_scan_global_and_project(junie_home):
    cands, note = JunieImporter(junie_home / ".junie", [junie_home / "proj"]).scan()
    assert len(cands) == 2
    assert all(c.confidence == 0.4 for c in cands)
    assert all("seed" in c.scope_tags for c in cands)
    assert all(c.source["client_id"] == "junie" for c in cands)
    paths = {c.source["imported_from"].replace("\\", "/") for c in cands}
    assert any(p.endswith(".junie/guidelines.md") for p in paths)


def test_junie_absent_is_normal(tmp_path):
    cands, note = JunieImporter(tmp_path / ".junie").scan()
    assert cands == []


def test_droid_scan(droid_home):
    cands, _ = DroidImporter(droid_home / ".factory", [droid_home / "droid-proj"]).scan()
    assert len(cands) == 2
    assert all("seed" in c.scope_tags for c in cands)
    assert all(c.source["client_id"] == "droid" for c in cands)


def test_tabnine_scan_project_only(batch2_proj, tmp_path):
    cands, _ = TabnineImporter(tmp_path / ".tabnine", [batch2_proj]).scan()
    assert len(cands) == 1
    assert cands[0].source["client_id"] == "tabnine"
    assert "seed" in cands[0].scope_tags


def test_devin_scan_devin_dir(batch2_proj):
    cands, note = DevinImporter([batch2_proj]).scan()
    assert len(cands) == 1
    assert cands[0].confidence == 0.3
    assert "seed" in cands[0].scope_tags
    assert cands[0].source["client_id"] == "devin"
    assert cands[0].source["project_root"] == str(batch2_proj)


def test_devin_no_roots():
    cands, _ = DevinImporter().scan()
    assert cands == []


def test_openhands_skills_as_skill_ref(batch2_proj):
    cands, _ = OpenHandsImporter([batch2_proj]).scan()
    assert len(cands) == 1
    c = cands[0]
    assert c.type == "skill_ref"
    assert {"seed", "skill"} <= set(c.scope_tags)
    assert c.source["client_id"] == "openhands"
    assert c.source["kind"] == "skill"


def test_batch2_importers_never_write_source_files(junie_home, droid_home, batch2_proj):
    """READ-ONLY: source trees byte-identical after scan+import."""

    async def _run():
        targets = [junie_home, droid_home, batch2_proj]
        before = {p: p.read_bytes() for t in targets for p in t.rglob("*.md") if p.is_file()}
        repo = InMemoryMemoryRepository(HashEmbedder())
        importers = [
            JunieImporter(junie_home / ".junie", [junie_home / "proj"]),
            DroidImporter(droid_home / ".factory", [droid_home / "droid-proj"]),
            TabnineImporter(droid_home / ".tabnine", [batch2_proj]),
            DevinImporter([batch2_proj]),
            OpenHandsImporter([batch2_proj]),
        ]
        for imp in importers:
            candidates, _ = imp.scan()
            await run_import(repo, candidates)
        after = {p: p.read_bytes() for t in targets for p in t.rglob("*.md") if p.is_file()}
        assert before == after

    asyncio.run(_run())


# ------------------------------------------------------------ batch 2 CLI
def test_cli_dry_run_batch2(junie_home, batch2_proj, capsys):
    rc = import_main(
        [
            "--harness",
            "junie",
            "--dry-run",
            "--junie-dir",
            str(junie_home / ".junie"),
            "--project-root",
            str(junie_home / "proj"),
        ]
    )
    assert rc == 0
    assert "junie" in capsys.readouterr().out

    rc = import_main(["--harness", "devin", "--dry-run", "--project-root", str(batch2_proj)])
    assert rc == 0
    assert "devin" in capsys.readouterr().out

    rc = import_main(["--harness", "openhands", "--dry-run", "--project-root", str(batch2_proj)])
    assert rc == 0
    assert "openhands" in capsys.readouterr().out


def test_cli_all_includes_batch2(junie_home, batch2_proj, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(junie_home))
    rc = import_main(
        [
            "--harness",
            "all",
            "--dry-run",
            "--junie-dir",
            str(junie_home / ".junie"),
            "--project-root",
            str(batch2_proj),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    for h in ["[junie]", "[droid]", "[tabnine]", "[devin]", "[openhands]"]:
        assert h in out, f"{h} missing from --harness all"


# ------------------------------------------------------------ batch 2 config specifics
def test_zencoder_uses_vendor_key():
    cfg = _load_config(CLIENTS_DIR / "zencoder-settings.json")
    assert "zencoder.mcpServers" in cfg and "mcpServers" not in cfg
    assert cfg["zencoder.mcpServers"]["porter"]["command"] == "python"


def test_droid_has_stdio_type():
    cfg = _load_config(CLIENTS_DIR / "droid-mcp.json")
    assert cfg["mcpServers"]["porter"]["type"] == "stdio"


def test_augment_has_transport_stdio():
    cfg = _load_config(CLIENTS_DIR / "augment-mcp.json")
    assert cfg["mcpServers"]["porter"]["transport"] == "stdio"


def test_antigravity_http_serverurl_and_caveat():
    cfg = _load_config(CLIENTS_DIR / "antigravity-mcp_config.json")
    entry = cfg["mcpServers"]["porter"]
    assert entry["serverUrl"] == "http://127.0.0.1:8000/mcp"
    assert "UNVERIFIED" in cfg["_comment"]


def test_windsurf_header_notes_devin_rename():
    cfg = _load_config(CLIENTS_DIR / "windsurf-mcp_config.json")
    assert "Devin" in cfg["_comment"]


def test_devin_config_exists_alongside_windsurf():
    cfg = _load_config(CLIENTS_DIR / "devin-mcp_config.json")
    assert "porter" in cfg["mcpServers"]


def test_emacs_elisp_shape():
    text = (CLIENTS_DIR / "emacs-gptel-mcp.el").read_text(encoding="utf-8")
    assert "{{PORTER_TOKEN}}" in text
    assert "mcp-hub-servers" in text
    assert "porter.mcp_stdio" in text
    # balanced parens (crude elisp sanity)
    assert text.count("(") == text.count(")")


def test_no_native_store_covers_batch2_implant_only():
    for h in ["continue", "zencoder", "neovim", "emacs", "antigravity", "kiro", "augment"]:
        assert h in NO_NATIVE_STORE, f"{h} missing from NO_NATIVE_STORE"
    # devin/junie/droid/tabnine/openhands have importers -> NOT here
    for h in ["devin", "junie", "droid", "tabnine", "openhands"]:
        assert h not in NO_NATIVE_STORE


# ------------------------------------------------------------ batch 2 adversarial
def test_devin_symlinked_md_refused(tmp_path):
    proj = tmp_path / "proj"
    devin = proj / ".devin"
    devin.mkdir(parents=True)
    outside = tmp_path / "evil.md"
    outside.write_text("EVIL\n")
    (devin / "rules.md").symlink_to(outside)
    cands, _ = DevinImporter([proj]).scan()
    assert all("EVIL" not in c.content for c in cands)


def test_devin_huge_file_skipped(tmp_path):
    devin = tmp_path / "proj" / ".devin"
    devin.mkdir(parents=True)
    big = devin / "big.md"
    big.write_bytes(b"x" * (9 * 1024 * 1024))
    cands, _ = DevinImporter([tmp_path / "proj"]).scan()
    assert cands == []


def test_junie_weird_frontmatter_no_crash(tmp_path):
    j = tmp_path / ".junie"
    j.mkdir()
    (j / "guidelines.md").write_text(
        "---\ntitle: [unclosed\n: bad: : yaml: :\n---\n\nBody text here.\n"
    )
    cands, _ = JunieImporter(j).scan()
    assert len(cands) == 1
    assert "Body text here." in cands[0].content
