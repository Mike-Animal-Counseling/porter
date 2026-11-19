"""Shared read boundary for handoffs, contradiction excerpts and exports."""

from collections.abc import Sequence


def can_read_memory(record, granted: Sequence[str]) -> bool:
    if record is None or record.status != "active":
        return False
    from .oauth import effective_tag_filter

    allowed = effective_tag_filter(list(granted))
    return allowed is None or bool(set(allowed).intersection(record.scope_tags or ()))
