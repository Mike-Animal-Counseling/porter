"""P2: E2EE encrypted memories + memory.share scope (PRD §9.5, §8).

E2EE (docs/DEVELOPER.md §6):
- real pynacl round-trip: encrypt -> POST -> GET -> decrypt == plaintext
  (sample-client helper path, key file at 0600)
- POST /api/v1/memories accepts either `content` OR
  (`content_ciphertext` + `encryption`); both/neither -> 422
- bad alg / bad base64 / tampered ciphertext handling
- search EXCLUDES encrypted memories (no embedding exists)
- list/get/sync/export/audit never expose plaintext (none exists server-side)

memory.share:
- owner mints restricted share tokens (memory.admin only)
- scopes are ALWAYS exactly [memory.read.<tag>...] — nothing else, ever
- privilege escalation probes: share token -> POST /shares, GET /clients,
  PATCH memories, POST /memories must be 403
- scope smuggling: memory.admin / ../ / "" / duplicates / unknown -> 400
- revoke -> 401 token_revoked immediately (< 60s); expired share rejected
- share token on /sync/delta works, tag-filtered
- rate-limit buckets: one share cannot burn another share's bucket
- panel /panel/shares: list, create (token shown once), revoke
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import jwt
import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from porter import oauth as oauth_logic
from porter.app import create_app
from porter.config import settings
from porter.embeddings import HashEmbedder
from porter.mcp_server import MCPDeps, build_mcp_server
from porter.oauth_store import InMemoryOAuthStore
from porter.repositories import InMemoryMemoryRepository

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REDIRECT = "http://localhost:9999/callback"


def _load_sample_client(monkeypatch, tmp_path):
    """Load the real sample client with an isolated key directory."""
    monkeypatch.setenv("PORTER_KEYS_DIR", str(tmp_path / "keys"))
    # Fresh load per test so KEYS_DIR picks up the isolated directory.
    sys.modules.pop("porter_sample_client", None)
    spec = importlib.util.spec_from_file_location(
        "porter_sample_client",
        os.path.join(REPO_ROOT, "clients", "python", "porter_client.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["porter_sample_client"] = mod
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------ OAuth helpers
def _setup_headers(admin_token=None):
    h = {"X-Porter-Setup-Token": settings.setup_token}
    if admin_token:
        h["Authorization"] = f"Bearer {admin_token}"
    return h


def _register(client: TestClient, name="test-cli", admin_token=None):
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
        params={"client_id": cid, "redirect_uri": REDIRECT, "scope": scopes, "state": "s1"},
    )
    assert r.status_code == 200, r.text
    r = client.post(
        "/oauth/authorize",
        data={
            "client_id": cid,
            "redirect_uri": REDIRECT,
            "state": "s1",
            "decision": "approve",
            "granted_scopes": scopes.split(),
            "requested_scopes": scopes.split(),
        },
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    return parse_qs(urlparse(r.headers["location"]).query)["code"][0]


def _token(client: TestClient, code: str, cid: str, secret: str) -> dict:
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": cid,
            "client_secret": secret,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture()
def oapp():
    """Auth-enabled app + in-memory backends + admin token."""
    repo = InMemoryMemoryRepository(HashEmbedder())
    store = InMemoryOAuthStore()
    app = create_app(repo, store, auth_enabled=True)
    client = TestClient(app)
    reg = _register(client, name="admin-cli")
    code = _authorize(client, reg["client_id"], "memory.admin")
    tok = _token(client, code, reg["client_id"], reg["client_secret"])
    return {
        "app": app,
        "client": client,
        "repo": repo,
        "store": store,
        "admin_token": tok["access_token"],
    }


def _authz(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _admin(oapp) -> dict:
    return _authz(oapp["admin_token"])


async def _make_active(repo, content="用户不吃香菜", tags=("dietary",), **kw):
    rec, _dups = await repo.create_pending(
        content=content,
        type="preference",
        scope_tags=list(tags),
        confidence=0.9,
        source={"client_id": "test"},
        **kw,
    )
    return await repo.approve(rec.id)


# ================================================================ E2EE
def test_e2ee_roundtrip_pynacl(oapp, monkeypatch, tmp_path):
    """Real pynacl round-trip via the sample-client helpers."""
    pc = _load_sample_client(monkeypatch, tmp_path)
    key_id = "2026-10-test-01"
    key = pc.generate_key()
    key_path = pc.save_key(key_id, key)
    # Key material at 0600 (helper path, loosely asserted).
    if os.name != "nt":  # Windows uses ACLs, not POSIX permission bits.
        assert stat.S_IMODE(os.stat(key_path).st_mode) == 0o600

    plaintext = "用户对花生过敏 — plaintext the server must never see"
    ciphertext_b64, encryption = pc.encrypt_content(plaintext, key_id)
    assert encryption == {"alg": "xchacha20-poly1305", "key_id": key_id}

    r = oapp["client"].post(
        "/api/v1/memories",
        json={
            "content_ciphertext": ciphertext_b64,
            "encryption": encryption,
            "type": "preference",
            "scope_tags": ["dietary"],
            "confidence": 0.95,
        },
        headers=_admin(oapp),
    )
    assert r.status_code == 202, r.text
    mem_id = r.json()["id"]
    # No duplicate candidates for encrypted writes (nothing comparable).
    assert r.json()["duplicate_candidates"] == []

    # Ciphertext round-trips verbatim through the pending inbox.
    r = oapp["client"].post(f"/api/v1/memories/{mem_id}/approve", headers=_admin(oapp))
    assert r.status_code == 200, r.text

    r = oapp["client"].get("/api/v1/memories", headers=_admin(oapp))
    assert r.status_code == 200, r.text
    got = next(m for m in r.json() if m["id"] == mem_id)
    assert got["encrypted"] is True
    assert got["key_id"] == key_id
    assert got["content"] == ciphertext_b64
    assert plaintext not in r.text  # no plaintext anywhere server-side

    # Client-side decrypt recovers the plaintext.
    assert (
        pc.decrypt_content(got["content"], {"alg": "xchacha20-poly1305", "key_id": key_id})
        == plaintext
    )


def test_e2ee_search_excludes_encrypted(oapp, monkeypatch, tmp_path):
    pc = _load_sample_client(monkeypatch, tmp_path)
    key = pc.generate_key()
    pc.save_key("k-search", key)
    ct, enc = pc.encrypt_content("用户对花生过敏", "k-search")

    async def _setup():
        rec, _ = await oapp["repo"].create_pending(
            content=ct,
            type="fact",
            scope_tags=["dietary"],
            confidence=1.0,
            source={},
            encrypted=True,
            key_id="k-search",
        )
        await oapp["repo"].approve(rec.id)
        await _make_active(oapp["repo"], content="用户不吃香菜", tags=("dietary",))

    asyncio.run(_setup())

    r = oapp["client"].post(
        "/api/v1/memories/search",
        json={"query": "用户不吃香菜", "top_k": 10},
        headers=_admin(oapp),
    )
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    # encrypted memories are excluded from search (no embedding exists);
    # only the plaintext memory matches.
    assert any("香菜" in m["content"] for m in results)
    assert not any("花生" in m["content"] for m in results)


def test_e2ee_create_validation(oapp):
    c = oapp["client"]
    good_ct = "aGVsbG8="  # valid base64
    good_enc = {"alg": "xchacha20-poly1305", "key_id": "k1"}

    def _post(body):
        return c.post("/api/v1/memories", json=body, headers=_admin(oapp))

    # bad alg -> 422
    r = _post({"content_ciphertext": good_ct, "encryption": {"alg": "aes-256-gcm", "key_id": "k1"}})
    assert r.status_code == 422, r.text
    # bad base64 -> 422 (server must not crash)
    r = _post({"content_ciphertext": "!!!not-base64!!!", "encryption": good_enc})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "invalid_input"
    # both content and ciphertext -> 422
    r = _post({"content": "plain", "content_ciphertext": good_ct, "encryption": good_enc})
    assert r.status_code == 422, r.text
    # neither -> 422
    r = _post({"type": "fact"})
    assert r.status_code == 422, r.text
    # ciphertext without encryption -> 422
    r = _post({"content_ciphertext": good_ct})
    assert r.status_code == 422, r.text
    # encryption without ciphertext -> 422
    r = _post({"encryption": good_enc})
    assert r.status_code == 422, r.text
    # key_id empty / too long -> 422
    r = _post(
        {"content_ciphertext": good_ct, "encryption": {"alg": "xchacha20-poly1305", "key_id": ""}}
    )
    assert r.status_code == 422, r.text
    r = _post(
        {
            "content_ciphertext": good_ct,
            "encryption": {"alg": "xchacha20-poly1305", "key_id": "k" * 65},
        }
    )
    assert r.status_code == 422, r.text
    # normal path still works
    r = _post({"content": "plain memory"})
    assert r.status_code == 202, r.text


def test_e2ee_tampered_ciphertext_fails_client_side(oapp, monkeypatch, tmp_path):
    """Valid base64 but tampered bytes: the server stores it fine (202) —
    it is ciphertext-blind — and the client's AEAD decrypt rejects it."""
    import base64

    pc = _load_sample_client(monkeypatch, tmp_path)
    pc.save_key("k-tamper", pc.generate_key())
    ct, enc = pc.encrypt_content("secret secret", "k-tamper")
    raw = bytearray(base64.b64decode(ct))
    raw[30] ^= 0xFF  # flip a byte inside the Poly1305 tag region
    tampered = base64.b64encode(bytes(raw)).decode()

    r = oapp["client"].post(
        "/api/v1/memories",
        json={"content_ciphertext": tampered, "encryption": enc},
        headers=_admin(oapp),
    )
    assert r.status_code == 202, r.text  # server is ciphertext-blind: no crash

    with pytest.raises(Exception):
        pc.decrypt_content(tampered, enc)  # AEAD auth fails client-side


