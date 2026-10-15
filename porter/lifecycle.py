"""Memory lifecycle engine (automation layer).

Where :mod:`porter.decay` is lazy-on-read with a single flat decay rate,
this module is the full lifecycle engine:

- **Half-life model**: different memory kinds decay at different rates.
  Project task state rots in weeks; personal preferences last a year.
  Effective confidence follows exponential half-life decay:
  ``confidence * 0.5 ** (idle_days / half_life)``.
- **Retrieval feedback**: memories that get retrieved earn confidence
  back (asymptotically toward 1.0) and reset their idle clock.
- **Background sweeper**: :func:`sweep` pages through active memories and
  archives the ones whose effective confidence fell below
  ``ARCHIVE_CONFIDENCE`` — the same threshold decay.py uses, so the two
  calibers agree on *when* to archive and differ only on *how fast*
  each kind gets there.
- **Contradiction flagging**: when a conflict is reported against a
  memory, its confidence is penalized so it surfaces for review sooner.

Compatibility note: :func:`effective_confidence` here is the NEW caliber
(half-life based). :mod:`porter.decay` is intentionally left untouched —
existing read paths keep their behavior; the sweeper is the migration
path toward the half-life model. Encrypted (E2EE) and user-pinned
memories never decay here either: ciphertext carries no meaningful
confidence signal and the server must stay blind.

Wiring spec (for the integrator; nothing in this module touches HTTP):
- ``POST /api/v1/admin/lifecycle/sweep`` (requires ``memory.admin``)
  should call :func:`sweep` and return its result dict as JSON
  (``archived_ids`` serialized via ``str()``).
- :func:`record_retrieval` should be called from the search/read path
  with the ids of the memories actually returned to the caller, so
  retrieval genuinely reinforces the memories that proved useful.
- :func:`flag_stale_on_contradiction` should be called when the
  conflicts/triage path reports a contradiction between two memories.

Portability note: the repository interface deliberately exposes no
generic in-place ``update()`` (provenance is append-only; edits go
through ``revise()`` which mints a new record). Confidence nudges here
are metadata-only adjustments in the spirit of ``touch_retrieved`` /
``set_pinned``: they are applied to the fetched record objects and
persisted via ``touch_retrieved`` for the idle clock. On
``InMemoryMemoryRepository`` the fetched object *is* the stored object,
so the confidence write persists. ``PostgresMemoryRepository`` has no
metadata-only confidence column write on the abstract interface, so
there the durable effect is the idle-clock reset (which restores
effective confidence to the stored value); a future
``adjust_confidence`` metadata op could make the write uniform.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import UUID

from .triage import ARCHIVE_CONFIDENCE, has_project_tag

if TYPE_CHECKING:  # pragma: no cover
    from .repositories import MemoryRepository

#: Fallback half-life (days) for (domain, kind) pairs not in HALF_LIVES.
DEFAULT_HALF_LIFE_DAYS = 90.0

#: Per-(domain, kind) half-lives in days. Project task state rots fast
#: (yesterday's WIP is noise next month); conventions and decisions are
#: durable institutional knowledge; personal preferences barely move.
HALF_LIVES: dict[tuple[str, str], float] = {
    ("project", "task_state"): 14.0,
    ("project", "episode"): 30.0,
    ("project", "fact"): 90.0,
    ("project", "convention"): 180.0,
    ("project", "decision"): 180.0,
    ("project", "skill_ref"): 180.0,
    ("personal", "preference"): 365.0,
    ("personal", "fact"): 180.0,
    ("personal", "episode"): 90.0,
    ("personal", "skill_ref"): 365.0,
}

#: Fraction of the remaining gap to 1.0 closed per retrieval.
RETRIEVAL_BOOST = 0.05
#: Confidence multiplier applied when a contradiction is reported.
CONTRADICTION_PENALTY = 0.7

#: scope_tags (normalized) that pin a record to a lifecycle kind,
#: checked before falling back to the stored ``type``.
_KIND_TAGS: dict[str, str] = {
    "task": "task_state",
    "task_state": "task_state",
    "task-state": "task_state",
    "wip": "task_state",
    "convention": "convention",
    "conventions": "convention",
    "decision": "decision",
    "decisions": "decision",
    "adr": "decision",
    "preference": "preference",
    "pref": "preference",
    "episode": "episode",
    "skill_ref": "skill_ref",
    "skill": "skill_ref",
    "fact": "fact",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _domain_of(record) -> str:
    """'project' when the record carries a project:<name> tag (or is
    explicitly typed as project); 'personal' otherwise. Mirrors the
    two-domain taxonomy: project memories must carry a project tag."""
    if getattr(record, "type", "") == "project" or has_project_tag(
        getattr(record, "scope_tags", ()) or ()
    ):
        return "project"
    return "personal"


def _kind_of(record) -> str:
    """Lifecycle kind: explicit kind tags win, then the stored type."""
    for tag in getattr(record, "scope_tags", ()) or ():
        kind = _KIND_TAGS.get(str(tag).casefold().strip())
        if kind:
            return kind
    mem_type = str(getattr(record, "type", "") or "").casefold().strip()
    if mem_type in ("preference", "personal"):
        return "preference"
    if mem_type == "episode":
        return "episode"
    if mem_type == "skill_ref":
        return "skill_ref"
    return "fact"


def half_life_days(record) -> float:
    """Half-life in days for a record, from (domain, kind).

    Domain comes from ``scope_tags`` (``project:<name>`` -> project,
    otherwise personal); kind comes from kind tags, else the stored
    type. Unknown combinations fall back to ``DEFAULT_HALF_LIFE_DAYS``.
    Pure function (testable).
    """
    return HALF_LIVES.get(
        (_domain_of(record), _kind_of(record)), DEFAULT_HALF_LIFE_DAYS
    )


def effective_confidence(record, at: datetime | None = None) -> float:
    """Stored confidence decayed by idle time under the half-life model.

    ``confidence * 0.5 ** (idle_days / half_life)``. Pure function.
    Encrypted and pinned memories bypass decay entirely (same rule as
    decay.py: the server stays blind, pins are user intent).
    """
    at = at or _now()
    if getattr(record, "encrypted", False) or getattr(record, "pinned", False):
        return float(record.confidence)
    last = getattr(record, "last_retrieved_at", None) or record.updated_at
    idle_days = max(0.0, (at - last).total_seconds() / 86400.0)
    half_life = half_life_days(record)
    return float(record.confidence) * (0.5 ** (idle_days / half_life))


def _idle_days(record, at: datetime) -> float:
    last = getattr(record, "last_retrieved_at", None) or record.updated_at
    return max(0.0, (at - last).total_seconds() / 86400.0)


async def record_retrieval(
    repo: "MemoryRepository", memory_ids: list, *, at: datetime | None = None
) -> None:
    """Reinforce memories that were just retrieved.

    For each active, non-encrypted, non-pinned memory: close 5% of the
    remaining confidence gap toward 1.0
    (``min(1.0, old + 0.05 * (1 - old))``) and reset the idle clock via
    ``repo.touch_retrieved``. The confidence write follows the same
    in-place pattern as ``auto_merge``; see the module docstring for the
    Postgres portability note.
    """
    now = at or _now()
    touched: list = []
    for mid in memory_ids:
        rec = await repo.get(mid)
        if rec is None or rec.status != "active":
            continue
        if rec.encrypted or rec.pinned:
            continue
        old = float(rec.confidence)
        rec.confidence = min(1.0, old + RETRIEVAL_BOOST * (1.0 - old))
        touched.append(rec.id)
    if touched:
        await repo.touch_retrieved(touched, at=now)


async def sweep(
    repo: "MemoryRepository",
    *,
    batch_size: int = 500,
    actor: str = "lifecycle_sweeper",
) -> dict:
    """Background sweeper: archive memories decayed past the threshold.

    Pages through active memories (``repo.list`` applies no decay, so
    scanning never resets idle clocks), computes the half-life
    effective confidence, and archives + audits (``lifecycle_archive``)
    anything below ``ARCHIVE_CONFIDENCE``. Encrypted and pinned
    memories are skipped.

    Idempotent: archived records leave the ``active`` listing, so a
    re-run archives nothing new. Offset bookkeeping compensates for
    records archived mid-page so no active record is skipped.
    """
    now = _now()
    scanned = 0
    archived_ids: list[UUID] = []
    offset = 0
    while True:
        batch = await repo.list(status="active", limit=batch_size, offset=offset)
        if not batch:
            break
        archived_in_batch = 0
        for rec in batch:
            scanned += 1
            if rec.encrypted or rec.pinned:
                continue
            eff = effective_confidence(rec, now)
            if eff < ARCHIVE_CONFIDENCE:
                await repo.archive(rec.id, actor_client_id=None)
                await repo.audit(
                    action="lifecycle_archive",
                    memory_ids=[rec.id],
                    client_id=None,
                    scopes_used=(),
                    meta={
                        "reason": "half_life_decay",
                        "actor": actor,
                        "effective_confidence": round(eff, 4),
                        "stored_confidence": float(rec.confidence),
                        "half_life_days": half_life_days(rec),
                        "idle_days": round(_idle_days(rec, now), 1),
                    },
                )
                archived_ids.append(rec.id)
                archived_in_batch += 1
        if len(batch) < batch_size:
            break
        # Archived rows leave the active listing; shift the next offset
        # back by that count so the page boundary lands correctly.
        offset += len(batch) - archived_in_batch
    return {
        "scanned": scanned,
        "archived": len(archived_ids),
        "archived_ids": archived_ids,
    }


async def flag_stale_on_contradiction(
    repo: "MemoryRepository", memory_id, contradicting_id
) -> None:
    """Penalize a memory contradicted by another.

    Multiplies the older memory's confidence by ``CONTRADICTION_PENALTY``
    (0.7) so it decays into review/archival sooner, and audits the flag
    (``contradiction_flag``) with both ids for the review UI. No import
    of the conflicts module needed — call this when a contradiction is
    reported. No-op for missing, non-active, encrypted, or pinned
    records.
    """
    rec = await repo.get(memory_id)
    if rec is None or rec.status != "active":
        return
    if rec.encrypted or rec.pinned:
        return
    old = float(rec.confidence)
    rec.confidence = old * CONTRADICTION_PENALTY
    await repo.audit(
        action="contradiction_flag",
        memory_ids=[rec.id, contradicting_id],
        client_id=None,
        scopes_used=(),
        meta={
            "reason": "contradiction_detected",
            "old_confidence": round(old, 4),
            "new_confidence": round(rec.confidence, 4),
            "penalty": CONTRADICTION_PENALTY,
            "contradicting_id": str(contradicting_id),
        },
    )
