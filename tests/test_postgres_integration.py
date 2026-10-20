"""Run against a dedicated *_test database; CI supplies Postgres + pgvector."""

import asyncio
import os
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import asyncpg
import pytest

from porter.db import LEGACY_MIGRATION_NAMES, create_pool, run_migrations
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
            # Adopt migration names used by the earlier local review while
            # preserving all rows and without executing the migrations again.
            for canonical, legacy in LEGACY_MIGRATION_NAMES.items():
                await pool.execute(
                    "UPDATE schema_migrations SET name = $1 WHERE name = $2", legacy, canonical
                )
            assert await run_migrations(isolated_url) == []
            assert await pool.fetchval("SELECT count(*) FROM schema_migrations") == 14

            from porter import conflicts, lifecycle, switch, teams
            from porter.e2ee_hub import (
                PostgresEnvelopeStore,
                generate_device_keypair,
                open_sealed_key,
                seal_key_for,
            )
            from porter.oauth_store import PostgresOAuthStore

            owner = await PostgresOAuthStore(pool).create_client(
                name="workspace-owner",
                client_id="owner-" + uuid4().hex,
                secret_hash="test-only-unused",
                redirect_uris=[],
            )
            team_store = teams.PostgresTeamStore(pool)
            workspace = await teams.create_workspace(
                team_store, repo, name="Persistent team", owner_client_id=owner.id
            )
            assert (
                await team_store.get_workspace_by_tag(workspace["project_tag"])
            ).name == "Persistent team"
            workspace_id = UUID(workspace["id"])
            member_id = uuid4()
            await teams.invite(
                team_store,
                repo,
                workspace_id=workspace_id,
                client_id=member_id,
                role="editor",
                actor_client_id=owner.id,
            )
            assert await teams.check_access(
                team_store, workspace_id=workspace_id, client_id=owner.id, need="admin"
            )
            assert await teams.check_access(
                team_store, workspace_id=workspace_id, client_id=member_id, need="write"
            )

            envelope_store = PostgresEnvelopeStore(pool)
            private, public = generate_device_keypair()
            await envelope_store.register_device("recipient", public)
            sealed = seal_key_for(b"k" * 32, public)
            await envelope_store.post_envelope("recipient", "sender", sealed)
            claimed = await envelope_store.claim_envelopes("recipient")
            assert len(claimed) == 1 and open_sealed_key(claimed[0].sealed, private) == b"k" * 32
            assert await envelope_store.claim_envelopes("recipient") == []

            records = []
            for content in ("Use stable formatting", "Prefer stable formatting"):
                record, _ = await repo.create_active(
                    content=content,
                    type="project",
                    scope_tags=["project:handoff"],
                    confidence=1,
                    source={},
                )
                records.append(record)
            handoff = await switch.build_handoff(
                repo,
                project="handoff",
                from_harness="codex",
                to_harness="cursor",
                actor_client_id=owner.id,
            )
            assert handoff["sections"]["in_progress"] or handoff["sections"]["conventions"]
            # An error after the first archive must roll back the merged row,
            # source changes, audit events and sync cursor together.
            before_count = await pool.fetchval("SELECT count(*) FROM memories")
            before_cursor = (await repo.get_changes(0, 10000))[1]
            original_archive = repo.archive

            async def fail_after_archive(*args, **kwargs):
                await original_archive(*args, **kwargs)
                raise RuntimeError("simulated interrupted merge")

            repo.archive = fail_after_archive
            try:
                with pytest.raises(RuntimeError, match="interrupted merge"):
                    await conflicts.semantic_merge(repo, [r.id for r in records])
            finally:
                repo.archive = original_archive
            assert await pool.fetchval("SELECT count(*) FROM memories") == before_count
            assert (await repo.get_changes(0, 10000))[1] == before_cursor
            for record in records:
                assert (await repo.get(record.id)).status == "active"
            merged = await conflicts.semantic_merge(
                repo, [r.id for r in records], actor_client_id=owner.id
            )
            for record in records:
                archived = await repo.get(record.id)
                assert archived.superseded_by == merged["id"]
                assert archived.source["porter_provenance"]["conflicts"]
            loser, _ = await repo.create_active(
                content="Old formatting decision",
                type="project",
                scope_tags=["project:handoff"],
                confidence=1,
                source={},
            )
            await conflicts.resolve_conflict(
                repo, merged["id"], [loser.id], resolution="updated", actor_client_id=owner.id
            )
            assert (await repo.get(loser.id)).superseded_by == merged["id"]
            stale, _ = await repo.create_active(
                content="Expired task state",
                type="project",
                scope_tags=["project:handoff"],
                confidence=0.1,
                source={},
            )
            assert stale.id in (await lifecycle.sweep(repo))["archived_ids"]
            await repo.audit(action="lifecycle_sweep", client_id=owner.id)
            await repo.audit(action="writeback", client_id=owner.id)
            await repo.audit(action="contradiction_flag", client_id=owner.id)
        finally:
            if pool is not None:
                await pool.close()
            await admin.execute(f'DROP DATABASE "{database}"')
            await admin.close()

    asyncio.run(run())