def test_e2ee_audit_export_snapshot_never_see_plaintext(oapp, monkeypatch, tmp_path):
    pc = _load_sample_client(monkeypatch, tmp_path)
    pc.save_key("k-meta", pc.generate_key())
    plaintext = "extremely secret phrase xyzzy"
    ct, enc = pc.encrypt_content(plaintext, "k-meta")

    async def _setup():
        rec, _ = await oapp["repo"].create_pending(
            content=ct,
            type="fact",
            scope_tags=["dietary"],
            confidence=1.0,
            source={},
            encrypted=True,
            key_id="k-meta",
        )
        await oapp["repo"].approve(rec.id)

    asyncio.run(_setup())

    # audit log: meta only, no content/ciphertext/plaintext
    r = oapp["client"].get("/api/v1/audit-log", headers=_admin(oapp))
    assert r.status_code == 200, r.text
    assert plaintext not in r.text
    assert ct not in r.text

    # export: ciphertext with the flag, no plaintext
    r = oapp["client"].get("/api/v1/export", headers=_admin(oapp))
    assert r.status_code == 200, r.text
    mem = next(m for m in r.json()["memories"])
    assert mem["encrypted"] is True
    assert mem["key_id"] == "k-meta"
    assert mem["content"] == ct
    assert plaintext not in r.text

    # sync delta: same guarantee
    r = oapp["client"].get("/api/v1/sync/delta", params={"since": 0}, headers=_admin(oapp))
    assert r.status_code == 200, r.text
    mem = next(c["memory"] for c in r.json()["changes"] if c["memory"] is not None)
    assert mem["encrypted"] is True
    assert mem["content"] == ct
    assert plaintext not in r.text


