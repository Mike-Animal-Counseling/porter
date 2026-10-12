"""Panel (F4) tests: routes, inbox actions, revoke wiring, snapshots, settings.

The panel is the owner interface: localhost-only gate + owner context.
Tests run against the in-memory backends; TestClient's host is "testclient",
which the gate allows (see test_panel_localhost_gate for the deny path).
"""
from __future__ import annotations

import io
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from porter.app import create_app
from porter.config import settings
from porter.embeddings import HashEmbedder
from porter.errors import Unauthorized
from porter.oauth_store import InMemoryOAuthStore
from porter.panel import panel_owner
from porter.repositories import InMemoryMemoryRepository
from porter.snapshots import InMemorySnapshotStore

REDIRECT = "http://localhost:9999/callback"


@pytest.fixture()
def papp():
    repo = InMemoryMemoryRepository(HashEmbedder())
    store = InMemoryOAuthStore()
    snap = InMemorySnapshotStore()
    app = create_app(repo, store, auth_enabled=False, snapshot_store=snap)
    return app, repo, store, snap


@pytest.fixture()
def pclient(papp) -> TestClient:
    app, _repo, _store, _snap = papp
    return TestClient(app)


@pytest.fixture()
def oapp():
    """Auth-enabled app for the revoke-wiring test (real token -> 401)."""
    repo = InMemoryMemoryRepository(HashEmbedder())
    store = InMemoryOAuthStore()
    snap = InMemorySnapshotStore()
    app = create_app(repo, store, auth_enabled=True, snapshot_store=snap)
    return TestClient(app), repo, store


async def _make_active(repo, content="用户不吃香菜", tags=("dietary",)):
    rec, _dups = await repo.create_pending(
        content=content, type="preference", scope_tags=list(tags),
        confidence=0.9, source={"client_id": "codex"},
    )
    return await repo.approve(rec.id)


def _setup_headers():
    return {"X-Porter-Setup-Token": settings.setup_token}


def _oauth_token_pair(c: TestClient, scopes: str):
    """Full authorization-code flow; returns (client_pk, access, refresh, cid, secret)."""
    r = c.post("/api/v1/clients", json={"name": "t", "redirect_uris": [REDIRECT]},
               headers=_setup_headers())
    assert r.status_code == 201, r.text
    body = r.json()
    cid, secret = body["client_id"], body["client_secret"]
    r = c.get("/oauth/authorize", params={
        "client_id": cid, "redirect_uri": REDIRECT, "scope": scopes, "state": "s"})
    assert r.status_code == 200, r.text
    r = c.post("/oauth/authorize", data={
        "client_id": cid, "redirect_uri": REDIRECT, "state": "s",
        "decision": "approve",
        "granted_scopes": scopes.split(), "requested_scopes": scopes.split(),
    }, follow_redirects=False)
    assert r.status_code == 302, r.text
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    r = c.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": REDIRECT, "client_id": cid, "client_secret": secret})
    assert r.status_code == 200, r.text
    tok = r.json()
    return body["id"], tok["access_token"], tok["refresh_token"], cid, secret


# ------------------------------------------------------------------ gate
def test_panel_localhost_gate_denies_remote():
    import asyncio
    scope = {
        "type": "http", "http_version": "1.1", "method": "GET", "path": "/panel/",
        "headers": [], "client": ("203.0.113.7", 4242),
        "server": ("127.0.0.1", 8000), "scheme": "http",
    }
    with pytest.raises(Unauthorized):
        asyncio.run(panel_owner(Request(scope)))


def test_every_panel_route_has_localhost_gate():
    """Structural guarantee: no future route can forget the localhost gate."""
    from porter.panel import router as panel_router

    for route in panel_router.routes:
        path = getattr(route, "path", "")
        dep_names = {
            getattr(getattr(d, "call", None), "__name__", "")
            for d in route.dependant.dependencies
        }
        assert "panel_owner" in dep_names, f"{path} is missing the localhost gate"


def test_panel_localhost_gate_allows_testclient():
    import asyncio
    scope = {
        "type": "http", "http_version": "1.1", "method": "GET", "path": "/panel/",
        "headers": [], "client": ("testclient", 50000),
        "server": ("testserver", 80), "scheme": "http",
    }
    asyncio.run(panel_owner(Request(scope)))  # must not raise


