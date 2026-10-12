"""MCP server tests (PRD §8 F3, §11, §14 T1/T3).

Covers both transports:
- stdio: real subprocess (`python -m porter.mcp_stdio`) driven with raw
  JSON-RPC 2.0 messages over Content-Length framing (the exact bytes a CLI
  like Codex/Cursor would send).
- Streamable HTTP: real uvicorn server + the official MCP SDK client
  (streamable_http_client + ClientSession).
- Auth/scopes (in-process server.call_tool): bad/revoked tokens rejected,
  scope enforcement server-side, tag-scoped filtering fail-closed,
  untrusted_data marking, pending inbox semantics, update-only-active.

Async tests follow the repo convention: sync wrapper + asyncio.run().
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
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
PROTO = "2025-06-18"


# ------------------------------------------------------------ OAuth helpers
def _setup_headers(admin_token=None):
    h = {"X-Porter-Setup-Token": settings.setup_token}
    if admin_token:
        h["Authorization"] = f"Bearer {admin_token}"
    return h


def _register(client: TestClient, name="mcp-test-cli", admin_token=None):
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


def _mint(client: TestClient, scopes: str, admin_token=None) -> tuple[str, str, str]:
    """Register + authorize + token. Returns (access_token, client_id, secret)."""
    reg = _register(client, admin_token=admin_token)
    code = _authorize(client, reg["client_id"], scopes)
    tok = _token(client, code, reg["client_id"], reg["client_secret"])
    return tok, reg["client_id"], reg["client_secret"]


@pytest.fixture()
def auth_bundle():
    """App (auth ON, MCP mounted) + repo/store + tokens.

    admin_token has memory.admin (used to register further test clients);
    token is a least-privilege read+write token for the MCP tests.
    """
    repo = InMemoryMemoryRepository(HashEmbedder())
    store = InMemoryOAuthStore()
    app = create_app(repo, store, auth_enabled=True)
    client = TestClient(app)
    admin_token, _, _ = _mint(client, "memory.admin")
    token, cid, secret = _mint(client, "memory.read memory.write",
                               admin_token=admin_token)
    return {
        "app": app, "client": client, "repo": repo, "store": store,
        "token": token, "cid": cid, "secret": secret,
        "admin_token": admin_token,
    }


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


def _set_token(monkeypatch, token: str | None):
    if token is None:
        monkeypatch.delenv("PORTER_TOKEN", raising=False)
    else:
        monkeypatch.setenv("PORTER_TOKEN", token)


# ------------------------------------------------------------ stdio transport
class _StdioDriver:
    """Minimal JSON-RPC-over-stdio driver.

    MCP stdio framing is newline-delimited JSON (one message per line),
    NOT Content-Length framing (that is LSP).
    """

    def __init__(self, env: dict):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "porter.mcp_stdio"],
            cwd=os.path.expanduser("~/workspace/porter"),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env,
        )

    def send(self, msg: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def recv(self) -> dict:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline().decode()
        assert line, "stdio server closed stdout unexpectedly"
        return json.loads(line)

    def request(self, method: str, params: dict, rid: int) -> dict:
        self.send({"jsonrpc": "2.0", "id": rid, "method": method,
                   "params": params})
        resp = self.recv()
        assert resp.get("id") == rid, resp
        return resp

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def test_stdio_full_handshake_and_tools():
    """Real subprocess, real bytes: initialize -> tools/list -> tools/call."""
    env = dict(os.environ, AUTH_ENABLED="0", PORTER_REPO="memory",
               EMBEDDER_KIND="hash", PORTER_TOKEN="ignored-in-dev")
    drv = _StdioDriver(env)
    try:
        r = drv.request("initialize", {
            "protocolVersion": PROTO, "capabilities": {},
            "clientInfo": {"name": "porter-test", "version": "0"},
        }, 1)
        assert "result" in r, r
        assert r["result"]["serverInfo"]["name"] == "porter"

        drv.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        r = drv.request("tools/list", {}, 2)
        names = {t["name"] for t in r["result"]["tools"]}
        assert names == {"memory_search", "memory_add", "memory_update",
                          "sync_delta"}, names
        descs = {t["name"]: t["description"] for t in r["result"]["tools"]}
        assert "UNTRUSTED" in descs["memory_search"]  # T3 notice present

        # memory_add -> pending inbox (mirrors REST 202 semantics)
        r = drv.request("tools/call", {
            "name": "memory_add",
            "arguments": {"content": "stdio test memory: avoids cilantro",
                          "type": "preference", "scope_tags": ["dietary"]},
        }, 3)
        assert r["result"].get("isError") is not True, r
        payload = json.loads(r["result"]["content"][0]["text"])
        assert payload["status"] == "pending_review"
        assert payload["id"]

        # pending memories are NOT searchable (same rule as REST)
        r = drv.request("tools/call", {
            "name": "memory_search",
            "arguments": {"query": "cilantro", "top_k": 5},
        }, 4)
        payload = json.loads(r["result"]["content"][0]["text"])
        assert payload["results"] == []

        # bad UUID -> anticipated tool error (isError result, not a crash)
        r = drv.request("tools/call", {
            "name": "memory_update",
            "arguments": {"id": "not-a-uuid", "content": "x"},
        }, 5)
        assert r["result"]["isError"] is True
        assert "porter:invalid_input" in r["result"]["content"][0]["text"]
    finally:
        drv.close()


# ------------------------------------------------------------ auth + scopes
def test_mcp_auth_rejects_bad_token(auth_bundle, monkeypatch):
    async def _run():
        server = _mcp_server_for(auth_bundle)
        _set_token(monkeypatch, "garbage-token")
        with pytest.raises(ToolError, match="porter:invalid_token"):
            await server.call_tool("memory_search", {"query": "x"})
    asyncio.run(_run())


def test_mcp_auth_rejects_revoked_token(auth_bundle, monkeypatch):
    token, cid, secret = (auth_bundle["token"], auth_bundle["cid"],
                          auth_bundle["secret"])
    client = auth_bundle["client"]
    r = client.post("/oauth/revoke", data={
        "token": token, "client_id": cid, "client_secret": secret})
    assert r.status_code == 200 and r.json()["revoked"]

    async def _run():
        server = _mcp_server_for(auth_bundle)
        _set_token(monkeypatch, token)
        with pytest.raises(ToolError, match="porter:token_revoked"):
            await server.call_tool("memory_search", {"query": "x"})
    asyncio.run(_run())


def test_mcp_auth_requires_token(auth_bundle, monkeypatch):
    async def _run():
        server = _mcp_server_for(auth_bundle)
        _set_token(monkeypatch, None)
        with pytest.raises(ToolError, match="porter:invalid_token"):
            await server.call_tool("memory_search", {"query": "x"})
    asyncio.run(_run())


def test_mcp_scope_enforcement(auth_bundle, monkeypatch):
    """A write-only token cannot search; a read-only token cannot add."""
    async def _run():
        client = auth_bundle["client"]
        write_only, _, _ = _mint(client, "memory.write",
                                   admin_token=auth_bundle["admin_token"])
        read_only, _, _ = _mint(client, "memory.read",
                                admin_token=auth_bundle["admin_token"])
        server = _mcp_server_for(auth_bundle)

        _set_token(monkeypatch, write_only)
        with pytest.raises(ToolError, match="porter:insufficient_scope"):
            await server.call_tool("memory_search", {"query": "x"})

        _set_token(monkeypatch, read_only)
        with pytest.raises(ToolError, match="porter:insufficient_scope"):
            await server.call_tool("memory_add", {"content": "nope"})
    asyncio.run(_run())


def test_mcp_search_marks_untrusted_and_audits(auth_bundle, monkeypatch):
    async def _run():
        repo = auth_bundle["repo"]
        rec, _ = await repo.create_pending(
            content="user avoids cilantro", type="preference",
            scope_tags=["dietary"], confidence=0.95, source={"via": "test"})
        await repo.approve(rec.id)
        server = _mcp_server_for(auth_bundle)
        _set_token(monkeypatch, auth_bundle["token"])

        result = await server.call_tool(
            "memory_search", {"query": "what foods to avoid", "top_k": 5})
        assert not result.is_error
        payload = json.loads(result.content[0].text)
        assert len(payload["results"]) >= 1
        hit = payload["results"][0]
        assert hit["untrusted_data"] is True  # PRD principle 5
        assert set(hit) >= {"id", "content", "type", "scope_tags",
                            "confidence", "score", "untrusted_data"}
        audits = await repo.audit_list(limit=5)
        assert any(a.action == "read" and a.meta.get("via") == "mcp"
                   for a in audits)
    asyncio.run(_run())


def test_mcp_tag_scoped_token_filters_server_side(auth_bundle, monkeypatch):
    """memory.read.dietary sees dietary only; asking for other tags -> []."""
    async def _run():
        repo, client = auth_bundle["repo"], auth_bundle["client"]
        for content, tags in [("avoids cilantro", ["dietary"]),
                              ("uses pnpm", ["coding_style"])]:
            rec, _ = await repo.create_pending(
                content=content, type="preference", scope_tags=tags,
                confidence=1.0, source={"via": "test"})
            await repo.approve(rec.id)
        token, _, _ = _mint(client, "memory.read.dietary",
                               admin_token=auth_bundle["admin_token"])
        server = _mcp_server_for(auth_bundle)
        _set_token(monkeypatch, token)

        result = await server.call_tool("memory_search", {"query": "food"})
        payload = json.loads(result.content[0].text)
        assert all("dietary" in h["scope_tags"] for h in payload["results"])
        assert not any("pnpm" in h["content"] for h in payload["results"])

        # fail closed: asking only for ungranted tags returns nothing
        result = await server.call_tool(
            "memory_search", {"query": "food", "tags": ["coding_style"]})
        assert json.loads(result.content[0].text)["results"] == []
    asyncio.run(_run())


def test_mcp_add_goes_to_pending_and_update_only_active(
        auth_bundle, monkeypatch):
    async def _run():
        repo = auth_bundle["repo"]
        server = _mcp_server_for(auth_bundle)
        _set_token(monkeypatch, auth_bundle["token"])

        result = await server.call_tool("memory_add", {
            "content": "prefers pnpm over npm", "type": "preference",
            "scope_tags": ["coding_style"]})
        payload = json.loads(result.content[0].text)
        assert payload["status"] == "pending_review"
        mid = payload["id"]

        # pending memories cannot be updated (review queue owns them)
        with pytest.raises(ToolError, match="porter:not_found"):
            await server.call_tool(
                "memory_update", {"id": mid, "content": "changed"})

        await repo.approve(uuid.UUID(mid))
        result = await server.call_tool(
            "memory_update", {"id": mid, "content": "prefers pnpm and bun"})
        payload = json.loads(result.content[0].text)
        assert payload["status"] == "active"
        # History-preserving edit (PRD US2): the old record is archived and
        # linked via superseded_by; the new content lives under a NEW id.
        assert payload["id"] != mid
        assert payload["revised_from"] == mid
        old = await repo.get(uuid.UUID(mid))
        assert old.status == "archived"
        assert str(old.superseded_by) == payload["id"]
        new = await repo.get(uuid.UUID(payload["id"]))
        assert new.content == "prefers pnpm and bun"
        assert new.status == "active"
    asyncio.run(_run())


def test_mcp_add_validates_input(auth_bundle, monkeypatch):
    async def _run():
        server = _mcp_server_for(auth_bundle)
        _set_token(monkeypatch, auth_bundle["token"])
        with pytest.raises(ToolError, match="porter:invalid_input"):
            await server.call_tool("memory_add", {"content": "x" * 20001})
        with pytest.raises(ToolError, match="porter:invalid_input"):
            await server.call_tool("memory_add", {"content": "ok",
                                                  "type": "bogus"})
    asyncio.run(_run())


def test_mcp_rest_and_mcp_share_state(auth_bundle, monkeypatch):
    """Same repo behind both transports: REST-approved memory via MCP."""
    async def _run():
        repo, client = auth_bundle["repo"], auth_bundle["client"]
        r = client.post(
            "/api/v1/memories",
            json={"content": "allergic to peanuts", "type": "preference",
                  "scope_tags": ["dietary"]},
            headers={"Authorization": f"Bearer {auth_bundle['token']}"})
        assert r.status_code == 202, r.text
        await repo.approve(uuid.UUID(r.json()["id"]))
        server = _mcp_server_for(auth_bundle)
        _set_token(monkeypatch, auth_bundle["token"])
        result = await server.call_tool("memory_search", {"query": "allergies"})
        payload = json.loads(result.content[0].text)
        assert any("peanuts" in h["content"] for h in payload["results"])
    asyncio.run(_run())


# ------------------------------------------------------------ Streamable HTTP
def _start_uvicorn(app):
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    srv = uvicorn.Server(config)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.05)
    assert srv.started, "uvicorn did not start"
    port = srv.servers[0].sockets[0].getsockname()[1]
    return srv, t, port


def _mint_via_http(base: str, scopes: str) -> str:
    """Full OAuth round-trip over raw HTTP against the running server."""
    import httpx2

    c = httpx2.Client(base_url=base, timeout=10.0, trust_env=False)
    r = c.post("/api/v1/clients",
               json={"name": "http-cli", "redirect_uris": [REDIRECT]},
               headers={"X-Porter-Setup-Token": settings.setup_token})
    assert r.status_code == 201, r.text
    reg = r.json()
    r = c.get("/oauth/authorize", params={
        "client_id": reg["client_id"], "redirect_uri": REDIRECT,
        "scope": scopes, "state": "s9"})
    assert r.status_code == 200, r.text
    r = c.post("/oauth/authorize", data={
        "client_id": reg["client_id"], "redirect_uri": REDIRECT,
        "state": "s9", "decision": "approve",
        "granted_scopes": scopes.split(), "requested_scopes": scopes.split()},
        follow_redirects=False)
    assert r.status_code == 302, r.text
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    r = c.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": REDIRECT, "client_id": reg["client_id"],
        "client_secret": reg["client_secret"]})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def test_streamable_http_with_real_sdk_client():
    """Real uvicorn + official SDK client: initialize/list/call over HTTP."""
    async def _run():
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        repo = InMemoryMemoryRepository(HashEmbedder())
        store = InMemoryOAuthStore()
        app = create_app(repo, store, auth_enabled=True)
        srv, thread, port = _start_uvicorn(app)
        try:
            base = f"http://127.0.0.1:{port}"
            token = await asyncio.to_thread(
                _mint_via_http, base, "memory.read memory.write")
            # seed one active memory via REST
            import httpx as _hx
            async with _hx.AsyncClient(base_url=base,
                                        trust_env=False) as rc:
                r = await rc.post(
                    "/api/v1/memories",
                    json={"content": "user avoids cilantro",
                          "type": "preference", "scope_tags": ["dietary"]},
                    headers={"Authorization": f"Bearer {token}"})
                assert r.status_code == 202, r.text
                await repo.approve(uuid.UUID(r.json()["id"]))

            http_client = httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {token}"}, timeout=15.0,
                trust_env=False)
            async with streamable_http_client(
                    f"{base}/mcp", http_client=http_client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    assert {t.name for t in listed.tools} == {
                        "memory_search", "memory_add", "memory_update",
                        "sync_delta"}
                    res = await session.call_tool(
                        "memory_search", {"query": "cilantro", "top_k": 5})
                    assert not res.is_error
                    payload = json.loads(res.content[0].text)
                    assert any(h["untrusted_data"] is True
                               for h in payload["results"])
        finally:
            srv.should_exit = True
            thread.join(timeout=10)
    asyncio.run(_run())


def test_streamable_http_rejects_unauthenticated():
    """No Bearer -> the FastAPI auth middleware 401s before MCP runs."""
    async def _run():
        import httpx2

        repo = InMemoryMemoryRepository(HashEmbedder())
        store = InMemoryOAuthStore()
        app = create_app(repo, store, auth_enabled=True)
        srv, thread, port = _start_uvicorn(app)
        try:
            async with httpx2.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}",
                    timeout=10.0, trust_env=False) as c:
                r = await c.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": PROTO,
                                     "capabilities": {},
                                     "clientInfo": {"name": "t",
                                                    "version": "0"}}},
                    headers={"Content-Type": "application/json",
                             "Accept": "application/json, text/event-stream"})
                assert r.status_code == 401, r.status_code
                assert r.json()["error"]["code"] == "invalid_token"
        finally:
            srv.should_exit = True
            thread.join(timeout=10)
    asyncio.run(_run())
