**English** | [中文](GRAPHITI_SHADOW.zh-CN.md)

# Graphiti shadow evaluation

The evaluation ran Graphiti 0.31.2 with standalone FalkorDB, the FalkorDB
Python driver 1.8.2 and `gpt-4.1-mini`. The corpus contains seven synthetic,
approved episodes across two projects. Nine questions cover ordinary lookup,
relationships and temporal recall. Each ran twice, serially. Postgres used
the deterministic hash embedder. The previous ranker and current project
ranker both read the same small PostgreSQL fixture.

| Slice | Previous Postgres Recall@5 | Current Postgres Recall@5 | Graphiti Recall@5 |
|---|---:|---:|---:|
| Ordinary | 1.0000 | 1.0000 | 0.6667 |
| Relationship | 0.7222 | 1.0000 | 1.0000 |
| Temporal | 1.0000 | 1.0000 | 1.0000 |
| Retrieval p95 | 4.7 ms | 6.9 ms | 233.1 ms |

Graphiti returned no cross-project candidates. The deliberately retained
expired source appeared in 18 raw graph candidate lists and 16 legacy
Postgres results. Current Postgres and revalidated Graphiti returned zero
expired hits. PostgreSQL also rejected an ID from the other project, a
superseded ID, and a candidate with the wrong version. Indexing took 28.45
seconds. Graphiti reported 30,529 LLM input tokens and 947 output tokens.
Embedding billing and production-scale concurrency were outside this run.

Keep graph reads disabled. Relationship and temporal recall tied Postgres
and ordinary recall fell. The corpus is small and ranks at most five results.
These measurements cover the synthetic fixtures and the standalone server.
FalkorDB Lite still needs platform, concurrent writes and recovery checks.

## Repeat the evaluation

Use a disposable local Postgres database ending in `_test` and a disposable
FalkorDB instance bound to loopback. The runner writes synthetic fixtures and
new graph prefixes into these services. Remove the disposable services after
the run. Install the Graphiti integration outside the Porter server environment.

```console
python -m pip install "graphiti-core[falkordb]==0.31.2"
python -m scripts.eval_graphiti --port 16379
```

Set `PORTER_TEST_DATABASE_URL` and `OPENAI_API_KEY` in the evaluation process.
Only the script's synthetic fixtures are sent to Graphiti's configured model.
The [runner](../scripts/eval_graphiti.py) rebuilds its index from PostgreSQL
records, uses separate graph groups and checks returned record IDs and
versions against PostgreSQL before scoring. Porter serves reads from Postgres
throughout the run. Stopping the evaluator or its FalkorDB instance leaves
normal recall and the existing Wiki tools available.
