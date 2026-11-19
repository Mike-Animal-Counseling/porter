"""Compare Graphiti/FalkorDB and Porter with synthetic approved handoffs."""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from urllib.parse import urlsplit
from uuid import uuid4

from porter.db import create_pool, run_migrations
from porter.embeddings import HashEmbedder
from porter.project_handoffs import current_handoffs, revalidate_candidates
from porter.repositories import PostgresMemoryRepository

FACTS = {
    "gateway": "HarborGateway forwards invoice events to CedarQueue.",
    "queue": "CedarQueue is consumed by LedgerWorker for accounting.",
    "pager": "LedgerWorker failures page the IndigoTeam rotation.",
    "old": "HarborGateway routed invoice retries to JuniperQueue until January 2024.",
    "new": "HarborGateway has routed invoice retries to CedarQueue since February 2024.",
    "tests": "The OliveRunner command is the verified test for invoice rounding.",
    "private": "HarborGateway routes the other project's invoices to SecretCoralQueue.",
}
QUESTIONS = [
    ("ordinary", "What is the OliveRunner invoice rounding test?", {"tests"}),
    ("ordinary", "Who gets paged when LedgerWorker fails?", {"pager"}),
    ("ordinary", "What does CedarQueue deliver to LedgerWorker?", {"queue"}),
    (
        "relationship",
        "Which team owns failures downstream of HarborGateway invoice events?",
        {"gateway", "queue", "pager"},
    ),
    (
        "relationship",
        "Trace invoice events from the gateway to the accounting consumer.",
        {"gateway", "queue"},
    ),
    ("relationship", "How can a CedarQueue backlog reach an on-call rotation?", {"queue", "pager"}),
    (
        "temporal",
        "Where do HarborGateway invoice retries go now, after the January migration?",
        {"new"},
    ),
    ("temporal", "What is the current replacement for JuniperQueue invoice retries?", {"new"}),
    ("temporal", "What has been the HarborGateway retry route since February 2024?", {"new"}),
]


async def seed(repo, tag, contents):
    rows = {}
    for key, content in contents.items():
        effective = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=40 if key == "new" else 0)
        source = {
            "pipeline": "native_handoff",
            "kind": "verified",
            "synthetic": True,
            "effective_from": effective.timestamp(),
            "effective_until": None,
        }
        row, _ = await repo.create_pending(
            content=content, type="fact", scope_tags=[tag], confidence=1.0, source=source
        )
        rows[key] = await repo.approve(row.id)
    return rows


def p95(values):
    return round(sorted(values)[math.ceil(len(values) * 0.95) - 1], 1)


