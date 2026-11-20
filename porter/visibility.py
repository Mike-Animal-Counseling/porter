"""Shared read boundary for handoffs, contradiction excerpts and exports."""

import time
from collections.abc import Sequence


def is_current_memory(record) -> bool:
    if record is None or record.status != "active":
        return False
    source = record.source
    if source.get("pipeline") != "native_handoff":
        return True
    if record.superseded_by is not None or "effective_until" not in source:
        return False
    start, end = source.get("effective_from"), source["effective_until"]
    now = time.time()
    return (
        type(start) in (int, float)
        and start <= now
        and (end is None or (type(end) in (int, float) and now < end))
    )


def can_read_memory(record, granted: Sequence[str]) -> bool:
    if not is_current_memory(record):
        return False
    from .oauth import effective_tag_filter

    allowed = effective_tag_filter(list(granted))
    return allowed is None or bool(set(allowed).intersection(record.scope_tags or ()))
