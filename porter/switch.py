"""Seamless harness switching core — the switch is a first-class event.

Porter's "seamless harness switching" vision: changing agent harnesses
mid-project (Claude Code -> Codex -> Cursor) should feel like switching
browser tabs, not switching computers. This module is the handoff engine
behind that moment:

- :class:`SwitchEvent` — the switch itself, persisted to the append-only
  audit log (action="harness_switch") so every handoff is traceable.
- :func:`build_handoff` — assembles a structured handoff payload for the
  destination harness: the project's working memory (grouped by handoff
  semantics) plus the user's personal memories (the luggage follows the
  person, not the tool).
- :func:`render_briefing` — renders the payload as a human-feeling
  natural-language briefing for the new harness ("welcome back" moment).
  Pure function, no repo access — safe to inject as agent context.

Heuristic classification (no LLM required): scope_tags first, then memory
type, then small content-keyword hints. Encrypted memories are classified
by tags/type only — the server never attempts decryption, and their
content (ciphertext) is passed through opaquely, exactly like
sync._record_to_dict.

Visibility: build_handoff includes ACTIVE memories only (same idea as
sync.build_sync_delta's non-admin rule). OAuth tag-scope filtering uses granted_scopes supplied by the REST/MCP layer.

-----------------------------------------------------------------------
WIRING SPEC (for the integrator — this module changes no existing files):

REST (suggested):
    POST /api/v1/switch/handoff
    Auth: Bearer token with `memory.read` scope. The transport supplies
    granted_scopes for filtering inside build_handoff.
    Request body (JSON):
        {"project": str, "from_harness": str, "to_harness": str,
         "top_k": int = 10}
      - project:      project/workspace name (normalized to project:<name>)
      - from_harness: harness the user is leaving, e.g. "claude-code"
      - to_harness:   harness the user is opening, e.g. "cursor"
      - top_k:        cap per memory fetch (project + personal), 1..200
    Response 200: the handoff payload dict returned by build_handoff.
    IMPORTANT: build_handoff already records the harness_switch audit
    event — the endpoint MUST NOT record it again.

MCP (suggested):
    tool name: switch_handoff
    params: project (str, required), from_harness (str, required),
            to_harness (str, required), top_k (int, default 10)
    returns: handoff payload dict (same shape as REST)
    scope: memory.read (enforced by the MCP layer via _need_scope)
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from .errors import InvalidInput
from .repositories import AuditRecord, MemoryRecord, MemoryRepository
from .visibility import can_read_memory

# ---------------------------------------------------------------------------
# SwitchEvent


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class SwitchEvent:
    """A harness switch: the user moves project work from one harness to
    another. Persisted via repo.audit (action="harness_switch") — the audit
    log is append-only, so the switch history is tamper-evident."""

    from_harness: str
    to_harness: str
    project: str
    at: datetime = field(default_factory=_now)
    actor: str | None = None  # free-form actor label (e.g. user id / device)

    def __post_init__(self) -> None:
        for f in ("from_harness", "to_harness", "project"):
            v = getattr(self, f)
            if not isinstance(v, str) or not v.strip():
                raise InvalidInput(f"{f} must be a non-empty string")

    async def record(
        self, repo: MemoryRepository, *, actor_client_id: UUID | None = None
    ) -> AuditRecord:
        """Persist the switch to the audit log. meta carries from/to/project
        (memory_ids is empty — the switch is about context, not a specific
        memory)."""
        return await repo.audit(
            action="harness_switch",
            client_id=actor_client_id,
            meta={
                "from_harness": self.from_harness,
                "to_harness": self.to_harness,
                "project": self.project,
                "at": self.at.isoformat(),
                "actor": self.actor,
            },
        )


# ---------------------------------------------------------------------------
# Handoff classification (heuristic, no LLM)

# Section -> lowercase tag hints. Checked in dict order; first match wins so
# a memory lives in exactly one section. Chinese aliases included because
# harness adapters and users tag in both languages.
_SECTION_TAGS: dict[str, frozenset[str]] = {
    "pitfalls": frozenset(
        {
            "pitfall",
            "pitfalls",
            "gotcha",
            "gotchas",
            "lesson",
            "lessons",
            "mistake",
            "mistakes",
            "debugging",
            "warning",
            "warnings",
            "踩坑",
            "教训",
        }
    ),
    "decisions": frozenset(
        {
            "decision",
            "decisions",
            "adr",
            "choice",
            "chosen",
            "decided",
            "决策",
            "决定",
        }
    ),
    "conventions": frozenset(
        {
            "convention",
            "conventions",
            "style",
            "standard",
            "standards",
            "rule",
            "rules",
            "guide",
            "guideline",
            "规范",
            "约定",
        }
    ),
    "in_progress": frozenset(
        {
            "task",
            "tasks",
            "todo",
            "wip",
            "in-progress",
            "in_progress",
            "status",
            "blocked",
            "doing",
            "progress",
            "任务",
            "状态",
            "进展",
        }
    ),
}

# Memory-type fallback when no tag hint matches.
_TYPE_FALLBACK: dict[str, str] = {
    "episode": "in_progress",  # narrative of what happened -> working state
    "skill_ref": "conventions",  # reusable how-to -> convention
}

# Content-keyword fallback (plaintext only — never applied to encrypted
# records, whose content is opaque ciphertext).
_CONTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "pitfalls": ("gotcha", "don't", "avoid", "warning", "failed", "broken", "踩坑", "别"),
    "decisions": ("decided", "chose", "decision:", "决定"),
    "conventions": ("always", "never", "must ", "convention", "规范", "约定"),
}

# Types that belong to the user's PERSONAL domain (the luggage follows the
# person). "preference" is the v1 type mapped by triage.OLD_TYPE_TO_NEW.
_PERSONAL_TYPES = frozenset({"personal", "preference"})

SECTIONS = ("in_progress", "decisions", "conventions", "pitfalls", "personal")

MAX_TOP_K = 200  # matches the repo.list limit ceiling


def _norm_tags(tags: Sequence[str] | None) -> set[str]:
    return {t.strip().lower() for t in (tags or ()) if t and t.strip()}


def _classify_project_memory(rec: MemoryRecord) -> str:
    """Pick one handoff section for a project-domain memory.

    Encrypted records are classified by tags/type only — their content is
    ciphertext and must never be keyword-scanned (nor decrypted)."""
    tags = _norm_tags(rec.scope_tags)
    for section, hints in _SECTION_TAGS.items():
        if tags & hints:
            return section
    fallback = _TYPE_FALLBACK.get((rec.type or "").strip().lower())
    if fallback:
        return fallback
    if not rec.encrypted:
        text = (rec.content or "").lower()
        for section, keywords in _CONTENT_KEYWORDS.items():
            if any(k in text for k in keywords):
                return section
    # Default bucket: unclassified working memory is still "in progress"
    # context the new harness should see, not something to drop.
    return "in_progress"


def _record_to_dict(rec: MemoryRecord) -> dict[str, Any]:
    """MemoryOut-like dict (JSON-native). Mirrors sync._record_to_dict so
    handoff entries share the exact wire shape; kept local so switch.py
    stays dependency-light.

    E2EE (P2): when rec.encrypted, `content` IS the base64 ciphertext —
    passed through opaquely, never decrypted server-side. The receiving
    harness decrypts client-side with its own key."""
    return {
        "id": str(rec.id),
        "content": rec.content,
        "type": rec.type,
        "scope_tags": list(rec.scope_tags),
        "source": dict(rec.source),
        "confidence": rec.confidence,
        "status": rec.status,
        "created_at": rec.created_at.isoformat(),
        "updated_at": rec.updated_at.isoformat(),
        "superseded_by": str(rec.superseded_by) if rec.superseded_by else None,
        "version": rec.version,
        "encrypted": bool(rec.encrypted),
        "key_id": rec.key_id,
        # PRD principle 5 (T3): handoff content is untrusted input for the
        # receiving agent — same marking search/sync results carry.
        "untrusted_data": True,
    }


# ---------------------------------------------------------------------------
# build_handoff


async def build_handoff(
    repo: MemoryRepository,
    *,
    project: str,
    from_harness: str,
    to_harness: str,
    top_k: int = 10,
    actor_client_id: UUID | None = None,
    record_event: bool = True,
    granted_scopes: Sequence[str] = ("memory.read",),
) -> dict[str, Any]:
    """Assemble the handoff payload for a harness switch.

    Pulls the project's project-domain memories (``project:<name>`` tag,
    normalized match) plus the user's personal-domain memories (the luggage
    follows the person), groups project memories by handoff semantics, and
    records the switch as an audit event (action="harness_switch").

    Visibility: ACTIVE memories only — the same rule sync.build_sync_delta
    applies to non-admin callers. granted_scopes filters both project and
    personal memories before inclusion in the response or audit event.

    Returns::
        {"project", "from_harness", "to_harness", "at",
         "sections": {"in_progress": [...], "decisions": [...],
                      "conventions": [...], "pitfalls": [...],
                      "personal": [...]}}   # each entry a MemoryOut-like dict

    Raises ValueError on an empty project/harness name or top_k < 1.
    """
    from .triage import project_names, project_tag

    if not isinstance(project, str) or not project.strip():
        raise InvalidInput("project must be a non-empty string")
    if not isinstance(from_harness, str) or not from_harness.strip():
        raise InvalidInput("from_harness must be a non-empty string")
    if not isinstance(to_harness, str) or not to_harness.strip():
        raise InvalidInput("to_harness must be a non-empty string")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
        raise InvalidInput("top_k must be an integer >= 1")
    top_k = min(top_k, MAX_TOP_K)

    want = next(iter(project_names([project_tag(project)])))

    # Project-domain working memory for THIS project. The tag filter does
    # the coarse pass; project_names() re-checks so case/format variants of
    # the tag still resolve to the same normalized project.
    proj_recs = await repo.list(status="active", tags=[f"project:{want}"], limit=top_k)
    proj_recs = [
        r
        for r in proj_recs
        if want in project_names(r.scope_tags or ()) and can_read_memory(r, granted_scopes)
    ]

    # Personal domain: the luggage follows the person, not the tool. All of
    # the user's personal memories ride along regardless of project tags.
    personal_recs: list[MemoryRecord] = []
    seen: set[UUID] = set()
    for ptype in ("personal", "preference"):
        for r in await repo.list(status="active", type=ptype, limit=top_k):
            if r.id not in seen and can_read_memory(r, granted_scopes):
                seen.add(r.id)
                personal_recs.append(r)
    personal_recs = personal_recs[:top_k]

    sections: dict[str, list[dict[str, Any]]] = {s: [] for s in SECTIONS}
    # Personal-typed records never double-count in project sections, even
    # when they also carry this project's tag.
    personal_ids = {r.id for r in personal_recs}
    classified: list[tuple[MemoryRecord, str]] = []
    for r in proj_recs:
        if r.id in personal_ids:
            continue
        classified.append((r, _classify_project_memory(r)))
    # Most-recent-first: the new harness wants "where did we leave off".
    classified.sort(key=lambda t: t[0].updated_at, reverse=True)
    for r, section in classified:
        sections[section].append(_record_to_dict(r))
    personal_recs.sort(key=lambda r: r.updated_at, reverse=True)
    sections["personal"] = [_record_to_dict(r) for r in personal_recs]

    if record_event:
        await SwitchEvent(
            from_harness=from_harness.strip(),
            to_harness=to_harness.strip(),
            project=project.strip(),
        ).record(repo, actor_client_id=actor_client_id)

    return {
        "project": project.strip(),
        "from_harness": from_harness.strip(),
        "to_harness": to_harness.strip(),
        "at": _now().isoformat(),
        "sections": sections,
    }


# ---------------------------------------------------------------------------
# render_briefing — the "welcome back" moment

_SUPPORTED_LANGS = ("en", "zh")


def _normalize_lang(lang: str | None) -> str:
    """Unknown / missing language falls back to English (repo convention)."""
    return lang if lang in _SUPPORTED_LANGS else "en"


_SECTION_TITLES: dict[str, dict[str, str]] = {
    "en": {
        "in_progress": "🚧 In progress",
        "decisions": "✅ Decisions",
        "conventions": "📏 Conventions",
        "pitfalls": "⚠️ Pitfalls",
        "personal": "👤 About you",
    },
    "zh": {
        "in_progress": "🚧 进行中",
        "decisions": "✅ 已经定下来的事",
        "conventions": "📏 约定",
        "pitfalls": "⚠️ 踩过的坑",
        "personal": "👤 关于你的事",
    },
}

_MAX_ENTRY_CHARS = 400  # keep the briefing scannable, not a data dump


def _rel_time(iso: str | None, lang: str = "en") -> str:
    """Human relative time ("3 days ago" / "3 天前更新"). Best-effort: any
    parse failure yields "" and the caller omits the suffix."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    secs = int((datetime.now(UTC) - dt).total_seconds())
    if secs < 0:
        return ""
    if lang == "zh":
        if secs < 60:
            return "刚刚"
        if secs < 3600:
            return f"{secs // 60} 分钟前"
        if secs < 86400:
            return f"{secs // 3600} 小时前"
        days = secs // 86400
        if days < 30:
            return f"{days} 天前"
        return f"{days // 30} 个月前"
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600} hr ago"
    days = secs // 86400
    if days < 30:
        return f"{days} days ago"
    return f"{days // 30} mo ago"


