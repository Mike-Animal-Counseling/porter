"""Scope and owner boundaries on the integrated switching endpoints."""

import asyncio
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError
from test_deployed_api import mint

from porter.app import create_app
from porter.auth import AuthContext, RevocationCache
from porter.embeddings import HashEmbedder
from porter.mcp_server import MCPDeps, build_mcp_server
from porter.oauth_store import InMemoryOAuthStore
from porter.ratelimit import TokenBucket
from porter.repositories import InMemoryMemoryRepository
from porter.writeback import WriteResult


@pytest.fixture()
def api():
    repo = InMemoryMemoryRepository(HashEmbedder())
    store = InMemoryOAuthStore()
    app = create_app(repo, store, auth_enabled=True, rate_limit_per_minute=10000, enable_mcp=False)
    client = TestClient(app, follow_redirects=False)
    admin = mint(client, "admin", "memory.admin")
    reader = mint(client, "reader", "memory.read.work")
    writer = mint(client, "writer", "memory.write")

    async def seed():
        records = {}
        for name, kind, tags in (
            ("visible", "project", ["work", "project:demo"]),
            ("private", "project", ["health", "project:demo"]),
            ("personal", "personal", ["health"]),
        ):
            record, _ = await repo.create_pending(
                content=name + " unique memory", type=kind, scope_tags=tags, confidence=1, source={}
            )
            await repo.approve(record.id)
            records[name] = record
        return records

    records = asyncio.run(seed())
    return client, repo, store, app, admin, reader, writer, records


def headers(token):
    return {"Authorization": "Bearer " + token["access_token"]}


def test_handoff_cannot_export_private_project_or_personal_memories(api):
    client, _repo, _store, _app, _admin, reader, _writer, records = api
    response = client.post(
        "/api/v1/switch/handoff",
        headers=headers(reader),
        json={"project": "demo", "from_harness": "codex", "to_harness": "cursor"},
    )
    assert response.status_code == 200
    assert str(records["visible"].id) in response.text
    assert str(records["private"].id) not in response.text
    assert str(records["personal"].id) not in response.text
    assert "private unique memory" not in response.text


def test_contradictions_hide_inaccessible_source(api):
    client, _repo, _store, _app, _admin, reader, _writer, records = api
    response = client.get(
        f"/api/v1/memories/{records['private'].id}/contradictions", headers=headers(reader)
    )
    assert response.status_code == 404


def test_writeback_defaults_to_preview_and_filters_before_writer(api, monkeypatch):
    from porter import writeback

    client, _repo, _store, _app, _admin, reader, _writer, records = api
    captured = {}

    class Writer:
        async def write(self, memories, **kwargs):
            captured.update(memories=memories, **kwargs)
            return WriteResult()

    monkeypatch.setattr(writeback, "get_writer", lambda _harness: Writer())
    response = client.post(
        "/api/v1/writeback/sync", headers=headers(reader), json={"harness": "codex"}
    )
    assert response.status_code == 200
    assert captured["dry_run"] is True
    assert [m["id"] for m in captured["memories"]] == [str(records["visible"].id)]
    assert (
        client.post(
            "/api/v1/writeback/sync",
            headers=headers(reader),
            json={"harness": "codex", "dry_run": False},
        ).status_code
        == 403
    )


def test_merge_resolution_and_key_management_require_owner_scope(api):
    client, _repo, _store, _app, _admin, reader, writer, records = api
    ids = [str(records["visible"].id), str(records["private"].id)]
    assert (
        client.post(
            "/api/v1/memories/merge", headers=headers(writer), json={"memory_ids": ids}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/memories/resolve",
            headers=headers(writer),
            json={"winner_id": ids[0], "loser_ids": ids[1:], "resolution": "newer"},
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/e2ee/envelopes", headers=headers(reader), params={"device_id": "other-device"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/e2ee/devices",
            headers=headers(reader),
            json={"device_id": "other-device", "public_key": "invalid"},
        ).status_code
        == 403
    )


def test_workspace_viewer_cannot_write_even_with_general_write_scope(api):
    client, repo, store, app, admin, _reader, writer, _records = api
    response = client.post("/api/v1/workspaces", headers=headers(admin), json={"name": "team"})
    assert response.status_code == 200
    workspace = response.json()
    writer_client = asyncio.run(store.get_client_by_client_id(writer["client_id"]))
    response = client.post(
        f"/api/v1/workspaces/{workspace['id']}/members",
        headers=headers(admin),
        json={"client_id": str(writer_client.id), "role": "viewer"},
    )
    assert response.status_code == 200
    response = client.post(
        "/api/v1/memories",
        headers=headers(writer),
        json={
            "content": "Team decision",
            "type": "project",
            "scope_tags": [workspace["project_tag"]],
        },
    )
    assert response.status_code == 403
    response = client.post(
        "/api/v1/memories",
        headers=headers(admin),
        json={
            "content": "Private preference",
            "type": "personal",
            "scope_tags": [workspace["project_tag"]],
        },
    )
    assert response.status_code == 422


def test_workspace_invite_by_non_owner_returns_forbidden(api):
    client, _repo, _store, _app, admin, _reader, writer, _records = api
    workspace = client.post(
        "/api/v1/workspaces", headers=headers(admin), json={"name": "team"}
    ).json()
    response = client.post(
        f"/api/v1/workspaces/{workspace['id']}/members",
        headers=headers(writer),
        json={"client_id": str(uuid4()), "role": "viewer"},
    )
    assert response.status_code == 403


def test_mcp_handoff_and_writeback_enforce_same_boundaries(api, monkeypatch):
    from porter import mcp_server, writeback

    _client, repo, store, _app, _admin, _reader, _writer, records = api

    async def auth(*_args):
        return AuthContext(None, "scoped", ["memory.read.work"])

    monkeypatch.setattr(mcp_server, "_resolve_mcp_auth", auth)
    captured = []

    class Writer:
        async def write(self, memories, **_kwargs):
            captured.extend(memories)
            return WriteResult()

    monkeypatch.setattr(writeback, "get_writer", lambda _harness: Writer())
    server = build_mcp_server(
        MCPDeps(
            repo_fn=lambda: repo,
            oauth_store_fn=lambda: store,
            revocation_cache_fn=lambda: RevocationCache(),
            rate_limiter_fn=lambda: TokenBucket(10000),
            auth_enabled_fn=lambda: True,
        )
    )

    async def run():
        result = await server.call_tool(
            "switch_handoff", {"project": "demo", "from_harness": "codex", "to_harness": "cursor"}
        )
        payload = json.loads(result.content[0].text)
        assert str(records["private"].id) not in json.dumps(payload)
        await server.call_tool("writeback_sync", {"harness": "codex"})
        assert [r["id"] for r in captured] == [str(records["visible"].id)]
        with pytest.raises(ToolError, match="memory.admin"):
            await server.call_tool("writeback_sync", {"harness": "codex", "dry_run": False})

    asyncio.run(run())