def test_mcp_memory_add_encrypted(oapp, monkeypatch, tmp_path):
    pc = _load_sample_client(monkeypatch, tmp_path)
    pc.save_key("k-mcp", pc.generate_key())
    ct, enc = pc.encrypt_content("mcp secret", "k-mcp")

    # MCP needs a memory.write token: reuse the admin token via env.
    monkeypatch.setenv("PORTER_TOKEN", oapp["admin_token"])
    server = build_mcp_server(
        MCPDeps(
            repo_fn=lambda: oapp["repo"],
            oauth_store_fn=lambda: oapp["store"],
            revocation_cache_fn=lambda: oapp["app"].state.revocation_cache,
            rate_limiter_fn=lambda: oapp["app"].state.rate_limiter,
            auth_enabled_fn=lambda: True,
        )
    )

    async def _run():
        result = await server.call_tool(
            "memory_add",
            {
                "content_ciphertext": ct,
                "encryption": enc,
                "type": "fact",
                "scope_tags": ["dietary"],
            },
        )
        assert not result.is_error, result.content[0].text
        res = json.loads(result.content[0].text)
        assert res["encrypted"] is True
        assert res["status"] == "pending_review"
        assert res["duplicate_candidates"] == []
        rec = await oapp["repo"].get(UUID(res["id"]))
        assert rec.encrypted is True and rec.key_id == "k-mcp"
        assert rec.embedding is None

        # identical validation over MCP: bad alg / both fields
        with pytest.raises(ToolError, match="porter:invalid_input"):
            await server.call_tool(
                "memory_add", {"content": "x", "content_ciphertext": ct, "encryption": enc}
            )
        with pytest.raises(ToolError, match="porter:invalid_input"):
            await server.call_tool(
                "memory_add",
                {"content_ciphertext": ct, "encryption": {"alg": "nope", "key_id": "k-mcp"}},
            )
        with pytest.raises(ToolError, match="porter:invalid_input"):
            await server.call_tool(
                "memory_add", {"content_ciphertext": "!!!not-base64!!!", "encryption": enc}
            )

    asyncio.run(_run())


