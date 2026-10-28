"""Owner shortcuts retain the Docker ownership boundary and recover in-page."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from porter.app import create_app
from porter.config import settings
from porter.embeddings import HashEmbedder
from porter.oauth_store import InMemoryOAuthStore
from porter.origin import OWNER_COOKIE
from porter.owner_login import MAX_LINKS, OwnerBrowserLinks
from porter.ratelimit import TokenBucket
from porter.repositories import InMemoryMemoryRepository


@pytest.fixture
def locked_client(monkeypatch):
    monkeypatch.setattr(settings, "setup_token", "owner-shortcut-test-key")
    app = create_app(
        InMemoryMemoryRepository(HashEmbedder()), InMemoryOAuthStore(), enable_mcp=False
    )
    return TestClient(app, base_url="http://127.0.0.1:8000", client=("172.19.0.1", 50000))


def issue(client):
    response = client.post(
        "/owner/browser-link", headers={"X-Porter-Setup-Token": settings.setup_token}
    )
    assert response.status_code == 200
    assert settings.setup_token not in response.text
    assert response.json()["expires_in"] == 120
    return response.json()["code"]


def test_local_helper_link_unlocks_once_with_existing_cookie_policy(locked_client):
    code = issue(locked_client)
    assert locked_client.get("/panel/memories").status_code == 401
    response = locked_client.post("/owner/login", data={"code": code}, follow_redirects=False)
    assert response.status_code == 303
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert "Max-Age=43200" in response.headers["set-cookie"]
    assert locked_client.get("/panel/memories").status_code == 200
    locked_client.cookies.clear()
    assert locked_client.post("/owner/login", data={"code": code}).status_code == 401
    assert OWNER_COOKIE not in locked_client.cookies


def test_browser_link_requires_real_key_even_with_forwarded_owner_claim(locked_client):
    for headers in [{}, {"X-Forwarded-For": "127.0.0.1"}, {"X-Porter-Setup-Token": "wrong"}]:
        assert locked_client.post("/owner/browser-link", headers=headers).status_code == 401


def test_cross_origin_link_creation_and_consumption_fail_closed(locked_client):
    response = locked_client.post(
        "/owner/browser-link",
        headers={"X-Porter-Setup-Token": settings.setup_token, "Origin": "http://evil.example"},
    )
    assert response.status_code == 401
    code = issue(locked_client)
    assert (
        locked_client.post(
            "/owner/login", data={"code": code}, headers={"Origin": "http://127.0.0.1:3000"}
        ).status_code
        == 401
    )
    assert (
        locked_client.post("/owner/login", data={"code": code}, follow_redirects=False).status_code
        == 303
    )


def test_expired_link_returns_recovery_page_without_echoing_credentials(locked_client):
    clock = [1.0]
    locked_client.app.state.owner_browser_links = OwnerBrowserLinks(lambda: clock[0])
    code = issue(locked_client)
    clock[0] = 121.0
    response = locked_client.post(
        "/owner/login", data={"code": code, "lang": "zh"}, headers={"Accept": "text/html"}
    )
    assert response.status_code == 401
    assert "已过期" in response.text and 'role="alert"' in response.text
    assert "Open-Porter.cmd" in response.text
    assert code not in response.text and settings.setup_token not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_manual_error_is_html_for_browsers_but_preserves_json_api(locked_client):
    secret = '<script>alert("not-echoed")</script>'
    response = locked_client.post(
        "/owner/login", data={"token": secret, "lang": "zh"}, headers={"Accept": "text/html"}
    )
    assert response.status_code == 401
    assert "密钥不匹配" in response.text and 'class="owner-manual" open' in response.text
    assert secret not in response.text
    response = locked_client.post("/owner/login", data={"token": secret})
    assert response.json()["error"]["code"] == "invalid_token"


def test_login_languages_and_resources_do_not_expose_owner_key(locked_client):
    for lang, title in [("en", "Open your memory library"), ("zh", "打开你的记忆库")]:
        response = locked_client.get("/owner/login", params={"lang": lang})
        assert response.status_code == 200 and title in response.text
        assert f'<html lang="{lang}">' in response.text
        assert "PORTER_SETUP_TOKEN" not in response.text
        assert settings.setup_token not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    for resource in ["css/owner-login.css", "js/owner-login.js"]:
        assert locked_client.get("/panel/static/" + resource).status_code == 200


def test_injected_language_and_ambiguous_credentials_do_not_unlock(locked_client):
    response = locked_client.get("/owner/login", params={"lang": "<script>alert(1)</script>"})
    assert "<script>alert(1)</script>" not in response.text
    code = issue(locked_client)
    assert (
        locked_client.post(
            "/owner/login", data={"token": settings.setup_token, "code": code}
        ).status_code
        == 401
    )


def test_link_store_is_bounded_key_bound_and_restart_local():
    links = OwnerBrowserLinks()
    codes = [links.issue("key") for _ in range(MAX_LINKS + 1)]
    assert len(links._codes) == MAX_LINKS
    assert all(code not in links._codes for code in codes)
    assert not links.consume(codes[0], "key")
    assert not links.consume(codes[-1], "rotated-key")
    assert not OwnerBrowserLinks().consume(codes[-1], "key")
    with ThreadPoolExecutor(max_workers=4) as executor:
        consumed = list(executor.map(lambda _: links.consume(codes[-1], "key"), range(4)))
    assert consumed.count(True) == 1


def test_login_rate_limit_is_a_recoverable_page(locked_client):
    locked_client.app.state.rate_limiter = TokenBucket(0)
    response = locked_client.post(
        "/owner/login", data={"token": "anything"}, headers={"Accept": "text/html"}
    )
    assert response.status_code == 429
    assert "Too many attempts" in response.text
    assert settings.setup_token not in response.text
