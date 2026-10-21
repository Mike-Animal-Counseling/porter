"""Exercise read boundaries on writes and graph metadata, across transports."""

import asyncio
import json
import os
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError
from test_deployed_api import mint

from porter.app import create_app
from porter.auth import RevocationCache
from porter.db import create_pool, run_migrations
from porter.embeddings import HashEmbedder
from porter.llm import StubLLMExtractor
from porter.mcp_server import MCPDeps, build_mcp_server
from porter.oauth_store import InMemoryOAuthStore
from porter.ratelimit import TokenBucket
from porter.repositories import InMemoryMemoryRepository, PostgresMemoryRepository
from porter.snapshots import SNAPSHOT_FORMAT, apply_snapshot_file, inspect_snapshot_file
from porter.triage import create_with_triage
from porter.wiki import InMemoryWikiStore, WikiService

TEXT = "The editor uses the bright violet color palette"


@pytest.fixture()
def api():
    repo = InMemoryMemoryRepository(HashEmbedder())
    store = InMemoryOAuthStore()
    app = create_app(repo, store, auth_enabled=True, rate_limit_per_minute=10000, enable_mcp=False)
    client = TestClient(app, follow_redirects=False)
    mint(client, "admin", "memory.admin")
    tokens = {
        "write_only": mint(client, "writer", "memory.write"),
        "work_writer": mint(client, "work writer", "memory.write memory.read.work"),
        "reader": mint(client, "reader", "memory.read.work"),
    }
    return client, repo, store, tokens


def server_for(repo, store, wiki=None):
    cache, limiter = RevocationCache(), TokenBucket(10000)
    return build_mcp_server(
        MCPDeps(
            repo_fn=lambda: repo,
            oauth_store_fn=lambda: store,
            revocation_cache_fn=lambda: cache,
            rate_limiter_fn=lambda: limiter,
            auth_enabled_fn=lambda: True,
            wiki_fn=lambda: wiki,
        )
    )


async def seed(repo, *, tags=("health",), type="fact", confidence=0.6):
    record, _ = await repo.create_active(
        content=TEXT,
        type=type,
        scope_tags=list(tags),
        confidence=confidence,
        source={},
    )
    return record


@pytest.mark.parametrize("grant", ["write_only", "work_writer"])
@pytest.mark.parametrize("confidence", [0.2, 1.0])
def test_rest_write_cannot_probe_or_refresh_private_duplicate(api, grant, confidence):
    client, repo, _store, tokens = api
    private = asyncio.run(seed(repo))
    response = client.post(
        "/api/v1/memories",
        headers={"Authorization": "Bearer " + tokens[grant]["access_token"]},
        json={
            "content": TEXT,
            "type": "fact",
            "scope_tags": ["work"],
            "confidence": confidence,
        },
    )
    assert response.status_code == 202
    assert response.json()["status"] != "merged"
    assert str(private.id) not in response.text
    assert private.confidence == 0.6


@pytest.mark.parametrize("grant", ["write_only", "work_writer"])
def test_scoped_writes_do_not_expose_pending_duplicate_ids(api, monkeypatch, grant):
    client, repo, _store, tokens = api
    monkeypatch.setattr("porter.triage.settings.approval_mode", "strict")
    pending, _ = asyncio.run(
        repo.create_pending(
            content=TEXT, type="fact", scope_tags=["work"], confidence=0.6, source={}
        )
    )
    response = client.post(
        "/api/v1/memories",
        headers={"Authorization": "Bearer " + tokens[grant]["access_token"]},
        json={"content": TEXT, "type": "fact", "scope_tags": ["work"]},
    )
    assert response.status_code == 202
    assert response.json()["duplicate_candidates"] == []
    assert str(pending.id) not in response.text
    assert pending.status == "pending"


@pytest.mark.parametrize("grant", ["write_only", "work_writer"])
def test_mcp_add_cannot_probe_or_refresh_private_duplicate(api, monkeypatch, grant):
    _client, repo, store, tokens = api
    monkeypatch.setenv("PORTER_TOKEN", tokens[grant]["access_token"])

    async def run():
        private = await seed(repo)
        result = await server_for(repo, store).call_tool(
            "memory_add", {"content": TEXT, "scope_tags": ["work"], "confidence": 0.2}
        )
        payload = json.loads(result.content[0].text)
        assert payload["status"] == "pending_review"
        assert str(private.id) not in json.dumps(payload)
        assert private.confidence == 0.6

    asyncio.run(run())