# ================================================================ memory.share
def _mint_share(client: TestClient, admin_headers: dict, **body) -> dict:
    r = client.post("/api/v1/shares", json=body, headers=admin_headers)
    assert r.status_code == 201, r.text
    data = r.json()
    assert set(data) == {"id", "name", "scopes", "created_at", "expires_at", "revoked", "token"}
    return data


def test_share_mint_and_tag_filtered_read(oapp):
    async def _setup():
        await _make_active(oapp["repo"], "dietary fact", ("dietary",))
        await _make_active(oapp["repo"], "work fact", ("work",))

    asyncio.run(_setup())

    share = _mint_share(
        oapp["client"], _admin(oapp), name="friend", tags=["dietary"], expires_in_hours=24
    )
    assert share["scopes"] == ["memory.read.dietary"]  # nothing else, ever
    assert share["revoked"] is False
    assert share["token"]

    # tokens are never listed
    r = oapp["client"].get("/api/v1/shares", headers=_admin(oapp))
    assert r.status_code == 200, r.text
    assert all("token" not in s for s in r.json())
    assert any(s["id"] == share["id"] for s in r.json())

    sh = _authz(share["token"])
    # tag-filtered search works
    r = oapp["client"].post(
        "/api/v1/memories/search", json={"query": "fact", "top_k": 10}, headers=sh
    )
    assert r.status_code == 200, r.text
    contents = [m["content"] for m in r.json()["results"]]
    assert any("dietary" in c for c in contents)
    assert not any("work fact" in c for c in contents)
    # tag-filtered list works
    r = oapp["client"].get("/api/v1/memories", headers=sh)
    assert r.status_code == 200, r.text
    assert all(m["scope_tags"] == ["dietary"] for m in r.json())


