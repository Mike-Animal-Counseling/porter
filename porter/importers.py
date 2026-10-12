"""Harness memory importers (PRD §8 F7).

One-time migration: read a coding harness's EXISTING local memory files and
bring them into Porter's pending-review inbox. After the import, incremental
changes flow through the MCP server — the importer is for the backlog.

Design rules (non-negotiable):
- READ-ONLY on harness files. We never write, move, or delete the source.
  Every file is opened with mode "r" only; any failure skips the file.
- Every imported memory lands in the PENDING inbox (never auto-active),
  with ``source.client_id`` = harness name and ``source.imported_from`` =
  the original file path. The user reviews before anything is searchable.
- Secrets scrubbing: a conservative regex pass redacts things that look
  like API keys/tokens BEFORE the content ever reaches Porter. Only the
  redaction COUNT is logged — never the values.
- Honesty over coverage: if a harness has no meaningful portable memory
  store, the importer says so (see ZedImporter) instead of inventing one.

Harness coverage (verified 2026-10-11, extended same day):
- Codex CLI: ``~/.codex/memories/`` — markdown files. FULL importer.
- Claude Code: ``~/.claude/projects/<encoded-cwd>/memory/*.md`` +
  per-project ``MEMORY.md`` + ``~/.claude/CLAUDE.md`` (seed preference).
  FULL importer; project-dir decoding is best-effort (documented below).
- Windsurf: ``~/.codeium/windsurf/memories/*.md``. Best-effort; absence
  is normal and documented.
- Cline: ``<project>/memory-bank/*.md`` (needs --project-root).
- Roo Code: ``<project>/memory-bank/*.md`` + ``.roo/rules/*.md``
  (rules imported as skill_ref).
- Gemini CLI: ``~/.gemini/GEMINI.md`` + ``<project>/GEMINI.md``
  (seed preferences, low confidence).
- Qwen Code: ``~/.qwen/QWEN.md`` (+ ``<project>/QWEN.md``).
- Cursor: NO documented native memory store. Best-effort support for the
  ``.cursor/memories/`` community convention (per-project markdown dir).
- Zed: NO native memory store at all. Honest no-op with documentation.
- OpenCode / Amp / Kilo Code / Kimi CLI / Copilot CLI / Aider: NO native
  portable memory store — MCP implant only (see NO_NATIVE_STORE).

Official data-export importers (best-effort, read-only on the export file):
- ChatGPT: ``conversations.json`` from the official export (direct ``.json``
  or the whole export ``.zip`` — parsed in memory, never extracted).
  One ``episode`` summary per conversation + one ``fact`` for the first
  user message ONLY if it matches the durable-statement heuristic.
- Claude: same ``conversations.json`` shape from Claude's export; both
  shapes are parsed defensively either way.
"""
from __future__ import annotations

import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

REDACTED = "[REDACTED]"

# ---------------------------------------------------------------------------
# Secrets scrubbing (conservative: prefer false negatives over mangling text)
# ---------------------------------------------------------------------------
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # provider-prefixed keys: redact the whole token
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "openai_key"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b"), "anthropic_key"),
    (re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), "github_token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "github_pat"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws_key_id"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"), "slack_token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     "private_key_block"),
]

# key = value assignments: keep the key name, redact the value.
_KV_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|apikey|secret|client[_-]?secret|auth[_-]?token"
    r"|access[_-]?token|password|passwd|pwd)\s*[:=]\s*['\"]?"
    r"([^\s'\";]{8,})['\"]?"
)


