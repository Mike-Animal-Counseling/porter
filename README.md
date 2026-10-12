**English** | [中文](README.zh-CN.md)

# Porter — Portable AI Memory & State Layer

> "Memory and state belong to the user and travel with them; apps are merely authorized guests that come later to read."
> Full product definition in the PRD: `~/workspace/your_files/porter-prd-v20.md`

**Current version: v0.1.0** · [CHANGELOG](CHANGELOG.md) · [Security Policy](SECURITY.md) · [Deployment Guide](DEPLOY.md)

W1: FastAPI memory CRUD + pgvector semantic search + pending-review queue + dedup + append-only audit.
W2: OAuth 2.0 authorization-code flow + scope enforcement + rate limiting + revocation blacklist.
F3: MCP server (stdio + Streamable HTTP, `memory_search`/`memory_add`/`memory_update`).
F4: Owner panel (memories / pending queue / clients / audit / snapshots / settings).
E2E: harness memory importer (Codex / Claude Code / Windsurf / Cline / Roo / Gemini / Qwen / Cursor / Junie / Droid / Tabnine / Devin / OpenHands; Zed/OpenCode/Amp/Kilo/Kimi/Copilot CLI/Aider/Continue/Zencoder/Neovim/Emacs/Antigravity/Kiro/Augment honestly no import) + US1/US3 acceptance tests.
Snapshots: Ed25519 signing (PRD §9.2) + optional AES-256-GCM encryption.

> **Self-host and connect 27 coding harnesses: see [DEPLOY.md](DEPLOY.md)**

## Architecture

```
porter/
├── porter/                 # Python package
│   ├── app.py              # FastAPI factory + all REST/OAuth routes
│   ├── auth.py             # Auth middleware: rate limit -> Bearer parse -> jti blacklist -> scope dependency
│   ├── origin.py           # Shared origin check: localhost gate + same-origin CSRF guard (shared by panel/OAuth)
│   ├── oauth.py            # OAuth logic: scope catalog/JWT/secret/consent screen/token issuance/rotation/revocation
│   ├── oauth_store.py      # OAuthStore: clients/auth_codes/tokens (Postgres/InMemory)
│   ├── ratelimit.py        # T5: in-memory token bucket (default 60 req/min/client)
│   ├── embeddings.py       # Embedder interface: hash(stub)/sentence-transformer/http/openai
│   ├── repositories.py     # MemoryRepository: Postgres(pgvector) / InMemory(numpy)
│   ├── mcp_server.py       # MCP server: tool definitions + scope auth (shared by stdio/HTTP)
│   ├── mcp_stdio.py        # stdio transport entry point (local CLIs)
│   ├── panel.py            # Owner panel (HTMX server-rendered, localhost only, English/Chinese bilingual)
│   ├── i18n.py             # Panel i18n: zh/en string tables + persisted language preference
│   ├── importers.py        # Harness memory importers (14, incl. honest Zed no-op; read-only)
│   ├── import_cli.py       # `porter import` CLI
│   ├── snapshots.py        # Snapshots: Ed25519 signing + optional AES-256-GCM encryption
│   ├── exporter.py         # porter-export/v1 one-click export
│   ├── schemas.py          # Pydantic models (PRD §11)
│   ├── errors.py           # Unified error shape {error: {code, message}}
│   ├── db.py               # asyncpg connection pool + migration runner (incl. no-transaction support)
│   ├── config.py           # Environment-variable config
│   ├── main.py             # API entry point
│   └── embed_server.py     # Standalone embedding service (docker-compose `embedder`)
├── migrations/
│   ├── 001_init.sql        # memories/clients/grants/tokens/audit_log(append-only trigger)/
│   │                       #   snapshots/sync_cursors
│   ├── 002_oauth.sql       # auth_codes table, tokens.token_hash, audit_action gains grant/refresh/security
│   └── 003_custom_tags.sql # User-defined custom scope_tags
├── api/Dockerfile          # ARG REQ_FILE switches api/embedder deps
├── docker-compose.yml      # api + db(pgvector/pg16) + embedder, one-command startup
├── clients/                # MCP client config templates (27 harnesses)
├── tests/                  # pytest: 156 tests (CRUD/search/dedup/audit/OAuth/MCP/panel/snapshot signing/i18n)
└── scripts/
    ├── demo.py             # W1 demo (owner context, no Docker needed)
    ├── demo_oauth.py       # W2 demo: register -> consent -> token -> read/write -> revoke -> rotate (no Docker)
    └── demo_e2e.py         # E2E demo
```