@pytest.mark.parametrize("kind,tags", [("fact", ["work", "general"]), ("project", ["work"])])
def test_auto_merge_preserves_readable_targets_domain_and_tags(api, kind, tags):
    client, repo, _store, tokens = api
    original = asyncio.run(seed(repo, tags=tags, type=kind))
    response = client.post(
        "/api/v1/memories",
        headers={"Authorization": "Bearer " + tokens["work_writer"]["access_token"]},
        json={"content": TEXT, "type": "fact", "scope_tags": ["work"]},
    )
    assert response.status_code == 202
    assert response.json()["status"] == "pending_review"
    assert response.json()["id"] != str(original.id)
    assert original.type == kind and original.scope_tags == tags
    assert original.confidence == 0.6


def test_same_domain_and_tags_can_still_merge_for_read_write_client(api):
    client, repo, _store, tokens = api
    original = asyncio.run(seed(repo, tags=["work"]))
    response = client.post(
        "/api/v1/memories",
        headers={"Authorization": "Bearer " + tokens["work_writer"]["access_token"]},
        json={"content": TEXT, "type": "fact", "scope_tags": ["work"]},
    )
    assert response.status_code == 202
    assert response.json()["merged_into"] == str(original.id)


@pytest.mark.parametrize("grant", ["write_only", "work_writer"])
def test_mcp_update_cannot_revise_inaccessible_target_or_stale_id(api, monkeypatch, grant):
    _client, repo, store, tokens = api
    monkeypatch.setenv("PORTER_TOKEN", tokens[grant]["access_token"])

    async def run():
        original = await seed(repo, tags=["work"])
        head = await repo.revise(original.id, scope_tags=["health"])
        server = server_for(repo, store)
        for target in (original.id, head.id):
            with pytest.raises(ToolError, match="porter:not_found"):
                await server.call_tool("memory_update", {"id": str(target), "content": "changed"})
        assert (await repo.get(head.id)).content == TEXT
        assert (await repo.get(head.id)).status == "active"

    asyncio.run(run())


def test_graph_metadata_requires_readable_active_originating_memory(api, monkeypatch):
    _client, repo, store, tokens = api
    monkeypatch.setenv("PORTER_TOKEN", tokens["reader"]["access_token"])

    async def run():
        wiki = WikiService(InMemoryWikiStore(), StubLLMExtractor(), enabled=True)
        root = await wiki.store.upsert_entity("shared", "topic")
        visible = await seed(repo, tags=["work"])
        private = await seed(repo)
        pending, _ = await repo.create_pending(
            content="pending", type="fact", scope_tags=["work"], confidence=1, source={}
        )
        archived = await seed(repo, tags=["work"])
        await repo.archive(archived.id)
        encrypted, _ = await repo.create_pending(
            content="eHl6",
            type="fact",
            scope_tags=["work"],
            confidence=1,
            source={},
            encrypted=True,
            key_id="test-key",
        )
        await repo.approve(encrypted.id)
        for label, origin in (
            ("visible", visible.id),
            ("private", private.id),
            ("pending", pending.id),
            ("archived", archived.id),
            ("encrypted", encrypted.id),
            ("unattributed", None),
        ):
            destination = await wiki.store.upsert_entity(label, "topic")
            await wiki.store.add_edge(root, destination, "co_occurs", origin)
        result = await server_for(repo, store, wiki).call_tool("memory_graph", {"entity": "shared"})
        payload = json.loads(result.content[0].text)
        assert [n["name"] for n in payload["neighbors"]] == ["visible"]
        assert payload["neighbors"][0]["via_memory_id"] == str(visible.id)
        assert str(private.id) not in json.dumps(payload)

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["personal", "project"])
def test_memory_list_accepts_both_current_domain_types(api, kind):
    client, repo, _store, tokens = api
    expected = asyncio.run(seed(repo, tags=["work"], type=kind))
    asyncio.run(seed(repo, tags=["work"], type="fact"))
    response = client.get(
        "/api/v1/memories",
        headers={"Authorization": "Bearer " + tokens["reader"]["access_token"]},
        params={"type": kind},
    )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [str(expected.id)]


