**English** | [中文](REVIEW.zh-CN.md)

# Porter review and repairs

Baseline: `1cbc3b9`. Latest local verification: 2026-10-19.

All thirteen initial upload commits exist. The last is
[`ada83e7`](https://github.com/Mike-Animal-Counseling/porter/commit/ada83e7),
`Porter: initial push (13/13)`. These numbers describe upload batches;
the last batch contains a UI design specification and does not establish
functional completion.

## Repairs

- Correct sync pagination, filtered-page acknowledgement, scoped deletion
  visibility and transaction commit ordering so clients do not miss changes.
- Add missing domain/audit migrations, decode asyncpg JSON values, serialize
  concurrent migrations and repair real startup failures without clearing data.
- Preserve revision metadata and use binary snapshot/key writes on Windows.
  Reject malformed snapshots, key path traversal and accidental key overwrites.
- Persist Docker settings, snapshots and stable credentials. Gate container
  browser access with an owner session and enforce exact-origin CSRF checks.
- Share the same database, signing secret and embedding service across stdio
  clients. Move model inference off the API event loop and avoid caching outages.
- Fix nested Docker requirements, default to CPU PyTorch, constrain tested
  model dependencies and exclude runtime secrets/data from the build context.
- Remove memory plaintext from automatic-merge audit records.
- Standardize packaging, lightweight development dependencies, Ruff,
  pre-commit, CI permissions, dependency updates and bilingual instructions.
  See [repository standards](REPOSITORY_STANDARDS.md).

## Verification and limits

The complete local suite passed **447 tests with no skips**, with real Postgres
and the isolated Docker services configured. The original Windows baseline was
363 passes and 11 failures. Ruff, formatting, pre-commit hooks, dependency
consistency and patch whitespace checks passed.

All eleven migrations ran twice on a fresh Postgres/pgvector database. Real
OAuth consent, independent HTTP/stdio MCP clients, encrypted snapshot restore,
revocation and API recreation persistence passed. The installed wheel includes
the migrations and panel resources; source and wheel distributions build.

BGE-M3 ran in a separate CPU container/database and passed the same API/MCP
flow. A fixture of 14 memories and 12 bilingual queries yielded R@1=58.3%,
R@3=91.7%, R@5=100%. Duplicate detection at 0.92 had precision 83.3% and
recall 33.3%, including one opposite-meaning pair flagged as similar. At the
existing 0.97 automatic-merge threshold, none of the ten distinct pairs would
merge. This small evaluation is not a general benchmark; see the
[recorded results](embedding-eval.2026-10-18.json).

Visual browser acceptance and per-harness conversations remain unverified.
The installed Codex CLI 0.160.0 parsed both stdio and HTTP templates;
that check does not execute tools. Templates alone do not capture conversations or inject context;
adapters must consume the MCP tools and sync feed. The fact extractor is the
regex stub; Ollama/OpenAI extraction remains unimplemented. Optional ML triage
and additional embedding models were not evaluated. Windows ACLs were not
independently audited. Remote CI has not run for these unpushed changes.

## Local preview

On 2026-10-19 the review also fixed host-based access to the local panel,
malformed-origin failures, inconsistent auth flags and silent storage fallback.
Inbox duplicate hints now reuse stored vectors without model requests or
retrieval/decay side effects. Postgres similarity probes return full records,
preserving the project tags needed by triage. The updated real BGE-M3 flow
passed OAuth, both MCP transports, snapshots and revocation. Prefer the new
Codex HTTP template, which requires only a scoped token. Bundled launches
disable proxy-header trust; TLS proxies use an explicit public origin.

The real-model panel is at `http://127.0.0.1:18010/panel`; unlock using
`PORTER_SETUP_TOKEN` in the private `.env.review`. It uses database `porter_bge`
and requires the retained review database and model containers.

For repeatable hash-based infrastructure acceptance, run
`python scripts/verify_deployment.py --keep-running`; its panel uses port 18000.
Switching embedding models requires a separate database or regenerated vectors,
even when dimensions match. Changes are tracked on the local review branch; they have not been pushed.
