"""End-to-end OAuth demo (PRD §10, US1/US3, T1/T2).

Walks the whole flow against the in-memory backend:
  register panel (bootstrap) -> admin token -> register reader client
  -> consent -> code -> token -> write memory -> 403 approve attempt
  -> panel approves -> scoped search hits -> revoke -> 401
  -> refresh rotation.

Usage: .venv/bin/python scripts/demo_oauth.py
"""
import os
import sys
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from porter.app import create_app
from porter.embeddings import HashEmbedder
from porter.oauth_store import InMemoryOAuthStore
from porter.repositories import InMemoryMemoryRepository

REDIRECT = "http://localhost:9999/callback"


def _tokens(c: TestClient, cid: str, secret: str, scopes: str) -> dict:
    r = c.get("/oauth/authorize", params={
        "client_id": cid, "redirect_uri": REDIRECT,
        "scope": scopes, "state": "demo"})
    assert r.status_code == 200, r.text
    r = c.post("/oauth/authorize", data={
        "client_id": cid, "redirect_uri": REDIRECT, "state": "demo",
        "decision": "approve",
        "granted_scopes": scopes.split(), "requested_scopes": scopes.split(),
    }, follow_redirects=False)
    assert r.status_code == 302, r.text
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    r = c.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": REDIRECT, "client_id": cid, "client_secret": secret})
    assert r.status_code == 200, r.text
    return r.json()


def main() -> None:
    c = TestClient(create_app(
        InMemoryMemoryRepository(HashEmbedder()),
        InMemoryOAuthStore(),
        auth_enabled=True,
    ))
    from porter.config import settings
    setup_h = {"X-Porter-Setup-Token": settings.setup_token}

    print("== 1. register panel client (bootstrap: needs the one-time setup token)")
    panel = c.post("/api/v1/clients", json={
        "name": "panel", "redirect_uris": [REDIRECT]}, headers=setup_h).json()
    print(f"   client_id={panel['client_id']}")
    print(f"   client_secret={panel['client_secret'][:10]}... (shown ONCE, never again)")
    ptok = _tokens(c, panel["client_id"], panel["client_secret"], "memory.admin")
    ph = {"Authorization": f"Bearer {ptok['access_token']}"}
    print("   panel holds memory.admin")

    print("\n== 2. register reader client (now requires the admin token)")
    reader = c.post("/api/v1/clients",
                    json={"name": "demo-cli", "redirect_uris": [REDIRECT]},
                    headers=ph).json()
    print(f"   client_id={reader['client_id']}")
    r = c.post("/api/v1/clients", json={"name": "evil"})
    print(f"   anonymous second registration -> {r.status_code} "
          f"{r.json()['error']['code']} (bootstrap spent)")

    print("\n== 3. consent screen + code + token (dietary-only scope)")
    r = c.get("/oauth/authorize", params={
        "client_id": reader["client_id"], "redirect_uri": REDIRECT,
        "scope": "memory.read.dietary memory.write", "state": "demo"})
    assert "饮食" in r.text and "memory.admin" not in r.text.replace("最高权限", "")
    print("   consent HTML lists each scope with a Chinese explanation")
    rtok = _tokens(c, reader["client_id"], reader["client_secret"],
                   "memory.read.dietary memory.write")
    print(f"   scope='{rtok['scope']}'  expires_in={rtok['expires_in']}s")
    rh = {"Authorization": f"Bearer {rtok['access_token']}"}

    print("\n== 4. write memory as reader -> pending; approve -> 403")
    mid = c.post("/api/v1/memories", json={
        "content": "User does not eat cilantro", "type": "preference",
        "scope_tags": ["dietary"]}, headers=rh).json()["id"]
    print(f"   wrote {mid} -> pending_review")
    r = c.post(f"/api/v1/memories/{mid}/approve", headers=rh)
    print(f"   reader approve -> {r.status_code} {r.json()['error']['code']}")

    print("\n== 5. panel approves; scoped search hits (T1: tag filter server-side)")
    assert c.post(f"/api/v1/memories/{mid}/approve", headers=ph).status_code == 200
    r = c.post("/api/v1/memories/search",
               json={"query": "cilantro", "top_k": 5}, headers=rh)
    hits = r.json()["results"]
    assert any(x["id"] == mid for x in hits)
    print(f"   search hits={len(hits)}, untrusted_data={hits[0]['untrusted_data']}")

    print("\n== 6. revoke access token -> immediate 401 (US3/T2)")
    r = c.post("/oauth/revoke", data={
        "token": rtok["access_token"], "client_id": reader["client_id"],
        "client_secret": reader["client_secret"]})
    assert r.json()["revoked"] is True
    r = c.post("/api/v1/memories/search", json={"query": "cilantro"}, headers=rh)
    print(f"   search after revoke -> {r.status_code} {r.json()['error']['code']}")

    print("\n== 7. refresh rotation: old refresh dies, new pair works")
    r = c.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": rtok["refresh_token"],
        "client_id": reader["client_id"], "client_secret": reader["client_secret"]})
    assert r.status_code == 200, r.text
    rtok2 = r.json()
    assert rtok2["refresh_token"] != rtok["refresh_token"]
    r = c.get("/api/v1/memories",
              headers={"Authorization": f"Bearer {rtok2['access_token']}"})
    print(f"   rotated pair works -> {r.status_code}; "
          "reuse-outside-grace nukes the client (covered in tests)")

    print("\nOK: full OAuth round trip demonstrated.")


if __name__ == "__main__":
    main()
