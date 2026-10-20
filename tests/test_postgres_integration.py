"""Run against a dedicated *_test database; CI supplies Postgres + pgvector."""

import asyncio
import os
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest

from porter.db import create_pool, run_migrations
from porter.embeddings import HashEmbedder
from porter.repositories import PostgresMemoryRepository
from porter.snapshots import (
    PostgresSnapshotStore,
    apply_snapshot_file,
    create_snapshot,
    inspect_snapshot_file,
)
from porter.sync import build_sync_delta
from porter.triage import create_with_triage


def test_persistent_memory_snapshot_and_scoped_sync(tmp_path):
    url = os.getenv("PORTER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("PORTER_TEST_DATABASE_URL is not set (real Postgres required)")
    if not urlsplit(url).path.rstrip("/").endswith("_test"):
        pytest.fail("integration tests require a dedicated database ending in _test")

    async def run():
        # Every run gets fresh data; cross-project duplicate detection must
        # not make a later run depend on memories from earlier test runs.
        database = "porter_" + uuid4().hex + "_test"
        admin = await asyncpg.connect(url)
        await admin.execute(f'CREATE DATABASE "{database}"')
        isolated_url = urlunsplit(urlsplit(url)._replace(path="/" + database))
        pool = None
        try:
            await run_migrations(isolated_url)
            assert await run_migrations(isolated_url) == []
            pool = await create_pool(isolated_url)
            repo = PostgresMemoryRepository(HashEmbedder(), pool)
            store = PostgresSnapshotStore(pool)
            tag = "project:" + uuid4().hex
            ids = []
            for kind in ("personal", "project"):
                rec, _ = await repo.create_pending(
                    content=f"中文 {kind} memory",
                    type=kind,
                    scope_tags=[tag],
                    confidence=1,
                    source={"client_id": "postgres-test"},
                )
                assert rec.source["client_id"] == "postgres-test"
                await repo.approve(rec.id)
                ids.append(rec.id)
            before_probe = await pool.fetchval(
                "SELECT last_retrieved_at FROM memories WHERE id = $1", rec.id
            )
            hits = await repo.find_similar_active(rec.embedding, threshold=0.99)
            hit = next(hit.record for hit in hits if hit.record.id == rec.id)
            assert hit.content == rec.content and hit.scope_tags == [tag]
            assert hit.source["client_id"] == "postgres-test"
            assert (
                await pool.fetchval("SELECT last_retrieved_at FROM memories WHERE id = $1", rec.id)
                == before_probe
            )
            await repo.audit(action="read", meta={"via": "postgres-regression"})
            assert (await repo.audit_list(limit=1))[0].meta == {"via": "postgres-regression"}
            automated, decision = await create_with_triage(
                repo,
                content="Use deterministic formatting for this repository",
                type="project",
                scope_tags=[tag],
                confidence=1,
                source={"client_id": "automation-test"},
            )
            assert decision.action == "auto_approve"
            assert (await repo.audit_list(limit=1))[0].action == "auto_approve"
            await repo.delete(automated.id)
            await repo.set_pinned(ids[1], True)
            _, head = await repo.get_changes(0, 10000)
            revised = await repo.revise(ids[1], content="Updated project memory")
            assert revised.pinned and revised.source["revised_from"] == str(ids[1])
            delta = await build_sync_delta(repo, since=head, limit=1, granted=["memory.read"])
            assert delta["changes"][0]["op"] == "archive"
            assert delta["changes"][0]["memory"] is None
            second = await build_sync_delta(
                repo, since=delta["cursor"], limit=1, granted=["memory.read"]
            )
            assert second["changes"][0]["memory_id"] == str(revised.id)
            rec, path = await create_snapshot(
                repo, store, tags=[tag], storage_dir=str(tmp_path), password="test-password"
            )
            assert rec.manifest["counts"]["memories"] == 2
            assert (await store.get(rec.id)).manifest == rec.manifest
            assert (await store.list())[0].id == rec.id
            assert inspect_snapshot_file(path, "test-password")["memory_count"] == 2
            report = await apply_snapshot_file(
                repo, repo.audit, path=path, password="test-password"
            )
            assert report["imported"] == 2
            _, head = await repo.get_changes(0, 10000)
            await repo.delete(revised.id)
            deleted = await build_sync_delta(
                repo, since=head, limit=100, granted=["memory.read"], project=tag[8:]
            )
            assert len(deleted["changes"]) == 1
            assert deleted["changes"][0]["memory"] is None
            hidden = await build_sync_delta(
                repo, since=head, limit=100, granted=["memory.read.dietary"]
            )
            assert hidden["changes"] == []
            assert await repo.search(query="personal", tags=[tag])
            # A later writer cannot publish a higher cursor while the first
            # change is uncommitted; the counter lock is held through commit.
            async with pool.acquire() as first, pool.acquire() as second:
                tx = first.transaction()
                await tx.start()
                waiting = None
                committed = False
                try:
                    seq1 = await first.fetchval(
                        "INSERT INTO change_log (memory_id, op) VALUES ($1, 'revise') RETURNING seq",
                        ids[0],
                    )
                    waiting = asyncio.create_task(
                        second.fetchval(
                            "INSERT INTO change_log (memory_id, op) VALUES ($1, 'revise') RETURNING seq",
                            ids[0],
                        )
                    )
                    done, _ = await asyncio.wait([waiting], timeout=0.1)
                    assert not done
                    await tx.commit()
                    committed = True
                    seq2 = await asyncio.wait_for(waiting, timeout=5)
                    assert seq2 == seq1 + 1
                finally:
                    if waiting is not None and not waiting.done():
                        waiting.cancel()
                        await asyncio.gather(waiting, return_exceptions=True)
                    if not committed:
                        await tx.rollback()
        finally:
            if pool is not None:
                await pool.close()
            await admin.execute(f'DROP DATABASE "{database}"')
            await admin.close()

    asyncio.run(run())