def test_share_cannot_escalate(oapp):
    share = _mint_share(
        oapp["client"], _admin(oapp), name="friend", tags=["dietary"], expires_in_hours=24
    )
    sh = _authz(share["token"])

    # write denied
    r = oapp["client"].post("/api/v1/memories", json={"content": "evil"}, headers=sh)
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "insufficient_scope"
    # cannot mint shares
    r = oapp["client"].post(
        "/api/v1/shares", json={"name": "x", "tags": ["dietary"], "expires_in_hours": 1}, headers=sh
    )
    assert r.status_code == 403, r.text
    # cannot list clients
    r = oapp["client"].get("/api/v1/clients", headers=sh)
    assert r.status_code == 403, r.text
    # cannot revise
    mem_id = "00000000-0000-0000-0000-000000000000"
    r = oapp["client"].patch(f"/api/v1/memories/{mem_id}", json={"content": "evil"}, headers=sh)
    assert r.status_code == 403, r.text
    # cannot list shares (memory.admin only)
    r = oapp["client"].get("/api/v1/shares", headers=sh)
    assert r.status_code == 403, r.text


def test_share_scope_smuggling_rejected(oapp):
    c = oapp["client"]

    def _bad(tags, **kw):
        body = {"name": "x", "tags": tags, "expires_in_hours": 1}
        body.update(kw)
        r = c.post("/api/v1/shares", json=body, headers=_admin(oapp))
        assert r.status_code == 400, (tags, r.text)
        assert r.json()["error"]["code"] == "invalid_scope"

    _bad(["memory.admin"])  # scope, not a tag
    _bad(["memory.read"])  # scope, not a tag
    _bad(["../"])  # path traversal-ish
    _bad([""])  # empty tag
    _bad(["dietary", "dietary"])  # duplicates
    _bad(["no_such_tag"])  # unknown tag
    _bad([])  # empty tag list
    # name / expiry range violations are 422 (body shape), not 400
    r = c.post(
        "/api/v1/shares",
        json={"name": "", "tags": ["dietary"], "expires_in_hours": 1},
        headers=_admin(oapp),
    )
    assert r.status_code == 422, r.text
    r = c.post(
        "/api/v1/shares",
        json={"name": "x", "tags": ["dietary"], "expires_in_hours": 0},
        headers=_admin(oapp),
    )
    assert r.status_code == 422, r.text
    r = c.post(
        "/api/v1/shares",
        json={"name": "x", "tags": ["dietary"], "expires_in_hours": 2161},
        headers=_admin(oapp),
    )
    assert r.status_code == 422, r.text


def test_share_revoke_kills_token_immediately(oapp):
    share = _mint_share(
        oapp["client"], _admin(oapp), name="friend", tags=["dietary"], expires_in_hours=24
    )
    sh = _authz(share["token"])
    r = oapp["client"].post("/api/v1/memories/search", json={"query": "x"}, headers=sh)
    assert r.status_code == 200, r.text

    r = oapp["client"].delete(f"/api/v1/shares/{share['id']}", headers=_admin(oapp))
    assert r.status_code == 204, r.text

    # token dies on the very next request (< 60s budget)
    r = oapp["client"].post("/api/v1/memories/search", json={"query": "x"}, headers=sh)
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "token_revoked"

    # unknown share -> 404
    r = oapp["client"].delete(
        "/api/v1/shares/00000000-0000-0000-0000-000000000000", headers=_admin(oapp)
    )
    assert r.status_code == 404, r.text


def test_share_expired_rejected(oapp):
    async def _mint_expired():
        # Share row already expired; JWT exp matches (jwt.decode rejects first).
        past = datetime.now(UTC) - timedelta(hours=1)
        share = await oapp["store"].create_share(
            name="old", scopes=["memory.read.dietary"], expires_at=past
        )
        now_ts = int(datetime.now(UTC).timestamp())
        payload = {
            "jti": str(share.id),
            "sub": "share",
            "cid": f"share:{share.id}",
            "scopes": ["memory.read.dietary"],
            "iat": now_ts - 7200,
            "exp": int(past.timestamp()),
        }
        return jwt.encode(payload, oauth_logic.jwt_signing_key(), algorithm="HS256")

    token = asyncio.run(_mint_expired())
    r = oapp["client"].post("/api/v1/memories/search", json={"query": "x"}, headers=_authz(token))
    assert r.status_code == 401, r.text


