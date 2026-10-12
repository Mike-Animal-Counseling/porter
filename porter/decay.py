"""Confidence decay for idle memories (automation layer).

Memories nobody retrieves lose confidence over time; when the effective
confidence drops below ARCHIVE_CONFIDENCE the memory is auto-archived
(audited, restorable — never deleted).

- Lazy: evaluated on read paths (search/get), never a background job.
  A memory nobody reads costs nothing and harms nothing; archiving is
  observed when the memory would otherwise surface.
- Never decays E2EE (ciphertext has no meaningful confidence signal and
  the server must stay blind) or user-pinned memories.
- Touching: any read that returns a memory resets its idle clock
  (last_retrieved_at), so frequently used memories never decay.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .triage import ARCHIVE_CONFIDENCE, DECAY_FACTOR, DECAY_PERIOD_DAYS


def _now() -> datetime:
    return datetime.now(timezone.utc)


def effective_confidence(record, at: datetime | None = None) -> float:
    """Stored confidence decayed by idle time. Pure function (testable)."""
    at = at or _now()
    if getattr(record, "encrypted", False) or getattr(record, "pinned", False):
        return float(record.confidence)
    last = getattr(record, "last_retrieved_at", None) or record.updated_at
    idle_days = max(0.0, (at - last).total_seconds() / 86400.0)
    periods = idle_days / DECAY_PERIOD_DAYS
    return float(record.confidence) * (DECAY_FACTOR**periods)


async def apply_on_read(repo, records: list, *, actor: str = "decay") -> list:
    """Decay hook for repository read paths.

    For each active record: touch last_retrieved_at; archive (change-logged
    + audited as 'auto_archive') when the effective confidence fell below
    ARCHIVE_CONFIDENCE. Returns the records that remain active, preserving
    order.
    """
    now = _now()
    alive = []
    touched: list = []
    for rec in records:
        if rec.status != "active" or rec.encrypted or rec.pinned:
            alive.append(rec)
            continue
        eff = effective_confidence(rec, now)
        if eff < ARCHIVE_CONFIDENCE:
            await repo.archive(rec.id, actor_client_id=None)
            await repo.audit(
                action="auto_archive", memory_ids=[rec.id],
                client_id=None, scopes_used=(),
                meta={
                    "reason": "confidence_decay",
                    "effective_confidence": round(eff, 4),
                    "stored_confidence": rec.confidence,
                },
            )
            continue
        touched.append(rec.id)
        alive.append(rec)
    if touched:
        await repo.touch_retrieved(touched, at=now)
        touched_set = set(touched)
        for rec in records:
            if rec.id in touched_set:
                rec.last_retrieved_at = now
    return alive
