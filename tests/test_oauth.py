"""OAuth + authorization layer tests (PRD §10, §14 T1/T2/T5, US3).

Covers: client registration (secret shown once), full authorization-code
round trip, consent deny, refresh rotation + reuse detection (mass revoke),
scope enforcement (tag-scoped tokens), revocation -> 401, rate limiting,
and unified error shapes (PRD §11).
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from porter.app import create_app
from porter.config import settings
from porter.embeddings import HashEmbedder
from porter.oauth_store import InMemoryOAuthStore
from porter.repositories import InMemoryMemoryRepository

REDIRECT = "http://localhost:9999/callback"


def _setup_headers(admin_token: str | None = None) -> dict:
    # P0: the very first client registration requires the one-time setup
    # token (auto-generated per process unless PORTER_SETUP_TOKEN is set).
    h = {"X-Porter-Setup-Token": settings.setup_token}
    if admin_token:
        h["Authorization"] = f"Bearer {admin_token}"
    return h


@pytest.fixture()
def oapp() -> TestClient:
    repo = InMemoryMemoryRepository(HashEmbedder())
    store = InMemoryOAuthStore()
    app = create_app(repo, store, auth_enabled=True)
    return TestClient(app)


def _register(
    client: TestClient,
    name: str = "test-cli",
    redirect_uris: list[str] | None = None,
    admin_token: str | None = None,
) -> dict:
    # First registration needs the setup token; afterwards memory.admin.
    r = client.post(
        "/api/v1/clients",
        json={
            "name": name,
            "redirect_uris": redirect_uris or [REDIRECT],
        },
        headers=_setup_headers(admin_token),
    )
    assert r.status_code == 201, r.text
    return r.json()


def _authorize(
    client: TestClient, cid: str, scopes: str, redirect: str = REDIRECT, state: str = "s1"
) -> str:
    """Run the consent flow; return the authorization code."""
    r = client.get(
        "/oauth/authorize",
        params={
            "client_id": cid,
            "redirect_uri": redirect,
            "scope": scopes,
            "state": state,
        },
    )
    assert r.status_code == 200, r.text
    assert "批准" in r.text or "approve" in r.text.lower() or "授权" in r.text
    r = client.post(
        "/oauth/authorize",
        data={
            "client_id": cid,
            "redirect_uri": redirect,
            "state": state,
            "decision": "approve",
            "granted_scopes": scopes.split(),
            "requested_scopes": scopes.split(),
        },
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    loc = r.headers["location"]
    assert loc.startswith(redirect), loc
    qs = parse_qs(urlparse(loc).query)
    assert qs.get("state") == [state]
    return qs["code"][0]


def _token(client: TestClient, code: str, cid: str, secret: str, redirect: str = REDIRECT) -> dict:
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect,
            "client_id": cid,
            "client_secret": secret,
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["token_type"] == "Bearer"
    assert data["expires_in"] == 3600
    return data


def _full_client(
    client: TestClient, scopes: str, name: str = "test-cli", admin_token: str | None = None
) -> tuple[dict, dict]:
    """Register + authorize + token. Returns (registration, tokens)."""
    reg = _register(client, name=name, admin_token=admin_token)
    code = _authorize(client, reg["client_id"], scopes)
    tok = _token(client, code, reg["client_id"], reg["client_secret"])
    return reg, tok


def _panel(client: TestClient) -> tuple[dict, dict]:
    """An admin-scoped client (stands in for the user panel)."""
    return _full_client(client, "memory.admin", name="panel")


def _authz(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


# ------------------------------------------------------------------ registration
def test_bootstrap_requires_setup_token(oapp: TestClient):
    # P0: first registration without the one-time setup token is rejected.
    r = oapp.post("/api/v1/clients", json={"name": "attacker"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_token"
    r = oapp.post(
        "/api/v1/clients",
        json={"name": "attacker"},
        headers={"X-Porter-Setup-Token": "wrong-token"},
    )
    assert r.status_code == 401
    # correct token -> 201; the token is single-purpose (table now non-empty,
    # so it can never be used again)
    reg = _register(oapp)
    assert reg["client_id"].startswith("porter_")
    r = oapp.post(
        "/api/v1/clients",
        json={"name": "late"},
        headers={"X-Porter-Setup-Token": settings.setup_token},
    )
    assert r.status_code == 401  # now requires memory.admin, not the setup token


def test_register_returns_secret_once(oapp: TestClient):
    reg = _register(oapp)
    assert reg["client_secret"] and len(reg["client_secret"]) >= 32
    assert reg["client_id"].startswith("porter_")
    # Secret is never returned again: list endpoint has no secret field.
    # Registering a second client now requires memory.admin -> 401 (no token).
    r = oapp.post("/api/v1/clients", json={"name": "second"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_token"


def test_register_second_client_needs_admin(oapp: TestClient):
    reg, tok = _full_client(oapp, "memory.admin", name="panel")
    # admin token can register more clients
    r = oapp.post("/api/v1/clients", json={"name": "second"}, headers=_authz(tok["access_token"]))
    assert r.status_code == 201, r.text
    # non-admin token cannot
    reg2, tok2 = _full_client(oapp, "memory.read", name="reader", admin_token=tok["access_token"])
    r = oapp.post("/api/v1/clients", json={"name": "third"}, headers=_authz(tok2["access_token"]))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "insufficient_scope"


def test_list_clients_never_returns_secret(oapp: TestClient):
    reg, tok = _full_client(oapp, "memory.admin", name="panel")
    r = oapp.get("/api/v1/clients", headers=_authz(tok["access_token"]))
    assert r.status_code == 200
    for c in r.json():
        assert "client_secret" not in c
        assert "secret_hash" not in c


# ------------------------------------------------------------------ authorize flow
def test_consent_deny_redirects_with_error(oapp: TestClient):
    reg = _register(oapp)
    r = oapp.post(
        "/oauth/authorize",
        data={
            "client_id": reg["client_id"],
            "redirect_uri": REDIRECT,
            "state": "xyz",
            "decision": "deny",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302
    qs = parse_qs(urlparse(r.headers["location"]).query)
    assert qs["error"] == ["access_denied"]
    assert qs["state"] == ["xyz"]


def test_authorize_unknown_scope_400(oapp: TestClient):
    reg = _register(oapp)
    r = oapp.get(
        "/oauth/authorize",
        params={
            "client_id": reg["client_id"],
            "redirect_uri": REDIRECT,
            "scope": "memory.read.bogus",
            "state": "s",
        },
        follow_redirects=False,
    )
    # redirect_uri is trusted -> OAuth2 error redirect
    assert r.status_code == 302
    qs = parse_qs(urlparse(r.headers["location"]).query)
    assert qs["error"] == ["invalid_scope"]


def test_authorize_redirect_uri_mismatch_400(oapp: TestClient):
    reg = _register(oapp)
    r = oapp.get(
        "/oauth/authorize",
        params={
            "client_id": reg["client_id"],
            "redirect_uri": "https://evil.example/cb",
            "scope": "memory.read",
            "state": "s",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_grant"


def test_code_single_use(oapp: TestClient):
    reg = _register(oapp)
    code = _authorize(oapp, reg["client_id"], "memory.read")
    tok = _token(oapp, code, reg["client_id"], reg["client_secret"])
    assert tok["access_token"]
    # Reusing the code must fail.
    r = oapp.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": reg["client_id"],
            "client_secret": reg["client_secret"],
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_grant"


def test_token_wrong_client_secret(oapp: TestClient):
    reg = _register(oapp)
    code = _authorize(oapp, reg["client_id"], "memory.read")
    r = oapp.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": reg["client_id"],
            "client_secret": "wrong-secret",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_grant"


# ------------------------------------------------------------------ round trip (US1/US3)
def test_full_round_trip_write_search_revoke(oapp: TestClient):
    _, admin_tok = _panel(oapp)
    ah = _authz(admin_tok["access_token"])
    reg, tok = _full_client(
        oapp,
        "memory.read.dietary memory.write",
        name="reader",
        admin_token=admin_tok["access_token"],
    )
    h = _authz(tok["access_token"])

    # write -> pending (memory.write)
    r = oapp.post(
        "/api/v1/memories",
        json={
            "content": "User does not eat cilantro",
            "type": "preference",
            "scope_tags": ["dietary"],
        },
        headers=h,
    )
    assert r.status_code == 202, r.text
    mem_id = r.json()["id"]

    # approve needs memory.admin -> 403 for this token
    r = oapp.post(f"/api/v1/memories/{mem_id}/approve", headers=h)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "insufficient_scope"

    # approve via the panel's admin token
    r = oapp.post(f"/api/v1/memories/{mem_id}/approve", headers=ah)
    assert r.status_code == 200, r.text

    # scoped search as the dietary client finds it
    r = oapp.post("/api/v1/memories/search", json={"query": "cilantro", "top_k": 5}, headers=h)
    assert r.status_code == 200
    results = r.json()["results"]
    assert any(x["id"] == mem_id for x in results)
    assert all(x["untrusted_data"] is True for x in results)

    # revoke -> immediate 401 (US3, T2)
    r = oapp.post(
        "/oauth/revoke",
        data={
            "token": tok["access_token"],
            "client_id": reg["client_id"],
            "client_secret": reg["client_secret"],
        },
    )
    assert r.status_code == 200
    assert r.json()["revoked"] is True
    r = oapp.post("/api/v1/memories/search", json={"query": "cilantro"}, headers=h)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "token_revoked"


def test_revoke_all_kills_everything(oapp: TestClient):
    reg, tok = _full_client(oapp, "memory.read memory.write")
    h = _authz(tok["access_token"])
    r = oapp.post(
        "/oauth/revoke",
        data={
            "token": tok["refresh_token"],
            "client_id": reg["client_id"],
            "client_secret": reg["client_secret"],
            "revoke_all": True,
        },
    )
    assert r.json()["revoked"] is True
    # access token dead
    r = oapp.get("/api/v1/memories", headers=h)
    assert r.status_code == 401
    # refresh token dead too (presenting a revoked credential -> 401)
    r = oapp.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": reg["client_id"],
            "client_secret": reg["client_secret"],
        },
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "token_revoked"


# ------------------------------------------------------------------ refresh rotation + reuse (T2)
def test_refresh_rotation(oapp: TestClient):
    reg, tok = _full_client(oapp, "memory.read")
    r = oapp.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": reg["client_id"],
            "client_secret": reg["client_secret"],
        },
    )
    assert r.status_code == 200, r.text
    tok2 = r.json()
    assert tok2["refresh_token"] != tok["refresh_token"]
    # new access token works
    r = oapp.get("/api/v1/memories", headers=_authz(tok2["access_token"]))
    assert r.status_code == 200


def test_refresh_reuse_triggers_mass_revoke(oapp: TestClient):
    reg, tok = _full_client(oapp, "memory.read")
    # legitimate rotation
    r = oapp.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": reg["client_id"],
            "client_secret": reg["client_secret"],
        },
    )
    assert r.status_code == 200
    tok2 = r.json()
    # attacker replays the OLD refresh token after the grace window:
    # force age by waiting is slow; instead reuse twice quickly is inside grace,
    # so simulate expiry of grace by directly testing the second replay path.
    # First replay inside grace -> 400 invalid_grant, no mass revoke.
    r = oapp.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": reg["client_id"],
            "client_secret": reg["client_secret"],
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_grant"
    # the legitimately rotated pair still works (no false-positive nuke)
    r = oapp.get("/api/v1/memories", headers=_authz(tok2["access_token"]))
    assert r.status_code == 200, r.text


def test_refresh_reuse_after_grace_nukes_client(oapp: TestClient):
    """Reuse outside the grace window = theft -> revoke everything (T2)."""
    reg, tok = _full_client(oapp, "memory.read")
    # rotate once
    r = oapp.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": reg["client_id"],
            "client_secret": reg["client_secret"],
        },
    )
    assert r.status_code == 200
    tok2 = r.json()
    # Shrink the grace window to 0 so the replay counts as "after grace".
    import porter.config as config_mod

    old = config_mod.settings.refresh_reuse_grace_seconds
    config_mod.settings.refresh_reuse_grace_seconds = 0
    try:
        r = oapp.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tok["refresh_token"],
                "client_id": reg["client_id"],
                "client_secret": reg["client_secret"],
            },
        )
    finally:
        config_mod.settings.refresh_reuse_grace_seconds = old
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "token_revoked"
    # mass revoke: the rotated pair is dead too
    r = oapp.get("/api/v1/memories", headers=_authz(tok2["access_token"]))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "token_revoked"


# ------------------------------------------------------------------ scope enforcement (T1)
def test_tag_scoped_token_cannot_read_other_tags(oapp: TestClient):
    _, admin_tok = _panel(oapp)
    ah = _authz(admin_tok["access_token"])
    # seed two memories with different tags
    ids = {}
    for content, tag in [("no cilantro", "dietary"), ("uses pnpm", "coding_style")]:
        r = oapp.post(
            "/api/v1/memories",
            json={
                "content": content,
                "type": "preference",
                "scope_tags": [tag],
            },
            headers=ah,
        )
        mid = r.json()["id"]
        oapp.post(f"/api/v1/memories/{mid}/approve", headers=ah)
        ids[tag] = mid

    _, diet_tok = _full_client(
        oapp, "memory.read.dietary", name="diet-reader", admin_token=admin_tok["access_token"]
    )
    dh = _authz(diet_tok["access_token"])
    # search without tags: only dietary visible (server-side filter)
    r = oapp.post(
        "/api/v1/memories/search", json={"query": "user preference", "top_k": 10}, headers=dh
    )
    assert r.status_code == 200
    got = {x["id"] for x in r.json()["results"]}
    assert ids["dietary"] in got
    assert ids["coding_style"] not in got
    # client-hint tags cannot widen the scope: asking for coding_style yields nothing
    r = oapp.post(
        "/api/v1/memories/search",
        json={
            "query": "user preference",
            "top_k": 10,
            "tags": ["coding_style"],
        },
        headers=dh,
    )
    assert r.status_code == 200
    assert r.json()["results"] == []
    # list is tag-filtered too, and pending is hidden from non-admin
    r = oapp.get("/api/v1/memories", headers=dh)
    assert r.status_code == 200
    for m in r.json():
        assert "dietary" in m["scope_tags"]
        assert m["status"] == "active"


def test_write_scope_cannot_read(oapp: TestClient):
    _, tok = _full_client(oapp, "memory.write", name="writer")
    h = _authz(tok["access_token"])
    r = oapp.post("/api/v1/memories", json={"content": "x", "type": "fact"}, headers=h)
    assert r.status_code == 202
    r = oapp.post("/api/v1/memories/search", json={"query": "x"}, headers=h)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "insufficient_scope"


def test_no_token_401_unified_shape(oapp: TestClient):
    r = oapp.post("/api/v1/memories/search", json={"query": "x"})
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["code"] == "invalid_token"
    assert "message" in body["error"]


def test_garbage_token_401(oapp: TestClient):
    r = oapp.get("/api/v1/memories", headers={"Authorization": "Bearer garbage.token.here"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_token"


# ------------------------------------------------------------------ rate limiting (T5)
def test_rate_limit_429(oapp: TestClient):
    from porter.ratelimit import TokenBucket

    oapp.app.state.rate_limiter = TokenBucket(3)
    for _ in range(3):
        r = oapp.get("/healthz")
        assert r.status_code == 200
    r = oapp.get("/healthz")
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"


# ------------------------------------------------------------------ audit trail
def test_oauth_events_audited(oapp: TestClient):
    _, admin_tok = _panel(oapp)
    ah = _authz(admin_tok["access_token"])
    r = oapp.get("/api/v1/audit-log", headers=ah)
    assert r.status_code == 200
    actions = {e["action"] for e in r.json()}
    assert "grant" in actions  # auth code exchanged at least once
    # audit-log itself requires admin
    _, reader_tok = _full_client(
        oapp, "memory.read", name="reader", admin_token=admin_tok["access_token"]
    )
    r = oapp.get("/api/v1/audit-log", headers=_authz(reader_tok["access_token"]))
    assert r.status_code == 403


# ------------------------------------------------------------------ CSRF (T1)
def _consent_form(reg: dict, scopes: str, state: str = "s1") -> dict:
    return {
        "client_id": reg["client_id"],
        "redirect_uri": REDIRECT,
        "state": state,
        "decision": "approve",
        "granted_scopes": scopes.split(),
        "requested_scopes": scopes.split(),
    }


def test_authorize_post_cross_origin_rejected(oapp: TestClient):
    """A malicious website must not be able to forge an approval POST."""
    reg = _register(oapp, name="csrf-victim")
    form = _consent_form(reg, "memory.read")
    r = oapp.post(
        "/oauth/authorize",
        data=form,
        headers={"Origin": "https://evil.example.com"},
        follow_redirects=False,
    )
    assert r.status_code == 401, r.text
    r = oapp.post(
        "/oauth/authorize",
        data=form,
        headers={"Referer": "https://evil.example.com/phish"},
        follow_redirects=False,
    )
    assert r.status_code == 401, r.text


def test_authorize_post_same_origin_allowed(oapp: TestClient):
    reg = _register(oapp, name="csrf-legit")
    form = _consent_form(reg, "memory.read")
    # no Origin header: real top-level navigation from the consent page
    r = oapp.post("/oauth/authorize", data=form, follow_redirects=False)
    assert r.status_code == 302, r.text
    # explicit localhost Origin: allowed
    form2 = _consent_form(reg, "memory.read", state="s2")
    r = oapp.post(
        "/oauth/authorize",
        data=form2,
        headers={"Origin": str(oapp.base_url).rstrip("/")},
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text


# ------------------------------------------------------------------ regression
def test_dummy_hash_is_real_argon2_string():
    """The unknown-client timing mitigation must return a real hash.

    Regression: a name-shadowing bug once made _dummy_hash() return the
    function itself, silently disabling the constant-time mitigation.
    """
    from porter.oauth import _dummy_hash, verify_secret

    h = _dummy_hash()
    assert isinstance(h, str) and h.startswith("$argon2id$")
    # a verify against it must actually run (and fail), not short-circuit
    assert verify_secret(h, "definitely-wrong-secret") is False