def _render_entry(entry: dict[str, Any], lang: str = "en") -> str:
    # Encrypted memories: never dump ciphertext into prose. The receiving
    # harness decrypts client-side; here we only signal that something
    # exists that Porter itself cannot read.
    if entry.get("encrypted"):
        if lang == "zh":
            return "🔒 一条加密记忆（只有你能解密，Porter 看不到内容）"
        return "🔒 An encrypted memory (only you can decrypt it — Porter can't see the contents)"
    content = (entry.get("content") or "").strip()
    if len(content) > _MAX_ENTRY_CHARS:
        content = content[:_MAX_ENTRY_CHARS].rstrip() + "…"
    rel = _rel_time(entry.get("updated_at"), lang)
    if lang == "zh":
        suffix = f"（{rel}更新）" if rel else ""
    else:
        suffix = f" (updated {rel})" if rel else ""
    return f"{content}{suffix}"


def render_briefing(handoff: dict[str, Any], lang: str | None = "en") -> str:
    """Render a handoff payload as a human-feeling briefing for the new
    harness. English-first (repo convention); pass lang="zh" for Chinese.
    Pure function — no repo access.

    The copy answers the three questions a switched-to agent actually has:
    where did we leave off / what's stuck / what are the conventions.
    Empty sections are skipped; a project with no memories at all gets an
    honest "blank slate" opening instead of a wall of empty headers."""
    lang = _normalize_lang(lang)
    zh = lang == "zh"
    titles = _SECTION_TITLES[lang]
    project = handoff.get("project") or ("这个项目" if zh else "this project")
    from_h = handoff.get("from_harness") or ("上一个工具" if zh else "the previous harness")
    to_h = handoff.get("to_harness") or ("这里" if zh else "here")
    sections = handoff.get("sections") or {}

    if zh:
        lines = [
            f"🔄 交接简报 · {project}",
            f"{from_h} → {to_h}，行李已送达。",
            "",
        ]
    else:
        lines = [
            f"🔄 Handoff briefing · {project}",
            f"{from_h} → {to_h} — your bags have arrived.",
            "",
        ]

    project_sections = [s for s in SECTIONS if s != "personal"]
    has_project_memories = any(sections.get(s) for s in project_sections)
    if has_project_memories:
        if zh:
            lines.append(
                f"欢迎回来！你上次在这个项目里用的是 {from_h}，"
                "我把它的记忆打包带来了——先看一眼再开工："
            )
        else:
            lines.append(
                f"Welcome back! Last time on this project you were using {from_h} — "
                "I've packed up its memories. Skim this before you start:"
            )
    else:
        if zh:
            lines.append(
                f"{project} 在 Porter 里还是白纸一张，{from_h} 没留下什么记录。"
                "不过关于你的事我都带着，先从下面开始："
            )
        else:
            lines.append(
                f"{project} is a blank slate in Porter — {from_h} left nothing behind. "
                "But everything about you came along; start here:"
            )
    lines.append("")

    for name in SECTIONS:
        entries = sections.get(name) or []
        if not entries:
            continue
        lines.append(f"## {titles[name]}")
        for e in entries:
            lines.append(f"- {_render_entry(e, lang)}")
        lines.append("")

    if zh:
        lines.append("💡 以上都是过去的记录转述，仅供参考——动手前还是按现状核实一下。")
    else:
        lines.append(
            "💡 All of the above is retold from past records — "
            "worth a quick sanity check against reality before you act."
        )
    return "\n".join(lines).rstrip() + "\n"
