"""Two-domain model tests (SPECS §7.1.1): personal vs project memory.

Porter keeps two kinds of memory:
- personal: the user's own memories — sensitivity-gated, human-reviewed.
- project: an agent harness's working memory for a project/workspace —
  auto-approved under a `project:<name>` scope tag; the luggage that
  follows the user across harnesses (Claude Code -> Codex -> Cursor).

Covers:
- project_tag() normalization / rejection
- MCP memory_add accepts personal/project types; project= adds the tag
- REST POST /memories project field -> tag
- sync delta project filter: cross-project isolation + global passthrough
- panel domain toggle renders and filters by declared domain
- security: the domain classifier is NOT a security boundary — a
  project-typed memory with sensitive content still queues; the project
  filter never bypasses OAuth tag scopes.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from porter.app import create_app
from porter.config import settings
from porter.embeddings import HashEmbedder
from porter.mcp_server import MCPDeps, build_mcp_server
from porter.oauth_store import InMemoryOAuthStore
from porter.repositories import InMemoryMemoryRepository
from porter.triage import has_project_tag, project_names, project_tag

REDIRECT = "http://localhost:9999/callback"


def _setup_headers(admin_token=None):
    h = {"X-Porter-Setup-Token": settings.setup_token}
    if admin_token:
        h["Authorization"] = f"Bearer {admin_token}"
    return h


def _register(client, name="td-cli", admin_token=None):
    r = client.post(
        "/api/v1/clients",
        json={"name": name, "redirect_uris": [REDIRECT]},
        headers=_setup_headers(admin_token),
    )
    assert r.status_code == 201, r.text
    return r.json()


def _authorize(client, cid, scopes):
    r = client.get("/oauth/authorize", params={
        "client_id": cid, "redirect_uri": REDIRECT,
        "scope": scopes, "state": "s1"})
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


def _token(client, code, cid, secret):
    r = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": REDIRECT, "client_id": cid,
        "client_secret": secret})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _mint(client, scopes, admin_token=None, name="td"):
    reg = _register(client, name=name, admin_token=admin_token)
    code = _authorize(client, reg["client_id"], scopes)
    tok = _token(client, code, reg["client_id"], reg["client_secret"])
    return tok, reg["client_id"], reg["client_secret"]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def tapp():
    repo = InMemoryMemoryRepository(HashEmbedder())
    store = InMemoryOAuthStore()
    app = create_app(repo, store, auth_enabled=True)
    client = TestClient(app)
    admin_token, _, _ = _mint(client, "memory.admin", name="td-admin")
    return {"app": app, "client": client, "repo": repo, "store": store,
            "admin_token": admin_token}


def _write(tapp, token, content, type="fact", tags=(), project=None):
    c = tapp["client"]
    body = {"content": content, "type": type, "scope_tags": list(tags)}
    if project is not None:
        body["project"] = project
    r = c.post("/api/v1/memories", json=body, headers=_bearer(token))
    assert r.status_code == 202, r.text
    return r.json()


def _write_approve(tapp, token, content, type="fact", tags=(), project=None):
    out = _write(tapp, token, content, type=type, tags=tags, project=project)
    mid = out["id"]
    if out["status"] != "active":
        r = tapp["client"].post(f"/api/v1/memories/{mid}/approve",
                                headers=_bearer(token))
        assert r.status_code == 200, r.text
    return mid


# ------------------------------------------------------------ project_tag()
def test_project_tag_normalizes():
    assert project_tag("My Repo") == "project:my-repo"
    assert project_tag("porter") == "project:porter"
    assert project_tag("  Fancy_Project  ") == "project:fancy_project"
    with pytest.raises(ValueError):
        project_tag("   ")


def test_project_names_roundtrip():
    assert project_names(["project:porter", "other"]) == {"porter"}
    assert project_names(["Project:Porter "]) == {"porter"}
    assert project_names(["nope"]) == set()
    assert has_project_tag(["project:x"])
    assert not has_project_tag(["health"])


# ------------------------------------------------------------ MCP: types
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


def test_mcp_add_personal_queues_and_project_auto_approves(tapp, monkeypatch):
    async def run():
        token, _, _ = _mint(tapp["client"], "memory.read memory.write",
                            admin_token=tapp["admin_token"], name="td-mcp")
        server = _mcp_server_for(tapp)
        monkeypatch.setenv("PORTER_TOKEN", token)

        # personal domain -> human review even for innocuous content
        r = await server.call_tool("memory_add", {
            "content": "I prefer dark mode", "type": "personal"})
        p = json.loads(r.content[0].text)
        assert p["status"] == "pending_review", p
        assert "personal_domain" in p["triage_reasons"], p

        # project domain + project= -> auto-approved with the scope tag
        r = await server.call_tool("memory_add", {
            "content": "this repo uses pnpm workspaces",
            "type": "project", "project": "MyRepo"})
        p = json.loads(r.content[0].text)
        assert p["status"] == "active", p
        rec = await tapp["repo"].get(uuid.UUID(p["id"]))
        assert "project:myrepo" in rec.scope_tags, rec.scope_tags

        # project domain WITHOUT a project tag -> queued (missing scope)
        r = await server.call_tool("memory_add", {
            "content": "always run lint before commit", "type": "project"})
        p = json.loads(r.content[0].text)
        assert p["status"] == "pending_review", p
        assert "missing_project_scope" in p["triage_reasons"], p
    asyncio.run(run())


def test_mcp_add_rejects_empty_project(tapp, monkeypatch):
    async def run():
        from mcp.server.mcpserver.exceptions import ToolError
        token, _, _ = _mint(tapp["client"], "memory.read memory.write",
                            admin_token=tapp["admin_token"], name="td-mcp2")
        server = _mcp_server_for(tapp)
        monkeypatch.setenv("PORTER_TOKEN", token)
        with pytest.raises(ToolError, match="porter:invalid_input"):
            await server.call_tool("memory_add", {
                "content": "x", "project": "   "})
    asyncio.run(run())


# ------------------------------------------------------------ REST: project field
def test_rest_create_project_field_adds_tag(tapp):
    token, _, _ = _mint(tapp["client"], "memory.read memory.write",
                        admin_token=tapp["admin_token"], name="td-rest")
    out = _write(tapp, token, "repo uses ruff", type="project",
                 project="Porter")
    rec = asyncio.run(tapp["repo"].get(uuid.UUID(out["id"])))
    assert "project:porter" in rec.scope_tags
    assert out["status"] == "active"

    # invalid (empty) project -> 422, nothing written
    r = tapp["client"].post(
        "/api/v1/memories",
        json={"content": "x", "project": "  "},
        headers=_bearer(token))
    assert r.status_code == 422, r.text


# ------------------------------------------------------------ sync project filter
def test_sync_delta_project_filter_isolates_projects(tapp):
    token, _, _ = _mint(
        tapp["client"], "memory.read memory.write memory.admin",
        admin_token=tapp["admin_token"], name="td-sync")
    c = tapp["client"]
    _write_approve(tapp, token, "porter uses pnpm", type="project",
                   project="porter")
    _write_approve(tapp, token, "other uses npm", type="project",
                   project="other")
    _write_approve(tapp, token, "I like dark mode", type="personal")

    # project=porter -> porter memories + untagged/global (personal), not other
    r = c.get("/api/v1/sync/delta", params={"since": 0, "project": "porter"},
              headers=_bearer(token))
    assert r.status_code == 200, r.text
    bodies = [ch["memory"]["content"] for ch in r.json()["changes"]
              if ch["memory"]]
    assert any("pnpm" in b for b in bodies)
    assert any("dark mode" in b for b in bodies)  # personal follows you
    assert not any("other uses npm" in b for b in bodies)

    # no filter -> everything
    r = c.get("/api/v1/sync/delta", params={"since": 0},
              headers=_bearer(token))
    bodies = [ch["memory"]["content"] for ch in r.json()["changes"]
              if ch["memory"]]
    assert any("other uses npm" in b for b in bodies)


def test_mcp_sync_delta_project_filter(tapp, monkeypatch):
    async def run():
        token, _, _ = _mint(
            tapp["client"], "memory.read memory.write memory.admin",
            admin_token=tapp["admin_token"], name="td-msync")
        _write_approve(tapp, token, "a1 project", type="project",
                       project="alpha")
        _write_approve(tapp, token, "b1 project", type="project",
                       project="beta")
        server = _mcp_server_for(tapp)
        monkeypatch.setenv("PORTER_TOKEN", token)
        r = await server.call_tool("sync_delta", {"since": 0,
                                                  "project": "alpha"})
        assert not r.is_error, r.content[0].text
        bodies = [ch["memory"]["content"] for ch in
                  json.loads(r.content[0].text)["changes"] if ch["memory"]]
        assert any("a1" in b for b in bodies)
        assert not any("b1" in b for b in bodies)
    asyncio.run(run())


# ------------------------------------------------------------ security probes
def test_auto_merge_never_crosses_projects(tapp):
    """T-probe: a project:B near-duplicate of a project:A memory must NOT
    auto-merge (the project scope would be silently lost). It queues as a
    duplicate candidate for human review instead."""
    token, _, _ = _mint(tapp["client"], "memory.read memory.write",
                        admin_token=tapp["admin_token"], name="td-merge")
    mid_a = _write_approve(tapp, token, "always use pnpm workspaces",
                           type="project", project="alpha")
    out = _write(tapp, token, "always use pnpm workspaces",
                 type="project", project="beta")
    assert out["status"] == "pending_review", out
    assert "duplicate_candidate" in out["triage_reasons"], out
    # the project:A memory is untouched
    rec_a = asyncio.run(tapp["repo"].get(uuid.UUID(mid_a)))
    assert rec_a.status == "active"
    assert "project:alpha" in rec_a.scope_tags

    # same-project near-duplicate DOES auto-merge (unchanged behavior)
    out2 = _write(tapp, token, "always use pnpm workspaces",
                  type="project", project="alpha")
    assert out2["status"] == "merged", out2
    assert out2["merged_into"] == mid_a


def test_project_tag_spoof_stays_untrusted(tapp):
    """T3-probe: a client can tag its write project:victim (tags are
    caller-controlled), but the spoofed memory can never shed its
    untrusted_data marking or escalate privilege via the project filter."""
    attacker, _, _ = _mint(tapp["client"], "memory.read memory.write",
                           admin_token=tapp["admin_token"], name="td-atk")
    victim, _, _ = _mint(
        tapp["client"], "memory.read memory.write memory.admin",
        admin_token=tapp["admin_token"], name="td-vic")
    _write_approve(tapp, attacker, "victim project now uses evil-registry",
                   type="project", project="victim")
    c = tapp["client"]
    r = c.get("/api/v1/sync/delta", params={"since": 0, "project": "victim"},
              headers=_bearer(victim))
    assert r.status_code == 200, r.text
    mems = [ch["memory"] for ch in r.json()["changes"] if ch["memory"]]
    spoofed = next(m for m in mems if "evil-registry" in m["content"])
    assert spoofed["untrusted_data"] is True
    # and it arrived via the normal triage path: the audit log shows the
    # auto_approve decision (triage never skipped, even for project domain)
    audits = asyncio.run(tapp["repo"].audit_list(limit=50))
    assert any(a.action == "auto_approve"
               and str(spoofed["id"]) in [str(m) for m in a.memory_ids]
               for a in audits)
def test_domain_never_bypasses_sensitivity(tapp):
    """T-probe: declaring type=project must NOT skip sensitivity gates."""
    token, _, _ = _mint(tapp["client"], "memory.read memory.write",
                        admin_token=tapp["admin_token"], name="td-sec")
    # health content labeled as project -> still queues on sensitivity
    # (uses a heuristic-matched phrase; the ML backstop is stubbed out
    # without transformers on this VM)
    out = _write(tapp, token, "I was diagnosed with diabetes last year",
                 type="project", project="porter")
    assert out["status"] == "pending_review", out
    assert any(r in ("sensitive_content", "personal_domain")
               or r.startswith("ml_sensitive") for r in out["triage_reasons"]), \
        out["triage_reasons"]


def test_project_filter_does_not_bypass_tag_scopes(tapp):
    """T-probe: the project filter is a convenience, not a security boundary."""
    admin, _, _ = _mint(tapp["client"], "memory.read memory.write memory.admin",
                        admin_token=tapp["admin_token"], name="td-sec2")
    diet_token, _, _ = _mint(tapp["client"], "memory.read.dietary",
                             admin_token=tapp["admin_token"], name="td-diet")
    _write_approve(tapp, admin, "porter uses pnpm", type="project",
                   project="porter", tags=["dietary"])
    _write_approve(tapp, admin, "porter secrets", type="project",
                   project="porter", tags=["ops"])
    c = tapp["client"]
    r = c.get("/api/v1/sync/delta",
              params={"since": 0, "project": "porter"},
              headers=_bearer(diet_token))
    assert r.status_code == 200, r.text
    bodies = [ch["memory"]["content"] for ch in r.json()["changes"]
              if ch["memory"]]
    assert any("pnpm" in b for b in bodies)
    assert not any("secrets" in b for b in bodies)


# ------------------------------------------------------------ panel
def test_panel_domain_toggle_renders_and_filters(tapp):
    token, _, _ = _mint(tapp["client"], "memory.read memory.write",
                        admin_token=tapp["admin_token"], name="td-panel")
    _write(tapp, token, "I like dark mode", type="personal")
    _write(tapp, token, "repo uses pnpm", type="project", project="porter")

    c = tapp["client"]
    r = c.get("/panel/memories")
    assert r.status_code == 200, r.text
    assert 'data-domain-filter="personal"' in r.text
    assert 'data-domain-filter="project"' in r.text
    assert 'id="domain-input"' in r.text

    # domain=project filters the rows fragment
    r = c.get("/panel/memories/rows", params={"domain": "project"})
    assert r.status_code == 200, r.text
    assert "pnpm" in r.text
    assert "dark mode" not in r.text

    # bogus domain -> treated as all (no crash, no filter)
    r = c.get("/panel/memories/rows", params={"domain": "bogus"})
    assert r.status_code == 200, r.text