async def evaluate(repo, graph, args):
    from graphiti_core.nodes import EpisodeType
    from openai import APIError
    from redis.exceptions import ConnectionError as RedisConnectionError

    nonce = uuid4().hex[:10]
    tag, private_tag = "shadow_" + nonce, "shadow_alt_" + nonce
    rows = await seed(repo, tag, {key: fact for key, fact in FACTS.items() if key != "private"})
    private = (await seed(repo, private_tag, {"private": FACTS["private"]}))["private"]
    mapping = {}
    started = time.perf_counter()
    for row in [*rows.values(), private]:
        result = await graph.add_episode(
            name=f"porter_{row.id}_v{row.version}",
            episode_body=row.content,
            source_description="Approved Porter synthetic project handoff",
            source=EpisodeType.text,
            reference_time=datetime.fromtimestamp(row.source["effective_from"], UTC),
            group_id=row.scope_tags[0],
        )
        mapping[result.episode.uuid] = (row.id, row.version)
    build_seconds = round(time.perf_counter() - started, 2)
    # Retain a stale record for the legacy ranker and a stale derived edge.
    await repo.pool.execute(
        """UPDATE memories SET source = jsonb_set(source, '{effective_until}',
            to_jsonb(EXTRACT(EPOCH FROM clock_timestamp() - INTERVAL '1 day')))
            WHERE id = $1""",
        rows["old"].id,
    )

    metrics = defaultdict(lambda: defaultdict(list))
    times = defaultdict(list)
    stale_graph, exposed, unavailable = 0, 0, 0
    expired_hits = defaultdict(int)
    for category, query, labels in QUESTIONS:
        wanted = {rows[key].id for key in labels}
        for _ in range(args.repeats):
            started = time.perf_counter()
            embedding = await repo.embedder.aembed_one(query)
            previous = [
                row["id"]
                for row in await repo.pool.fetch(
                    """SELECT id FROM memories WHERE status = 'active' AND NOT encrypted
                   AND scope_tags && ARRAY[$1]::text[] AND embedding IS NOT NULL
                   ORDER BY embedding <=> $2::vector LIMIT 5""",
                    tag,
                    PostgresMemoryRepository._vec_literal(embedding),
                )
            ]
            times["baseline_pg"].append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            current = [item["id"] for item in await current_handoffs(repo, tag, query)]
            times["current_pg"].append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            # A graph failure is recorded as no candidates. PostgreSQL still
            # supplies the normal result and is never modified by the graph.
            try:
                async with asyncio.timeout(10):
                    edges = await graph.search(query, group_ids=[tag], num_results=5)
            except (TimeoutError, ConnectionError, RedisConnectionError, APIError):
                edges = []
                unavailable += 1
            candidates = dict(
                mapping[ep] for edge in edges for ep in edge.episodes if ep in mapping
            )
            stale_graph += rows["old"].id in candidates
            exposed += private.id in candidates
            shadow = (await revalidate_candidates(repo, tag, candidates))[:5]
            times["graphiti"].append((time.perf_counter() - started) * 1000)
            for name, ids in (
                ("baseline_pg", previous),
                ("current_pg", current),
                ("graphiti", shadow),
            ):
                metrics[category][name].append(len(wanted.intersection(ids[:5])) / len(wanted))
                expired_hits[name] += rows["old"].id in ids

    revised = await repo.revise(
        rows["gateway"].id, content="HarborGateway forwards invoices to BirchQueue."
    )
    isolation_passed = await revalidate_candidates(
        repo,
        tag,
        {
            private.id: private.version,
            rows["old"].id: rows["old"].version,
            rows["gateway"].id: rows["gateway"].version,
            revised.id: revised.version + 1,
            rows["new"].id: rows["new"].version,
        },
    ) == [rows["new"].id]
    recall = {
        category: {name: round(sum(values) / len(values), 4) for name, values in variants.items()}
        for category, variants in metrics.items()
    }
    latency = {name: p95(values) for name, values in times.items()}
    usage = graph.llm_client.token_tracker.get_total_usage()
    return {
        "graphiti": version("graphiti-core"),
        "falkordb_client": version("falkordb"),
        "extraction_model": args.model,
        "postgres_embedder": "hash",
        "fixture_episodes": len(mapping),
        "queries": len(QUESTIONS),
        "repeats": args.repeats,
        "concurrent_queries": 1,
        "recall_at_5": recall,
        "p95_ms": latency,
        "index_seconds": build_seconds,
        "llm_input_tokens": usage.input_tokens,
        "llm_output_tokens": usage.output_tokens,
        "raw_cross_project_candidates": exposed,
        "raw_stale_candidate_queries": stale_graph,
        "expired_hits": dict(expired_hits),
        "unavailable_graph_queries": unavailable,
        "postgres_version_scope_revalidation_passed": isolation_passed,
        "graph_read_gate": (
            isolation_passed
            and exposed == 0
            and unavailable == 0
            and latency["graphiti"] <= 1000
            and recall["ordinary"]["graphiti"] >= recall["ordinary"]["current_pg"]
            and any(
                recall[cat]["graphiti"] > recall[cat]["current_pg"]
                for cat in ("relationship", "temporal")
            )
        ),
    }


async def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=16379)
    parser.add_argument("--model", default="gpt-4.1-mini")
    parser.add_argument("--repeats", type=int, default=2, choices=range(1, 6))
    args = parser.parse_args()
    database_url = os.environ["PORTER_TEST_DATABASE_URL"]
    parsed = urlsplit(database_url)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or not parsed.path.endswith(
        "_test"
    ):
        parser.error("Use a disposable loopback PostgreSQL database ending in _test")
    os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"
    from graphiti_core import Graphiti
    from graphiti_core.driver.falkordb_driver import FalkorDriver
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_client import OpenAIClient

    await run_migrations(database_url)
    pool = await create_pool(database_url)
    driver = FalkorDriver(host="127.0.0.1", port=args.port)
    client = OpenAIClient(config=LLMConfig(model=args.model, small_model=args.model))
    graph = Graphiti(graph_driver=driver, llm_client=client, max_coroutines=2)
    try:
        await graph.build_indices_and_constraints()
        report = await evaluate(PostgresMemoryRepository(HashEmbedder(), pool), graph, args)
        print(json.dumps(report, indent=2))
    finally:
        await graph.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
