"""Shared export payload builder (porter-export/v1).

Used by both GET /api/v1/export (app.py) and GET /panel/export (panel.py)
so the two surfaces can never drift apart.
"""
from __future__ import annotations

from datetime import datetime, timezone


async def build_export_payload(repo) -> dict:
    memories = await repo.list(limit=100_000)
    return {
        "format": "porter-export/v1",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "memory_count": len(memories),
        "memories": [
            {
                "id": str(m.id),
                "content": m.content,
                "type": m.type,
                "scope_tags": list(m.scope_tags),
                "confidence": m.confidence,
                "status": m.status,
                "source": dict(m.source),
                "created_at": m.created_at.isoformat(),
                "updated_at": m.updated_at.isoformat(),
                "superseded_by": str(m.superseded_by) if m.superseded_by else None,
                # E2EE (P2): encrypted memories export as ciphertext with the
                # flag — there is no plaintext server-side to leak.
                "encrypted": bool(m.encrypted),
                "key_id": m.key_id,
            }
            for m in memories
        ],
        "note": "Embeddings are not exported; re-embed on import. "
                "Audit log exports as metadata only (no memory content).",
    }