## Quick start

### Option A: Docker (production shape, Postgres + pgvector + BGE-M3)

```bash
cd ~/workspace/porter
cp .env.example .env
# Production: you MUST set PORTER_JWT_SECRET (random, 32+ bytes)
docker compose up --build
# API: http://localhost:8000   Docs: http://localhost:8000/docs
```

On first boot, the API's lifespan runs migrations automatically, and the embedder downloads BGE-M3 (~2.3GB,
cached in the `porter-models` volume, one-time). `scripts/migrate.py` remains for manual runs.

### Option B: Local dev (no Docker, in-memory + stub embedding)

```bash
cd ~/workspace/porter
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python scripts/demo.py        # W1 end-to-end demo
.venv/bin/python scripts/demo_oauth.py  # W2 OAuth full-flow demo
.venv/bin/python -m pytest tests/ -q    # run tests
PORTER_REPO=memory .venv/bin/python -m porter.main   # start the API (:8000)
```

## OAuth flow (PRD §10)

```
1. POST /api/v1/clients            Register client -> client_id + client_secret (secret shown only once)
2. GET  /oauth/authorize            Consent screen: per-scope explanations + per-item checkboxes
3. POST /oauth/authorize            Approve -> 302 redirect_uri?code=... (10 min TTL, single-use)
4. POST /oauth/token                code + secret -> access_token(1h, JWT) + refresh_token(30d)
5. API calls carry Authorization: Bearer <access_token>
6. POST /oauth/token                grant_type=refresh_token -> rotation (old refresh invalidated; reuse = theft -> revoke all)
7. POST /oauth/revoke               Revoke one token, or revoke_all=true to revoke all (effective within 60s)
```

```bash
# 1. Register (first time needs a one-time setup token; afterwards needs memory.admin)
#    The setup token is in the service startup log (auto-generated when PORTER_SETUP_TOKEN is unset),
#    or use your own PORTER_SETUP_TOKEN.
curl -s -X POST localhost:8000/api/v1/clients -H 'Content-Type: application/json' \
  -H "X-Porter-Setup-Token: <setup-token-from-startup-log>" -d '{
  "name": "my-cli", "redirect_uris": ["http://localhost:9999/callback"]
}'
# -> {"client_id": "porter_...", "client_secret": "...", ...}  # secret appears only this once!

# 2. Open the consent screen in a browser (human approval)
open "http://localhost:8000/oauth/authorize?client_id=porter_...&redirect_uri=http://localhost:9999/callback&scope=memory.read.dietary%20memory.write&state=xyz"

# 3. Exchange the code for tokens
curl -s -X POST localhost:8000/oauth/token \
  --data-urlencode "grant_type=authorization_code" \
  --data-urlencode "code=<from-redirect>" \
  --data-urlencode "redirect_uri=http://localhost:9999/callback" \
  --data-urlencode "client_id=porter_..." \
  --data-urlencode "client_secret=<secret>"
# -> {"access_token": "eyJ...", "token_type": "Bearer", "expires_in": 3600,
#     "refresh_token": "porter_rt_...", "scope": "memory.read.dietary memory.write"}

# 4. Call APIs with the token (scopes enforced server-side)
curl -s -X POST localhost:8000/api/v1/memories/search \
  -H "Authorization: Bearer eyJ..." -H 'Content-Type: application/json' \
  -d '{"query": "what foods should I avoid?", "top_k": 5}'

# 5. Refresh / revoke
curl -s -X POST localhost:8000/oauth/token \
  --data-urlencode "grant_type=refresh_token" \
  --data-urlencode "refresh_token=porter_rt_..." \
  --data-urlencode "client_id=porter_..." --data-urlencode "client_secret=<secret>"
curl -s -X POST localhost:8000/oauth/revoke \
  --data-urlencode "token=eyJ..." \
  --data-urlencode "client_id=porter_..." --data-urlencode "client_secret=<secret>"
# After revocation, API calls with that token -> 401 {"error": {"code": "token_revoked", ...}}
```

