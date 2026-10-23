"""Dependency failures remain clear and private over REST and MCP writes."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError
from test_deployed_api import mint

from porter.app import create_app
from porter.auth import RevocationCache
from porter.embeddings import EmbeddingTimeout, EmbeddingUnavailable, HttpEmbedder, OpenAIEmbedder
from porter.mcp_server import MCPDeps, build_mcp_server
from porter.oauth_store import InMemoryOAuthStore
from porter.ratelimit import TokenBucket
from porter.repositories import InMemoryMemoryRepository

PRIVATE = "synthetic-private-memory-and-secret"
UPSTREAM = "http://synthetic-user:synthetic-password@embedder.invalid:8001"


def failing_post(kind):
    def post(url, **_kwargs):
        request = httpx.Request("POST", url)
        if kind == "timeout":
            raise httpx.ReadTimeout(PRIVATE, request=request)
        if kind == "connect":
            raise httpx.ConnectError(PRIVATE, request=request)
        if kind == "http":
            return httpx.Response(503, text=PRIVATE, request=request)
        return httpx.Response(200, text=PRIVATE, request=request)

    return post


@pytest.mark.parametrize("provider", ["http", "openai"])
@pytest.mark.parametrize("kind", ["timeout", "connect", "http", "json"])
def test_embedding_http_failures_are_stable_and_do_not_expose_upstream_details(
    monkeypatch, provider, kind
):
    monkeypatch.setattr(httpx, "post", failing_post(kind))
    embedder = HttpEmbedder(UPSTREAM) if provider == "http" else OpenAIEmbedder("synthetic-api-key")
    error = EmbeddingTimeout if kind == "timeout" else EmbeddingUnavailable
    with pytest.raises(error) as caught:
        embedder.embed_one(PRIVATE)
    expected_status = 504 if kind == "timeout" else 503
    assert caught.value.status_code == expected_status
    assert caught.value.code == (
        "dependency_timeout" if kind == "timeout" else "dependency_unavailable"
    )
    message = str(caught.value)
    for private in (PRIVATE, UPSTREAM, "synthetic-password", "synthetic-api-key"):
        assert private not in message
    assert caught.value.__suppress_context__ is True


def test_http_embedding_success_preserves_timeout_and_vectors(monkeypatch):
    received = {}

    def post(url, **kwargs):
        received.update(url=url, **kwargs)
        return httpx.Response(
            200,
            json={"model": "synthetic-model", "vectors": [[0.0, 1.0]]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", post)
    embedder = HttpEmbedder("http://embedder.invalid/", dim=2, timeout=7.5)
    assert embedder.embed_one(PRIVATE) == [0.0, 1.0]
    assert embedder.model_id == "synthetic-model"
    assert received == {
        "url": "http://embedder.invalid/embed",
        "json": {"texts": [PRIVATE]},
        "timeout": 7.5,
    }
    assert HttpEmbedder("http://embedder.invalid").timeout == 30.0


def test_openai_embedding_success_preserves_timeout_and_normalization(monkeypatch):
    received = {}

    def post(url, **kwargs):
        received.update(url=url, **kwargs)
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [3.0, 4.0]}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", post)
    embedder = OpenAIEmbedder("synthetic-api-key", dim=2)
    assert embedder.embed_one(PRIVATE) == [0.6, 0.8]
    assert received["timeout"] == 60.0
    assert received["json"]["input"] == [PRIVATE]


@pytest.fixture()
def authenticated_api():
    repo = InMemoryMemoryRepository(HttpEmbedder(UPSTREAM))
    store = InMemoryOAuthStore()
    app = create_app(repo, store, auth_enabled=True, enable_mcp=False, rate_limit_per_minute=10000)
    client = TestClient(app, follow_redirects=False)
    mint(client, "synthetic owner", "memory.admin")
    token = mint(client, "synthetic client", "memory.read memory.write")["access_token"]
    return client, repo, store, token


@pytest.mark.parametrize("kind", ["timeout", "connect", "http"])
@pytest.mark.parametrize("operation", ["create", "search"])
def test_rest_embedding_failure_has_dependency_error_and_does_not_write(
    authenticated_api, monkeypatch, kind, operation
):
    client, repo, _store, token = authenticated_api
    monkeypatch.setattr(httpx, "post", failing_post(kind))
    path = "/api/v1/memories" + ("/search" if operation == "search" else "")
    payload = {"query": PRIVATE} if operation == "search" else {"content": PRIVATE, "type": "fact"}
    response = client.post(path, json=payload, headers={"Authorization": "Bearer " + token})
    assert response.status_code == (504 if kind == "timeout" else 503)
    assert response.json()["error"]["code"] == (
        "dependency_timeout" if kind == "timeout" else "dependency_unavailable"
    )
    assert PRIVATE not in response.text and UPSTREAM not in response.text
    assert asyncio.run(repo.list()) == []
    assert asyncio.run(repo.get_changes(0, 10))[0] == []


@pytest.mark.parametrize("kind", ["timeout", "connect", "http"])
@pytest.mark.parametrize("tool", ["memory_add", "memory_search"])
def test_mcp_embedding_failure_has_porter_error_and_does_not_write(
    authenticated_api, monkeypatch, kind, tool
):
    _client, repo, store, token = authenticated_api
    monkeypatch.setattr(httpx, "post", failing_post(kind))
    monkeypatch.setenv("PORTER_TOKEN", token)
    cache, limiter = RevocationCache(), TokenBucket(10000)
    server = build_mcp_server(
        MCPDeps(
            repo_fn=lambda: repo,
            oauth_store_fn=lambda: store,
            revocation_cache_fn=lambda: cache,
            rate_limiter_fn=lambda: limiter,
            auth_enabled_fn=lambda: True,
        )
    )

    async def run():
        payload = {"query": PRIVATE} if tool == "memory_search" else {"content": PRIVATE}
        code = "dependency_timeout" if kind == "timeout" else "dependency_unavailable"
        with pytest.raises(ToolError, match="porter:" + code) as caught:
            await server.call_tool(tool, payload)
        assert PRIVATE not in str(caught.value) and UPSTREAM not in str(caught.value)
        assert await repo.list() == []
        assert (await repo.get_changes(0, 10))[0] == []

    asyncio.run(run())
