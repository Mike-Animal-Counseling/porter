"""Audit log append-only enforcement (PRD §13.3, threat T7).

Two layers:
1. DB trigger in migrations/001_init.sql blocks UPDATE/DELETE at the database
   level (verified here by inspecting the migration).
2. The in-memory repository mirrors the rule (lock_audit) so the domain
   invariant is tested without Postgres.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from porter.repositories import InMemoryMemoryRepository


def test_migration_defines_append_only_triggers():
    sql = Path(__file__).resolve().parent.parent.joinpath(
        "migrations", "001_init.sql").read_text()
    assert "forbid_audit_mutation" in sql
    assert "BEFORE UPDATE ON audit_log" in sql
    assert "BEFORE DELETE ON audit_log" in sql
    assert "RAISE EXCEPTION 'audit_log is append-only" in sql


def test_inmemory_audit_rejects_mutation_after_lock(repo: InMemoryMemoryRepository):
    import asyncio

    async def _run():
        await repo.audit(action="write")
        repo.lock_audit()  # simulate the DB trigger
        with pytest.raises(RuntimeError, match="append-only"):
            await repo.audit(action="write")

    asyncio.run(_run())


def test_delete_keeps_audit_event_without_content(client: TestClient):
    r = client.post("/api/v1/memories", json={
        "content": "secret memory to be deleted", "type": "fact",
        "scope_tags": ["general"],
    })
    mid = r.json()["id"]
    client.post(f"/api/v1/memories/{mid}/approve")
    client.delete(f"/api/v1/memories/{mid}")

    r = client.get("/api/v1/audit-log", params={"limit": 20})
    deletes = [e for e in r.json() if e["action"] == "delete"]
    assert deletes and mid in deletes[0]["memory_ids"]
    # audit meta must not contain the memory content (PRD US5)
    blob = str(r.json())
    assert "secret memory to be deleted" not in blob
