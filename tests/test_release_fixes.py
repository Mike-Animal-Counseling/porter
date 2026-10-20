"""Regressions for data loss, persistent storage and local deployment."""

import asyncio
import importlib.util
import json
from datetime import UTC
from pathlib import Path

import pytest
import yaml

from porter.embeddings import HashEmbedder
from porter.repositories import InMemoryMemoryRepository, _row_to_record
from porter.snapshots import (
    InMemorySnapshotStore,
    apply_snapshot_file,
    create_snapshot,
    inspect_snapshot_file,
)
from porter.sync import build_sync_delta


async def active(repo, text, tags=(), type="fact"):
    rec, _ = await repo.create_pending(
        content=text,
        type=type,
        scope_tags=list(tags),
        confidence=1,
        source={},
    )
    return await repo.approve(rec.id)


def test_sync_drains_every_page_without_skipping_hidden_rows():
    async def run():
        repo = InMemoryMemoryRepository(HashEmbedder())
        wanted = await active(repo, "visible", ["dietary"])
        await active(repo, "hidden", ["finance"])
        last = await active(repo, "visible again", ["dietary"])
        cursor, received, empty_pages = 0, set(), 0
        while True:
            page = await build_sync_delta(
                repo, since=cursor, limit=1, granted=["memory.read.dietary"]
            )
            if page["cursor"] == cursor:
                break
            assert page["cursor"] == cursor + 1
            empty_pages += not page["changes"]
            received.update(c["memory_id"] for c in page["changes"])
            cursor = page["cursor"]
        assert received == {str(wanted.id), str(last.id)}
        assert cursor == 6 and empty_pages == 2

    asyncio.run(run())


def test_deleted_memory_keeps_tag_and_project_visibility():
    async def run():
        repo = InMemoryMemoryRepository(HashEmbedder())
        mine = await active(repo, "mine", ["dietary", "project:alpha"])
        other = await active(repo, "other", ["finance", "project:beta"])
        _, head = await repo.get_changes(0, 100)
        await repo.delete(mine.id)
        await repo.delete(other.id)
        scoped = await build_sync_delta(
            repo, since=head, limit=100, granted=["memory.read.dietary"], project="alpha"
        )
        assert [c["memory_id"] for c in scoped["changes"]] == [str(mine.id)]
        assert scoped["changes"][0]["memory"] is None
        full = await build_sync_delta(repo, since=head, limit=100, granted=["memory.read"])
        assert len(full["changes"]) == 2

    asyncio.run(run())


def test_revision_retires_old_id_and_preserves_pin():
    async def run():
        repo = InMemoryMemoryRepository(HashEmbedder())
        old = await active(repo, "old", ["project:alpha"], "project")
        old.triage_reasons = ["low_risk"]
        await repo.set_pinned(old.id, True)
        _, head = await repo.get_changes(0, 100)
        new = await repo.revise(old.id, content="new")
        assert new.pinned and new.triage_reasons == ["low_risk"]
        page = await build_sync_delta(repo, since=head, limit=100, granted=["memory.read"])
        assert [(c["op"], c["memory_id"]) for c in page["changes"]] == [
            ("archive", str(old.id)),
            ("revise", str(new.id)),
        ]
        assert page["changes"][0]["memory"] is None
        assert page["changes"][0]["status"] == "archived"

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["personal", "project"])
def test_new_memory_types_survive_multiple_encrypted_snapshots(kind, tmp_path):
    async def run():
        repo = InMemoryMemoryRepository(HashEmbedder())
        await active(repo, "中文记忆\nwith newlines", ["project:alpha"], kind)
        store = InMemorySnapshotStore()
        _, first = await create_snapshot(
            repo, store, password="test-password", storage_dir=str(tmp_path)
        )
        _, second = await create_snapshot(
            repo, store, password="test-password", storage_dir=str(tmp_path)
        )
        assert first != second
        assert inspect_snapshot_file(first, "test-password")["memory_count"] == 1
        restored = InMemoryMemoryRepository(HashEmbedder())
        await apply_snapshot_file(restored, restored.audit, path=second, password="test-password")
        records = await restored.list()
        assert len(records) == 1 and records[0].type == kind
        assert records[0].content == "中文记忆\nwith newlines"
        assert records[0].status == "pending"

    asyncio.run(run())


def test_asyncpg_default_json_text_is_decoded():
    from datetime import datetime
    from uuid import uuid4

    now = datetime.now(UTC)
    rec = _row_to_record(
        {
            "id": uuid4(),
            "content": "hello",
            "type": "fact",
            "scope_tags": [],
            "source": '{"client_id":"real-postgres"}',
            "confidence": 1,
            "status": "active",
            "embedding": None,
            "created_at": now,
            "updated_at": now,
            "superseded_by": None,
            "version": 1,
            "encrypted": False,
            "key_id": None,
            "last_retrieved_at": now,
            "pinned": False,
            "triage_reasons": '["low_risk"]',
        }
    )
    assert rec.source == {"client_id": "real-postgres"}


def test_stdio_uses_configured_embedding_url(monkeypatch):
    from porter import mcp_stdio
    from porter.config import settings

    monkeypatch.setenv("PORTER_REPO", "memory")
    monkeypatch.setenv("EMBEDDER_KIND", "http")
    monkeypatch.delenv("PORTER_EMBEDDER", raising=False)
    monkeypatch.setattr(settings, "embedder_url", "http://127.0.0.1:8001")
    deps, pool = asyncio.run(mcp_stdio._build_deps())
    assert pool is None
    assert deps.repo_fn().embedder.base_url == "http://127.0.0.1:8001"


