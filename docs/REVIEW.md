**English** | [中文](REVIEW.zh-CN.md)

# Porter review and repairs

Original baseline: `1cbc3b9`; integrated upstream through `4000dd5`.
Latest local verification: 2026-10-20. Earlier repairs are committed as `93bf45e`.

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

## Earlier verification baseline and limits

The earlier complete local suite passed **589 tests with no skips**, with real Postgres
and the isolated Docker services configured. The original Windows baseline was
363 passes and 11 failures. Ruff, formatting, pre-commit hooks, dependency
consistency and patch whitespace checks passed.

All fourteen migrations ran twice on a fresh Postgres/pgvector database. Real
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
regex stub by default; optional local Ollama is implemented and always queues
facts for review (see [extraction](EXTRACTION.md)). OpenAI extraction remains unimplemented. Optional ML triage
and additional embedding models were not evaluated. Windows ACLs were not
independently audited. Remote CI status is reported on the review pull request.

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
even when dimensions match. Changes are tracked on `fix/porter-review-hardening`; the review pull request records remote CI status.

## Upstream switching integration (2026-10-19)

The review integrates seven newer upstream commits rather than replacing them.
It preserves the upstream key-envelope/workspace migrations and adopts the old
local migration names without rerunning SQL or clearing existing databases.

- Handoff, contradiction and writeback results filter by the caller's scopes.
  Writeback defaults to preview; actual server-side writes, memory merge/resolve
  and key-envelope management require `memory.admin`.
- Workspace writes require editor/owner membership; personal memories cannot
  enter workspaces. Workspace creation requires a registered OAuth client.
- PostgreSQL persists archive provenance and supersession links. Merge and
  resolution run atomically with row locks; an injected mid-merge failure
  leaves source records, record counts and sync cursors unchanged.
- Key-envelope claiming uses valid PostgreSQL SQL and remains atomic. PyNaCl
  is installed in the API image. Windows writeback preserves literal paths,
  surrounding CRLF content and originals when replacement fails.

Fresh-database, legacy-migration, workspace-role, sealed-key roundtrip and
interrupted-merge integration checks pass. Docker acceptance also covers the
new endpoints and all eleven MCP tools, including container recreation.
The workspace onboarding UI and per-device delegated key access are not a
completed multi-user product; key management is deliberately owner-only.

## Owner handoff and durable feedback

The panel now provides `/panel/handoff`: project suggestions, bilingual preview,
copy and text download. Preview does not record a switch; downloading records
the handoff. Pending records and other projects stay out of the briefing, HTML
is escaped, and the owner/CSRF gates apply to every route. Responses are not cached.

Lifecycle retrieval/contradiction helpers now persist confidence on both
backends, publish sync changes, and avoid lost PostgreSQL updates. They remain
explicit helpers; ordinary searches keep the existing idle-clock behavior.
Legacy `preference` records also obey the workspace personal-memory boundary.

The optional Ollama extractor uses bounded structured output off the API event
loop. All its facts await human approval; model errors cannot silently switch
backends. A local synthetic test exercised real model capture through MCP.
The tiny model's greeting false positive is recorded rather than hidden by
the protocol tests. Production extraction is disabled by default.

## Acceptance, access and interface review (2026-10-20)

This pass adds reproducible user acceptance instructions and corrects remaining
access boundaries found while reviewing the real write and retrieval paths:

- Graph neighbor names, edges and originating UUIDs require a readable, active,
  unencrypted source memory; unattributed/private/pending/archived edges are
  excluded rather than treating entity names as harmless metadata.
- Write-only or tag-restricted clients cannot use duplicate probes to reveal
  private memory IDs or refresh private confidence values. Similarity candidates
  and returned duplicate IDs use the caller's read scopes.
- Automatic merge requires a readable target with the same memory type and
  complete tag set, preserving domains and workspace boundaries.
- MCP updates require read visibility of the resolved active head, including
  requests using a stale revision ID.
- REST list filtering accepts the shared memory taxonomy, including `personal`
  and `project`, instead of rejecting these current types.
- PostgreSQL pending/active duplicate queries return full record metadata,
  allowing production visibility checks instead of filtering UUID-only stubs.
- Snapshot validation checks the entire batch's personal/workspace boundary
  before import. PostgreSQL imports and their audit entry run in one transaction;
  an injected mid-import failure must not leave a partial batch or sync changes.

OAuth write consent now describes tiered and strict approval accurately. New
regression coverage is in [acceptance security tests](../tests/test_acceptance_security.py),
including a real PostgreSQL duplicate/filter and failed-import rollback case.

Dependency review also found issues in the pinned model/runtime dependencies.
Constraints were updated for `filelock` (4.2.3), `setuptools` (84.0.0) and
`certifi` (2026.10.19). The Docker build fetches only PyTorch from the CPU index
without its dependencies, then resolves ordinary dependencies under both
constraint files; `pip check` and a CPU-only assertion run during the build.
Both API and real-model images rebuilt successfully with these constraints.
After refreshing both services while retaining their data/cache volumes, the
real BGE-M3 deployed OAuth/HTTP/stdio/snapshot/revocation acceptance passed.
The [post-update audit](DEPENDENCY_AUDIT.md) queried 94 exact package/version
combinations without matching a known OSV advisory. This does not cover base
image OS packages or guarantee that the application has no vulnerabilities.

Panel changes preserve the existing style while adding accessible control and
navigation names, drawer focus handling, narrow-screen navigation state, phone
targets, localized failure feedback, corrected contrast tokens, reduced motion
and bounded canvas listeners. The dedicated HTTP/HTML/CSS suite passed **42
cases**. JavaScript interactions and actual rendered layout remain unverified:
browser connection attempts returned “No browser is available.” See
[UI review](UI_REVIEW.md) for source evidence and the human checks; this is not
a WCAG certification or visual acceptance.

The English/Chinese README, deployment and setup guides now distinguish
templates from verified clients, explain plaintext/encrypted memory boundaries,
and prioritize limited OAuth scopes and HTTP MCP. Missing promotional-media
claims were removed. [Acceptance criteria](ACCEPTANCE.md) separate completed
automation from the user's two-tool and browser sign-off. The final complete
suite passed **650 tests, with no skips**, including real PostgreSQL, the
deployed HTTP/stdio flow, container recreation and the isolated local Ollama
integration. The earlier 589-test result and CI run at `c3c5fdf` are historical
evidence. [PR #1](https://github.com/Mike-Animal-Counseling/porter/pull/1)
records the final head and its six remote CI jobs; check that exact head when
signing off a candidate.