def scrub_secrets(text: str) -> tuple[str, int]:
    """Redact secret-looking values. Returns (scrubbed_text, redaction_count).

    Only counts are ever reported — values are never logged or returned.
    """
    count = 0

    def _kv_repl(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{m.group(1)}={REDACTED}"

    text = _KV_PATTERN.sub(_kv_repl, text)
    for pattern, _label in _SECRET_PATTERNS:
        text, n = pattern.subn(REDACTED, text)
        count += n
    return text, count


# ---------------------------------------------------------------------------
# Candidate model
# ---------------------------------------------------------------------------
@dataclass
class ImportCandidate:
    """One memory parsed from a harness file, ready for the inbox."""

    title: str
    content: str
    type: str = "fact"  # preference | fact | episode | skill_ref
    scope_tags: list[str] = field(default_factory=list)
    confidence: float = 0.5  # imports are never fully trusted
    source: dict[str, Any] = field(default_factory=dict)
    imported_from: str = ""
    redactions: int = 0  # filled by the scrub pass at import time


_VALID_TYPES = ("preference", "fact", "episode", "skill_ref")


def import_dedupe_key(source: Mapping[str, Any]) -> str:
    """Layer-1 dedupe key for an import candidate's source dict.

    Normally the ``imported_from`` path (one file = one memory). Export
    importers emit several memories per file, so when ``conversation_id``
    (and ``kind``) are present they join the key — otherwise a whole
    export would collapse to its first conversation on re-import.
    """
    base = str(source.get("imported_from") or "")
    cid = source.get("conversation_id")
    if not cid:
        return base
    key = f"{base}#conv:{cid}"
    kind = source.get("kind")
    return f"{key}:{kind}" if kind else key

# Largest single file the importer will read (DoS guard: a runaway agent log
# renamed .md must not OOM the import; real memory files are kilobytes).
_MAX_IMPORT_FILE_BYTES = 8 * 1024 * 1024


def _parse_markdown_file(path: Path) -> ImportCandidate | None:
    """Robust markdown parse: optional YAML frontmatter + body.

    Never assumes an exact schema. Title precedence: frontmatter `title` >
    first `# heading` > filename stem. Returns None for empty/unreadable.
    READ-ONLY: opens the file in text-read mode only. Symlinks are refused
    (callers scanning directories already skip them; this is the choke
    point for direct paths like ``MEMORY.md`` / ``CLAUDE.md``).
    """
    try:
        if path.is_symlink():
            return None
        if path.stat().st_size > _MAX_IMPORT_FILE_BYTES:
            return None  # runaway log renamed .md: skip, don't OOM the import
        raw = path.open("r", encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    if not raw.strip():
        return None

    front: dict[str, str] = {}
    body = raw
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            fm_text = raw[3:end]
            body = raw[end + 4 :].lstrip("\n")
            for line in fm_text.splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    front[k.strip().lower()] = v.strip().strip("'\"")

    title = front.get("title", "")
    try:
        mtime = datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).isoformat()
    except OSError:
        mtime = ""
    lines = body.splitlines()
    if not title and lines and lines[0].lstrip().startswith("#"):
        title = lines[0].lstrip("# ").strip()
        body = "\n".join(lines[1:]).lstrip("\n")
    if not title:
        title = path.stem.replace("-", " ").replace("_", " ").strip() or path.name
    body = body.strip()
    content = body if body else title
    if len(content) > 20_000:  # same cap as the REST API
        content = content[:20_000]

    mem_type = front.get("type", "fact")
    if mem_type not in _VALID_TYPES:
        mem_type = "fact"
    tags: list[str] = []
    raw_tags = front.get("tags", "")
    if raw_tags:
        tags = [
            t.strip().lower().replace(" ", "_")
            for t in re.split(r"[,\s]+", raw_tags)
            if t.strip() and len(t.strip()) <= 64
        ][:20]

    return ImportCandidate(
        title=title[:200],
        content=content,
        type=mem_type,
        scope_tags=tags,
        confidence=0.5,
        source={"mtime": mtime},
        imported_from=str(path),
    )


def _iter_md_files(root: Path):
    """Yield ``*.md`` files under *root*, skipping symlinks.

    Defense: a symlink inside a harness directory (malicious or stale)
    must not pull arbitrary outside files into the import. Files under
    symlinked subdirectories are skipped too (resolve-and-contain check).
    """
    if not root.is_dir() or root.is_symlink():
        return
    try:
        anchor = root.resolve()
    except OSError:
        return
    for path in sorted(root.rglob("*.md")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            path.resolve().relative_to(anchor)
        except (OSError, ValueError):
            continue  # escapes root via a symlinked parent dir
        yield path


class HarnessImporter:
    """Base: scan() -> (candidates, note). Subclasses are read-only."""

    harness: str = "unknown"

    def scan(self) -> tuple[list[ImportCandidate], str]:
        raise NotImplementedError


class CodexImporter(HarnessImporter):
    """Codex CLI: reads ``~/.codex/memories/**/*.md`` (markdown files)."""

    harness = "codex"

    def __init__(self, memories_dir: str | os.PathLike[str] | None = None):
        self.memories_dir = Path(
            memories_dir or os.path.expanduser("~/.codex/memories")
        )

    def scan(self) -> tuple[list[ImportCandidate], str]:
        if not self.memories_dir.is_dir():
            return [], (
                f"no Codex memories directory at {self.memories_dir} — "
                "nothing to import (Codex may never have saved memories, "
                "or CODEX_HOME points elsewhere)"
            )
        candidates: list[ImportCandidate] = []
        for path in _iter_md_files(self.memories_dir):
            cand = _parse_markdown_file(path)
            if cand is None:
                continue
            cand.source.update(
                {
                    "client_id": "codex",
                    "imported_from": str(path),
                    "imported_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            candidates.append(cand)
        return candidates, f"scanned {self.memories_dir}: {len(candidates)} memories"


class CursorImporter(HarnessImporter):
    """Cursor: NO documented native memory store.

    Cursor's "Memories" feature has no filesystem path, schema, or export
    API (verified 2026-10-11). This importer therefore only covers the
    ``.cursor/memories/`` community convention (per-project markdown dirs,
    e.g. from cursor-commands-style skills). Pass project roots explicitly.
    """

    harness = "cursor"

    def __init__(self, project_roots: list[str | os.PathLike[str]] | None = None):
        self.project_roots = [Path(p) for p in (project_roots or [])]

    def scan(self) -> tuple[list[ImportCandidate], str]:
        candidates: list[ImportCandidate] = []
        scanned: list[str] = []
        for root in self.project_roots:
            mem_dir = root / ".cursor" / "memories"
            scanned.append(str(mem_dir))
            if not mem_dir.is_dir():
                continue
            for path in _iter_md_files(mem_dir):
                cand = _parse_markdown_file(path)
                if cand is None:
                    continue
                cand.source.update(
                    {
                        "client_id": "cursor",
                        "imported_from": str(path),
                        "imported_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                candidates.append(cand)
        note = (
            "Cursor has no native portable memory store; scanned community "
            f"convention dirs {scanned}: {len(candidates)} memories"
        )
        return candidates, note


class ZedImporter(HarnessImporter):
    """Zed: NO native memory store. Honest no-op.

    Zed keeps AI context in rules files (``.rules`` / ``AGENTS.md`` /
    ``~/.config/zed/AGENTS.md``) and editor state — neither is a memory
    store with a portable schema. Importing rules as "memories" would
    pollute the inbox with instructions, so we deliberately do NOT do that.
    Memories live in Porter going forward via the MCP server.
    """

    harness = "zed"

    def scan(self) -> tuple[list[ImportCandidate], str]:
        return [], (
            "Zed has no native portable memory store (verified 2026-10-11): "
            "nothing to import. Going forward, Zed writes memories to Porter "
            "through the MCP server instead."
        )


def _decode_claude_project_dir(name: str) -> str:
    """Best-effort decode of Claude Code's ``<encoded-cwd>`` directory name.

    Claude Code stores per-project data under ``~/.claude/projects/`` with
    the working directory encoded (observed form: ``-Users-name-project``
    for ``/Users/name/project``). The exact algorithm is internal and may
    change; this heuristic strips the leading dash and restores slashes.
    Original dashes in path components are ambiguous and cannot be
    recovered — the raw encoded name is always kept in
    ``source.project_dir`` alongside this guess.
    """
    stripped = name[1:] if name.startswith("-") else name
    return "/" + stripped.replace("-", "/")


def _seed_candidate(
    path: Path,
    *,
    client_id: str,
    confidence: float,
    tags: list[str],
    mem_type: str = "preference",
) -> ImportCandidate | None:
    """Parse an instruction/config-style file as a low-confidence seed.

    Files like CLAUDE.md / GEMINI.md are instructions, not episodic
    memories — they enter the inbox as ``preference`` seeds the user can
    keep or discard. Tagged ``seed`` so the panel can filter them.
    """
    cand = _parse_markdown_file(path)
    if cand is None:
        return None
    cand.type = mem_type
    cand.confidence = confidence
    cand.scope_tags = sorted(set(cand.scope_tags) | set(tags) | {"seed"})
    cand.source.update(
        {
            "client_id": client_id,
            "imported_from": str(path),
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "seed": True,
        }
    )
    return cand


def _auto_memory_dir(claude_home: Path) -> Path | None:
    """Read ``autoMemoryDirectory`` from ``~/.claude/settings.json`` if set.

    Best-effort: any parse failure -> None (the default locations are still
    scanned). Never raises.
    """
    try:
        import json

        settings = json.loads(
            (claude_home / "settings.json").read_text(encoding="utf-8")
        )
        raw = settings.get("autoMemoryDirectory")
        if isinstance(raw, str) and raw.strip():
            p = Path(os.path.expanduser(raw.strip()))
            return p if p.is_dir() else None
    except (OSError, ValueError):
        pass
    return None


class ClaudeCodeImporter(HarnessImporter):
    """Claude Code: ``~/.claude/projects/<encoded-cwd>/memory/*.md``,
    per-project ``MEMORY.md``, plus ``~/.claude/CLAUDE.md`` as a seed.

    Project directories are decoded best-effort (see
    :func:`_decode_claude_project_dir`); both the guess (``source.project``)
    and the raw name (``source.project_dir``) are recorded.
    """

    harness = "claude-code"

    def __init__(self, claude_home: str | os.PathLike[str] | None = None):
        self.claude_home = Path(
            claude_home or os.path.expanduser("~/.claude")
        )

    def scan(self) -> tuple[list[ImportCandidate], str]:
        candidates: list[ImportCandidate] = []
        scanned: list[str] = []
        projects = self.claude_home / "projects"
        if projects.is_dir():
            for proj in sorted(projects.iterdir()):
                if not proj.is_dir() or proj.is_symlink():
                    continue
                decoded = _decode_claude_project_dir(proj.name)
                mem_dir = proj / "memory"
                scanned.append(str(mem_dir))
                for path in _iter_md_files(mem_dir):
                    cand = _parse_markdown_file(path)
                    if cand is None:
                        continue
                    cand.source.update(
                        {
                            "client_id": "claude-code",
                            "imported_from": str(path),
                            "imported_at": datetime.now(
                                timezone.utc
                            ).isoformat(),
                            "project": decoded,
                            "project_dir": proj.name,
                        }
                    )
                    candidates.append(cand)
                # Per-project MEMORY.md (sibling of the memory/ dir).
                top = _parse_markdown_file(proj / "MEMORY.md")
                if top is not None:
                    top.source.update(
                        {
                            "client_id": "claude-code",
                            "imported_from": str(proj / "MEMORY.md"),
                            "imported_at": datetime.now(
                                timezone.utc
                            ).isoformat(),
                            "project": decoded,
                            "project_dir": proj.name,
                        }
                    )
                    candidates.append(top)
        # autoMemoryDirectory override (scanned additionally, tagged).
        override = _auto_memory_dir(self.claude_home)
        if override is not None:
            scanned.append(f"{override} (autoMemoryDirectory)")
            for path in _iter_md_files(override):
                cand = _parse_markdown_file(path)
                if cand is None:
                    continue
                cand.source.update(
                    {
                        "client_id": "claude-code",
                        "imported_from": str(path),
                        "imported_at": datetime.now(timezone.utc).isoformat(),
                        "auto_memory_dir": True,
                    }
                )
                candidates.append(cand)
        # Global CLAUDE.md as a low-confidence seed preference.
        seed = self.claude_home / "CLAUDE.md"
        if seed.is_file() and not seed.is_symlink():
            cand = _seed_candidate(
                seed, client_id="claude-code", confidence=0.3, tags=[]
            )
            if cand is not None:
                candidates.append(cand)
        note = (
            f"scanned Claude Code locations {scanned or ['(none found)']}: "
            f"{len(candidates)} memories "
            "(project-dir decoding is best-effort)"
        )
        return candidates, note


class WindsurfImporter(HarnessImporter):
    """Windsurf: ``~/.codeium/windsurf/memories/*.md``.

    Best-effort: Windsurf documents no public memory schema, and the
    directory is frequently absent (memories live server-side for many
    users). Absence is normal — reported, not an error.
    """

    harness = "windsurf"

    def __init__(self, memories_dir: str | os.PathLike[str] | None = None):
        self.memories_dir = Path(
            memories_dir
            or os.path.expanduser("~/.codeium/windsurf/memories")
        )

    def scan(self) -> tuple[list[ImportCandidate], str]:
        if not self.memories_dir.is_dir():
            return [], (
                f"no Windsurf memories at {self.memories_dir} — nothing to "
                "import (absence is normal; Windsurf keeps most memories "
                "server-side)"
            )
        candidates: list[ImportCandidate] = []
        for path in _iter_md_files(self.memories_dir):
            cand = _parse_markdown_file(path)
            if cand is None:
                continue
            cand.source.update(
                {
                    "client_id": "windsurf",
                    "imported_from": str(path),
                    "imported_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            candidates.append(cand)
        return (
            candidates,
            f"scanned {self.memories_dir}: {len(candidates)} memories",
        )


class _MemoryBankImporter(HarnessImporter):
    """Shared base: ``<project>/memory-bank/*.md`` (Cline / Roo Code)."""

    harness = "memory-bank"

    def __init__(
        self,
        project_roots: list[str | os.PathLike[str]] | None = None,
        *,
        client_id: str,
    ):
        self.project_roots = [Path(p) for p in (project_roots or [])]
        self._client_id = client_id

    def _tag_candidate(
        self, cand: ImportCandidate, path: Path, **extra: Any
    ) -> None:
        cand.source.update(
            {
                "client_id": self._client_id,
                "imported_from": str(path),
                "imported_at": datetime.now(timezone.utc).isoformat(),
                **extra,
            }
        )

    def scan(self) -> tuple[list[ImportCandidate], str]:
        candidates: list[ImportCandidate] = []
        scanned: list[str] = []
        for root in self.project_roots:
            mem_dir = root / "memory-bank"
            scanned.append(str(mem_dir))
            if not mem_dir.is_dir():
                continue
            for path in _iter_md_files(mem_dir):
                cand = _parse_markdown_file(path)
                if cand is None:
                    continue
                self._tag_candidate(cand, path, project_root=str(root))
                candidates.append(cand)
        return candidates, (
            f"scanned memory-bank dirs {scanned or ['(no project roots)']}: "
            f"{len(candidates)} memories"
        )


class ClineImporter(_MemoryBankImporter):
    """Cline: ``<project>/memory-bank/*.md``. Needs --project-root."""

    harness = "cline"

    def __init__(
        self, project_roots: list[str | os.PathLike[str]] | None = None
    ):
        super().__init__(project_roots, client_id="cline")


class RooImporter(_MemoryBankImporter):
    """Roo Code: ``<project>/memory-bank/*.md`` + ``.roo/rules/*.md``.

    Rules files are imported as ``skill_ref`` (they are durable
    instructions, not episodic memories), tagged ``rule``.
    """

    harness = "roo"

    def __init__(
        self, project_roots: list[str | os.PathLike[str]] | None = None
    ):
        super().__init__(project_roots, client_id="roo")

    def scan(self) -> tuple[list[ImportCandidate], str]:
        candidates, note = super().scan()
        scanned_rules: list[str] = []
        for root in self.project_roots:
            rules_dir = root / ".roo" / "rules"
            scanned_rules.append(str(rules_dir))
            if not rules_dir.is_dir():
                continue
            for path in _iter_md_files(rules_dir):
                cand = _parse_markdown_file(path)
                if cand is None:
                    continue
                cand.type = "skill_ref"
                cand.scope_tags = sorted(set(cand.scope_tags) | {"rule"})
                self._tag_candidate(
                    cand, path, project_root=str(root), kind="rule"
                )
                candidates.append(cand)
        return candidates, (
            f"{note}; rules {scanned_rules or ['(no project roots)']}: "
            f"{len(candidates)} total"
        )


class _GlobalMdSeedImporter(HarnessImporter):
    """Shared base: ``~/.<tool>/<NAME>.md`` seed-preference importers."""

    harness = "seed"
    tool_dir_name = ""
    file_name = ""
    # Project-level path when it differs from the bare file name
    # (e.g. Junie keeps project guidelines at <project>/.junie/guidelines.md).
    project_file_name: str | None = None
    seed_confidence = 0.4

    def __init__(
        self,
        home_dir: str | os.PathLike[str] | None = None,
        project_roots: list[str | os.PathLike[str]] | None = None,
    ):
        self.home_dir = Path(
            home_dir or os.path.expanduser(f"~/.{self.tool_dir_name}")
        )
        self.project_roots = [Path(p) for p in (project_roots or [])]

    def scan(self) -> tuple[list[ImportCandidate], str]:
        candidates: list[ImportCandidate] = []
        seen = [str(self.home_dir / self.file_name)]
        home_seed = self.home_dir / self.file_name
        if not home_seed.is_symlink():  # same symlink guard as elsewhere
            seed = _seed_candidate(
                home_seed,
                client_id=self.harness,
                confidence=self.seed_confidence,
                tags=[],
            )
            if seed is not None:
                candidates.append(seed)
        for root in self.project_roots:
            project_file = self.project_file_name or self.file_name
            p = root / project_file
            seen.append(str(p))
            if p.is_symlink():
                continue
            cand = _seed_candidate(
                p,
                client_id=self.harness,
                confidence=self.seed_confidence,
                tags=[],
            )
            if cand is not None:
                cand.source["project_root"] = str(root)
                candidates.append(cand)
        return candidates, (
            f"scanned {seen}: {len(candidates)} seed preferences "
            f"(confidence {self.seed_confidence})"
        )


class GeminiImporter(_GlobalMdSeedImporter):
    """Gemini CLI: ``~/.gemini/GEMINI.md`` + ``<project>/GEMINI.md``.

    Imported as low-confidence seed preferences — these files are
    instructions, not episodic memories.
    """

    harness = "gemini"
    tool_dir_name = "gemini"
    file_name = "GEMINI.md"
    seed_confidence = 0.4


class QwenImporter(_GlobalMdSeedImporter):
    """Qwen Code: ``~/.qwen/QWEN.md`` (+ ``<project>/QWEN.md``).

    Same treatment as Gemini: low-confidence seed preferences.
    """

    harness = "qwen"
    tool_dir_name = "qwen"
    file_name = "QWEN.md"
    seed_confidence = 0.4


class JunieImporter(_GlobalMdSeedImporter):
    """JetBrains Junie: ``~/.junie/guidelines.md`` + ``<project>/.junie/guidelines.md``.

    Guidelines are instructions, not episodic memories — imported as
    low-confidence seed preferences.
    """

    harness = "junie"
    tool_dir_name = "junie"
    file_name = "guidelines.md"
    project_file_name = ".junie/guidelines.md"
    seed_confidence = 0.4


class DroidImporter(_GlobalMdSeedImporter):
    """Factory Droid: ``~/.factory/AGENTS.md`` (+ ``<project>/AGENTS.md``).

    AGENTS.md files are instructions — imported as low-confidence seed
    preferences.
    """

    harness = "droid"
    tool_dir_name = "factory"
    file_name = "AGENTS.md"
    seed_confidence = 0.4


class TabnineImporter(_GlobalMdSeedImporter):
    """Tabnine: ``<project>/TABNINE.md``.

    Project convention file; there is no global equivalent (the
    ``~/.tabnine/TABNINE.md`` probe is harmless and simply yields nothing).
    Imported as a low-confidence seed preference.
    """

    harness = "tabnine"
    tool_dir_name = "tabnine"
    file_name = "TABNINE.md"
    seed_confidence = 0.4


class DevinImporter(HarnessImporter):
    """Devin (incl. Devin Desktop, formerly Windsurf): ``<project>/.devin/**/*.md``.

    Rules/skills markdown under ``.devin/`` is imported as low-confidence
    seed preferences (confidence 0.3 — these dirs can be noisy). Needs
    --project-root. Symlink-safe via _iter_md_files.
    """

    harness = "devin"
    seed_confidence = 0.3

    def __init__(
        self, project_roots: list[str | os.PathLike[str]] | None = None
    ):
        self.project_roots = [Path(p) for p in (project_roots or [])]

    def scan(self) -> tuple[list[ImportCandidate], str]:
        candidates: list[ImportCandidate] = []
        scanned: list[str] = []
        for root in self.project_roots:
            devin_dir = root / ".devin"
            scanned.append(str(devin_dir))
            if not devin_dir.is_dir():
                continue
            for path in _iter_md_files(devin_dir):
                cand = _seed_candidate(
                    path,
                    client_id=self.harness,
                    confidence=self.seed_confidence,
                    tags=[],
                )
                if cand is None:
                    continue
                cand.source["project_root"] = str(root)
                candidates.append(cand)
        return candidates, (
            f"scanned .devin dirs {scanned or ['(no project roots)']}: "
            f"{len(candidates)} seed preferences "
            f"(confidence {self.seed_confidence})"
        )


class OpenHandsImporter(HarnessImporter):
    """OpenHands: ``<project>/.agents/skills/*/SKILL.md``.

    Skill files are durable instructions — imported as ``skill_ref``
    (tagged ``seed`` + ``skill``), confidence 0.4. Needs --project-root.
    """

    harness = "openhands"
    seed_confidence = 0.4

    def __init__(
        self, project_roots: list[str | os.PathLike[str]] | None = None
    ):
        self.project_roots = [Path(p) for p in (project_roots or [])]

    def scan(self) -> tuple[list[ImportCandidate], str]:
        candidates: list[ImportCandidate] = []
        scanned: list[str] = []
        for root in self.project_roots:
            skills_dir = root / ".agents" / "skills"
            scanned.append(str(skills_dir))
            if not skills_dir.is_dir():
                continue
            for path in _iter_md_files(skills_dir):
                cand = _parse_markdown_file(path)
                if cand is None:
                    continue
                cand.type = "skill_ref"
                cand.confidence = self.seed_confidence
                cand.scope_tags = sorted(
                    set(cand.scope_tags) | {"seed", "skill"}
                )
                cand.source.update(
                    {
                        "client_id": self.harness,
                        "imported_from": str(path),
                        "imported_at": datetime.now(timezone.utc).isoformat(),
                        "project_root": str(root),
                        "kind": "skill",
                    }
                )
                candidates.append(cand)
        return candidates, (
            f"scanned OpenHands skill dirs {scanned or ['(no project roots)']}: "
            f"{len(candidates)} skill_refs (confidence {self.seed_confidence})"
        )


# ---------------------------------------------------------------------------
# Official data-export importers: ChatGPT / Claude conversations.json
# ---------------------------------------------------------------------------

# Largest export payload the importer will parse (DoS guard). Official
# exports can be tens of MB for heavy users; 200MB keeps real exports
# working while bounding json-parsing memory.
_MAX_EXPORT_BYTES = 200 * 1024 * 1024

# Zip guards (zip bombs): max entry count and max TOTAL claimed
# uncompressed size. We sum ZipInfo.file_size BEFORE decompressing
# anything, and additionally cap the actual read — a lying header cannot
# force more bytes out of us than _MAX_EXPORT_BYTES.
_MAX_ZIP_ENTRIES = 100_000
_MAX_ZIP_UNCOMPRESSED = 200 * 1024 * 1024

# Per-message cap, mirrors the API content cap (20k).
_MAX_MESSAGE_CHARS = 20_000

# First-user-message chars kept for a fact candidate (task spec).
_FACT_CHARS = 2000


def _read_export_bytes(path: Path) -> tuple[bytes | None, str]:
    """Read conversations.json from a .json path or a .zip (in memory).

    Returns (bytes, "") on success, (None, warning) on any failure.
    READ-ONLY: the file is never written to; zip members are never
    extracted to disk (zip-slip is impossible by construction).
    """
    try:
        if path.is_symlink():
            return None, f"{path}: symlinks are refused — skipped"
    except OSError:
        return None, f"{path}: unreadable — skipped"
    suffix = path.suffix.lower()
    if suffix == ".zip":
        return _read_conversations_from_zip(path)
    if suffix != ".json":
        return None, (
            f"{path}: expected a .json file or a .zip containing "
            "conversations.json — skipped"
        )
    try:
        if path.stat().st_size > _MAX_EXPORT_BYTES:
            return None, (
                f"{path}: file exceeds "
                f"{_MAX_EXPORT_BYTES // (1024 * 1024)}MB — skipped"
            )
        return path.read_bytes(), ""
    except OSError as exc:
        return None, f"{path}: unreadable ({exc}) — skipped"


def _read_conversations_from_zip(path: Path) -> tuple[bytes | None, str]:
    """Find conversations.json in a zip and read it WITHOUT extracting."""
    try:
        if path.stat().st_size > _MAX_ZIP_UNCOMPRESSED:
            return None, f"{path}: zip file too large — skipped"
        zf = zipfile.ZipFile(path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        return None, f"{path}: not a readable zip ({exc}) — skipped"
    try:
        try:
            infos = zf.infolist()
        except (zipfile.BadZipFile, RuntimeError) as exc:
            return None, f"{path}: cannot list zip entries ({exc}) — skipped"
        if len(infos) > _MAX_ZIP_ENTRIES:
            return None, (
                f"{path}: zip has {len(infos)} entries "
                f"(>{_MAX_ZIP_ENTRIES}) — rejected as a precaution"
            )
        total = 0
        for info in infos:
            total += info.file_size
            if total > _MAX_ZIP_UNCOMPRESSED:
                return None, (
                    f"{path}: zip claims >"
                    f"{_MAX_ZIP_UNCOMPRESSED // (1024 * 1024)}MB "
                    "uncompressed — rejected (zip-bomb guard)"
                )
        target = None
        for info in infos:
            if info.is_dir():
                continue
            # Match by basename only; the file is read in memory, never
            # written out, so hostile entry paths cannot escape anywhere.
            if info.filename.replace("\\", "/").rsplit("/", 1)[-1] == (
                "conversations.json"
            ):
                target = info
                break
        if target is None:
            return None, f"{path}: no conversations.json in zip — skipped"
        if target.file_size > _MAX_EXPORT_BYTES:
            return None, (
                f"{path}: conversations.json claims "
                f">{_MAX_EXPORT_BYTES // (1024 * 1024)}MB — rejected"
            )
        try:
            with zf.open(target, "r") as fh:
                data = fh.read(_MAX_EXPORT_BYTES + 1)
        except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
            return None, (
                f"{path}: cannot read conversations.json ({exc}) — skipped"
            )
        if len(data) > _MAX_EXPORT_BYTES:
            return None, (
                f"{path}: conversations.json exceeds size cap — rejected"
            )
        return data, ""
    finally:
        zf.close()


def _clean_text(text: str) -> str:
    """Strip null bytes and cap length. Never raises on hostile input."""
    if not isinstance(text, str):
        return ""
    text = text.replace("\x00", "")
    if len(text) > _MAX_MESSAGE_CHARS:
        text = text[:_MAX_MESSAGE_CHARS]
    return text


def _to_datetime(ts: Any) -> datetime | None:
    """Epoch seconds (ChatGPT) or ISO-8601 (Claude) -> aware datetime."""
    if isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def _date_range(dts: list[datetime | None]) -> str:
    known = [d for d in dts if d is not None]
    if not known:
        return "dates unknown"
    start, end = min(known).date().isoformat(), max(known).date().isoformat()
    return start if start == end else f"{start} → {end}"


def _extract_chatgpt_messages(conv: dict) -> list[tuple[str, str, Any]]:
    """ChatGPT shape: conv["mapping"] is {node_id: node}.

    Returns [(role, text, raw_timestamp)] ordered by create_time.
    Defensive: nodes without a message, non-text content types, and
    non-string parts (multimodal payloads) are skipped, never crash.
    """
    mapping = conv.get("mapping")
    if not isinstance(mapping, dict):
        return []
    out: list[tuple[str, str, Any]] = []
    for node in mapping.values():
        if not isinstance(node, dict):
            continue
        msg = node.get("message")
        if not isinstance(msg, dict):
            continue  # e.g. the root node has message: null
        author = msg.get("author")
        role = author.get("role") if isinstance(author, dict) else None
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        text = ""
        if isinstance(content, dict):
            parts = content.get("parts")
            if isinstance(parts, list):
                text = "".join(p for p in parts if isinstance(p, str))
        text = _clean_text(text)
        if not text.strip():
            continue
        out.append((role, text, msg.get("create_time")))
    # Sort by timestamp, oldest first. Timestamps in the wild are mixed
    # (epoch numbers, ISO strings, nulls, garbage): normalize defensively
    # so one hostile node cannot TypeError-crash the whole import. Unknown
    # timestamps sort last; the sort is stable so ties keep file order.
    def _sort_key(m: tuple[str, str, Any]) -> tuple[bool, float]:
        dt = _to_datetime(m[2])
        return (dt is None, dt.timestamp() if dt is not None else 0.0)

    out.sort(key=_sort_key)
    return out


def _extract_claude_messages(conv: dict) -> list[tuple[str, str, Any]]:
    """Claude shape: conv["chat_messages"] is a list of message dicts."""
    raw = conv.get("chat_messages")
    if not isinstance(raw, list):
        return []
    out: list[tuple[str, str, Any]] = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        sender = m.get("sender")
        role = {"human": "user", "assistant": "assistant"}.get(sender)
        if role is None:
            continue
        text = _clean_text(m.get("text"))
        if not text.strip():
            continue
        out.append((role, text, m.get("created_at")))
    return out


def _parse_export_conversation(conv: Any, index: int):
    """Return (conversation_id, title, messages) or None if malformed.

    Shape-agnostic: tries the ChatGPT ``mapping`` shape first, then the
    Claude ``chat_messages`` shape. A non-dict entry or an entry with
    neither shape -> None (skipped with a count, never a crash).
    """
    if not isinstance(conv, dict):
        return None
    cid = conv.get("id") or conv.get("conversation_id") or conv.get("uuid")
    cid = str(cid) if cid is not None else f"conv-{index}"
    title = conv.get("title") or conv.get("name") or "Untitled conversation"
    title = str(title).strip() or "Untitled conversation"
    messages = _extract_chatgpt_messages(conv) or _extract_claude_messages(conv)
    return cid, title, messages


# A first user message becomes a ``fact`` candidate ONLY if it matches one
# of these (case-insensitive) patterns. This is a RECALL-ORIENTED keyword
# heuristic, not NLP, and its limits are honest:
# - misses paraphrases ("call me Alex" has no "my name is");
# - catches some transient sentences ("I am writing an essay on X"
#   matches "i am a"? No — the word boundary saves that one — but "I am
#   a student writing..." matches and is arguably durable anyway).
# False positives are cheap (the fact still needs inbox approval); false
# negatives only lose the fact candidate — the episode summary remains.
DURABLE_STATEMENT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bmy name is\b",
        r"\bi['\u2019]m a\b",
        r"\bi am a\b",
        r"\bi live in\b",
        r"\bi work (at|as|for)\b",
        r"\bmy job is\b",
        r"\bmy birthday\b",
        r"\bmy (favorite|favourite)\b",
        r"\bi (like|love|prefer)\b",
        r"\bi (hate|dislike)\b",
        r"\bi don['\u2019]t like\b",
        r"\bremember (that|to)\b",
        r"\bfrom now on\b",
        r"\bgoing forward\b",
        r"\bkeep in mind\b",
        r"\bplease always\b",
    )
)


def _looks_durable(text: str) -> bool:
    return any(p.search(text) for p in DURABLE_STATEMENT_PATTERNS)


class _ChatExportImporter(HarnessImporter):
    """Shared base for official ChatGPT / Claude data-export importers.

    Reads ``conversations.json`` from the official export — either directly
    as a ``.json`` file or inside a ``.zip`` (parsed in memory, never
    extracted to disk). READ-ONLY: the export file is never modified.

    What's extracted (honest, conservative):
    - ONE ``episode`` memory per conversation: a summary line
      ``"<Product> conversation '<title>' (<n> messages, <date range>)"``
      where n counts user+assistant messages. Full transcripts are
      deliberately NOT imported — they are long, mostly non-durable, and
      would flood the inbox.
    - Optionally ONE ``fact`` memory: the FIRST user message, truncated to
      2000 chars, ONLY if it matches the durable-statement heuristic
      (see DURABLE_STATEMENT_PATTERNS). Everything else in the
      conversation is skipped.

    Both land in the pending inbox at confidence=0.5 with
    ``source.client_id`` = "chatgpt"/"claude", ``source.imported_from`` =
    the export path, ``source.conversation_id`` = the conversation's id,
    and ``source.kind`` = "episode"/"fact" (so re-imports dedupe per
    conversation, not per file). Best-effort: review everything in the
    inbox.
    """

    harness = "export"
    client_id = "export"
    product_name = "Export"

    def __init__(self, export_path: str | os.PathLike[str] | None = None):
        self.export_path = Path(export_path) if export_path else None

    def _candidates_for(
        self, cid: str, title: str, messages: list[tuple[str, str, Any]]
    ) -> list[ImportCandidate]:
        n = len(messages)
        dates = _date_range([_to_datetime(ts) for _, _, ts in messages])
        short_title = title if len(title) <= 120 else title[:117] + "..."
        episode_content = (
            f"{self.product_name} conversation '{short_title}' "
            f"({n} messages, {dates})"
        )
        base_source = {
            "client_id": self.client_id,
            "imported_from": str(self.export_path),
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "conversation_id": cid,
        }
        cands = [
            ImportCandidate(
                title=f"{self.product_name}: {title}"[:200],
                content=episode_content,
                type="episode",
                confidence=0.5,
                source={**base_source, "kind": "episode"},
                imported_from=str(self.export_path),
            )
        ]
        first_user = next((t for role, t, _ in messages if role == "user"),
                          None)
        if first_user and _looks_durable(first_user):
            preview = " ".join(first_user.split())
            cands.append(
                ImportCandidate(
                    title=(preview[:77] + "...") if len(preview) > 80
                    else preview,
                    content=first_user[:_FACT_CHARS],
                    type="fact",
                    confidence=0.5,
                    source={**base_source, "kind": "fact"},
                    imported_from=str(self.export_path),
                )
            )
        return cands

    def scan(self) -> tuple[list[ImportCandidate], str]:
        if self.export_path is None:
            return [], "no export file given (pass --file ...) — skipped"
        raw, warning = _read_export_bytes(self.export_path)
        if raw is None:
            return [], warning
        try:
            # errors="replace" for non-UTF8 bytes; strip null bytes that
            # would otherwise survive into stored content.
            data = json.loads(
                raw.decode("utf-8", errors="replace").replace("\x00", "")
            )
        except (ValueError, RecursionError, MemoryError) as exc:
            return [], (
                f"{self.export_path}: not parseable as JSON "
                f"({type(exc).__name__}) — skipped, nothing imported"
            )
        if not isinstance(data, list):
            return [], (
                f"{self.export_path}: top level is "
                f"{type(data).__name__}, not a list of conversations — "
                "skipped (matches neither the ChatGPT nor the Claude "
                "export shape)"
            )
        candidates: list[ImportCandidate] = []
        empty = malformed = 0
        for i, conv in enumerate(data):
            parsed = _parse_export_conversation(conv, i)
            if parsed is None:
                malformed += 1
                continue
            cid, title, messages = parsed
            if not messages:
                empty += 1  # no user/assistant text: nothing worth indexing
                continue
            candidates.extend(self._candidates_for(cid, title, messages))
        note = (
            f"scanned {self.export_path}: {len(data)} conversations, "
            f"{len(candidates)} memories "
            f"({empty} empty, {malformed} malformed skipped)"
        )
        return candidates, note


class ChatGPTExportImporter(_ChatExportImporter):
    """ChatGPT official data export: ``conversations.json``.

    From ChatGPT → Settings → Data controls → Export data. Accepts the raw
    ``conversations.json`` or the whole export ``.zip``.
    """

    harness = "chatgpt"
    client_id = "chatgpt"
    product_name = "ChatGPT"


class ClaudeExportImporter(_ChatExportImporter):
    """Claude official data export: ``conversations.json``.

    From claude.ai → Settings → Privacy → Export data. Both export shapes
    are parsed defensively either way (see _parse_export_conversation).
    """

    harness = "claude"
    client_id = "claude"
    product_name = "Claude"


# ---------------------------------------------------------------------------
# Harnesses with NO native portable memory store (MCP implant only).
# Documented honestly — no parsers are invented for these.
# ---------------------------------------------------------------------------
NO_NATIVE_STORE: dict[str, str] = {
    "opencode": (
        "OpenCode keeps no portable memory store on disk "
        "(verified 2026-10-11). Memories live in Porter going forward "
        "via the MCP server."
    ),
    "amp": (
        "Amp keeps no portable memory store on disk "
        "(verified 2026-10-11). Memories live in Porter going forward "
        "via the MCP server."
    ),
    "kilo": (
        "Kilo Code keeps no portable memory store on disk "
        "(verified 2026-10-11). Memories live in Porter going forward "
        "via the MCP server."
    ),
    "kimi": (
        "Kimi CLI (K2) keeps no portable memory store on disk "
        "(verified 2026-10-11). Memories live in Porter going forward "
        "via the MCP server."
    ),
    "copilot-cli": (
        "GitHub Copilot CLI keeps no portable memory store on disk "
        "(verified 2026-10-11). Memories live in Porter going forward "
        "via the MCP server."
    ),
    "aider": (
        "Aider is stateless per run and keeps no memory store "
        "(verified 2026-10-11). Memories live in Porter going forward "
        "via the MCP server."
    ),
    "continue": (
        "Continue.dev keeps no portable memory store on disk (rules only, "
        "verified 2026-10-12). Memories live in Porter going forward "
        "via the MCP server (Agent mode)."
    ),
    "zencoder": (
        "Zencoder keeps no portable memory store on disk "
        "(verified 2026-10-12). Memories live in Porter going forward "
        "via the MCP server."
    ),
    "neovim": (
        "Neovim AI plugins (avante.nvim / codecompanion.nvim) keep no "
        "portable memory store on disk (verified 2026-10-12). Memories "
        "live in Porter going forward via the MCP server (mcphub.nvim)."
    ),
    "emacs": (
        "Emacs (gptel) keeps no portable memory store on disk "
        "(verified 2026-10-12). Memories live in Porter going forward "
        "via the MCP server (mcp.el)."
    ),
    "antigravity": (
        "Google Antigravity keeps no portable memory store on disk "
        "(verified 2026-10-12). Memories live in Porter going forward "
        "via the MCP server (HTTP transport)."
    ),
    "kiro": (
        "Kiro keeps no portable memory store on disk "
        "(verified 2026-10-12). Memories live in Porter going forward "
        "via the MCP server."
    ),
    "augment": (
        "Augment Code keeps no portable memory store on disk "
        "(verified 2026-10-12). Memories live in Porter going forward "
        "via the MCP server."
    ),
}


IMPORTERS: dict[str, type[HarnessImporter]] = {
    "codex": CodexImporter,
    "cursor": CursorImporter,
    "zed": ZedImporter,
    "claude-code": ClaudeCodeImporter,
    "windsurf": WindsurfImporter,
    "cline": ClineImporter,
    "roo": RooImporter,
    "gemini": GeminiImporter,
    "qwen": QwenImporter,
    "junie": JunieImporter,
    "droid": DroidImporter,
    "tabnine": TabnineImporter,
    "devin": DevinImporter,
    "openhands": OpenHandsImporter,
    # Official data exports need --file (a .json path or a .zip); they are
    # intentionally NOT part of `--harness all`.
    "chatgpt": ChatGPTExportImporter,
    "claude": ClaudeExportImporter,
}


# ---------------------------------------------------------------------------
# Import runner (drives a MemoryRepository directly; the CLI goes via REST)
# ---------------------------------------------------------------------------
async def run_import(
    repo: Any,
    candidates: list[ImportCandidate],
    *,
    skip_duplicates: bool = True,
) -> dict[str, Any]:
    """Scrub + dedupe + insert candidates into the pending inbox.

    Dedupe is two-layer:
      1. same ``source.imported_from`` path already imported -> skip
      2. semantic similarity > 0.92 to an existing ACTIVE memory -> skip
         (reported as duplicate, never silently merged)
    """
    # Layer 1: paths already imported (any status). Export importers emit
    # several memories per file, so the key includes conversation_id/kind
    # when present (see import_dedupe_key); for every other importer it
    # is exactly the imported_from path, i.e. unchanged behavior.
    existing_keys: set[str] = set()
    for status in ("pending", "active", "archived"):
        for rec in await repo.list(status=status, limit=10_000):
            existing_keys.add(import_dedupe_key(rec.source or {}))

    summary: dict[str, Any] = {
        "scanned": len(candidates),
        "imported": 0,
        "skipped_already_imported": 0,
        "skipped_duplicate": 0,
        "redactions": 0,
        "imported_ids": [],
        "duplicate_of": [],
    }
    for cand in candidates:
        key = import_dedupe_key(
            {**cand.source, "imported_from": cand.imported_from}
        )
        if key in existing_keys:
            summary["skipped_already_imported"] += 1
            continue
        scrubbed, n_redact = scrub_secrets(cand.content)
        summary["redactions"] += n_redact
        source = dict(cand.source)
        source["redactions"] = n_redact
        # Layer 2: semantic dedupe against active memories. Query with the
        # content alone (mirrors create_pending's own dedupe, which embeds
        # content only — prepending the title would dilute the score).
        if skip_duplicates:
            hits = await repo.search(query=scrubbed[:2000], top_k=3)
            if hits and hits[0].score > 0.92:
                summary["skipped_duplicate"] += 1
                summary["duplicate_of"].append(str(hits[0].record.id))
                continue
        rec, _dups = await repo.create_pending(
            content=scrubbed,
            type=cand.type,
            scope_tags=cand.scope_tags,
            confidence=cand.confidence,
            source=source,
        )
        await repo.audit(
            action="write",
            memory_ids=[rec.id],
            scopes_used=["memory.admin"],
            client_id=None,
            meta={"via": "importer",
                  "harness": source.get("client_id"),
                  "imported_from": cand.imported_from,
                  "redactions": n_redact},
        )
        existing_keys.add(key)
        summary["imported"] += 1
        summary["imported_ids"].append(str(rec.id))
    return summary
