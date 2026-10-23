"""Regressions for host trust, proxy ownership and fail-closed configuration."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from porter import __version__
from porter.app import create_app
from porter.config import Settings, repository_kind, settings
from porter.embeddings import HashEmbedder
from porter.oauth_store import InMemoryOAuthStore
from porter.repositories import InMemoryMemoryRepository


def local_client(monkeypatch, host="127.0.0.1", **kwargs):
    monkeypatch.setattr(settings, "setup_token", "runtime-test-secret")
    app = create_app(
        InMemoryMemoryRepository(HashEmbedder()),
        InMemoryOAuthStore(),
        auth_enabled=True,
        enable_mcp=False,
    )
    return TestClient(app, base_url=f"http://{host}", client=("127.0.0.1", 50000), **kwargs)


@pytest.mark.parametrize(
    "path", ["/panel/memories", "/owner/login", "/oauth/authorize", "/mcp", "/healthz"]
)
def test_untrusted_host_cannot_reach_any_app_entry(monkeypatch, path):
    client = local_client(monkeypatch)
    response = client.get(
        path, headers={"Host": "rebind.example", "Origin": "http://rebind.example"}
    )
    assert response.status_code == 400


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_loopback_hosts_keep_native_owner_access(monkeypatch, host):
    client = local_client(monkeypatch, host=host)
    assert client.get("/healthz").status_code == 200
    assert client.get("/panel/memories").status_code == 200


def test_custom_domain_requires_login_even_from_loopback_proxy(monkeypatch):
    monkeypatch.setattr(settings, "allowed_hosts", ["memory.example"])
    client = local_client(monkeypatch, host="memory.example")
    assert client.get("/panel/", follow_redirects=False).headers["location"] == "/owner/login"
    assert client.get("/panel/memories").status_code == 401
    assert (
        client.get("/panel/memories", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 401
    )
    assert (
        client.post(
            "/owner/login", data={"token": settings.setup_token}, follow_redirects=False
        ).status_code
        == 303
    )
    assert client.get("/panel/memories").status_code == 200


@pytest.mark.parametrize("header", ["Origin", "Referer"])
@pytest.mark.parametrize(
    "value", ["http://[invalid", "http://localhost:notaport", "null", "http://user@127.0.0.1"]
)
def test_malformed_origins_fail_without_server_errors(monkeypatch, header, value):
    client = local_client(monkeypatch)
    response = client.post(
        "/owner/login", data={"token": settings.setup_token}, headers={header: value}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


def test_origin_normalizes_default_ports_but_rejects_other_ports(monkeypatch):
    client = local_client(monkeypatch)
    assert (
        client.post(
            "/owner/login",
            data={"token": settings.setup_token},
            headers={"Origin": "http://127.0.0.1:80"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert (
        client.post(
            "/owner/login",
            data={"token": settings.setup_token},
            headers={"Origin": "http://127.0.0.1:8000"},
        ).status_code
        == 401
    )


def test_explicit_public_origin_supports_tls_proxy_without_forwarded_trust(monkeypatch):
    monkeypatch.setattr(settings, "allowed_hosts", ["memory.example"])
    monkeypatch.setattr(settings, "public_origin", "https://memory.example")
    client = local_client(monkeypatch, host="memory.example")
    login = client.post(
        "/owner/login",
        data={"token": settings.setup_token},
        headers={"Origin": "https://memory.example"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert "Secure" in login.headers["set-cookie"]
    assert client.get("https://memory.example/panel/memories").status_code == 200
    assert (
        client.post(
            "/owner/login",
            data={"token": settings.setup_token},
            headers={"Origin": "http://memory.example", "X-Forwarded-Proto": "https"},
        ).status_code
        == 401
    )


@pytest.mark.parametrize(
    "value, enabled",
    [
        ("1", True),
        ("true", True),
        (" YES ", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
    ],
)
def test_auth_flag_has_same_meaning_in_api_and_stdio(monkeypatch, value, enabled):
    from porter.mcp_stdio import _build_deps

    monkeypatch.setenv("AUTH_ENABLED", value)
    monkeypatch.setenv("PORTER_REPO", "memory")
    monkeypatch.setenv("PORTER_EMBEDDER", "hash")
    assert Settings().auth_enabled is enabled
    deps, pool = asyncio.run(_build_deps())
    assert pool is None
    assert deps.auth_enabled_fn() is enabled


@pytest.mark.parametrize("value", ["", "treu", "2"])
def test_invalid_auth_flag_stops_api_and_stdio(monkeypatch, value):
    from porter.mcp_stdio import _build_deps

    monkeypatch.setenv("AUTH_ENABLED", value)
    with pytest.raises(ValueError, match="AUTH_ENABLED"):
        Settings()
    with pytest.raises(ValueError, match="AUTH_ENABLED"):
        asyncio.run(_build_deps())


def test_repository_typo_cannot_silently_select_volatile_storage(monkeypatch):
    from porter.main import _use_postgres
    from porter.mcp_stdio import _build_deps

    monkeypatch.setenv("PORTER_REPO", "postgre")
    for select in (repository_kind, _use_postgres):
        with pytest.raises(ValueError, match="PORTER_REPO"):
            select()
    with pytest.raises(ValueError, match="PORTER_REPO"):
        asyncio.run(_build_deps())


@pytest.mark.parametrize(
    "value", ["*", "*.example.com", "localhost:8000", "http://localhost", "", "localhost,"]
)
def test_host_configuration_requires_exact_nonempty_hosts(monkeypatch, value):
    monkeypatch.setenv("PORTER_ALLOWED_HOSTS", value)
    with pytest.raises(ValueError, match="PORTER_ALLOWED_HOSTS"):
        Settings()


@pytest.mark.parametrize(
    "value",
    [
        "https://memory.example/panel",
        "https://user@memory.example",
        "https://memory.example:bad",
        "//memory.example",
    ],
)
def test_invalid_public_origin_is_rejected(monkeypatch, value):
    monkeypatch.setenv("PORTER_PUBLIC_ORIGIN", value)
    with pytest.raises(ValueError, match="PORTER_PUBLIC_ORIGIN"):
        Settings()


def test_http_metadata_uses_the_package_version(monkeypatch):
    client = local_client(monkeypatch)
    assert client.get("/openapi.json").json()["info"]["version"] == __version__
    assert client.get("/healthz").json()["version"] == __version__