def test_share_forged_jti_rejected(oapp):
    # sub=share with an unknown jti fails closed (no such share row).
    payload = {
        "jti": "00000000-0000-0000-0000-000000000000",
        "sub": "share",
        "cid": "share:00000000-0000-0000-0000-000000000000",
        "scopes": ["memory.read.dietary", "memory.admin"],
        "iat": 1_700_000_000,
        "exp": 9_999_999_999,
    }
    token = jwt.encode(payload, oauth_logic.jwt_signing_key(), algorithm="HS256")
    r = oapp["client"].post("/api/v1/memories/search", json={"query": "x"}, headers=_authz(token))
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "token_revoked"


def test_share_sync_delta_tag_filtered(oapp):
    async def _setup():
        await _make_active(oapp["repo"], "dietary fact", ("dietary",))
        await _make_active(oapp["repo"], "work fact", ("work",))

    asyncio.run(_setup())

    share = _mint_share(
        oapp["client"], _admin(oapp), name="sync-friend", tags=["dietary"], expires_in_hours=24
    )
    r = oapp["client"].get(
        "/api/v1/sync/delta", params={"since": 0}, headers=_authz(share["token"])
    )
    assert r.status_code == 200, r.text
    bodies = [c["memory"]["content"] for c in r.json()["changes"] if c["memory"] is not None]
    assert any("dietary" in b for b in bodies)
    assert not any("work fact" in b for b in bodies)


def test_share_rate_limit_buckets_isolated():
    """One share cannot burn another share's rate-limit bucket (T5)."""
    repo = InMemoryMemoryRepository(HashEmbedder())
    store = InMemoryOAuthStore()
    app = create_app(repo, store, auth_enabled=True, rate_limit_per_minute=2)
    client = TestClient(app)

    async def _mint_two():
        s1, t1 = await oauth_logic.mint_share_token(
            store, name="a", tags=["dietary"], expires_in_hours=1
        )
        s2, t2 = await oauth_logic.mint_share_token(
            store, name="b", tags=["dietary"], expires_in_hours=1
        )
        return t1, t2

    t1, t2 = asyncio.run(_mint_two())

    def _search(tok):
        return client.post("/api/v1/memories/search", json={"query": "x"}, headers=_authz(tok))

    assert _search(t1).status_code == 200
    assert _search(t1).status_code == 200
    assert _search(t1).status_code == 429  # share A exhausted its own bucket
    assert _search(t2).status_code == 200  # share B unaffected


def test_share_custom_tag(oapp):
    asyncio.run(oapp["store"].add_custom_tag(tag="parenting", label="育儿"))
    share = _mint_share(
        oapp["client"], _admin(oapp), name="custom", tags=["parenting"], expires_in_hours=24
    )
    assert share["scopes"] == ["memory.read.parenting"]
    r = oapp["client"].post(
        "/api/v1/memories/search", json={"query": "x"}, headers=_authz(share["token"])
    )
    assert r.status_code == 200, r.text


# ================================================================ panel
@pytest.fixture()
def papp():
    repo = InMemoryMemoryRepository(HashEmbedder())
    store = InMemoryOAuthStore()
    app = create_app(repo, store, auth_enabled=False)
    return app, repo, store


def test_panel_shares_flow(papp):
    app, repo, store = papp
    c = TestClient(app)
    # nav entry present
    r = c.get("/panel/shares")
    assert r.status_code == 200, r.text
    assert "分享" in r.text or "Shares" in r.text

    # create via panel (token shown once)
    r = c.post(
        "/panel/shares/new",
        data={
            "name": "panel-friend",
            "tags": ["dietary"],
            "expires_in_hours": "48",
        },
    )
    assert r.status_code == 200, r.text
    assert "只显示这一次" in r.text or "only this once" in r.text
    assert r.text.count('pre class="secret"') == 1

    # listed without the token
    r = c.get("/panel/shares")
    assert r.status_code == 200, r.text
    assert "panel-friend" in r.text
    assert "dietary" in r.text
    assert 'pre class="secret"' not in r.text

    # revoke via panel
    shares = asyncio.run(store.list_shares())
    assert len(shares) == 1
    r = c.post(f"/panel/shares/{shares[0].id}/revoke", follow_redirects=False)
    assert r.status_code == 302, r.text
    shares = asyncio.run(store.list_shares())
    assert shares[0].revoked is True