# ------------------------------------------------------------------ pages
def test_panel_index_redirects(pclient):
    r = pclient.get("/panel/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].endswith("/panel/memories")


def test_memories_page_renders(pclient):
    r = pclient.get("/panel/memories")
    assert r.status_code == 200
    assert "记忆" in r.text
    assert "htmx.min.js" in r.text


def test_memories_rows_htmx(pclient, papp):
    import asyncio

    _app, repo, _store, _snap = papp
    asyncio.run(_make_active(repo))
    r = pclient.get("/panel/memories/rows", params={"q": "香菜", "mode": "text"})
    assert r.status_code == 200
    assert "香菜" in r.text
    r = pclient.get("/panel/memories/rows",
                    params={"q": "香菜", "mode": "semantic"})
    assert r.status_code == 200
    assert "香菜" in r.text


def test_memory_detail_edit_history_chain(pclient, papp):
    import asyncio

    _app, repo, _store, _snap = papp
    rec = asyncio.run(_make_active(repo, "用户爱喝美式", ("general",)))
    old_id = rec.id

    r = pclient.get(f"/panel/memories/{old_id}")
    assert r.status_code == 200
    assert "美式" in r.text

    r = pclient.post(f"/panel/memories/{old_id}/edit", data={
        "content": "用户爱喝拿铁", "type": "preference",
        "scope_tags": "general", "confidence": "0.8",
    }, follow_redirects=False)
    assert r.status_code == 302, r.text

    async def check():
        new_id = UUID(r.headers["location"].split("/panel/memories/")[1].split("?")[0])
        new = await repo.get(new_id)
        old = await repo.get(old_id)
        assert new.content == "用户爱喝拿铁"
        assert old.status == "archived"
        assert old.superseded_by == new.id
        # audit kept
        rows = await repo.audit_list(limit=10)
        assert any(x.action == "write" and x.meta.get("revised_from") == str(old_id)
                   for x in rows)
    asyncio.run(check())


def test_memory_delete_confirm_flow(pclient, papp):
    import asyncio

    _app, repo, _store, _snap = papp
    rec = asyncio.run(_make_active(repo))
    # Redesign: no separate confirm page — the detail drawer carries an
    # hx-confirm delete button instead.
    r = pclient.get(f"/panel/memories/{rec.id}")
    assert r.status_code == 200
    assert f'hx-post="/panel/memories/{rec.id}/delete"' in r.text
    assert "hx-confirm" in r.text
    r = pclient.post(f"/panel/memories/{rec.id}/delete", follow_redirects=False)
    assert r.status_code == 302

    async def check():
        assert await repo.get(rec.id) is None
        rows = await repo.audit_list(limit=10)
        assert any(x.action == "delete" for x in rows)
    asyncio.run(check())


def test_revise_api_endpoint(pclient, papp):
    import asyncio

    _app, repo, _store, _snap = papp
    rec = asyncio.run(_make_active(repo))
    r = pclient.patch(f"/api/v1/memories/{rec.id}",
                      json={"content": "改过的内容"})
    assert r.status_code == 200, r.text
    # US7: PATCH now returns MemoryReviseOut {memory, conflict, loser_pending_id}
    assert r.json()["memory"]["content"] == "改过的内容"
    assert r.json()["conflict"] is False
    assert r.json()["loser_pending_id"] is None
    r = pclient.patch(f"/api/v1/memories/{rec.id}", json={})
    assert r.status_code == 422


# ------------------------------------------------------------------ inbox
def test_inbox_approve_and_reject(pclient, papp):
    import asyncio

    _app, repo, _store, _snap = papp

    async def seed():
        a, _ = await repo.create_pending(
            content="待批准A", type="fact", scope_tags=[], confidence=1.0, source={})
        b, _ = await repo.create_pending(
            content="待拒绝B", type="fact", scope_tags=[], confidence=1.0, source={})
        return a, b
    a, b = asyncio.run(seed())

    r = pclient.get("/panel/inbox")
    assert r.status_code == 200
    assert "待批准A" in r.text and "待拒绝B" in r.text

    r = pclient.post(f"/panel/inbox/{a.id}/approve", follow_redirects=False)
    assert r.status_code == 302
    r = pclient.post(f"/panel/inbox/{b.id}/reject", follow_redirects=False)
    assert r.status_code == 302

    async def check():
        assert (await repo.get(a.id)).status == "active"
        assert await repo.get(b.id) is None
    asyncio.run(check())


# ------------------------------------------------------------------ clients + revoke wiring
def test_panel_client_register_shows_secret_once(pclient):
    r = pclient.post("/panel/clients/new", data={
        "name": "Codex", "redirect_uris": "http://localhost:9999/callback"},
        follow_redirects=False)
    assert r.status_code == 200
    assert "只显示这一次" in r.text
    assert "porter_" in r.text


def test_panel_revoke_kills_tokens_end_to_end(oapp):
    """US3 via the panel: revoke -> the client's token gets 401 immediately."""
    c, repo, store = oapp
    client_pk, access, _refresh, _cid, _secret = _oauth_token_pair(
        c, "memory.read memory.write")

    # Sanity: the token works before revoke.
    r = c.get("/api/v1/memories",
              headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 200, r.text

    # Panel one-click revoke (owner context, no bearer needed).
    r = c.post(f"/panel/clients/{client_pk}/revoke", follow_redirects=False)
    assert r.status_code == 302, r.text

    # The same access token is now dead.
    r = c.get("/api/v1/memories",
              headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "token_revoked"


def test_panel_revoke_confirm_page(oapp):
    c, _repo, _store = oapp
    client_pk, _a, _r, _cid, _s = _oauth_token_pair(c, "memory.read")
    # Redesign: no separate confirm page — the clients page carries an
    # hx-confirm revoke button instead.
    r = c.get("/panel/clients")
    assert r.status_code == 200
    assert f'hx-post="/panel/clients/{client_pk}/revoke"' in r.text
    assert "hx-confirm" in r.text


def test_clients_page_lists_scopes_zh(oapp):
    c, _repo, _store = oapp
    _oauth_token_pair(c, "memory.read.dietary memory.write")
    r = c.get("/panel/clients")
    assert r.status_code == 200
    assert "饮食" in r.text  # Chinese scope description


# ------------------------------------------------------------------ audit
def test_audit_page_shows_integrity_note(pclient, papp):
    import asyncio

    _app, repo, _store, _snap = papp
    asyncio.run(_make_active(repo))
    r = pclient.get("/panel/audit")
    assert r.status_code == 200
    assert "append-only" in r.text
    assert "不记录记忆内容文本" in r.text


# ------------------------------------------------------------------ scope tags
def test_scope_tags_api_and_panel(pclient):
    r = pclient.get("/api/v1/scope-tags")
    assert r.status_code == 200
    assert r.json()["preset"]["dietary"] == "饮食"
    assert r.json()["custom"] == []

    r = pclient.post("/api/v1/scope-tags", json={"tag": "parenting", "label": "育儿"})
    assert r.status_code == 201, r.text

    r = pclient.get("/api/v1/scope-tags")
    assert {"tag": "parenting", "label": "育儿"} in r.json()["custom"]

    # preset tags are protected
    r = pclient.post("/api/v1/scope-tags", json={"tag": "dietary", "label": "xx"})
    assert r.status_code == 422
    r = pclient.delete("/api/v1/scope-tags/dietary")
    assert r.status_code == 422

    r = pclient.delete("/api/v1/scope-tags/parenting")
    assert r.status_code == 204

    # panel settings shows preset tags
    r = pclient.get("/panel/settings")
    assert r.status_code == 200
    assert "饮食" in r.text


def test_custom_tag_scope_validates_in_consent(oapp):
    c, _repo, _store = oapp
    # scope-tags API needs memory.admin; the panel (owner) can manage tags:
    r = c.post("/panel/settings/tags/add",
               data={"tag": "parenting", "label": "育儿"})
    assert r.status_code in (302, 200), r.text

    _pk, _a, _r, cid, _s = _oauth_token_pair(c, "memory.read.parenting")
    # consent screen renders the custom tag description
    r = c.get("/oauth/authorize", params={
        "client_id": cid, "redirect_uri": REDIRECT,
        "scope": "memory.read.parenting", "state": "s"})
    assert r.status_code == 200
    assert "育儿" in r.text


# ------------------------------------------------------------------ export
def test_panel_export_download(pclient, papp):
    import asyncio

    _app, repo, _store, _snap = papp
    asyncio.run(_make_active(repo))
    r = pclient.get("/panel/export")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    payload = r.json()
    assert payload["format"] == "porter-export/v1"
    assert payload["memory_count"] >= 1
    assert all("content" in m for m in payload["memories"])


# ------------------------------------------------------------------ snapshots
def test_snapshot_create_download_apply_flow(pclient, papp, tmp_path):
    import asyncio
    from porter import config as config_mod

    _app, repo, store, snap = papp
    asyncio.run(_make_active(repo, "快照测试记忆", ("general",)))
    config_mod.settings.snapshot_dir = str(tmp_path)

    # create via panel
    r = pclient.post("/panel/snapshots/create",
                     data={"password": "s3cret", "tags": ["general"]},
                     follow_redirects=False)
    assert r.status_code == 302, r.text

    r = pclient.get("/panel/snapshots")
    assert r.status_code == 200
    assert "条记忆" in r.text
    assert "加密" in r.text

    async def get_rec():
        recs = await snap.list(limit=5)
        assert len(recs) == 1
        return recs[0]
    rec = asyncio.run(get_rec())

    # download
    r = pclient.get(f"/panel/snapshots/{rec.id}/download")
    assert r.status_code == 200
    assert r.content.startswith(b"PORTER1")  # encrypted magic

    # apply: upload -> dry-run -> confirm
    files = {"file": ("snap.porter", io.BytesIO(r.content),
                      "application/octet-stream")}
    r = pclient.post("/panel/snapshots/apply", files=files,
                     data={"password": "s3cret"})
    assert r.status_code == 200
    assert "Dry-run" in r.text
    assert "确认导入" in r.text

    # extract the hidden upload field and confirm
    import re
    m = re.search(r'name="upload" value="([^"]+)"', r.text)
    assert m, "dry-run page must carry the upload reference"
    r = pclient.post("/panel/snapshots/apply/confirm",
                     data={"upload": m.group(1), "password": "s3cret"},
                     follow_redirects=False)
    assert r.status_code == 302, r.text
    assert "/panel/inbox" in r.headers["location"]

    async def check_inbox():
        pend = await repo.list(status="pending", limit=100)
        assert any("快照测试记忆" in x.content for x in pend)
    asyncio.run(check_inbox())

    config_mod.settings.snapshot_dir = "snapshots"  # restore default


def test_snapshot_apply_wrong_password_rejected(pclient, papp, tmp_path):
    import asyncio
    from porter import config as config_mod

    _app, repo, _store, snap = papp
    asyncio.run(_make_active(repo))
    config_mod.settings.snapshot_dir = str(tmp_path)
    r = pclient.post("/panel/snapshots/create", data={"password": "right"},
                     follow_redirects=False)
    assert r.status_code == 302

    async def get_rec():
        return (await snap.list(limit=5))[0]
    rec = asyncio.run(get_rec())
    r = pclient.get(f"/panel/snapshots/{rec.id}/download")
    files = {"file": ("s.porter", io.BytesIO(r.content),
                      "application/octet-stream")}
    r = pclient.post("/panel/snapshots/apply", files=files,
                     data={"password": "WRONG"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_input"
    config_mod.settings.snapshot_dir = "snapshots"


# ------------------------------------------------- review fixes (F4/F3 deep)
def test_panel_edit_enforces_content_limit(pclient, papp):
    import asyncio
    _app, repo, _store, _snap = papp
    rec = asyncio.run(_make_active(repo))
    r = pclient.post(f"/panel/memories/{rec.id}/edit",
                     data={"content": "x" * 20001, "type": "fact",
                           "scope_tags": "", "confidence": "0.5"},
                     follow_redirects=False)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_input"


def test_panel_edit_validates_tags(pclient, papp):
    import asyncio
    _app, repo, _store, _snap = papp
    rec = asyncio.run(_make_active(repo))
    r = pclient.post(f"/panel/memories/{rec.id}/edit",
                     data={"content": "ok", "type": "fact",
                           "scope_tags": "x" * 100, "confidence": "0.5"},
                     follow_redirects=False)
    assert r.status_code == 422


def test_panel_rejects_cross_origin_post(pclient, papp):
    import asyncio
    _app, repo, _store, _snap = papp
    rec = asyncio.run(_make_active(repo))
    # forged cross-site POST (browser would send Origin: evil.com)
    r = pclient.post(f"/panel/memories/{rec.id}/delete",
                     headers={"Origin": "http://evil.com"},
                     follow_redirects=False)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_token"
    # memory untouched
    assert asyncio.run(repo.get(rec.id)) is not None
    # same-origin and headerless (curl) requests still work
    r = pclient.post(f"/panel/memories/{rec.id}/delete",
                     headers={"Origin": "http://127.0.0.1:8000"},
                     follow_redirects=False)
    assert r.status_code == 302
    assert asyncio.run(repo.get(rec.id)) is None


def test_snapshot_apply_rejects_malformed_entries(pclient, papp, tmp_path):
    import json as _json
    from porter import config as config_mod

    _app, repo, _store, _snap = papp
    config_mod.settings.snapshot_dir = str(tmp_path)
    try:
        bad = {"format": "porter-snapshot/v1", "memories": [
            {"type": "fact"},                                        # missing content
        ]}
        files = {"file": ("bad.porter.json", _json.dumps(bad), "application/json")}
        r = pclient.post("/panel/snapshots/apply", files=files, data={})
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "invalid_input"

        bad2 = {"format": "porter-snapshot/v1", "memories": [
            {"content": "x" * 20001, "type": "fact"},                # oversized
        ]}
        files = {"file": ("bad2.porter.json", _json.dumps(bad2), "application/json")}
        r = pclient.post("/panel/snapshots/apply", files=files, data={})
        assert r.status_code == 422

        bad3 = {"format": "porter-snapshot/v1", "memories": [
            {"content": "ok", "type": "nonsense"},                   # bad type
        ]}
        files = {"file": ("bad3.porter.json", _json.dumps(bad3), "application/json")}
        r = pclient.post("/panel/snapshots/apply", files=files, data={})
        assert r.status_code == 422
    finally:
        config_mod.settings.snapshot_dir = "snapshots"  # restore default