### Scope catalog (PRD §10.3)

| scope | description |
|---|---|
| `memory.read` | Read all memories |
| `memory.read.<tag>` | Read memories with a single tag (10 preset tags: `dietary` dietary, `coding_style` coding style, `workflow` workflow preferences, `work` work info, `travel` travel, `health` health, `finance` finance, `social` social relationships, `device` device environment, `general` general) |
| `memory.write` | Write (lands in the pending-review queue, needs approval) |
| `memory.admin` | Approve/delete/audit/client management (not granted by default; red warning on the consent screen) |
| `state.export` / `state.import` | Snapshot export/import (P1) |

Unknown scope → `400 {"error": {"code": "invalid_scope", ...}}`.

## MCP Server (PRD §8 F3)

Porter exposes memory as MCP tools for harnesses like Claude Code / Codex / OpenCode / Cursor:

| tool | description | required scope |
|---|---|---|
| `memory_search(query, top_k=5, tags=[])` | Semantic search, returns memories (each marked `untrusted_data: true`) | `memory.read` or `memory.read.<tag>` (tag filtering enforced server-side) |
| `memory_add(content, type="fact", scope_tags=[])` | Writes to the **pending-review queue** (202 semantics: returns id + `pending_review` + dedup candidates) | `memory.write` |
| `memory_update(id, content)` | Updates an **active** memory's content (re-embeds); history chain preserved: the old version is archived and linked to the **new id** via `superseded_by` (same as REST PATCH) | `memory.write` |

Two transports:

```bash
# 1) stdio (preferred for local CLIs): token via the PORTER_TOKEN env var
PORTER_TOKEN=eyJ... PORTER_REPO=postgres DATABASE_URL=... \
  python -m porter.mcp_stdio

# 2) Streamable HTTP (remote harnesses): mounted at the API's /mcp
#    POST http://localhost:8000/mcp, token via the Authorization header
```

