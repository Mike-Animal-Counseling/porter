**English** | [中文](CHANGELOG.zh-CN.md)

# Changelog

All notable changes to Porter are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/).

## [Unreleased]

### Added

**New embedding challengers** — `porter/embeddings.py` gains
`HarrierEmbedder` (`microsoft/harrier-oss-v1-0.6b`, MIT, 1024 dim —
drop-in A/B vs BGE-M3/Qwen3, no pgvector migration) and
`EmbeddingGemmaEmbedder` (`google/embeddinggemma-300m`, 768 dim, Gemma
license — eval only pending license review + migration).
`PORTER_EMBEDDER=harrier|embeddinggemma`; both wired into
`scripts/eval_embeddings.py`
(`--embedders hash,qwen3,bge-m3,harrier,embeddinggemma`). Real benchmark
still needs a real machine (torch + model download).
**Project-scoped sync ("luggage follows you")** — `GET /api/v1/sync/delta`
and the MCP `sync_delta` tool accept `project=<name>`: the delta then
carries only that project's working memory (records tagged
`project:<name>`, normalized match) PLUS untagged/global records (the
personal domain follows the user everywhere). A harness in project X
never sees project Y's working memory. Convenience filter, NOT a
security boundary — OAuth tag scopes gate visibility first, synced
content stays `untrusted_data`. (SPECS.md §5.1)

**`project` convenience parameter** — `POST /api/v1/memories` and MCP
`memory_add` accept `project="<name>"`, mapped to a normalized
`project:<name>` scope tag (`porter/triage.py::project_tag`; empty names
rejected, overlong names fail tag validation loudly). Harnesses no
longer hand-roll the tag format.

**Panel domain views** — the Memories page gains a Personal / Project /
All segmented toggle (declared-domain filter; the ML-resolved domain is
a triage-time signal, not a stored field) plus `personal`/`project`
type labels in both languages.

**Aider importer** — `<project>/.aider.chat.history.md` chat history is
imported as per-session project-domain `episode` seeds (conversation
context is project working memory, SPECS §7.1.1). Best-effort; absence
is normal.

**Two memory domains (core product framing)** — triage now classifies
every write into the **personal** domain (your own memories —
preferences, health, finance, PII; sensitivity-gated, human-reviewed)
or the **project** domain (a project's ENTIRE working memory —
instructions, task state, decisions, codebase knowledge, pitfalls; the
luggage that follows you across harnesses). Rules: personal → queue;
project → auto-approve under a mandatory `project:<name>` scope
(`missing_project_scope` if absent); conflicts are per-project
(cross-project similarities are not conflicts). v1 `preference` type
maps to `personal`. (SPECS.md §7.1.1; README §How it works)

**ML triage model** (`porter/triage_model.py`) — open-source second
opinion over the rules: `heuristic` (rule wrapper), `zeroshot`
(`MoritzLaurer/mDeBERTa-v3-base-mnli-xnli`, multilingual zh/en,
~100–500ms CPU per write), `finetuned` (local 6-label
`microsoft/mdeberta-v3-base` head). `PORTER_TRIAGE_MODEL=auto` default
(finetuned → zeroshot → heuristic); model flags queue as
`ml_sensitive:<category>`. Rules stay the pre-filter and the fallback
(2s timeout → heuristic, audit-logged); E2EE memories never reach the
model; scores LRU-cached per content hash. (docs/DEVELOPER.md §5a)

**Triage training loop** (predict → correct → train → deploy) —
`scripts/triage_label_batch.py` (emit correctable JSONL:
`{domain, project, sensitivity, predicted_by}`), `scripts/finetune_triage.py`
(6-label fine-tune, per-label P/R/F1 on eval split, `--dry-run`),
deploy via `PORTER_TRIAGE_MODEL_PATH` (default `models/triage-v1`).
A few hundred hand-corrected labels beat zero-shot. (docs/DEVELOPER.md §5b)

**Instruction-file ingestion as project memories** — Cursor `.cursorrules`
+ `.cursor/rules/*.mdc` and new `WarpImporter` (`~/.warp/WARP.md` +
`<project>/WARP.md`) join the existing AGENTS.md / CLAUDE.md / GEMINI.md /
QWEN.md seeds as `project`-domain inbox seeds with `project:<dir>` tags
(documented as one source among many for the project domain).

