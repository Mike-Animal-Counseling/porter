"""P1 auto-sync core (PRD §9.5, F9, US7).

Hub-and-spoke: Porter is the source of truth; each harness adapter keeps an
incremental cursor and pulls `GET /api/v1/sync/delta?since=<cursor>`.

This module holds the delta-building logic shared by the REST endpoint and
the MCP `sync_delta` tool so both enforce identical visibility rules:

- Requires `memory.read` (enforced by the callers via auth.require_scopes /
  MCP _need_scope) plus server-side tag-scope filtering (same
  effective_tag_filter/apply_tag_filter logic as search).
- `memory` is null for `delete` tombstones, and for `create` of a
  still-pending record when the caller lacks `memory.admin` (inbox rule:
  unapproved content never leaks to adapters).
- Non-admin callers only see changes for currently-active memories +
  delete tombstones.
- `status` is the record's current status; null for `delete` tombstones
  (the row no longer exists).
- Fetching a delta updates the adapter's last_seen (online status) but
  NEVER advances its cursor — the cursor moves only on an explicit
  POST /api/v1/sync/cursor ack.
"""
from __future__ import annotations

from typing import Any

from . import oauth as oauth_logic
from .repositories import MemoryRepository


def _record_to_dict(rec) -> dict[str, Any]:
    """MemoryOut-like dict (JSON-native). Mirrors app._to_out, kept here so
    MCP (which has no FastAPI response model) shares the exact shape."""
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
        # E2EE (P2): ciphertext travels as content with the flag; the sync
        # consumer decrypts client-side. Never plaintext server-side.
        "encrypted": bool(rec.encrypted),
        "key_id": rec.key_id,
        # PRD principle 5 (T3): synced content is untrusted input for the
        # downstream agent — same marking search results carry.
        "untrusted_data": True,
    }


async def build_sync_delta(
    repo: MemoryRepository,
    *,
    since: int,
    limit: int,
    granted: list[str],
) -> dict[str, Any]:
    """Build the delta payload shared by REST GET /sync/delta and the MCP
    sync_delta tool. `granted` is the caller's verified scope list.

    Returns {"changes": [...], "cursor": head_seq}. Each change:
    {seq, memory_id, op, status, at, actor_client_id, memory}.
    """
    raw, head = await repo.get_changes(since, limit)
    is_admin = "memory.admin" in granted
    allowed = oauth_logic.effective_tag_filter(granted)

    changes: list[dict[str, Any]] = []
    for c in raw:
        rec = None
        if c["op"] != "delete":
            # The subject of create/approve/revise changes is the record as
            # it exists NOW (e.g. a create change later approved carries the
            # now-active record). A missing row is a stale reference — the
            # delete tombstone is a separate change — so skip it.
            rec = await repo.get(c["memory_id"])
            if rec is None:
                continue
            if allowed is not None and not set(allowed).intersection(rec.scope_tags):
                continue  # tag-scope filtering, server-side (T1)
        if not is_admin and c["op"] != "delete":
            # Non-admin adapters see currently-active memories only. This
            # also enforces the inbox rule: a pending create change whose
            # record is still pending is invisible here entirely.
            if rec is None or rec.status != "active":
                continue
        memory = None
        if c["op"] != "delete" and rec is not None and (
            is_admin or rec.status == "active"
        ):
            memory = _record_to_dict(rec)
        changes.append(
            {
                "seq": c["seq"],
                "memory_id": str(c["memory_id"]),
                "op": c["op"],
                "status": rec.status if rec is not None else None,
                "at": c["at"].isoformat(),
                "actor_client_id": (
                    str(c["actor_client_id"])
                    if c["actor_client_id"] is not None
                    else None
                ),
                "memory": memory,
            }
        )
    return {"changes": changes, "cursor": head}


async def touch_cursor(repo: MemoryRepository, adapter_id: str) -> None:
    """Bump last_seen for an adapter WITHOUT moving its cursor (explicit
    ack only). Called on every delta fetch for online-status tracking."""
    cursors = {c["adapter_id"]: c["cursor"] for c in await repo.list_cursors()}
    await repo.upsert_cursor(adapter_id, cursors.get(adapter_id, 0))