Auth is the same machinery as REST: Bearer token signature verification + revocation checks + server-side scope enforcement; failures return as MCP tool errors (with a `porter:<code>:` prefix), never silently. The `/mcp` path requires a valid token at the gateway layer (anonymous callers can't even initialize). All retrieved memory content is treated as **untrusted data** (T3), stated in the tool descriptions.

Client config templates live in [`clients/`](clients/) (27: codex/cursor/zed/claude-code/opencode/gemini/qwen/amp/windsurf/roo/cline/kilocode/kimi/copilot-cli/aider/devin/continue/junie/droid/openhands/tabnine/zencoder/neovim/emacs/antigravity/kiro/augment; `{{PORTER_TOKEN}}` placeholder; target paths are in each file's header comment and DEPLOY.md §5). Note the key-name differences across harnesses: OpenCode uses top-level `mcp`, Amp uses `amp.mcpServers`, Aider uses YAML `mcp-servers:`.

## For developers (integrating third-party apps)

Want to plug Porter into your own AI app? Full integration guide: **[`docs/DEVELOPER.md`](docs/DEVELOPER.md)** (US6) — OAuth authorization-code flow (with curl examples), the scope catalog and tag-filter semantics, MCP tools, P1 sync polling, P2 end-to-end encryption, the error code table, security notes for integrators.

Ready-to-use sample clients (OAuth + search/add/update + E2EE + 60s sync polling):

| Language | Path | Dependencies |
|---|---|---|
| Python | [`clients/python/porter_client.py`](clients/python/porter_client.py) | stdlib + `requests` + `pynacl` |
| TypeScript | [`clients/typescript/porter-client.ts`](clients/typescript/porter-client.ts) | `fetch` (built into Node 18+) + `libsodium-wrappers` |

Demo flow for both clients: `register → print consent URL → search → add → poll once` (`PORTER_SETUP_TOKEN` / `PORTER_AUTH_CODE` via environment variables).

## Security notes (PRD §14 threat model)

| Threat | Mitigations in this release |
|---|---|
| T1 scope-exceeding reads | Scopes enforced server-side; `memory.read.<tag>` filters at the SQL layer, client-passed tags are hints only; empty intersection fails closed with an empty result |
| T2 token leakage | Access tokens last 1h; refresh rotation + reuse detection (reuse outside the 10s grace window → full client revocation + `security` audit); revocation blacklist cached 30s, revocations take effect within 60s |
| T3 memory poisoning | Search results marked `untrusted_data: true`; write provenance tracked; pending-review queue is the human gate |
| T4 local machine exfiltrated (database stolen) | refresh token / auth code stored as sha256 only; client secret stored as argon2 hash only; **physical-machine and network security are the operator's responsibility** |
| T5 malicious write flooding | 60 req/min/client rate limit (429 `rate_limited`); `memory.write` only reaches the pending-review queue |
| T7 audit tampering | DB trigger forbids modifying/deleting audit rows; grant/revoke/refresh/security events are all audited |

**v1 trust assumptions (important)**:
- The consent screen (`/oauth/authorize`) and panel have no login: **anyone who can reach the server's network can operate them**. Binds only `127.0.0.1` by default; if exposure is needed, place it behind a trusted intranet / authenticated reverse proxy. The consent POST has a same-origin CSRF guard — forged cross-site approvals get 401; permissive CORS has been removed.
- First client registration needs a one-time setup token (`X-Porter-Setup-Token`, auto-generated per process and loudly printed to the startup log when unset); later registrations need `memory.admin`. Complete the first authorization soon after first boot.
- When `PORTER_JWT_SECRET` is unset, a temporary signing key is generated on every restart and all old access tokens die (refresh tokens are unaffected — they can be exchanged again). Always set it in production.

## Key design decisions

| Decision | Rationale |
|---|---|
| Access token = JWT (HS256) | Stateless verification; jti blacklist for revocation (DB + 30s in-memory cache) |
| Refresh token = opaque | 32 random bytes, DB stores sha256 only; rotation, reuse detection against theft |
| Auth code = opaque | 10-minute, single-use, atomically `consume`d in DB against replay |
| Writes go to the pending-review queue first | Anti-junk (PRD §8 F1); takes effect via `POST /memories/:id/approve` |
| Dedup threshold 0.92 | Writes with vector similarity > 0.92 return `duplicate_candidates`, never auto-merge |
| Audit append-only | DB trigger forbids UPDATE/DELETE (defense in depth) + no app-layer update/delete path |
| Search results are untrusted | All search hits carry `untrusted_data: true` (anti prompt-injection, PRD principle 5) |
| Embedder interface | `hash` (stub, local dev/tests) / `sentence-transformer` (BGE-M3, production) / `http` (embedder service) / `openai` (optional, requires explicit opt-in) |
| Repository interface | `PostgresMemoryRepository` (production) / `InMemoryMemoryRepository` (tests/DB-less environments), same domain rules |
| Rate limiting | In-memory token bucket; counts are per worker under multi-worker deployments (acceptable for self-hosted v1; Redis in P2) |

## Environment variables

See `.env.example`:

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql://porter:porter@localhost:5432/porter` | Postgres connection string |
| `PORTER_REPO` | `memory` | `memory` (in-memory) / `postgres` |
| `EMBEDDER_KIND` | `hash` | `hash` / `sentence-transformer` / `http` / `openai` |
| `AUTH_ENABLED` | `1` | `0` disables auth (local dev only) |
| `PORTER_JWT_SECRET` | (random temporary) | Must be set in production! |
| `PORTER_RATE_LIMIT_PER_MINUTE` | `60` | T5 rate limit |

## Owner panel (PRD §8 F4)

A server-rendered panel (HTMX + minimal CSS, no heavy frontend framework), mounted at `/panel`.
English by default, switchable to English/Chinese in Settings (`porter/i18n.py`, preference persisted in
`~/.porter/settings.json`).

**Access**: `http://127.0.0.1:8000/panel` (localhost only). The panel is the owner's interface:
the auth middleware treats `/panel` as a public path prefix (no bearer needed), but every route enforces
the localhost gate (`panel_owner` dependency + structured tests guaranteeing no route is missed); `main.py`
binds only `127.0.0.1` by default. All mutations from the panel go through the same
repository/store calls as the REST endpoints + audit logging — no back doors.

**Pages** (PRD §17 information architecture):

| Page | Path | What it does |
|---|---|---|
| Memories | `/panel/memories` | Full-text/semantic search toggle (instant HTMX search), filter by tag/type/source; open a detail view to edit (history chain kept) or delete (requires confirmation) |
| Pending queue | `/panel/inbox` | Approve / edit & approve / reject; suspected duplicates hit at write time (similarity > 0.92) are shown side-by-side |
| Clients | `/panel/clients` | Client list (scope descriptions, last active), register new clients (secret shown once), one-click revoke (confirm, effective everywhere within 60 seconds) |
| Audit | `/panel/audit` | Log stream (who/when/action/memories affected/scope), filter by source and action; append-only integrity note |
| Snapshots | `/panel/snapshots` | Create snapshots (pick tags, set password → AES-256-GCM encryption), history list & download, upload & apply (dry-run preview → import into pending queue on confirm) |
| Settings | `/panel/settings` | Add/remove custom scope_tags (preset tags protected), one-click export (porter-export/v1 JSON), backup notes |

**Screenshot notes** (text version; the actual running UI is authoritative):
- Memories: top search box + filters; table columns: content / type / status / source / created at.
- Pending queue: one card per memory, Approve/Edit/Reject buttons on the right, suspected duplicates shown in a yellow warning box.
- Clients: table columns: client / scopes / last active / revoke button.
- Audit: green integrity banner on top; table columns: time / who / action / memories involved / scope.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
# 99 tests: CRUD / pending-review flow / search recall / dedup / audit + full OAuth flow
# (registration / consent / CSRF / token issuance / rotation / reuse detection / scope isolation / revocation / rate limiting / error shapes)
# + panel (routes / access gate / pending ops / revocation wiring / snapshots / tags / export)
# + MCP (both transports / auth / scopes / untrusted marking) + snapshot Ed25519 signing (8 tests)
```

## Not yet verified in the build environment (honesty list)

The build machine has no Docker / real Postgres / large memory. The following was **not executed**
in this environment and must be verified in CI or on first real deploy (see [SECURITY.md](SECURITY.md)):

- Execution of migrations `001`–`003` on real `pgvector/pg16` + the audit trigger actually firing
- BGE-M3 embedding quality & latency (build machine lacks RAM; interface + HTTP plumbing already tested)
- `docker compose up` end-to-end (compose YAML validated, Dockerfile reviewed)
- Real harness handshakes (27 configs written to each vendor's MCP spec, untested against the actual CLIs; Copilot CLI's config path especially needs real-machine confirmation)

## Roadmap

- [x] W1: memory CRUD + search + audit (incl. review)
- [x] W2: OAuth authorization-code flow + scope enforcement + rate limiting + revocation (incl. review)
- [x] F4: owner panel (memories/pending/clients/audit/snapshots/settings) + custom tags + export
- [x] F3: MCP server (stdio + Streamable HTTP) + client config templates
- [x] E2E: harness memory importer (Codex / Claude Code / Windsurf / Cline / Roo / Gemini / Qwen / Cursor / Junie / Droid / Tabnine / Devin / OpenHands; others honestly no import) + 27-harness MCP configs
- [x] Snapshots v1: Ed25519 signing + optional AES-256-GCM encryption
- [x] Release: versioning + CHANGELOG + SECURITY.md + red-team walkthrough
- [ ] P1: auto-sync (`sync_cursors` table + `/sync/delta` stub already reserved)