def test_poll_acknowledges_empty_pages_and_drains_before_sleep(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "clients/python/porter_client.py"
    spec = importlib.util.spec_from_file_location("release_sample", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    client = mod.PorterClient("http://localhost:8000", "test")
    pages = iter(
        [
            {"cursor": 1, "changes": []},
            {"cursor": 2, "changes": [{"seq": 2}]},
            {"cursor": 2, "changes": []},
        ]
    )
    acks, received = [], []
    monkeypatch.setattr(client, "get_delta", lambda **kw: next(pages))
    monkeypatch.setattr(client, "ack_cursor", acks.append)

    def stop(_):
        raise InterruptedError("finished")

    monkeypatch.setattr(mod.time, "sleep", stop)
    with pytest.raises(InterruptedError):
        client.poll_forever(on_change=received.append)
    assert acks == [1, 2] and received == [{"seq": 2}]


def test_compose_exposes_local_dependencies_and_persists_owner_data():
    cfg = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
    )
    services = cfg["services"]
    assert services["db"]["ports"] == ["127.0.0.1:5432:5432"]
    assert services["embedder"]["ports"] == ["127.0.0.1:8001:8001"]
    assert "PORTER_JWT_SECRET" in services["api"]["environment"]
    assert services["api"]["volumes"] == ["porter-data:/home/porter/data"]


def test_transient_triage_errors_are_retried():
    from porter import triage_model as tm

    class Flaky(tm.BaseTriageModel):
        backend = "release-regression"
        calls = 0

        def score(self, content, tags):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary error")
            return tm.TriageScore(sensitive=True, backend=self.backend)

    tm.clear_score_cache()
    model = Flaky()
    assert tm.cached_score(model, "retry", ()).latency_ms < 0
    assert tm.cached_score(model, "retry", ()).sensitive


@pytest.mark.parametrize("project", ["", "   ", "x" * 80])
def test_invalid_sync_project_is_rejected(client, project):
    response = client.get("/api/v1/sync/delta", params={"project": project})
    assert response.status_code == 422


def test_container_browser_unlocks_without_trusting_forwarded_headers(monkeypatch):
    from fastapi.testclient import TestClient

    from porter.app import create_app
    from porter.config import settings
    from porter.oauth_store import InMemoryOAuthStore
    from porter.origin import OWNER_COOKIE

    monkeypatch.setattr(settings, "setup_token", "test-owner-secret")
    app = create_app(
        InMemoryMemoryRepository(HashEmbedder()), InMemoryOAuthStore(), auth_enabled=False
    )
    c = TestClient(app, base_url="http://127.0.0.1:8000", client=("172.19.0.1", 50000))
    assert c.get("/panel/", follow_redirects=False).headers["location"] == "/owner/login"
    assert c.get("/owner/login").status_code == 200
    assert c.get("/panel/memories", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 401
    assert c.post("/owner/login", data={"token": "错误的密钥"}).status_code == 401
    assert (
        c.get(
            "/oauth/authorize", params={"client_id": "x", "redirect_uri": "http://localhost"}
        ).status_code
        == 401
    )
    wrong_origin = c.post(
        "/owner/login",
        data={"token": settings.setup_token},
        headers={"Origin": "http://127.0.0.1:3000"},
    )
    assert wrong_origin.status_code == 401
    unlocked = c.post("/owner/login", data={"token": settings.setup_token}, follow_redirects=False)
    assert unlocked.status_code == 303
    assert "HttpOnly" in unlocked.headers["set-cookie"]
    assert "SameSite=strict" in unlocked.headers["set-cookie"]
    assert c.get("/panel/memories").status_code == 200
    assert 'action="/owner/logout"' in c.get("/panel/memories").text
    cookie = c.cookies[OWNER_COOKIE]
    c.cookies.clear()
    c.cookies.set(OWNER_COOKIE, cookie[:-1] + ("0" if cookie[-1] != "0" else "1"))
    assert c.get("/panel/memories").status_code == 401
    c.cookies.clear()
    c.post("/owner/login", data={"token": settings.setup_token}, follow_redirects=False)
    assert c.post("/owner/logout", follow_redirects=False).status_code == 303
    assert c.get("/panel/memories").status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {"format": "porter-snapshot/v1", "memories": None},
        {"format": "porter-snapshot/v1", "signature": "invalid", "memories": []},
        {"format": "porter-snapshot/v1", "memories": [{"content": "x", "source": 42}]},
        {"format": "porter-snapshot/v1", "memories": [{"content": "x", "scope_tags": [42]}]},
    ],
)
def test_malformed_snapshot_fails_cleanly(body, tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(ValueError):
        inspect_snapshot_file(str(path), None)


def test_key_identifiers_cannot_escape_directory_or_overwrite_keys(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "clients/python/porter_client.py"
    spec = importlib.util.spec_from_file_location("key_sample", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "KEYS_DIR", tmp_path / "keys")
    with pytest.raises(ValueError):
        mod.save_key("../outside", mod.generate_key())
    key = mod.generate_key()
    mod.save_key("existing", key)
    with pytest.raises(FileExistsError):
        mod.save_key("existing", mod.generate_key())
    assert mod.load_key("existing") == key


def test_embedding_inference_does_not_block_other_requests():
    import threading
    import time

    from porter.embeddings import Embedder

    started, release = threading.Event(), threading.Event()

    class Slow(Embedder):
        def embed(self, texts):
            started.set()
            assert release.wait(timeout=5)
            return [[1.0] for _ in texts]

    async def run():
        task = asyncio.create_task(Slow().aembed_one("hello"))
        deadline = time.monotonic() + 2
        while not started.is_set():
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        # This coroutine can progress while inference is still waiting.
        release.set()
        assert await task == [1.0]

    asyncio.run(run())