def test_panel_encrypted_badge(papp):
    app, repo, store = papp
    c = TestClient(app)

    async def _setup():
        rec, _ = await repo.create_pending(
            content="aGVsbG8=",
            type="fact",
            scope_tags=["dietary"],
            confidence=1.0,
            source={},
            encrypted=True,
            key_id="k-panel",
        )
        await repo.approve(rec.id)
        return rec

    rec = asyncio.run(_setup())

    r = c.get("/panel/memories")
    assert r.status_code == 200, r.text
    assert "已加密" in r.text or "Encrypted" in r.text

    r = c.get(f"/panel/memories/{rec.id}")
    assert r.status_code == 200, r.text
    assert "k-panel" in r.text  # key_id shown as plaintext metadata


def test_e2ee_revise_carries_encryption(oapp):
    """Encryption rides the history chain; the server stays ciphertext-blind."""

    async def _run():
        rec, _ = await oapp["repo"].create_pending(
            content="Y2lwaGVydGV4dDE=",
            type="fact",
            scope_tags=["dietary"],
            confidence=1.0,
            source={},
            encrypted=True,
            key_id="k-rev",
        )
        new = await oapp["repo"].revise(rec.id, content="bmV3Y2lwaGVy")
        assert new.encrypted is True
        assert new.key_id == "k-rev"
        assert new.embedding is None
        assert new.content == "bmV3Y2lwaGVy"
        # conflict loser preserves encryption too
        _new2, loser = await oapp["repo"].update_with_version(
            new.id, expected_version=1, content="dGhpcmQ="
        )
        assert loser.encrypted is True and loser.key_id == "k-rev"
        assert loser.embedding is None

    asyncio.run(_run())


def test_e2ee_snapshot_roundtrip(oapp, tmp_path):
    from porter import snapshots as snap_logic
    from porter.snapshots import InMemorySnapshotStore

    async def _run():
        rec, _ = await oapp["repo"].create_pending(
            content="c25hcHNob3Q=",
            type="fact",
            scope_tags=["dietary"],
            confidence=1.0,
            source={},
            encrypted=True,
            key_id="k-snap",
        )
        await oapp["repo"].approve(rec.id)
        store = InMemorySnapshotStore()
        _srec, path = await snap_logic.create_snapshot(
            oapp["repo"], store, storage_dir=str(tmp_path)
        )

        async def _audit(**kw):
            return None

        repo2 = InMemoryMemoryRepository(HashEmbedder())
        report = await snap_logic.apply_snapshot_file(repo2, _audit, path=path, password=None)
        assert report["imported"] == 1
        got = (await repo2.list())[0]
        assert got.encrypted is True
        assert got.key_id == "k-snap"
        assert got.content == "c25hcHNob3Q="
        assert got.embedding is None
        # and still excluded from search after import
        assert await repo2.search(query="snapshot", top_k=5) == []

    asyncio.run(_run())


def test_i18n_key_completeness():
    """Every panel string exists in both zh and en (shares/E2EE included)."""
    from porter.i18n import STRINGS

    zh, en = STRINGS["zh"], STRINGS["en"]
    assert set(zh) == set(en), (
        "i18n key mismatch: "
        f"zh-only={sorted(set(zh) - set(en))} en-only={sorted(set(en) - set(zh))}"
    )
    for key in (
        "nav_shares",
        "title_shares",
        "badge_encrypted",
        "btn_mint_share",
        "share_new_done_warn",
    ):
        assert key in zh and key in en
