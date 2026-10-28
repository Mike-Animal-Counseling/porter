"""Bidirectional write-back: Porter -> harness-native memory files.

The importers (porter/importers.py) are READ-ONLY: they migrate a harness's
existing memory files into Porter's inbox. This module is the reverse
direction — it renders Porter memories back into each harness's NATIVE
format, so a memory added in Porter shows up in the next harness as if it
had always been there ("the luggage unpacks itself").

Safety rules (non-negotiable):
- Marker zones only: every write is wrapped in
  ``<!-- porter-sync:start -->`` / ``<!-- porter-sync:end -->``. An
  existing zone is replaced in place; user content outside the markers is
  never touched.
- Backup before write: the original file is copied to
  ``.porter-backup/<UTC-timestamp>/`` next to the target before any
  modification. The backup keeps a ``.bak`` suffix so future importer
  scans (``*.md`` globs) never pick it up as a memory.
- dry_run=True performs zero disk writes (no backups either) and only
  reports what would change.
- Eligibility: only ``status == "active"`` memories are written.
  Encrypted (E2EE) memories are never rendered in cleartext.
  Personal-domain memories flagged sensitive by triage
  (``sensitive_tag`` / ``sensitive_content`` / ``ml_sensitive:*``) are
  skipped unless ``include_sensitive=True``. Project-domain memories are
  written by default. Empty content is skipped.
- Symlinks are refused as write targets (same guard as the importers):
  a symlinked target is skipped, never followed.

Path knowledge mirrors porter/importers.py (verified 2026-10-11/12):
- Codex: ``~/.codex/memories/`` -> Porter owns ``porter.md`` there.
- Claude Code: ``~/.claude/projects/<encoded-cwd>/memory/porter.md`` when
  the project dir can be matched, else the marker zone in
  ``~/.claude/CLAUDE.md``.
- Cursor: ``<project>/.cursorrules`` marker zone (per-project; Cursor has
  no global memory store).
- Windsurf: ``<project>/.windsurfrules`` marker zone, else the global
  ``~/.codeium/windsurf/memories/porter.md``.
- Cline / Roo Code: ``<project>/memory-bank/porter.md``.
- Fallback (unknown harness): ``<project>/AGENTS.md`` marker zone.

Wiring (spec only — this module is transport-agnostic, no endpoints here):
- REST: ``POST /api/v1/writeback/sync``
  body ``{"harness": "cursor", "project": "/repo", "dry_run": false,
  "include_sensitive": false}``
  -> 200 ``{"written_files": [...], "skipped": [...],
  "backed_up": [...], "note": "..."}`` (see WriteResult.to_dict)
- MCP tool: ``writeback_sync(harness, project?, dry_run?,
  include_sensitive?)`` with identical semantics.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "MARK_START",
    "MARK_END",
    "WriteResult",
    "HarnessWriter",
    "CodexWriter",
    "ClaudeCodeWriter",
    "CursorWriter",
    "WindsurfWriter",
    "ClineWriter",
    "RooWriter",
    "GenericAgentsMdWriter",
    "WRITERS",
    "get_writer",
]

# ---------------------------------------------------------------------------
# Marker zone
# ---------------------------------------------------------------------------
MARK_START = "<!-- porter-sync:start -->"
MARK_END = "<!-- porter-sync:end -->"

_ZONE_RE = re.compile(
    r"<!-- porter-sync:start -->.*?<!-- porter-sync:end -->",
    re.DOTALL,
)


def _safe_project_root(project: str | None) -> Path | None:
    """Validate a caller-supplied project root for write-back targets.

    Returns the resolved Path, or None when the input is unusable. Rejects:
    - empty / non-absolute paths (write-back targets must be explicit)
    - paths containing ``..`` components (path traversal)
    The project root is caller-specified by design (like the importer's
    project_roots); this is defense-in-depth, not the security boundary
    (that is the memory.read scope on the endpoint).
    """
    if not project or not isinstance(project, str):
        return None
    if ".." in Path(project).parts:
        return None
    p = Path(project)
    if not p.is_absolute():
        return None
    return p


def _apply_marker_zone(existing: str, block: str) -> str:
    """Insert or replace the porter-sync marker zone in *existing* text.

    Content outside the markers is returned byte-for-byte unchanged. When
    no complete zone exists, a new one is appended after a blank line; a
    lone start marker without its end (corrupt) is left alone and a fresh
    complete zone is appended instead of trying to repair it.
    """
    zone = f"{MARK_START}\n{block}\n{MARK_END}"
    if _ZONE_RE.search(existing):
        return _ZONE_RE.sub(lambda _match: zone, existing, count=1)
    if not existing.strip():
        return zone + "\n"
    tail = existing if existing.endswith("\n") else existing + "\n"
    return f"{tail}\n{zone}\n"


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------
_SENSITIVE_REASONS = ("sensitive_tag", "sensitive_content")


def _triage_flagged_sensitive(mem: Mapping[str, Any]) -> bool:
    """True when triage marked this memory sensitive (see porter/triage.py).

    Matches the exact reason strings the triage layer emits:
    ``sensitive_tag``, ``sensitive_content``, and ``ml_sensitive:<cat>``.
    """
    reasons = mem.get("triage_reasons") or []
    for r in reasons:
        if r in _SENSITIVE_REASONS or str(r).startswith("ml_sensitive:"):
            return True
    return False


def _eligible(mem: Mapping[str, Any], *, include_sensitive: bool) -> tuple[bool, str]:
    """Eligibility gate for write-back. Returns (ok, reason)."""
    if mem.get("status") != "active":
        return False, f"status={mem.get('status')!r} (not active)"
    if mem.get("encrypted"):
        return False, "encrypted (E2EE ciphertext is never rendered)"
    if not include_sensitive and mem.get("type") == "personal" and _triage_flagged_sensitive(mem):
        return False, "sensitive personal memory (triage-flagged)"
    if not (mem.get("content") or "").strip():
        return False, "empty content"
    return True, ""


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S-%fZ")


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
@dataclass
class WriteResult:
    """Outcome of one write() call."""

    written_files: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    backed_up: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form for the REST / MCP wiring (see module docstring)."""
        return {
            "written_files": list(self.written_files),
            "skipped": list(self.skipped),
            "backed_up": list(self.backed_up),
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Base writer
# ---------------------------------------------------------------------------
class HarnessWriter:
    """Base: render eligible memories into a harness's native file(s)."""

    harness: str = "unknown"

    def __init__(self, home: str | os.PathLike[str] | None = None):
        self.home = Path(home or os.path.expanduser("~"))

    # -- subclass hooks ----------------------------------------------------
    def _targets(self, project: str | None) -> tuple[list[Path], str]:
        """(target files, note fragment). Empty list = nothing to write to."""
        raise NotImplementedError

    def _header_lines(self, memories: list[Mapping[str, Any]], *, project: str | None) -> list[str]:
        when = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        scope = project or "global"
        return [
            f"Porter synced memories ({len(memories)}), scope: {scope}, synced {when}.",
            "Managed by Porter — edit outside the markers, not inside.",
            "",
        ]

    # -- rendering ----------------------------------------------------------
    def _render_one(self, mem: Mapping[str, Any]) -> list[str]:
        content = (mem.get("content") or "").strip()
        first = next((ln.strip() for ln in content.splitlines() if ln.strip()), "")
        heading = first[:80] if first else f"memory {str(mem.get('id', '?'))[:8]}"
        tags = ", ".join(mem.get("scope_tags") or [])
        meta = f"type: {mem.get('type', '?')}"
        if tags:
            meta += f" · tags: {tags}"
        return [f"### {heading}", "", content, "", f"*{meta}*", ""]

    def _render(self, memories: list[Mapping[str, Any]], *, project: str | None) -> str:
        lines = self._header_lines(memories, project=project)
        for mem in memories:
            lines.extend(self._render_one(mem))
        return "\n".join(lines).rstrip() + "\n"

    # -- main entry ----------------------------------------------------------
    async def write(
        self,
        memories: list[dict],
        *,
        project: str | None = None,
        dry_run: bool = False,
        include_sensitive: bool = False,
    ) -> WriteResult:
        """Write eligible memories to the harness's native file(s).

        Never raises on a bad target — problems are reported in
        ``skipped`` so one broken file cannot abort the whole sync.
        """
        res = WriteResult()
        eligible: list[Mapping[str, Any]] = []
        for mem in memories:
            ok, reason = _eligible(mem, include_sensitive=include_sensitive)
            if ok:
                eligible.append(mem)
            else:
                res.skipped.append(f"{mem.get('id', '?')}: {reason}")

        targets, target_note = self._targets(project)
        note_parts = [target_note] if target_note else []
        if not eligible:
            note_parts.append("no eligible memories to write back")
            res.note = "; ".join(note_parts)
            return res
        if not targets:
            note_parts.append("no write target for this harness/project combination")
            res.note = "; ".join(note_parts)
            return res

        block = self._render(eligible, project=project)
        for target in targets:
            if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
                res.skipped.append(f"{target}: symlink target refused")
                continue
            try:
                existing = ""
                if target.is_file():
                    with target.open(encoding="utf-8", newline="") as stream:
                        existing = stream.read()
            except (OSError, UnicodeError) as exc:
                res.skipped.append(f"{target}: unreadable ({exc})")
                continue
            new_text = _apply_marker_zone(existing, block)
            if new_text == existing:
                res.skipped.append(f"{target}: already up to date")
                continue
            if dry_run:
                res.written_files.append(str(target))
                continue
            try:
                if target.is_file():
                    bdir = target.parent / ".porter-backup" / _utc_stamp()
                    bdir.mkdir(parents=True, exist_ok=True)
                    bpath = bdir / (target.name + ".bak")
                    shutil.copy2(target, bpath)
                    res.backed_up.append(str(bpath))
                target.parent.mkdir(parents=True, exist_ok=True)
                mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o600
                descriptor, temporary = tempfile.mkstemp(prefix=".porter-write-", dir=target.parent)
                try:
                    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                        stream.write(new_text)
                    os.chmod(temporary, mode)
                    os.replace(temporary, target)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            except OSError as exc:
                res.skipped.append(f"{target}: write failed ({exc})")
                continue
            res.written_files.append(str(target))

        note_parts.append(
            f"{len(eligible)} eligible, {len(res.written_files)} "
            f"file(s) {'would be ' if dry_run else ''}written"
        )
        if dry_run:
            note_parts.append("dry run — nothing written to disk")
        res.note = "; ".join(note_parts)
        return res


# ---------------------------------------------------------------------------
# Claude Code helpers (mirror porter/importers.py's best-effort codec)
# ---------------------------------------------------------------------------
def _norm_project(project: str) -> str:
    p = project.strip()
    if not p.startswith("/"):
        p = "/" + p
    return p.rstrip("/") or "/"


def _decode_claude_project_dir(name: str) -> str:
    """Best-effort decode of ``~/.claude/projects/<encoded-cwd>``.

    Mirrors importers._decode_claude_project_dir (observed form:
    ``-Users-name-project`` for ``/Users/name/project``). Dashes inside
    real path components are ambiguous — kept as a heuristic on purpose.
    """
    stripped = name[1:] if name.startswith("-") else name
    return "/" + stripped.replace("-", "/")


def _encode_claude_project_dir(project: str) -> str:
    """Inverse of the decode heuristic above (best-effort)."""
    return "-" + _norm_project(project).strip("/").replace("/", "-")


# ---------------------------------------------------------------------------
# Concrete writers
# ---------------------------------------------------------------------------
class CodexWriter(HarnessWriter):
    """Codex CLI: Porter owns ``~/.codex/memories/porter.md`` outright."""

    harness = "codex"

    def __init__(
        self,
        home: str | os.PathLike[str] | None = None,
        memories_dir: str | os.PathLike[str] | None = None,
    ):
        super().__init__(home)
        self.memories_dir = (
            Path(memories_dir) if memories_dir else self.home / ".codex" / "memories"
        )

    def _targets(self, project: str | None) -> tuple[list[Path], str]:
        return [self.memories_dir / "porter.md"], ""


class ClaudeCodeWriter(HarnessWriter):
    """Claude Code: per-project ``memory/porter.md`` when the project dir
    matches, otherwise the marker zone in global ``~/.claude/CLAUDE.md``."""

    harness = "claude-code"

    def __init__(
        self,
        home: str | os.PathLike[str] | None = None,
        claude_home: str | os.PathLike[str] | None = None,
    ):
        super().__init__(home)
        self.claude_home = Path(claude_home) if claude_home else self.home / ".claude"

    def _targets(self, project: str | None) -> tuple[list[Path], str]:
        if project:
            norm = _norm_project(project)
            projects = self.claude_home / "projects"
            if projects.is_dir():
                for d in sorted(projects.iterdir()):
                    if not d.is_dir() or d.is_symlink():
                        continue
                    if (
                        d.name == _encode_claude_project_dir(norm)
                        or _decode_claude_project_dir(d.name) == norm
                    ):
                        return [d / "memory" / "porter.md"], (
                            f"matched Claude Code project dir {d.name}"
                        )
            return [self.claude_home / "CLAUDE.md"], (
                "no matching ~/.claude/projects dir; falling back to global CLAUDE.md"
            )
        return [self.claude_home / "CLAUDE.md"], ("no project given; writing to global CLAUDE.md")


class CursorWriter(HarnessWriter):
    """Cursor: marker zone in ``<project>/.cursorrules``.

    Cursor documents no global memory store (see CursorImporter), so a
    project root is required.
    """

    harness = "cursor"

    def _targets(self, project: str | None) -> tuple[list[Path], str]:
        root = _safe_project_root(project)
        if root is None:
            return [], "Cursor write-back needs an absolute project root"
        return [root / ".cursorrules"], ""


class WindsurfWriter(HarnessWriter):
    """Windsurf: ``<project>/.windsurfrules`` marker zone; without a
    project, the global ``~/.codeium/windsurf/memories/porter.md``."""

    harness = "windsurf"

    def __init__(
        self,
        home: str | os.PathLike[str] | None = None,
        memories_dir: str | os.PathLike[str] | None = None,
    ):
        super().__init__(home)
        self.memories_dir = (
            Path(memories_dir) if memories_dir else self.home / ".codeium" / "windsurf" / "memories"
        )

    def _targets(self, project: str | None) -> tuple[list[Path], str]:
        if project:
            root = _safe_project_root(project)
            if root is None:
                return [], "absolute project root required"
            return [root / ".windsurfrules"], ""
        return [self.memories_dir / "porter.md"], (
            "no project given; writing to global Windsurf memories"
        )


class ClineWriter(HarnessWriter):
    """Cline: Porter owns ``<project>/memory-bank/porter.md``."""

    harness = "cline"

    def _targets(self, project: str | None) -> tuple[list[Path], str]:
        if not project:
            return [], "memory-bank write-back needs a project root"
        root = _safe_project_root(project)
        if root is None:
            return [], "absolute project root required"
        return [root / "memory-bank" / "porter.md"], ""


class RooWriter(HarnessWriter):
    """Roo Code: Porter owns ``<project>/memory-bank/porter.md``."""

    harness = "roo"

    def _targets(self, project: str | None) -> tuple[list[Path], str]:
        if not project:
            return [], "memory-bank write-back needs a project root"
        root = _safe_project_root(project)
        if root is None:
            return [], "absolute project root required"
        return [root / "memory-bank" / "porter.md"], ""


class GenericAgentsMdWriter(HarnessWriter):
    """Fallback: marker zone in ``<project>/AGENTS.md`` (unknown harness)."""

    harness = "generic"

    def _targets(self, project: str | None) -> tuple[list[Path], str]:
        if not project:
            return [], "AGENTS.md write-back needs a project root"
        root = _safe_project_root(project)
        if root is None:
            return [], "absolute project root required"
        return [root / "AGENTS.md"], ""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
WRITERS: dict[str, HarnessWriter] = {
    "claude-code": ClaudeCodeWriter(),
    "codex": CodexWriter(),
    "cursor": CursorWriter(),
    "windsurf": WindsurfWriter(),
    "cline": ClineWriter(),
    "roo": RooWriter(),
}


def get_writer(harness: str) -> HarnessWriter:
    """Return the writer for *harness*.

    Unknown harness names fall back to GenericAgentsMdWriter (marker zone
    in ``<project>/AGENTS.md``) — the most widely-read convention —
    rather than failing.
    """
    return WRITERS.get((harness or "").lower(), GenericAgentsMdWriter())
