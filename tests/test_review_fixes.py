"""Regression tests for W1 code/security review findings.

Covers: InsufficientScope shape, unified 422 error format (PRD §11),
input size limits, tag validation, audit filters, approve auditing,
and scopes_used honesty.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from porter.errors import InsufficientScope
from porter.repositories import InMemoryMemoryRepository


def test_insufficient_scope_is_403_not_501():
    # Regression: duplicated class attrs once made this 501/sync_not_available.
    e = InsufficientScope("missing required scope(s): memory.admin")
    assert e.status_code == 403
    assert e.code == "insufficient_scope"


def test_422_uses_unified_error_format(client: TestClient):
    # PRD §11: every error is {error: {code, message}} — including 422s.
    r = client.post("/api/v1/memories", json={"content": "x", "type": "bogus"})
    assert r.status_code == 422
    body = r.json()
    assert set(body.keys()) == {"error"}
    assert body["error"]["code"] == "invalid_input"
    assert body["error"]["message"]

    r = client.post("/api/v1/memories/not-a-uuid/approve")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_input"

    r = client.get("/api/v1/memories", params={"status": "bogus"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_input"


def test_content_max_length_rejected(client: TestClient):
    r = client.post("/api/v1/memories", json={"content": "x" * 20_001, "type": "fact"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_input"
    # boundary: exactly at the limit is fine
    r = client.post("/api/v1/memories", json={"content": "x" * 20_000, "type": "fact"})
    assert r.status_code == 202


def test_query_max_length_rejected(client: TestClient):
    r = client.post("/api/v1/memories/search",
                    json={"query": "x" * 2_001, "top_k": 5})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_input"


def test_tag_validation(client: TestClient):
    # empty tag
    r = client.post("/api/v1/memories",
                    json={"content": "hi", "type": "fact", "scope_tags": [""]})
    assert r.status_code == 422
    # too many tags
    r = client.post("/api/v1/memories",
                    json={"content": "hi", "type": "fact",
                          "scope_tags": [f"t{i}" for i in range(21)]})
    assert r.status_code == 422
    # tag too long
    r = client.post("/api/v1/memories",
                    json={"content": "hi", "type": "fact", "scope_tags": ["x" * 65]})
    assert r.status_code == 422


def test_audit_list_filters(repo: InMemoryMemoryRepository):
    c1, c2 = uuid4(), uuid4()

    async def _run():
        await repo.audit(action="write", client_id=c1)
        await repo.audit(action="read", client_id=c2)
        await repo.audit(action="write", client_id=c1)
        by_client = await repo.audit_list(client_id=c1)
        assert len(by_client) == 2
        assert all(r.client_id == c1 for r in by_client)
        future = datetime.now(timezone.utc) + timedelta(seconds=60)
        assert await repo.audit_list(since=future) == []
        past = datetime.now(timezone.utc) - timedelta(seconds=60)
        assert len(await repo.audit_list(since=past)) == 3

    asyncio.run(_run())


def test_approve_writes_audit_entry(client: TestClient):
    r = client.post("/api/v1/memories",
                    json={"content": "approve me", "type": "fact"})
    mid = r.json()["id"]
    client.post(f"/api/v1/memories/{mid}/approve")
    r = client.get("/api/v1/audit-log", params={"limit": 20})
    transitions = [e for e in r.json()
                   if e.get("meta", {}).get("transition") == "pending->active"]
    assert transitions and mid in transitions[0]["memory_ids"]
    assert transitions[0]["scopes_used"]  # honestly recorded


def test_delete_audit_carries_scopes(client: TestClient):
    r = client.post("/api/v1/memories",
                    json={"content": "delete me", "type": "fact"})
    mid = r.json()["id"]
    client.delete(f"/api/v1/memories/{mid}")
    r = client.get("/api/v1/audit-log", params={"limit": 20})
    deletes = [e for e in r.json() if e["action"] == "delete"]
    assert deletes and mid in deletes[0]["memory_ids"]
    assert deletes[0]["scopes_used"]  # was empty before the fix


def test_audit_log_query_filters(client: TestClient):
    r = client.get("/api/v1/audit-log",
                   params={"since": "2999-01-01T00:00:00+00:00"})
    assert r.status_code == 200
    assert r.json() == []
