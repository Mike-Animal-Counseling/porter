"""Shared read boundary for handoffs, contradiction excerpts and exports."""

from collections.abc import Sequence

from .oauth import effective_tag_filter


def can_read_memory(record, granted: Sequence[str]) -> bool:
    if record is None or record.status != "active":
        return False
    allowed = effective_tag_filter(list(granted))
    return allowed is None or bool(set(allowed).intersection(record.scope_tags or ()))