@pytest.mark.parametrize("preview", [True, False])
def test_snapshot_rejects_personal_workspace_entry_before_any_import(tmp_path, preview):
    repo = InMemoryMemoryRepository(HashEmbedder())
    path = tmp_path / "boundary.porter.json"
    path.write_text(
        json.dumps(
            {
                "format": SNAPSHOT_FORMAT,
                "memories": [
                    {"content": "valid first entry", "type": "fact"},
                    {
                        "content": "private second entry",
                        "type": "personal",
                        "scope_tags": ["workspace:12345678"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    async def run():
        with pytest.raises(ValueError, match="personal memories must not carry workspace tags"):
            if preview:
                inspect_snapshot_file(str(path), None)
            else:
                await apply_snapshot_file(repo, repo.audit, path=str(path), password=None)
        assert await repo.list() == []
        assert await repo.audit_list() == []

    asyncio.run(run())


def test_postgres_duplicate_filter_and_snapshot_import_rollback(tmp_path):
    url = os.getenv("PORTER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("PORTER_TEST_DATABASE_URL is not set (real Postgres required)")
    if not urlsplit(url).path.rstrip("/").endswith("_test"):
        pytest.fail("integration tests require a dedicated database ending in _test")

    async def run():
        database = "porter_" + uuid4().hex + "_test"
        admin = await asyncpg.connect(url)
        await admin.execute(f'CREATE DATABASE "{database}"')
        isolated_url = urlunsplit(urlsplit(url)._replace(path="/" + database))
        pool = None
        try:
            await run_migrations(isolated_url)
            pool = await create_pool(isolated_url)
            repo = PostgresMemoryRepository(HashEmbedder(), pool)
            visible = await seed(repo, tags=["work"])
            private = await seed(repo)
            pending, _ = await repo.create_pending(
                content=TEXT, type="fact", scope_tags=["work"], confidence=0.2, source={}
            )
            _, decision = await create_with_triage(
                repo,
                content=TEXT,
                type="fact",
                scope_tags=["work"],
                confidence=0.2,
                source={},
                scopes_used=["memory.write", "memory.read.work"],
            )
            assert decision.action == "queue"
            assert decision.duplicate_ids == [visible.id]
            assert private.id not in decision.duplicate_ids
            assert pending.id not in decision.duplicate_ids
            # A readable target with different tags cannot be refreshed
            # through a write to a less restrictive boundary.
            _, decision = await create_with_triage(
                repo,
                content=TEXT,
                type="fact",
                scope_tags=["work", "general"],
                confidence=1,
                source={},
                scopes_used=["memory.write", "memory.read.work"],
            )
            assert decision.action == "queue"
            assert (await repo.get(visible.id)).confidence <= 0.6

            before_count = await pool.fetchval("SELECT COUNT(*) FROM memories")
            before_changes = await pool.fetchval("SELECT COUNT(*) FROM change_log")
            path = tmp_path / "atomic.porter.json"
            path.write_text(
                json.dumps(
                    {
                        "format": SNAPSHOT_FORMAT,
                        "memories": [
                            {"content": "first imported entry"},
                            {"content": "second imported entry"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            original_create = repo.create_pending
            imported_count = 0

            async def failing_create(**kwargs):
                nonlocal imported_count
                result = await original_create(**kwargs)
                imported_count += 1
                if imported_count == 2:
                    raise RuntimeError("synthetic import failure")
                return result

            repo.create_pending = failing_create
            try:
                with pytest.raises(RuntimeError, match="synthetic import failure"):
                    await apply_snapshot_file(repo, repo.audit, path=str(path), password=None)
            finally:
                repo.create_pending = original_create
            assert await pool.fetchval("SELECT COUNT(*) FROM memories") == before_count
            assert await pool.fetchval("SELECT COUNT(*) FROM change_log") == before_changes
            assert (
                await pool.fetchval(
                    "SELECT COUNT(*) FROM audit_log WHERE action = 'snapshot_apply'"
                )
                == 0
            )
        finally:
            if pool is not None:
                await pool.close()
            await admin.execute(f'DROP DATABASE "{database}" WITH (FORCE)')
            await admin.close()

    asyncio.run(run())