**Automation layer (tiered approval, auto-merge, decay, auto-capture, digest)**
- `porter/triage.py`: tiered approval. Low-risk writes auto-approve (audited,
  reason `low_risk`); queue for human review on content-based sensitive
  detection (NFKC-normalized EN/ZH patterns — content, not just tags, so tag
  smuggling can't bypass), sensitive scope tags, similarity in [0.70, 0.92)
  (possible conflict), or confidence < 0.6. Similarity ≥ 0.92 keeps the
  dedup-candidates path. E2EE memories always queue, never content-scanned.
  `PORTER_APPROVAL_MODE=strict` restores the v1 "everything queues" behavior.
- Auto-merge: similarity ≥ 0.97 to an active memory folds in automatically
  (audited, `merged` status, history chain kept); tags are NOT unioned (T1).
- `porter/decay.py`: lazy confidence decay (×0.95 per 30 idle days, computed
  on read); below 0.2 → auto-archived (audited, restorable). Skips E2EE and
  pinned memories. New `POST /memories/{id}/pin` (admin).
- `memory_capture` MCP tool: extracts durable facts from a conversation
  transcript via `porter/llm.py` (`extract_facts` protocol; regex stub by
  default, `PORTER_LLM_EXTRACTOR` for real backends). Each fact goes through
  triage; ≤10 facts/call, each burns a rate-limit token (T5).
- `GET /api/v1/digest`: pending count + by-reason breakdown, recent
  auto-approvals, conflicts (pending contents admin-only, tag-scoped).
- Panel inbox shows triage reason chips; owner panel writes go through triage.
- Migration `007_automation.sql`: `last_retrieved_at` (idle clock for decay,
  backfilled from `updated_at`), `pinned`, `triage_reasons` columns.
  (`confidence` already existed since migration 001.)

**Panel "restrained" design pass (95% restrained, 5% soul)**
- Accent indigo `#6E6BF2` → warm gold `#D4A24E` (hover `#E5B45E`,
  muted `rgba(212,162,78,.14)`); dark surfaces shifted to warm charcoal;
  light theme gold adjusted for contrast (`#A87B2A`).
- Page headings (`h1`) → Chinese serif display stack
  (`"Noto Serif SC","Songti SC","STSong","SimSun",serif`); body keeps clean
  sans with CJK coverage; mono for IDs/cursors/hashes unchanged.
- Sync page: thin gold heartbeat strip (pure CSS/SVG animated dash, no JS
  lib); liveness tied to real adapter `last_seen` — animated when an
  adapter is online, calm flat line + "静默 / quiet" when none.
- Memories page: Table / Graph view toggle; Graph shows a mini
  force-directed constellation (vanilla canvas, nodes from the real memory
  list, links on shared tags; click node → existing slide-over detail via
  HTMX; labels drawn with `fillText` only, never `innerHTML`).
- Warmer microcopy (zh/en): inbox-zero "你的记忆星空很干净",
  sync empty-state, constellation hints.
- Instrument-grade polish: hairline card inset highlight, gold view-toggle
  active state, button press feedback, drawer easing curve, tabular-nums
  for timestamps/IDs, 2-line clamp on long memory snippets.

**Panel UI redesign (per `ui-redesign-spec.md`)**
- Dark-first design system with light mode via CSS-variable token swap:
  `porter/static/css/tokens.css` (exact dark/light token sets, single
  indigo-violet accent `#6E6BF2`, semantic status colors) +
  `porter/static/css/app.css` (layout shell, buttons/inputs/badges/cards/
  tables, per-page styles). No Tailwind, no CDN — offline-first preserved.
- New layout shell: 248px sidebar (8 nav items with icons, inbox count
  badge, sync status dot, collapsible to icon rail <1100px, drawer <720px)
  + sticky 56px header (breadcrumb, search, 中文/EN segmented control,
  theme toggle persisted in localStorage, default dark).
- Inbox rebuilt triage-first: cards with quiet metadata eyebrow,
  confidence bar, Approve / Edit & approve / Reject, Approve-all with
  confirm; HTMX actions swap cards out with toasts (no full reload).
- Memories: dense table + slide-over detail drawer (HTMX fragment) with
  metadata grid, confidence bar, history chain, inline edit form.
- Sync: stat header (online / lagging / median lag), adapter cards with
  status badges + lag meters, "How sync works" explainer, 15s HTMX
  auto-refresh.
- Clients/Shares as cards with copy buttons; revoke via `hx-confirm`
  (separate confirm pages removed).
- Audit: 1h/24h/7d/30d time-range filter, CSV export (`/panel/audit.csv`),
  dense table with action badges.
- Snapshots as cards with signature/encryption badges.
- Settings: grouped cards, danger zone (type-to-confirm delete-all
  memories + full instance reset, both audited), CSV export link.
- Toast system (`HX-Trigger` → bottom-right, auto-dismiss 4s); flash
  `?msg=` now renders as toast via `panel.js`.
- `porter/static/js/panel.js`: theme, toasts, copy-to-clipboard, drawer,
  sidebar, language switch (fetch POST + reload).
- Inline SVG `icon()` helper (24 icons, no emoji); `?msg=` banner removed.
- New i18n keys (zh+en, completeness test green): nav/shell, sync stats,
  danger zone, audit ranges, empty states.

**Second harness batch: 12 more harnesses (27 total)**
- 12 new MCP client configs in `clients/`: `devin-mcp_config.json`
  (`~/.config/devin/mcp_config.json`), `continue-config.json`
  (`~/.continue/config.json`, Agent mode only), `junie-mcp.json`
  (`~/.junie/mcp/mcp.json`), `droid-mcp.json` (`~/.factory/mcp.json`,
  `type: stdio`), `openhands-mcp.json` (`~/.openhands/mcp.json`),
  `tabnine-settings.json` (restart required), `zencoder-settings.json`
  (VS Code `zencoder.mcpServers` key), `neovim-mcphub-servers.json`
  (via mcphub.nvim, covers avante.nvim + codecompanion.nvim),
  `emacs-gptel-mcp.el` (via mcp.el), `antigravity-mcp_config.json`
  (HTTP `serverUrl`; Authorization-header support UNVERIFIED — flagged),
  `kiro-mcp.json`, `augment-mcp.json` (`transport: stdio`).
- 5 new seed importers (read-only, pending inbox, `seed`-tagged):
  `junie` (`~/.junie/guidelines.md` + `<project>/.junie/guidelines.md`),
  `droid` (`~/.factory/AGENTS.md` + project), `tabnine`
  (`<project>/TABNINE.md`), `devin` (`<project>/.devin/**/*.md`,
  confidence 0.3), `openhands` (`<project>/.agents/skills/*/SKILL.md`
  as `skill_ref`). New CLI flags `--junie-dir`, `--factory-dir`;
  `--harness all` now covers 14 importers.
- `NO_NATIVE_STORE` registry extended: continue, zencoder, neovim,
  emacs, antigravity, kiro, augment (MCP implant only, documented
  honestly). Windsurf config header notes the Devin Desktop rename
  (2026-06); both configs ship.
- `_GlobalMdSeedImporter` gains optional `project_file_name` for
  harnesses whose project-level file lives in a subdir (Junie).

**F9 auto-sync core / P1 (PRD §9.5, US7)**
- New `GET /api/v1/sync/delta?since=<int>&limit=<int,default 100,max 500>`
  (requires `memory.read`): incremental change feed
  `{changes: [{seq, memory_id, op, status, at, actor_client_id, memory}], cursor: <head seq>}`.
  Server-side tag-scope filtering; `memory` is null for delete tombstones
  and for still-pending creates seen by non-admin callers (inbox rule);
  non-admin callers see only currently-active memories + delete tombstones.
  Fetches are audited as `action='read'` with `meta={"via":"sync"}` and
  refresh the adapter's `last_seen` without advancing its cursor.
- New `POST /api/v1/sync/cursor {"cursor": int}` (any valid bearer, no
  special scope): explicit cursor ack; the adapter is identified by its
  own token's public `client_id` (or `"owner"` with auth disabled).
  Returns `{adapter_id, cursor, last_seen}`.
- Every mutation appends to a `change_log` feed
  (`create_pending`→`create`, `approve`→`approve`, `revise`→`revise`
  against the NEW record, `delete`→`delete`), carrying `actor_client_id`
  (new optional kwarg on all mutating repo methods, backward compatible).
  Migration `004_sync.sql`: `change_log` table, `memories.version INT`
  (optimistic-concurrency counter, +1 per revise), and
  `sync_cursors(device_id TEXT, cursor TEXT)` →
  `(adapter_id TEXT PK, cursor BIGINT NOT NULL DEFAULT 0, last_seen)`.
- US7 conflicts: new `update_with_version(memory_id, *, expected_version,
  content, actor_client_id)` — resolves the `superseded_by` chain to the
  current head, then: match/None → plain revise; mismatch →
  last-writer-wins still applies AND the overwritten content is preserved
  as a new pending inbox record
  (`source={"conflict_of": <new_id>, "client_id": ...}`, confidence
  preserved). `PATCH /api/v1/memories/{id}` accepts optional
  `expected_version` and returns 200
  `{memory, conflict, loser_pending_id}` on conflict (never 409);
  MCP `memory_update(id, content)` gains the same optional
  `expected_version` param reporting `conflict`/`loser_pending_id`.
- New MCP tool `sync_delta(since=0, limit=100)` with the same auth, scope,
  and visibility rules as the REST endpoint.
- New panel page `/panel/sync` (+ nav entry): adapter table from OAuth
  clients ∪ sync cursors — online dot (last_seen < 5 min), cursor, lag
  behind head, last seen. 12 new i18n keys in zh + en.
- `MemoryOut` now includes `version`.

**P2 E2EE encrypted memories (docs/DEVELOPER.md §6, PRD §9.5)**
- `POST /api/v1/memories` accepts either `content` (normal) or
  (`content_ciphertext` + `encryption{alg: "xchacha20-poly1305", key_id}`)
  — never both, never neither (422); ciphertext must be valid base64
  (1..40000 chars), `key_id` 1..64 chars.
- Migration `005_e2ee_share.sql`: `memories.encrypted BOOLEAN NOT NULL
  DEFAULT FALSE`, `memories.key_id TEXT`. Ciphertext is stored as-is in
  `content` with `embedding = NULL`: never embedded, never deduplicated,
  never searchable (search excludes encrypted rows explicitly in both
  backends), never logged — audit stays meta-only (no plaintext exists
  server-side to leak).
- `MemoryOut` (+ sync delta, export, snapshots) gains `encrypted: bool`
  and `key_id: str | None`; reads return the ciphertext in `content` with
  the flag. Encryption rides the revise/update_with_version history chain;
  snapshot export/import round-trips the flag. The server never decrypts.
- MCP `memory_add` accepts the same `content_ciphertext` + `encryption`
  params with identical validation; the result reports `encrypted: true`.
- Panel: "🔒 已加密 / Encrypted" badge on encrypted rows (memories list,
  inbox, detail page shows `key_id`); never attempts decryption.
- Sample clients' `decrypt_memory` helpers now accept the real server
  response shape (`content` + `encrypted` + `key_id`).

**P2 `memory.share` — owner-minted restricted share tokens (PRD §8)**
- New `POST /api/v1/shares` (requires `memory.admin`):
  `{name 1..64, tags [preset+custom], expires_in_hours 1..2160}`.
  Every tag validated (400 `invalid_scope` on `memory.admin`, `../`,
  empty, duplicates, unknown); minted scopes are ALWAYS exactly
  `[memory.read.<tag>...]` — nothing else, ever. Returns
  `{id, name, scopes, expires_at, token}`; the token is shown exactly
  once, never stored, never logged.
- Migration `005_e2ee_share.sql` also creates the `shares` table
  `(id UUID PK, name, scopes TEXT[], created_at, expires_at, revoked)`.
- Auth: share JWTs (`sub="share"`, jti = share id) resolve against
  `shares` after the tokens-table lookup misses — accepted iff found AND
  not revoked AND not expired — via a single helper shared by
  `verify_bearer_token` (MCP) and `_resolve_auth` (HTTP), so REST and MCP
  cannot diverge. `AuthContext(client_pk=None, client_id="share:<id>")`.
- New `GET /api/v1/shares` (tokens never listed) and
  `DELETE /api/v1/shares/{id}` (revokes; invalidates the revocation cache
  so the token dies on its next request, < 60s).
- Rate limiting (T5): `client_pk=None` identities (shares + dev owner)
  now key on `share:<verified client_id>` instead of sharing one
  `client:None` bucket — one share can no longer burn every other share's
  bucket (same fix applied to the MCP `mcp:share:` bucket).
- New panel page `/panel/shares` (+ nav entry): list (name, tags, expiry,
  status), create form (name + tag checkboxes + expiry hours, token shown
  once), revoke button. 20 new i18n keys in zh + en.
- Share tokens work on `/sync/delta` (tag-filtered) and MCP search;
  writes, client management, share minting, and PATCH are 403.

**Panel i18n (Chinese/English)**
- New `porter/i18n.py`: every user-facing panel string extracted into
  `STRINGS = {"zh": {...}, "en": {...}}` + `t(key, lang)` helper (zh
  fallback so a missing key can never 500 the panel; a test fails on any
  key missing from either language).
- Language setting on the Settings page (中文 / English dropdown);
  persisted to `~/.porter/settings.json` (0600, atomic replace;
  `PORTER_SETTINGS_PATH` override for tests), survives restarts, Chinese
  default. `<html lang>` set correctly (`zh-CN` / `en`).
- Also fixed a stale panel note: snapshots ARE Ed25519-signed since the
  v0.1.0 red-team pass (the panel still claimed they were unsigned).

12-harness extension (not yet tagged — will ride the next release;
v0.1.0 stays untouched below). Choice rationale: v0.1.0 is already
tagged in git; the harness extension is additive (new importers +
config templates, no breaking changes), so it accumulates here until
the next version cut.

### Fixed

- **Auto-merge is now project-scoped**: a project-domain write no longer
  auto-merges into a near-identical memory from a DIFFERENT project
  (the project scope would have been silently lost). Cross-project
  near-duplicates queue as `duplicate_candidate` for human review
  instead; same-project auto-merge behavior is unchanged.
- MCP `memory_add` and `memory_capture` now accept `type="personal"` /
  `type="project"` (previously 422'd despite the schema allowing them);
  panel type labels cover both domains in English and Chinese.

### Added

**Harness importers (PRD §8 F7)**
- Claude Code: `~/.claude/projects/<encoded-cwd>/memory/*.md` +
  per-project `MEMORY.md` + `~/.claude/CLAUDE.md` seed preference
  (confidence 0.3, tagged `seed`); `autoMemoryDirectory` override
  honored when parseable; project-dir decoding is best-effort
  (raw encoded name always kept in `source.project_dir`).
- Windsurf: `~/.codeium/windsurf/memories/*.md` (best-effort; absence
  is normal and documented).
- Cline: `<project>/memory-bank/*.md` (needs `--project-root`).
- Roo Code: `<project>/memory-bank/*.md` + `.roo/rules/*.md`
  (rules imported as `skill_ref`, tagged `rule`).
- Gemini CLI: `~/.gemini/GEMINI.md` (+ project `GEMINI.md`) as
  low-confidence seed preferences (0.4).
- Qwen Code: `~/.qwen/QWEN.md` (+ project) — same treatment.
- All scanners skip symlinks (a symlink inside a harness dir can no
  longer pull outside files into the import).
- Honest registry `NO_NATIVE_STORE`: OpenCode / Amp / Kilo Code /
  Kimi CLI / Copilot CLI / Aider have no portable memory store —
  MCP implant only, no parsers invented.

**MCP client configs (`clients/`, 15 total)**
- New: `claude-code-mcp.json`, `opencode.json` (top-level `mcp` key),
  `gemini-settings.json`, `qwen-settings.json`,
  `amp-settings.json` (`amp.mcpServers` key),
  `windsurf-mcp_config.json`, `roo-mcp.json`, `cline-mcp.json`,
  `kilocode-mcp.json`, `kimi-mcp.json`,
  `copilot-cli-mcp.json` (`type: stdio`; config path UNVERIFIED —
  confirm on a real install), `aider-conf.yml` (`mcp-servers:` key).
- Every file carries a header comment: target path(s), how to apply,
  and UNVERIFIED caveats where they exist.

### Docs
- `DEPLOY.md`: 15-harness read/implant matrix (§3), per-harness
  connect table (§5).
- `README.md`: harness support list updated.

## [0.1.0] — 2026-10-11

First public release. Full P0 scope per PRD v2.0
(`~/workspace/your_files/porter-prd-v20.md`): a self-hosted,
user-owned memory + state layer for coding-agent harnesses.

### Added

**Memory core (F1, PRD §8)**
- Memory CRUD: `POST /memories` (writes land in a pending-review inbox,
  HTTP 202), `GET /memories` (filter by status/tag/type),
  `POST /memories/search` (pgvector semantic search, top-k, tag filter),
  `POST /memories/:id/approve`, `DELETE /memories/:id` (true hard delete:
  row + vector removed, audit keeps id only).
- Duplicate detection on write (similarity > 0.92 returns
  `duplicate_candidates` instead of silently piling up).
- Pending memories are invisible to search until approved.
- Every search result is marked `untrusted_data: true` (PRD §5:
  retrieved memory is untrusted input to downstream agents).
- Append-only audit log (`audit_log` table protected by a DB trigger
  forbidding UPDATE/DELETE; deletion events keep metadata, never content).

**OAuth-style authorization (F2, PRD §10)**
- Client registration (`POST /api/v1/clients`; secret shown once,
  argon2-hashed at rest).
- Standard authorization-code flow: `GET/POST /oauth/authorize` with a
  consent screen (per-scope Chinese explanations, per-scope checkboxes,
  deny → `access_denied`), one-time 10-minute codes bound to the client.
- `POST /oauth/token` (authorization_code + refresh_token grants);
  access tokens are HS256 JWTs (1h), refresh tokens are opaque (30d)
  with rotation.
- Refresh-token reuse detection: reuse after the grace window revokes
  ALL of the client's tokens and emits a `security` audit event (T2).
- Immediate revocation (`POST /oauth/revoke`, single token or
  `revoke_all`); revoked access tokens fail within 60s via jti blacklist.
- Scope catalog: `memory.read`, `memory.read.<tag>` (10 preset tags,
  user-extensible), `memory.write`, `memory.admin`, `state.export`,
  `state.import`. **Enforced server-side** — tag scopes filter at the
  repository layer, fail-closed; client-supplied tags can never widen
  scope.
- Rate limiting: 60 req/min per client, in-memory token bucket (T5).

**MCP server (F3, PRD §8)**
- Tools: `memory_search`, `memory_add` (→ pending inbox),
  `memory_update`; tool descriptions warn models that returned content
  is untrusted data (T3).
- Transports: **stdio** (local CLIs) and **Streamable HTTP** (`/mcp`).
- Bearer-token auth + scope enforcement on every tool call, wired to
  the same machinery as the REST API.
- Paste-ready client configs: `clients/codex-config.toml`,
  `clients/cursor-mcp.json`, `clients/zed-settings.json`.

**Owner panel (F4, PRD §8, §17)**
- Server-rendered (HTMX), Chinese UI, localhost-only.
- Memories (search: semantic/full-text, filters), pending inbox
  (approve / edit+approve / reject, duplicate hints), clients
  (scopes, last active, one-click revoke), audit stream, snapshots,
  settings (custom scope tags, export-all, backup notes).

**Harness memory importer (F7)**
- `porter import` CLI: reads a coding harness's local memory files
  (Codex `~/.codex/memories/` first) into the pending-review inbox with
  source attribution. Read-only against harness files — Porter never
  modifies the harness's own store.

**Snapshots (F6 v1, PRD §9)**
- Versioned, Ed25519-signed snapshot format: the instance keypair is
  generated on first use (private key 0o600 under
  `<snapshot_dir>/.keys/`); the signature block travels inside the file
  so another machine can verify it. `inspect`/`apply` fail closed on
  tampering; legacy unsigned files are accepted with an explicit
  warning. Trust-on-first-use: any valid key verifies (forged-but-valid
  snapshots still land in the pending inbox, never active).
- Optional AES-256-GCM encryption with user password.
- `verify` (dry-run listing) and `apply` (conflicts → pending queue,
  never silent overwrite). Panel UI for create/list.

**Deployment (F5)**
- `docker compose up --build` brings up api + Postgres 16/pgvector +
  embedder; migrations run automatically on startup.
- `DEPLOY.md`: full production deployment guide.

### Security fixes found during phased reviews (all fixed pre-release)

- W1: `InsufficientScope` mis-mapped to 501 (would have broken all
  scope denials in W2); asyncpg pool bound to a throwaway event loop at
  import (would have failed every query under uvicorn); validation
  errors leaked FastAPI's default shape instead of the unified
  `{error:{code,message}}`; no input size limits (1 MB content
  accepted); `superseded_by` FK missing `ON DELETE SET NULL` (broke
  true delete, US5); SELECT-then-UPDATE TOCTOU in approve.
- W2 build: revocation cache keyed by `str(jti)` but invalidated with
  `UUID` object → revoked tokens stayed valid; tag-filter fail-open
  (empty scope/tag intersection returned everything).
- W2 review (P0): open first-client registration + consent without owner
  auth + `0.0.0.0` bind = network neighbor could self-register and
  self-approve `memory.admin` during setup → fixed with one-time setup
  token (`X-Porter-Setup-Token`) and localhost-only default binding.
- W2 review (P1): rate-limit keyed on unverified JWT `sub` (bypass with
  forged tokens); bare `except Exception` in revoke path; argon2
  skipped for missing clients (timing side-channel); silent refresh
  grace path (now emits `security` audit event).
- Release red-team (P1): `POST /oauth/authorize` had no CSRF protection —
  a malicious website could forge a cross-origin approval POST and grant
  scopes to an attacker's registered client (scope escalation). Fixed
  with a shared same-origin check (`porter/origin.py`, also used by the
  panel); permissive CORS (`*`/`*`/`*`) removed entirely.
- Release red-team (P1): implemented Ed25519 snapshot signing (PRD §9.2;
  was deferred with docs overclaiming it). Instance keypair, 0o600 key
  file, signature embedded in the file (verifiable cross-machine);
  inspect/apply fail closed on tampering; legacy unsigned files accepted
  with an explicit warning. Trust-on-first-use: any valid key verifies
  (forged-but-valid snapshots still land in the pending inbox).
- Release red-team (P2): importer skips files > 8 MB (DoS guard);
  escaped status interpolation in panel templates (defense in depth).

### Changed
- `AUTH_ENABLED` defaults to `1` (Bearer enforcement on).
- API binds `127.0.0.1` by default; compose publishes `127.0.0.1:8000`.

### Known limitations (v0.1.0)
- Embedder defaults to a hash stub on small machines; set
  `EMBEDDER_KIND=sentence-transformer` (BGE-M3, ~2.3 GB, needs 2–4 GB
  RAM) or `openai` (sends memory text to OpenAI — explicit opt-in).
- Migration SQL + pgvector paths reviewed but not executed against live
  Postgres in CI (no Docker in build env) — see SECURITY.md.
- Rate limiter is per-process; multi-worker needs Redis (P2).
- Consent screen has no login in v1 — network access is the trust
  boundary (bind localhost / trusted network).
- `/sync/delta` returns 501 (P1 auto-sync stub, PRD §9.5).

## Deferred to P1/P2 (per PRD)
- P1: hub-and-spoke auto-sync (delta cursor, adapters), memory quality
  (auto-extract, merge suggestions), `porter-export` importers for
  ChatGPT/Claude dumps.
- P2: end-to-end-encrypted sync, multi-user, memory sharing
  (`memory.share`), open-spec standardization.
