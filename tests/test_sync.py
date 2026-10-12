"""P1 auto-sync core tests (PRD §9.5 F9, US7).

Covers the exact REST/MCP contracts a sibling agent builds sample clients
against:

- GET /api/v1/sync/delta?since=<int>&limit=<int> ->
  {"changes": [...], "cursor": <head seq>}; each change
  {seq, memory_id, op, status, at, actor_client_id, memory}.
- POST /api/v1/sync/cursor {"cursor": int} -> {adapter_id, cursor, last_seen}.
- US7: A writes+approves, B polls and sees the change promptly (the 60s
  window is simulated by asserting immediately); concurrent edits
  last-writer-wins with the loser preserved in the pending inbox.
- Visibility rules: tag-scoped tokens see only their tags' changes;
  pending creates are invisible to non-admin delta fetches; delete
  tombstones (memory=null) are visible to everyone.
- Validation: since must be an int >= 0 (422 otherwise).
- MCP sync_delta tool + memory_update expected_version mirror the REST rules.
- Panel /panel/sync renders the adapter table with the nav entry.
"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from porter.app import create_app
from porter.config import settings
from porter.embeddings import HashEmbedder
from porter.mcp_server import MCPDeps, build_mcp_server
from porter.oauth_store import InMemoryOAuthStore
from porter.repositories import InMemoryMemoryRepository

REDIRECT = "http://localhost:9999/callback"


# ------------------------------------------------------------ OAuth helpers
def _setup_headers(admin_token=None):
    # P0: the very first client registration requires the one-time setup
    # token (auto-generated per process unless PORTER_SETUP_TOKEN is set).
    h = {"X-Porter-Setup-Token": settings.setup_token}
    if admin_token:
        h["Authorization"] = f"Bearer {admin_token}"
    return h


def _register(client: TestClient, name: str = "sync-cli", admin_token=None):
    r = client.post(
        "/api/v1/clients",
        json={"name": name, "redirect_uris": [REDIRECT]},
        headers=_setup_headers(admin_token),
    )
    assert r.status_code == 201, r.text
    return r.json()


def _authorize(client: TestClient, cid: str, scopes: str) -> str:
    r = client.get(
        "/oauth/authorize",
        params={"client_id": cid, "redirect_uri": REDIRECT,
                "scope": scopes, "state": "s1"},
    )
    assert r.status_code == 200, r.text
    r = client.post(
        "/oauth/authorize",
        data={"client_id": cid, "redirect_uri": REDIRECT, "state": "s1",
              "decision": "approve", "granted_scopes": scopes.split(),
              "requested_scopes": scopes.split()},
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    return parse_qs(urlparse(r.headers["location"]).query)["code"][0]


def _token(client: TestClient, code: str, cid: str, secret: str) -> str:
    r = client.post(
        "/oauth/token",
        data={"grant_type": "authorization_code", "code": code,
              "redirect_uri": REDIRECT, "client_id": cid,
              "client_secret": secret},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _mint(client: TestClient, scopes: str, admin_token=None, name="sync-cli"):
    """Register + authorize + token. Returns (access_token, client_id, secret)."""
    reg = _register(client, name=name, admin_token=admin_token)
    code = _authorize(client, reg["client_id"], scopes)
    tok = _token(client, code, reg["client_id"], reg["client_secret"])
    return tok, reg["client_id"], reg["client_secret"]


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def sapp():
    """Auth-enabled app + repo/store + an admin token for minting clients."""
    repo = InMemoryMemoryRepository(HashEmbedder())
    store = InMemoryOAuthStore()
    app = create_app(repo, store, auth_enabled=True)
    client = TestClient(app)
    admin_token, _, _ = _mint(client, "memory.admin")
    return {
        "app": app, "client": client, "repo": repo, "store": store,
        "admin_token": admin_token,
    }


def _write_approve(sapp, token, content, tags=()):
    """Adapter writes a memory; owner-side approval (approve needs memory.admin,
    which in the real flow is the user at the panel / consent screen)."""
    c = sapp["client"]
    r = c.post(
        "/api/v1/memories",
        json={"content": content, "type": "fact", "scope_tags": list(tags)},
        headers=_bearer(token),
    )
    assert r.status_code == 202, r.text
    mid = r.json()["id"]
    r = c.post(f"/api/v1/memories/{mid}/approve", headers=_bearer(token))
    assert r.status_code == 200, r.text
    return mid


# ------------------------------------------------------------ delta feed (US7 happy path)
def test_delta_two_adapters_see_approve_with_content(sapp):
    # Adapter A (write+admin so it can approve in this test harness) writes
    # and approves; adapter B (read-only) polls and promptly sees the
    # change with full content (the simulated 60s window: assert now).
    token_a, cid_a, _ = _mint(
        sapp["client"], "memory.read memory.write memory.admin",
        admin_token=sapp["admin_token"], name="adapter-a")
    token_b, cid_b, _ = _mint(
        sapp["client"], "memory.read",
        admin_token=sapp["admin_token"], name="adapter-b")

    mid = _write_approve(sapp, token_a, "user prefers dark mode", ["workflow"])

    r = sapp["client"].get("/api/v1/sync/delta?since=0",
                           headers=_bearer(token_b))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"changes", "cursor"}
    assert body["cursor"] >= 2  # create + approve changes at least
    approves = [c for c in body["changes"] if c["op"] == "approve"]
    assert len(approves) == 1
    ch = approves[0]
    assert set(ch) == {"seq", "memory_id", "op", "status",
                       "at", "actor_client_id", "memory"}
    assert ch["memory_id"] == mid
    assert ch["status"] == "active"
    assert ch["actor_client_id"] is not None  # A's client pk
    assert ch["memory"] is not None
    assert ch["memory"]["content"] == "user prefers dark mode"
    assert ch["memory"]["status"] == "active"
    assert ch["memory"]["version"] == 1

    # Cursor is NOT auto-advanced by the fetch: B acks explicitly.
    r = sapp["client"].post("/api/v1/sync/cursor", json={"cursor": body["cursor"]},
                            headers=_bearer(token_b))
    assert r.status_code == 200, r.text
    ack = r.json()
    assert set(ack) == {"adapter_id", "cursor", "last_seen"}
    assert ack["adapter_id"] == cid_b  # the token's public client_id
    assert ack["cursor"] == body["cursor"]
    assert ack["last_seen"]  # ISO timestamp present

    # After the ack, no changes remain.
    r = sapp["client"].get(f"/api/v1/sync/delta?since={body['cursor']}",
                           headers=_bearer(token_b))
    assert r.status_code == 200
    assert r.json()["changes"] == []
    assert r.json()["cursor"] == body["cursor"]


def test_cursor_upsert_overwrites_and_returns_last_seen(sapp):
    token_b, cid_b, _ = _mint(
        sapp["client"], "memory.read",
        admin_token=sapp["admin_token"], name="adapter-b")
    c = sapp["client"]
    r = c.post("/api/v1/sync/cursor", json={"cursor": 0},
               headers=_bearer(token_b))
    assert r.status_code == 200
    first_seen = r.json()["last_seen"]
    r = c.post("/api/v1/sync/cursor", json={"cursor": 41},
               headers=_bearer(token_b))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["adapter_id"] == cid_b
    assert body["cursor"] == 41
    assert body["last_seen"] >= first_seen

    # No special scope needed: a write-only token can also ack its cursor.
    token_w, cid_w, _ = _mint(
        sapp["client"], "memory.write",
        admin_token=sapp["admin_token"], name="adapter-w")
    r = c.post("/api/v1/sync/cursor", json={"cursor": 3},
               headers=_bearer(token_w))
    assert r.status_code == 200, r.text
    assert r.json()["adapter_id"] == cid_w

    # Anonymous (no bearer) is rejected.
    r = c.post("/api/v1/sync/cursor", json={"cursor": 1})
    assert r.status_code == 401


def test_cursor_rejects_negative(sapp):
    token_b, _, _ = _mint(
        sapp["client"], "memory.read",
        admin_token=sapp["admin_token"], name="adapter-b")
    r = sapp["client"].post("/api/v1/sync/cursor", json={"cursor": -1},
                            headers=_bearer(token_b))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_input"


def test_delta_since_validation(sapp):
    token_b, _, _ = _mint(
        sapp["client"], "memory.read",
        admin_token=sapp["admin_token"], name="adapter-b")
    c = sapp["client"]
    r = c.get("/api/v1/sync/delta?since=-1", headers=_bearer(token_b))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "invalid_input"
    r = c.get("/api/v1/sync/delta?since=abc", headers=_bearer(token_b))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "invalid_input"
    r = c.get("/api/v1/sync/delta?limit=600", headers=_bearer(token_b))
    assert r.status_code == 422, r.text


def test_delta_requires_memory_read_scope(sapp):
    token_w, _, _ = _mint(
        sapp["client"], "memory.write",
        admin_token=sapp["admin_token"], name="adapter-w")
    r = sapp["client"].get("/api/v1/sync/delta?since=0",
                           headers=_bearer(token_w))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "insufficient_scope"
    r = sapp["client"].get("/api/v1/sync/delta?since=0")
    assert r.status_code == 401


def test_sync_fetch_is_audited(sapp):
    token_a, _, _ = _mint(
        sapp["client"], "memory.read memory.write memory.admin",
        admin_token=sapp["admin_token"], name="adapter-a")
    token_b, _, _ = _mint(
        sapp["client"], "memory.read",
        admin_token=sapp["admin_token"], name="adapter-b")
    _write_approve(sapp, token_a, "audited sync fetch", ["general"])
    r = sapp["client"].get("/api/v1/sync/delta?since=0",
                           headers=_bearer(token_b))
    assert r.status_code == 200

    async def check():
        rows = await sapp["repo"].audit_list(limit=10)
        return [a for a in rows
                if a.action == "read" and a.meta.get("via") == "sync"]
    hits = asyncio.run(check())
    assert len(hits) == 1
    assert hits[0].meta["via"] == "sync"


def test_delta_fetch_touches_last_seen_not_cursor(sapp):
    token_b, cid_b, _ = _mint(
        sapp["client"], "memory.read",
        admin_token=sapp["admin_token"], name="adapter-b")
    c = sapp["client"]
    c.post("/api/v1/sync/cursor", json={"cursor": 0},
           headers=_bearer(token_b))
    c.get("/api/v1/sync/delta?since=0", headers=_bearer(token_b))

    async def check():
        rows = await sapp["repo"].list_cursors()
        return [x for x in rows if x["adapter_id"] == cid_b]
    rows = asyncio.run(check())
    assert len(rows) == 1
    assert rows[0]["cursor"] == 0  # NOT auto-advanced
    assert rows[0]["last_seen"] is not None


# ------------------------------------------------------------ visibility rules
def test_pending_create_invisible_to_non_admin_delta(sapp):
    token_a, _, _ = _mint(
        sapp["client"], "memory.read memory.write memory.admin",
        admin_token=sapp["admin_token"], name="adapter-a")
    token_b, _, _ = _mint(
        sapp["client"], "memory.read",
        admin_token=sapp["admin_token"], name="adapter-b")
    # A writes but does NOT approve: unapproved content must never leak.
    # (Sensitive content forces the triage queue even in tiered mode.)
    r = sapp["client"].post(
        "/api/v1/memories",
        json={"content": "secret pending thought: my medication is lisinopril",
              "type": "fact", "scope_tags": ["general"]},
        headers=_bearer(token_a),
    )
    assert r.status_code == 202
    assert r.json()["status"] == "pending_review"
    r = sapp["client"].get("/api/v1/sync/delta?since=0",
                           headers=_bearer(token_b))
    assert r.status_code == 200
    assert all(c["op"] != "create" or c["memory"] is not None
               for c in r.json()["changes"])
    assert not any("secret pending thought" in json.dumps(c)
                   for c in r.json()["changes"])
    # The admin token sees the pending create with content attached.
    r = sapp["client"].get("/api/v1/sync/delta?since=0",
                           headers=_bearer(token_a))
    assert r.status_code == 200
    creates = [c for c in r.json()["changes"] if c["op"] == "create"]
    assert any(c["memory"] is not None
               and c["memory"]["content"] == "secret pending thought: my medication is lisinopril"
               and c["memory"]["status"] == "pending"
               for c in creates)


def test_delete_tombstone_visible_to_non_admin(sapp):
    token_a, _, _ = _mint(
        sapp["client"], "memory.read memory.write memory.admin",
        admin_token=sapp["admin_token"], name="adapter-a")
    token_b, _, _ = _mint(
        sapp["client"], "memory.read",
        admin_token=sapp["admin_token"], name="adapter-b")
    mid = _write_approve(sapp, token_a, "doomed memory", ["general"])
    # B consumes everything first.
    r = sapp["client"].get("/api/v1/sync/delta?since=0",
                           headers=_bearer(token_b))
    head = r.json()["cursor"]
    r = sapp["client"].delete(f"/api/v1/memories/{mid}",
                              headers=_bearer(token_a))
    assert r.status_code == 204
    r = sapp["client"].get(f"/api/v1/sync/delta?since={head}",
                           headers=_bearer(token_b))
    assert r.status_code == 200
    changes = r.json()["changes"]
    assert len(changes) == 1
    tomb = changes[0]
    assert tomb["op"] == "delete"
    assert tomb["memory_id"] == mid
    assert tomb["memory"] is None  # tombstone carries no content
    assert tomb["status"] is None


def test_tag_scoped_token_sees_only_its_tags_changes(sapp):
    token_a, _, _ = _mint(
        sapp["client"], "memory.read memory.write memory.admin",
        admin_token=sapp["admin_token"], name="adapter-a")
    token_diet, _, _ = _mint(
        sapp["client"], "memory.read.dietary",
        admin_token=sapp["admin_token"], name="adapter-diet")
    _write_approve(sapp, token_a, "avoids cilantro", ["dietary"])
    _write_approve(sapp, token_a, "uses pnpm", ["coding_style"])
    r = sapp["client"].get("/api/v1/sync/delta?since=0",
                           headers=_bearer(token_diet))
    assert r.status_code == 200
    changes = r.json()["changes"]
    assert changes, "expected the dietary changes to be visible"
    for c in changes:
        assert c["op"] != "delete"
        assert "dietary" in c["memory"]["scope_tags"]
    assert not any("pnpm" in json.dumps(c) for c in changes)


def test_revise_change_subject_is_new_record(sapp):
    token_a, _, _ = _mint(
        sapp["client"], "memory.read memory.write memory.admin",
        admin_token=sapp["admin_token"], name="adapter-a")
    token_b, _, _ = _mint(
        sapp["client"], "memory.read",
        admin_token=sapp["admin_token"], name="adapter-b")
    mid = _write_approve(sapp, token_a, "old content", ["general"])
    r = sapp["client"].patch(f"/api/v1/memories/{mid}",
                             json={"content": "new content"},
                             headers=_bearer(token_a))
    assert r.status_code == 200
    new_id = r.json()["memory"]["id"]
    assert r.json()["memory"]["version"] == 2
    r = sapp["client"].get("/api/v1/sync/delta?since=0",
                           headers=_bearer(token_b))
    revises = [c for c in r.json()["changes"] if c["op"] == "revise"]
    assert len(revises) == 1
    assert revises[0]["memory_id"] == new_id
    assert revises[0]["memory"]["content"] == "new content"
    assert revises[0]["status"] == "active"


# ------------------------------------------------------------ US7 conflicts
def test_conflict_lww_loser_preserved_in_pending_inbox(sapp):
    # A and B both read v1. A updates (expected_version=1) -> OK.
    # B updates (expected_version=1) -> 200 conflict=true; the loser's
    # (A's) content lands in the pending inbox, nothing silently dropped.
    token_a, _, _ = _mint(
        sapp["client"], "memory.read memory.write memory.admin",
        admin_token=sapp["admin_token"], name="adapter-a")
    token_b, _, _ = _mint(
        sapp["client"], "memory.read memory.write memory.admin",
        admin_token=sapp["admin_token"], name="adapter-b")
    mid = _write_approve(sapp, token_a, "v1 content", ["general"])

    c = sapp["client"]
    r = c.patch(f"/api/v1/memories/{mid}",
                json={"content": "A's edit", "expected_version": 1},
                headers=_bearer(token_a))
    assert r.status_code == 200, r.text
    assert r.json()["conflict"] is False
    assert r.json()["loser_pending_id"] is None
    assert r.json()["memory"]["content"] == "A's edit"
    assert r.json()["memory"]["version"] == 2

    r = c.patch(f"/api/v1/memories/{mid}",
                json={"content": "B's edit", "expected_version": 1},
                headers=_bearer(token_b))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["conflict"] is True
    assert body["loser_pending_id"] is not None
    assert body["memory"]["content"] == "B's edit"  # last-writer-wins
    assert body["memory"]["version"] == 3

    # The loser's content (A's edit) is preserved as a NEW pending record.
    loser_id = body["loser_pending_id"]
    r = c.get("/api/v1/memories?status=pending",
              headers=_bearer(token_a))
    assert r.status_code == 200
    pendings = {m["id"]: m for m in r.json()}
    assert loser_id in pendings
    loser = pendings[loser_id]
    assert loser["content"] == "A's edit"  # overwritten content preserved
    assert loser["confidence"] == 1.0  # confidence preserved
    assert loser["source"]["conflict_of"] == body["memory"]["id"]

    # B's sync feed shows the revise + the conflict create for the loser.
    r = c.get("/api/v1/sync/delta?since=0", headers=_bearer(token_b))
    ops = [(x["op"], x["memory"]["content"] if x["memory"] else None)
           for x in r.json()["changes"]]
    assert ("revise", "B's edit") in ops


def test_expected_version_match_is_plain_revise(sapp):
    token_a, _, _ = _mint(
        sapp["client"], "memory.read memory.write memory.admin",
        admin_token=sapp["admin_token"], name="adapter-a")
    mid = _write_approve(sapp, token_a, "v1", ["general"])
    r = sapp["client"].patch(
        f"/api/v1/memories/{mid}",
        json={"content": "v2", "expected_version": 1},
        headers=_bearer(token_a))
    assert r.status_code == 200, r.text
    assert r.json()["conflict"] is False
    assert r.json()["memory"]["version"] == 2
    r = sapp["client"].patch(
        f"/api/v1/memories/{r.json()['memory']['id']}",
        json={"content": "v3", "expected_version": 2},
        headers=_bearer(token_a))
    assert r.status_code == 200
    assert r.json()["conflict"] is False
    assert r.json()["memory"]["version"] == 3

    async def check():
        pendings = await sapp["repo"].list(status="pending")
        return pendings
    assert asyncio.run(check()) == []  # no loser records created


def test_revise_without_expected_version_unchanged(sapp):
    token_a, _, _ = _mint(
        sapp["client"], "memory.read memory.write memory.admin",
        admin_token=sapp["admin_token"], name="adapter-a")
    mid = _write_approve(sapp, token_a, "plain", ["general"])
    r = sapp["client"].patch(f"/api/v1/memories/{mid}",
                             json={"content": "plain v2"},
                             headers=_bearer(token_a))
    assert r.status_code == 200
    assert r.json()["conflict"] is False
    assert r.json()["memory"]["version"] == 2


def test_expected_version_requires_content(sapp):
    token_a, _, _ = _mint(
        sapp["client"], "memory.read memory.write memory.admin",
        admin_token=sapp["admin_token"], name="adapter-a")
    mid = _write_approve(sapp, token_a, "x", ["general"])
    r = sapp["client"].patch(f"/api/v1/memories/{mid}",
                             json={"expected_version": 1},
                             headers=_bearer(token_a))
    assert r.status_code == 422


def test_repo_update_with_version_unit():
    """Repository-level contract: (new, loser) tuples."""
    async def run():
        repo = InMemoryMemoryRepository(HashEmbedder())
        rec, _ = await repo.create_pending(
            content="v1", type="fact", scope_tags=[], confidence=0.8,
            source={})
        # expected_version None -> plain revise, loser None
        new, loser = await repo.update_with_version(
            rec.id, expected_version=None, content="v2")
        assert loser is None and new.version == 2
        # matching version -> plain revise
        new2, loser2 = await repo.update_with_version(
            new.id, expected_version=2, content="v3")
        assert loser2 is None and new2.version == 3
        # mismatch -> LWW + loser pending record
        new3, loser3 = await repo.update_with_version(
            new2.id, expected_version=2, content="v4")
        assert new3.version == 4 and new3.content == "v4"
        assert loser3 is not None
        assert loser3.status == "pending"
        assert loser3.content == "v3"  # overwritten content
        assert loser3.confidence == 0.8
        assert loser3.source["conflict_of"] == str(new3.id)
        # missing id -> (None, None)
        from uuid import uuid4
        assert await repo.update_with_version(
            uuid4(), expected_version=1, content="z") == (None, None)
    asyncio.run(run())


# ------------------------------------------------------------ MCP
def _mcp_server_for(bundle):
    app = bundle["app"]
    return build_mcp_server(
        MCPDeps(
            repo_fn=lambda: bundle["repo"],
            oauth_store_fn=lambda: bundle["store"],
            revocation_cache_fn=lambda: app.state.revocation_cache,
            rate_limiter_fn=lambda: app.state.rate_limiter,
            auth_enabled_fn=lambda: True,
        )
    )


def test_mcp_sync_delta_tool(sapp, monkeypatch):
    async def run():
        repo = sapp["repo"]
        token_a, _, _ = _mint(
            sapp["client"], "memory.read memory.write memory.admin",
            admin_token=sapp["admin_token"], name="adapter-a")
        token_diet, _, _ = _mint(
            sapp["client"], "memory.read.dietary",
            admin_token=sapp["admin_token"], name="adapter-diet")
        _write_approve(sapp, token_a, "avoids cilantro", ["dietary"])
        _write_approve(sapp, token_a, "uses pnpm", ["coding_style"])

        server = _mcp_server_for(sapp)
        monkeypatch.setenv("PORTER_TOKEN", token_diet)
        result = await server.call_tool("sync_delta", {"since": 0})
        assert not result.is_error, result.content[0].text
        payload = json.loads(result.content[0].text)
        assert set(payload) == {"changes", "cursor"}
        assert payload["cursor"] >= 4
        for c in payload["changes"]:
            assert "dietary" in c["memory"]["scope_tags"]
        assert not any("pnpm" in json.dumps(c) for c in payload["changes"])

        # negative since is rejected as a tool error, not a crash
        with pytest.raises(ToolError, match="porter:invalid_input"):
            await server.call_tool("sync_delta", {"since": -1})

        # cursor was NOT auto-advanced by the fetch
        cursors = await repo.list_cursors()
        assert all(c["cursor"] == 0 for c in cursors)
    asyncio.run(run())


def test_mcp_memory_update_expected_version_conflict(sapp, monkeypatch):
    async def run():
        token_a, _, _ = _mint(
            sapp["client"], "memory.read memory.write memory.admin",
            admin_token=sapp["admin_token"], name="adapter-a")
        mid = _write_approve(sapp, token_a, "v1", ["general"])
        server = _mcp_server_for(sapp)
        monkeypatch.setenv("PORTER_TOKEN", token_a)

        r = await server.call_tool(
            "memory_update",
            {"id": mid, "content": "A edit", "expected_version": 1})
        p = json.loads(r.content[0].text)
        assert p["conflict"] is False
        assert p["version"] == 2

        r = await server.call_tool(
            "memory_update",
            {"id": mid, "content": "B edit", "expected_version": 1})
        p = json.loads(r.content[0].text)
        assert p["conflict"] is True
        assert p["loser_pending_id"] is not None
        assert p["version"] == 3
    asyncio.run(run())


# ------------------------------------------------------------ panel
def test_panel_sync_page_renders(sapp):
    # Panel runs auth-disabled in this fixture style: use a dedicated client.
    repo = sapp["repo"]
    store = sapp["store"]
    app = create_app(repo, store, auth_enabled=False)
    pclient = TestClient(app)
    r = pclient.get("/panel/sync")
    assert r.status_code == 200, r.text
    assert "Sync" in r.text  # nav_sync + page title (en default)
    assert "/panel/sync" in r.text  # nav entry present

    async def seed():
        await repo.upsert_cursor("adapter-x", 7)
    asyncio.run(seed())
    r = pclient.get("/panel/sync")
    assert r.status_code == 200
    assert "adapter-x" in r.text
    assert "#7" in r.text  # cursor rendered as mono #<seq>
