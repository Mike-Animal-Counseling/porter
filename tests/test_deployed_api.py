"""Acceptance against a separately running Docker stack (opt-in).

Use only the isolated verification stack: PORTER_TEST_BASE_URL,
PORTER_TEST_SETUP_TOKEN and PORTER_TEST_JWT_SECRET. Optional container
recreation is restricted to the porter-review stack on port 18000.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
REDIRECT = "http://localhost:18765/callback"


def mint(owner, name, scopes):
    response = owner.post("/panel/clients/new", data={"name": name, "redirect_uris": REDIRECT})
    assert response.status_code == 200
    client_id = re.search(
        r'<code class="small">(porter_[A-Za-z0-9_-]+)</code>', response.text
    ).group(1)
    secret = re.search(r'<pre class="secret"><span>([^<]+)</span>', response.text).group(1)
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "scope": scopes,
        "state": uuid4().hex,
    }
    assert owner.get("/oauth/authorize", params=params).status_code == 200
    response = owner.post(
        "/oauth/authorize",
        data={
            **params,
            "decision": "approve",
            "granted_scopes": scopes.split(),
            "requested_scopes": scopes.split(),
        },
    )
    assert response.status_code == 302
    redirect = parse_qs(urlparse(response.headers["location"]).query)
    assert redirect["state"] == [params["state"]]
    response = owner.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": redirect["code"][0],
            "client_id": client_id,
            "client_secret": secret,
            "redirect_uri": REDIRECT,
        },
    )
    assert response.status_code == 200
    return {"client_id": client_id, "client_secret": secret, **response.json()}


async def verify_mcp(base, token, project, memory_id):
    import httpx2
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client

    async def check(read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}
            assert {"memory_search", "memory_add", "sync_delta"} <= tools
            assert {
                "switch_handoff",
                "writeback_sync",
                "memory_contradictions",
                "memory_merge",
                "memory_resolve",
            } <= tools
            result = await session.call_tool(
                "memory_search", {"query": "pytest", "tags": ["project:" + project]}
            )
            assert not result.is_error
            records = json.loads(result.content[0].text)["results"]
            assert any(r["id"] == memory_id and r["untrusted_data"] for r in records)

    async with httpx2.AsyncClient(
        headers={"Authorization": "Bearer " + token}, trust_env=False
    ) as transport:
        async with streamable_http_client(base + "/mcp", http_client=transport) as (read, write):
            await check(read, write)
    database = os.getenv("PORTER_TEST_STDIO_DATABASE_URL")
    if database:
        env = {
            **os.environ,
            "PORTER_TOKEN": token,
            "PORTER_REPO": "postgres",
            "DATABASE_URL": database,
            "PORTER_JWT_SECRET": os.environ["PORTER_TEST_JWT_SECRET"],
            "AUTH_ENABLED": "1",
            "PORTER_EMBEDDER": "http",
            "EMBEDDER_URL": os.environ["PORTER_TEST_EMBEDDER_URL"],
            "PYTHONPATH": str(ROOT),
        }
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "porter.mcp_stdio"], env=env, cwd=str(ROOT)
        )
        async with stdio_client(params) as (read, write):
            await check(read, write)


def test_deployed_oauth_memory_mcp_snapshot_and_recreation():
    base = os.getenv("PORTER_TEST_BASE_URL")
    setup = os.getenv("PORTER_TEST_SETUP_TOKEN")
    if not base or not setup:
        pytest.skip("running isolated Docker stack is not configured")
    project = "acceptance-" + uuid4().hex[:12]
    tag = "project:" + project
    with httpx.Client(base_url=base, timeout=20, trust_env=False) as owner:
        assert owner.get("/healthz").status_code == 200
        for path in ("/panel/memories", "/oauth/authorize", "/mcp"):
            assert owner.get(path, headers={"Host": "rebind.example"}).status_code == 400
        assert (
            owner.post(
                "/owner/login", data={"token": setup}, headers={"Origin": "http://[invalid"}
            ).status_code
            == 401
        )
        assert (
            owner.post("/owner/login", data={"token": setup}, headers={"Origin": base}).status_code
            == 303
        )
        for page in (
            "memories",
            "inbox",
            "sync",
            "clients",
            "shares",
            "audit",
            "snapshots",
            "settings",
        ):
            assert owner.get("/panel/" + page).status_code == 200, page
        admin = mint(owner, project + "-admin", "memory.admin")
        reader = mint(owner, project + "-reader", "memory.read")
        auth = {"Authorization": "Bearer " + admin["access_token"]}
        read_auth = {"Authorization": "Bearer " + reader["access_token"]}
        # Exercise upstream routes against the real production stores.
        handoff = owner.post(
            "/api/v1/switch/handoff",
            headers=read_auth,
            json={"project": project, "from_harness": "codex", "to_harness": "cursor"},
        )
        assert handoff.status_code == 200
        preview = owner.post("/api/v1/writeback/sync", headers=read_auth, json={"harness": "codex"})
        assert preview.status_code == 200
        assert (
            owner.post(
                "/api/v1/writeback/sync",
                headers=read_auth,
                json={"harness": "codex", "dry_run": False},
            ).status_code
            == 403
        )
        workspace = owner.post("/api/v1/workspaces", headers=auth, json={"name": project})
        assert workspace.status_code == 200
        assert workspace.json()["id"] in {
            w["id"] for w in owner.get("/api/v1/workspaces", headers=auth).json()["workspaces"]
        }
        from porter.e2ee_hub import generate_device_keypair, open_sealed_key, seal_key_for

        private_key, public_key = generate_device_keypair()
        device_id = project + "-device"
        assert (
            owner.post(
                "/api/v1/e2ee/devices",
                headers=auth,
                json={"device_id": device_id, "public_key": public_key},
            ).status_code
            == 200
        )
        sealed = seal_key_for(b"d" * 32, public_key)
        assert (
            owner.post(
                "/api/v1/e2ee/envelopes",
                headers=auth,
                json={"to_device": device_id, "from_device": device_id, "sealed": sealed},
            ).status_code
            == 200
        )
        claim = owner.get("/api/v1/e2ee/envelopes", headers=auth, params={"device_id": device_id})
        assert claim.status_code == 200
        assert open_sealed_key(claim.json()["envelopes"][0]["sealed"], private_key) == b"d" * 32
        assert (
            owner.get(
                "/api/v1/e2ee/envelopes", headers=auth, params={"device_id": device_id}
            ).json()["envelopes"]
            == []
        )
        assert (
            owner.get(
                "/api/v1/e2ee/envelopes", headers=read_auth, params={"device_id": device_id}
            ).status_code
            == 403
        )
        assert owner.post("/api/v1/admin/lifecycle/sweep", headers=auth).status_code == 200
        response = owner.post(
            "/api/v1/memories",
            headers=auth,
            json={
                "content": "Repository runs pytest on pull requests",
                "type": "project",
                "project": project,
            },
        )
        assert response.status_code == 202
        assert response.json()["status"] in ("active", "pending_review")
        mid = response.json()["id"]
        if response.json()["status"] == "pending_review":
            assert "duplicate_candidate" in response.json()["triage_reasons"]
            assert owner.post(f"/api/v1/memories/{mid}/approve", headers=auth).status_code == 200
        response = owner.post(
            "/api/v1/memories",
            headers=auth,
            json={
                "content": "Preferred font is Fira Code",
                "type": "personal",
                "scope_tags": [tag],
            },
        )
        assert response.status_code == 202 and response.json()["status"] == "pending_review"
        pending = response.json()["id"]
        delta = owner.get(
            "/api/v1/sync/delta", headers=read_auth, params={"project": project}
        ).json()
        assert pending not in {c["memory_id"] for c in delta["changes"]}
        assert owner.post(f"/api/v1/memories/{pending}/approve", headers=auth).status_code == 200
        asyncio.run(verify_mcp(base, reader["access_token"], project, mid))
        response = owner.post(
            "/api/v1/snapshots",
            headers=auth,
            json={"tags": [tag], "password": "acceptance-password"},
        )
        assert response.status_code == 201
        snapshot_id = response.json()["id"]
        saved = owner.get(f"/api/v1/snapshots/{snapshot_id}/download", headers=auth)
        assert saved.status_code == 200 and saved.content.startswith(b"PORTER1")
        files = {"file": ("acceptance.porter", saved.content, "application/octet-stream")}
        preview = owner.post(
            "/api/v1/snapshots/apply",
            headers=auth,
            files=files,
            data={"password": "acceptance-password", "dry_run": "true"},
        )
        assert preview.status_code == 200 and preview.json()["memory_count"] == 2
        restored = owner.post(
            "/api/v1/snapshots/apply",
            headers=auth,
            files=files,
            data={"password": "acceptance-password", "dry_run": "false"},
        )
        assert restored.status_code == 200 and restored.json()["imported"] == 2
        assert owner.post("/panel/settings/language", data={"lang": "zh"}).status_code == 302
        if os.getenv("PORTER_TEST_RECREATE") == "1":
            assert base == "http://127.0.0.1:18000", (
                "recreation is limited to the isolated verification port"
            )
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "--env-file",
                    ".env.review",
                    "-p",
                    "porter-review",
                    "-f",
                    "docker-compose.yml",
                    "-f",
                    "docker-compose.verify.yml",
                    "up",
                    "-d",
                    "--no-deps",
                    "--force-recreate",
                    "api",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                timeout=45,
            )
            deadline = time.monotonic() + 30
            while True:
                try:
                    if owner.get("/healthz", timeout=2).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert time.monotonic() < deadline, "API did not recover after recreation"
                time.sleep(0.25)
            persisted = owner.post(
                "/api/v1/memories/search", headers=auth, json={"query": "pytest", "tags": [tag]}
            )
            assert persisted.status_code == 200
            assert mid in {r["id"] for r in persisted.json()["results"]}
            assert '<html lang="zh-CN">' in owner.get("/panel/settings").text
            assert (
                owner.get(f"/api/v1/snapshots/{snapshot_id}/download", headers=auth).content
                == saved.content
            )
        owner.post("/panel/settings/language", data={"lang": "en"})
        revoked = owner.post(
            "/oauth/revoke",
            data={
                "client_id": reader["client_id"],
                "client_secret": reader["client_secret"],
                "token": reader["access_token"],
                "revoke_all": "true",
            },
        )
        assert revoked.status_code == 200
        assert (
            owner.post(
                "/api/v1/memories/search", headers=read_auth, json={"query": "pytest"}
            ).status_code
            == 401
        )
