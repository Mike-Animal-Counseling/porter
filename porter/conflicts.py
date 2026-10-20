"""Semantic conflict detection, merging, and provenance (lineage) for memories.

Porter's write path already does last-writer-wins with loser preservation
(`MemoryRepository.update_with_version`): a version conflict still applies
the write and parks the overwritten content as a new pending inbox record.
This module goes one layer deeper:

- provenance: every memory carries a ``porter_provenance`` block in its
  ``source`` dict, recording origin (harness/actor/time), merges, and
  conflict resolutions. The block is append-only lineage that survives
  revise/archive, so any record can answer "where did I come from?".
- detect_contradictions: embedding-similarity pre-filter plus an
  extensible, LLM-free rule list (``CONTRADICTION_RULES``) that names *how*
  two semantically close memories disagree (negation flip, antonym swap,
  numeric conflict, status reversal).
- semantic_merge: fold several memories into one new record. The sources
  are archived with ``superseded_by`` pointing at the new id, and the new
  record's provenance lists every source. Runs through the normal write
  path (``create_active`` + ``archive``), so the change log and audit trail
  stay complete.
- resolve_conflict: the primitive behind the pending-confirmation queue —
  pick a winner, archive the losers, record the resolution in each loser's
  provenance.

Suggested REST wiring (not implemented here — this module is storage-layer
logic, transport-agnostic):

    GET  /api/v1/memories/{id}/contradictions
        -> detect_contradictions(repo, id, threshold=..., top_k=...)
    POST /api/v1/memories/merge
        body: {"memory_ids": [...], "strategy": "newest_wins_per_aspect",
               "content": "<only for manual_pick>"}
        -> semantic_merge(...)
    POST /api/v1/memories/resolve
        body: {"winner_id": "<uuid>", "loser_ids": [...],
               "resolution": "superseded_by:<winner_id>"}
        -> resolve_conflict(...)

Design notes:

- Provenance helpers (``get_provenance`` / ``append_provenance``) are pure
  functions over the ``source`` dict; they never touch the DB.
- Archive writes persist superseded_by and provenance on both backends.
  PostgreSQL merges and resolutions use a single transaction with source-row
  locks; a failed operation rolls back records, audit and sync changes together.
- Encrypted (E2EE) memories are never contradiction-scanned or merged:
  the server is ciphertext-blind and has no embedding to compare.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from .errors import InvalidInput, NotFound
from .repositories import MemoryRecord, MemoryRepository
from .visibility import can_read_memory

__all__ = [
    "PROVENANCE_KEY",
    "CONTRADICTION_RULES",
    "MERGE_STRATEGIES",
    "get_provenance",
    "append_provenance",
    "detect_contradictions",
    "semantic_merge",
    "resolve_conflict",
    "resolution_superseded_by",
    "resolution_merged_into",
]

#: Key inside every memory's ``source`` dict holding the lineage block.
PROVENANCE_KEY = "porter_provenance"

#: Merge strategies accepted by :func:`semantic_merge`.
MERGE_STRATEGIES = (
    "newest_wins",  # whole newest record wins (content/type/tags/confidence)
    "newest_wins_per_aspect",  # content+type from newest; tags unioned; confidence max
    "manual_pick",  # caller-supplied content; tags unioned; confidence max
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


# ---------------------------------------------------------------------------
# Provenance


def _provenance_from_source(source: dict[str, Any] | None) -> dict[str, Any]:
    """Defensive copy of the provenance block inside a source dict."""
    prov: dict[str, Any] = {"origin": None, "merges": [], "conflicts": []}
    raw = (source or {}).get(PROVENANCE_KEY)
    if not isinstance(raw, dict):
        return prov
    if isinstance(raw.get("origin"), dict):
        prov["origin"] = dict(raw["origin"])
    for key in ("merges", "conflicts"):
        items = raw.get(key)
        if isinstance(items, list):
            prov[key] = [dict(i) if isinstance(i, dict) else i for i in items]
    return prov


def get_provenance(record: MemoryRecord) -> dict[str, Any]:
    """Return a memory's lineage block (never mutates the record).

    Shape::

        {"origin": {"harness": str, "at": str, "actor": str} | None,
         "merges": [{"from_id": str, "at": str, "strategy": str}],
         "conflicts": [{"with_id": str, "at": str, "resolution": str}]}

    Records written before provenance existed (or without it) yield a
    blank block with ``origin`` None and empty lists.
    """
    return _provenance_from_source(record.source)


def append_provenance(record_source: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    """Pure function: return a copy of ``record_source`` with one provenance
    event appended. The input dict is never mutated and nothing is written
    to the DB — the caller persists the result through the normal write
    path.

    ``event`` must carry ``kind`` in {"origin", "merge", "conflict"}:

    - origin:   {"kind": "origin", "harness": str, "actor": str, "at": str?}
                (re)sets the origin block.
    - merge:    {"kind": "merge", "from_id": UUID|str, "strategy": str,
                 "at": str?} — appended to ``merges``.
    - conflict: {"kind": "conflict", "with_id": UUID|str,
                 "resolution": str, "at": str?} — appended to ``conflicts``.

    ``at`` defaults to now (UTC, ISO-8601) when omitted.
    """
    kind = event.get("kind")
    if kind not in ("origin", "merge", "conflict"):
        raise InvalidInput(f"event kind must be one of origin|merge|conflict, got {kind!r}")
    source = dict(record_source or {})
    prov = _provenance_from_source(source)
    fields = {k: v for k, v in event.items() if k != "kind"}
    at = fields.get("at") or _utcnow_iso()
    if kind == "origin":
        prov["origin"] = {
            "harness": fields.get("harness"),
            "at": at,
            "actor": fields.get("actor"),
        }
    elif kind == "merge":
        if "from_id" not in fields:
            raise InvalidInput("merge event requires from_id")
        prov["merges"].append(
            {
                "from_id": str(fields["from_id"]),
                "at": at,
                "strategy": fields.get("strategy", "unknown"),
            }
        )
    else:  # conflict
        if "with_id" not in fields:
            raise InvalidInput("conflict event requires with_id")
        prov["conflicts"].append(
            {
                "with_id": str(fields["with_id"]),
                "at": at,
                "resolution": fields.get("resolution", "unresolved"),
            }
        )
    source[PROVENANCE_KEY] = prov
    return source


# ---------------------------------------------------------------------------
# Contradiction rules (heuristic, LLM-free)
#
# Rules run only on candidates that already passed the embedding-similarity
# pre-filter, so each rule answers *how* two semantically close memories
# disagree — not whether they are related at all. Rules are (name, check)
# pairs; append to CONTRADICTION_RULES to extend. Each check takes two
# contents and returns True when the signal fires (symmetric).

_CJK_NEGATIONS = ("不", "没", "未", "别", "莫", "勿", "无")
_EN_NEGATIONS = ("not", "no", "never", "without")
_CONTRACTION_NEG = "n't"


def _negation_markers(text: str) -> set[str]:
    low = text.lower()
    found = {m for m in _CJK_NEGATIONS if m in low}
    # "非常" (very) is not a negation — exclude it from the "非" marker.
    if re.search(r"非(?!常)", low):
        found.add("非")
    if _CONTRACTION_NEG in low:
        found.add(_CONTRACTION_NEG)
    for m in _EN_NEGATIONS:
        if re.search(rf"\b{re.escape(m)}\b", low):
            found.add(m)
    return found


def _strip_negations(text: str) -> str:
    out = text
    for m in _CJK_NEGATIONS:
        out = out.replace(m, "")
    out = re.sub(r"非(?!常)", "", out)
    out = out.replace(_CONTRACTION_NEG, "")
    for m in _EN_NEGATIONS:
        out = re.sub(rf"\b{re.escape(m)}\b", "", out, flags=re.IGNORECASE)
    return out


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _rule_negation_flip(a: str, b: str) -> bool:
    """One side negates what the other asserts ("用tabs" vs "不要用tabs")."""
    if _negation_markers(a) == _negation_markers(b):
        return False
    return _ratio(_strip_negations(a), _strip_negations(b)) >= 0.6


_ANTONYM_PAIRS: tuple[tuple[str, str], ...] = (
    ("tabs", "spaces"),
    ("true", "false"),
    ("yes", "no"),
    ("allow", "deny"),
    ("enable", "disable"),
    ("before", "after"),
    ("add", "remove"),
    ("create", "delete"),
    ("开", "关"),
    ("是", "否"),
    ("允许", "禁止"),
    ("启用", "禁用"),
)


def _is_swap(a: str, b: str, x: str, y: str) -> bool:
    """Genuine swap: x in a and y in b (or vice versa), not co-occurring
    inside either side (which would be a comparison, not a contradiction)."""
    la, lb = a.lower(), b.lower()
    swapped = (x in la and y in lb) or (y in la and x in lb)
    cooccur = (x in la and y in la) or (x in lb and y in lb)
    return swapped and not cooccur


def _rule_antonym_swap(a: str, b: str) -> bool:
    """Same claim with an antonym swapped in ("用tabs" vs "用spaces")."""
    return any(_is_swap(a, b, x, y) for x, y in _ANTONYM_PAIRS)


_NUMBER_RE = re.compile(r"\d+(?:\.\d+)*")


def _rule_numeric_conflict(a: str, b: str) -> bool:
    """Same skeleton, different numbers ("Python 3.11" vs "Python 3.12")."""
    na, nb = _NUMBER_RE.findall(a), _NUMBER_RE.findall(b)
    if not na or not nb or sorted(na) == sorted(nb):
        return False
    return _ratio(_NUMBER_RE.sub("#", a), _NUMBER_RE.sub("#", b)) >= 0.7


_STATUS_PAIRS: tuple[tuple[str, str], ...] = (
    ("已完成", "未完成"),
    ("已迁移", "未迁移"),
    ("已上线", "未上线"),
    ("已部署", "未部署"),
    ("done", "pending"),
    ("completed", "not completed"),
    ("上线", "下线"),
    ("开启", "关闭"),
)


def _rule_status_reversal(a: str, b: str) -> bool:
    """Opposite lifecycle status ("已迁移" vs "未迁移")."""
    return any(_is_swap(a, b, x, y) for x, y in _STATUS_PAIRS)


#: Extensible rule registry: (signal name, check(content_a, content_b)).
CONTRADICTION_RULES: list[tuple[str, Callable[[str, str], bool]]] = [
    ("negation_flip", _rule_negation_flip),
    ("antonym_swap", _rule_antonym_swap),
    ("numeric_conflict", _rule_numeric_conflict),
    ("status_reversal", _rule_status_reversal),
]


async def detect_contradictions(
    repo: MemoryRepository,
    memory_id: UUID,
    *,
    threshold: float = 0.75,
    top_k: int = 10,
    granted_scopes: Sequence[str] = ("memory.read",),
) -> list[dict[str, Any]]:
    """Find active memories that semantically contradict the given one.

    Embedding similarity (``find_similar_active``) is the pre-filter;
    ``CONTRADICTION_RULES`` then name the disagreement signals. Encrypted
    memories are never scanned (ciphertext-blind). Returns at most
    ``top_k`` dicts::

        {"id": UUID, "similarity": float, "signals": [str],
         "excerpt": str}

    ``threshold`` defaults to 0.75 for production embeddings; the coarse
    HashEmbedder used in tests needs a lower value (see tests).
    """
    record = await repo.get(memory_id)
    if not can_read_memory(record, granted_scopes) or record.encrypted:
        return []
    vec = record.embedding or await repo.embedder.aembed_one(record.content)
    # +1: the probe usually returns the record itself; dropped below.
    scored = await repo.find_similar_active(vec, threshold=threshold, limit=top_k + 1)
    out: list[dict[str, Any]] = []
    for s in scored:
        if s.record.id == record.id:
            continue
        other = s.record
        if not can_read_memory(other, granted_scopes) or other.encrypted:
            continue
        signals = [
            name for name, rule in CONTRADICTION_RULES if rule(record.content, other.content)
        ]
        if signals:
            out.append(
                {
                    "id": other.id,
                    "similarity": round(s.score, 4),
                    "signals": signals,
                    "excerpt": other.content[:200],
                    "untrusted_data": True,
                }
            )
    return out[:top_k]


# ---------------------------------------------------------------------------
# Merge


def _ordered_newest_last(records: list[MemoryRecord]) -> list[MemoryRecord]:
    return sorted(records, key=lambda r: (r.updated_at, r.created_at))


async def semantic_merge(
    repo: MemoryRepository,
    memory_ids: list[UUID],
    *,
    strategy: str = "newest_wins_per_aspect",
    content: str | None = None,
    actor_client_id: UUID | None = None,
) -> dict[str, Any]:
    """Fold several memories into one new record.

    The new record is written through the normal path (``create_active``,
    so embedding/duplicate handling/change log all apply); every source is
    archived with ``superseded_by`` pointing at the new id and a
    ``merged_into:<new_id>`` provenance entry; the new record's
    ``provenance["merges"]`` lists every source with the strategy used.
    An audit event (``semantic_merge``) ties it together.

    Strategies:

    - ``newest_wins``: the newest record wins wholesale (content, type,
      tags, confidence).
    - ``newest_wins_per_aspect`` (default): content and type from the
      newest; scope_tags unioned; confidence is the max.
    - ``manual_pick``: ``content`` (required) is used verbatim; type from
      the newest; tags unioned; confidence max.

    Raises InvalidInput on unknown strategy, fewer than two distinct ids,
    missing/non-active/encrypted sources, or manual_pick without content.
    """
    async with repo.atomic(memory_ids):
        if strategy not in MERGE_STRATEGIES:
            raise InvalidInput(
                f"unknown merge strategy {strategy!r}; expected one of {MERGE_STRATEGIES}"
            )
        if strategy == "manual_pick" and not content:
            raise InvalidInput("manual_pick requires the merged content")
        ids = list(dict.fromkeys(memory_ids))  # dedupe, keep order
        if len(ids) < 2:
            raise InvalidInput("semantic_merge requires at least two memory ids")

        records: list[MemoryRecord] = []
        problems: list[str] = []
        for mid in ids:
            rec = await repo.get(mid)
            if rec is None:
                raise NotFound(f"memory {mid} not found")
            elif rec.encrypted:
                problems.append(f"{mid}: encrypted memories cannot be merged")
            elif rec.status != "active":
                problems.append(f"{mid}: status is {rec.status!r}, not active")
            else:
                records.append(rec)
        if problems:
            raise InvalidInput("cannot merge: " + "; ".join(problems))

        ordered = _ordered_newest_last(records)
        newest = ordered[-1]
        merged_content = content if strategy == "manual_pick" else newest.content
        if strategy == "newest_wins":
            merged_type = newest.type
            merged_tags = list(newest.scope_tags)
            merged_conf = newest.confidence
        else:  # newest_wins_per_aspect / manual_pick
            merged_type = newest.type
            merged_tags = sorted({t for r in ordered for t in r.scope_tags})
            merged_conf = max(r.confidence for r in ordered)

        now_iso = _utcnow_iso()
        new_source = dict(newest.source)
        prov = _provenance_from_source(new_source)
        if prov["origin"] is None:
            prov["origin"] = {
                "harness": new_source.get("harness", "porter-merge"),
                "at": now_iso,
                "actor": str(actor_client_id) if actor_client_id else None,
            }
        for r in ordered:
            prov["merges"].append({"from_id": str(r.id), "at": now_iso, "strategy": strategy})
        new_source[PROVENANCE_KEY] = prov

        new_rec, _dups = await repo.create_active(
            content=merged_content,
            type=merged_type,
            scope_tags=merged_tags,
            confidence=merged_conf,
            source=new_source,
            actor_client_id=actor_client_id,
            triage_reasons=["semantic_merge"],
        )

        archived_ids: list[UUID] = []
        for r in ordered:
            arch = await repo.archive(
                r.id,
                actor_client_id=actor_client_id,
                superseded_by=new_rec.id,
                source=append_provenance(
                    r.source,
                    {
                        "kind": "conflict",
                        "with_id": new_rec.id,
                        "resolution": resolution_merged_into(new_rec.id),
                    },
                ),
            )
            if arch is None:
                continue
            archived_ids.append(arch.id)

        await repo.audit(
            action="semantic_merge",
            memory_ids=[new_rec.id, *archived_ids],
            client_id=actor_client_id,
            meta={
                "strategy": strategy,
                "merged_ids": [str(r.id) for r in ordered],
            },
        )
        return {
            "id": new_rec.id,
            "merged_ids": [r.id for r in ordered],
            "archived_ids": archived_ids,
            "strategy": strategy,
            "content": merged_content,
        }


# ---------------------------------------------------------------------------
# Conflict resolution (pending-confirmation queue primitive)


def resolution_superseded_by(winner_id: UUID) -> str:
    """Resolution string convention: this record was superseded by winner."""
    return f"superseded_by:{winner_id}"


def resolution_merged_into(new_id: UUID) -> str:
    """Resolution string convention: this record was merged into new_id."""
    return f"merged_into:{new_id}"


async def resolve_conflict(
    repo: MemoryRepository,
    winner_id: UUID,
    loser_ids: list[UUID],
    *,
    resolution: str,
    actor_client_id: UUID | None = None,
) -> dict[str, Any]:
    """Resolve a contradiction set: winner stays active, losers are archived.

    Each archived loser gets ``superseded_by`` -> winner and a provenance
    ``conflicts`` entry recording ``resolution`` (convention:
    ``"superseded_by:<winner_id>"`` via :func:`resolution_superseded_by`,
    or ``"merged_into:<new_id>"`` via :func:`resolution_merged_into`).
    Missing or already-archived losers are skipped (idempotent — resolving
    twice is safe). An audit event (``resolve_conflict``) is recorded.

    This is the primitive the pending-confirmation queue uses: e.g. after
    ``update_with_version`` parks a loser in the inbox, approving one side
    calls ``resolve_conflict(winner, [loser_pending...],
    resolution=resolution_superseded_by(winner))``.
    """
    async with repo.atomic([winner_id, *loser_ids]):
        winner = await repo.get(winner_id)
        if winner is None:
            raise NotFound(f"winner memory {winner_id} not found")
        if winner.status != "active":
            raise InvalidInput(
                f"winner memory {winner_id} has status {winner.status!r}, expected 'active'"
            )

        archived_ids: list[UUID] = []
        skipped_ids: list[UUID] = []
        seen: set[UUID] = set()
        for lid in loser_ids:
            if lid in seen or lid == winner_id:
                continue
            seen.add(lid)
            rec = await repo.get(lid)
            if rec is None or rec.status == "archived":
                skipped_ids.append(lid)
                continue
            arch = await repo.archive(
                lid,
                actor_client_id=actor_client_id,
                superseded_by=winner_id,
                source=append_provenance(
                    rec.source,
                    {
                        "kind": "conflict",
                        "with_id": winner_id,
                        "resolution": resolution,
                    },
                ),
            )
            if arch is None:
                skipped_ids.append(lid)
                continue
            archived_ids.append(arch.id)

        await repo.audit(
            action="resolve_conflict",
            memory_ids=[winner_id, *archived_ids],
            client_id=actor_client_id,
            meta={"resolution": resolution},
        )
        return {
            "winner_id": winner_id,
            "archived_ids": archived_ids,
            "skipped_ids": skipped_ids,
            "resolution": resolution,
        }
