**English** | [中文](DEPLOY.zh-CN.md)

# Porter — Deploy & Connect Guide

Run Porter on your own machine and wire it into your coding harnesses
(Codex, Claude Code, Cursor, Zed, Windsurf, Cline, Roo Code, Gemini CLI,
Qwen Code, OpenCode, Amp, Kilo Code, Kimi CLI, Copilot CLI, Aider —
see the support matrix in §3).
Estimated time: ~20 minutes (mostly Docker image pulls).

> Verified locally on 2026-10-18: real Postgres/pgvector with all 11
> migrations, Docker persistence, OAuth, HTTP/stdio MCP, encrypted snapshots,
> revocation and BGE-M3 CPU inference. The small embedding benchmark is in
> `docs/embedding-eval.2026-10-18.json`; it is not a general quality guarantee.
> Individual installed harnesses and visual browser acceptance remain unverified.

---

## 0. Prerequisites

- Docker + Docker Compose 2.24.4+ (for Postgres/pgvector + the embedding service)
- Python 3.12+ (for the Porter package / MCP stdio server)
- One of: Codex CLI, Zed, Cursor (any order — this guide does Codex first)

## 1. Start the stack

```bash
cd /path/to/porter
python scripts/setup_local.py  # preserves an existing .env; never prints secrets
docker compose up --build -d
```

The embedder image installs CPU-only PyTorch by default, with tested model
constraints in `requirements-embedder-lock.txt`. BGE-M3 weights download on
first start (about 3.8 GB total cache in the acceptance run). The API
waits for the embedder healthcheck before serving, so give it a few minutes:

```bash
docker compose logs -f embedder   # wait for "healthy"
curl http://localhost:8000/healthz
```

## 2. Register the owner client (one-time bootstrap)

First open `http://127.0.0.1:8000/panel` in your browser. Docker gateway
connections are redirected to `/owner/login`; unlock with `PORTER_SETUP_TOKEN`
from `.env`. The owner session lasts 12 hours and also protects OAuth consent.
Native loopback connections retain local owner access.

```bash
SETUP=$(grep PORTER_SETUP_TOKEN .env | cut -d= -f2)
curl -s -X POST http://localhost:8000/api/v1/clients \
  -H "X-Porter-Setup-Token: $SETUP" \
  -H 'Content-Type: application/json' \
  -d '{"name":"owner-panel","redirect_uris":["http://localhost:9999/callback"]}'
# -> {"client_id":"...","client_secret":"..."}  (secret shown ONCE — save it)
```

Open the consent flow in your browser to grant `memory.admin` (+ read/write):

```
http://localhost:8000/oauth/authorize?client_id=<id>&redirect_uri=http://localhost:9999/callback&scope=memory.read%20memory.write%20memory.admin&state=x
```

Approve every scope, copy the `code` from the redirect URL, then:

```bash
curl -s -X POST http://localhost:8000/oauth/token \
  -d grant_type=authorization_code -d code=<code> \
  -d redirect_uri=http://localhost:9999/callback \
  -d client_id=<id> -d client_secret=<secret>
# -> {"access_token":"...","refresh_token":"..."}
export PORTER_TOKEN=<access_token>
```

The panel is at `http://localhost:8000/panel` (owner context, localhost only).
Default language is English; switch to English anytime on the **Settings**
page (preference persists in `~/.porter/settings.json`).

## 3. Import your existing harness memories (read-only on your files)

The importer NEVER modifies your harness files — it only reads them, and
everything lands in the pending-review inbox first.

### Harness support matrix

| Harness | Read existing memories? | Implant via MCP? | Import command |
|---|---|---|---|
| Codex CLI | ✅ `~/.codex/memories/**/*.md` | ✅ `clients/codex-config.toml` | `--harness codex` |
| Claude Code | ✅ `~/.claude/projects/*/memory/*.md` + `MEMORY.md` + `CLAUDE.md` seed | ✅ `clients/claude-code-mcp.json` | `--harness claude-code` |
| Windsurf | ⚠️ `~/.codeium/windsurf/memories/*.md` (often absent — normal) | ✅ `clients/windsurf-mcp_config.json` | `--harness windsurf` |
| Cline | ✅ `<project>/memory-bank/*.md` | ✅ `clients/cline-mcp.json` | `--harness cline --project-root …` |
| Roo Code | ✅ `<project>/memory-bank/*.md` + `.roo/rules/*.md` (as skill_ref) | ✅ `clients/roo-mcp.json` | `--harness roo --project-root …` |
| Gemini CLI | ⚠️ `~/.gemini/GEMINI.md` (+ project) — seed preferences only | ✅ `clients/gemini-settings.json` | `--harness gemini` |
| Qwen Code | ⚠️ `~/.qwen/QWEN.md` (+ project) — seed preferences only | ✅ `clients/qwen-settings.json` | `--harness qwen` |
| ChatGPT export | ✅ `conversations.json` (`.json` or export `.zip`, in memory) | — | `--harness chatgpt --file …` |
| Claude export | ✅ `conversations.json` (`.json` or export `.zip`, in memory) | — | `--harness claude --file …` |
| Cursor | ⚠️ `.cursor/memories/` community convention only (no native store) | ✅ `clients/cursor-mcp.json` | `--harness cursor --project-root …` |
| Junie | ⚠️ `~/.junie/guidelines.md` (+ `<project>/.junie/guidelines.md`) — seed only | ✅ `clients/junie-mcp.json` | `--harness junie` |
| Factory Droid | ⚠️ `~/.factory/AGENTS.md` (+ project) — seed only | ✅ `clients/droid-mcp.json` | `--harness droid` |
| Tabnine | ⚠️ `<project>/TABNINE.md` — seed only | ✅ `clients/tabnine-settings.json` (restart required) | `--harness tabnine --project-root …` |
| Devin | ⚠️ `<project>/.devin/**/*.md` — seed only (conf. 0.3) | ✅ `clients/devin-mcp_config.json` | `--harness devin --project-root …` |
| OpenHands | ⚠️ `<project>/.agents/skills/*/SKILL.md` (as skill_ref) | ✅ `clients/openhands-mcp.json` | `--harness openhands --project-root …` |
| Zed | ❌ no native store (honest no-op) | ✅ `clients/zed-settings.json` | — |
| OpenCode | ❌ no native store | ✅ `clients/opencode.json` (top-level `mcp` key) | — |
| Amp | ❌ no native store | ✅ `clients/amp-settings.json` (`amp.mcpServers` key) | — |
| Kilo Code | ❌ no native store | ✅ `clients/kilocode-mcp.json` | — |
| Kimi CLI | ❌ no native store | ✅ `clients/kimi-mcp.json` | — |
| Copilot CLI | ❌ no native store | ✅ `clients/copilot-cli-mcp.json` (verify config path on your install) | — |
| Aider | ❌ stateless, no store | ✅ `clients/aider-conf.yml` (`mcp-servers:` key) | — |
| Continue.dev | ❌ no native store | ✅ `clients/continue-config.json` (Agent mode only) | — |
| Zencoder | ❌ no native store | ✅ `clients/zencoder-settings.json` (`zencoder.mcpServers` key) | — |
| Neovim (avante/codecompanion) | ❌ no native store | ✅ `clients/neovim-mcphub-servers.json` (via mcphub.nvim) | — |
| Emacs (gptel) | ❌ no native store | ✅ `clients/emacs-gptel-mcp.el` (via mcp.el) | — |
| Google Antigravity | ❌ no native store | ✅ `clients/antigravity-mcp_config.json` (HTTP `serverUrl`; auth header UNVERIFIED) | — |
| Kiro | ❌ no native store | ✅ `clients/kiro-mcp.json` | — |
| Augment Code | ❌ no native store | ✅ `clients/augment-mcp.json` | — |

```bash
cd /path/to/porter
# Everything with a native store, one shot (cline/roo need --project-root
# to include their project-level memory banks):
python -m porter.import_cli --harness all --dry-run
python -m porter.import_cli --harness all --project-root ~/myproj \
  --token "$PORTER_TOKEN"

# Or per harness:
# Codex: reads ~/.codex/memories/**/*.md
python -m porter.import_cli --harness codex --dry-run
python -m porter.import_cli --harness codex --token "$PORTER_TOKEN"

# Claude Code: project memories + MEMORY.md + CLAUDE.md seed
# (project-dir decoding is best-effort; raw names are kept for reference)
python -m porter.import_cli --harness claude-code --token "$PORTER_TOKEN"

# Cursor: no native memory store exists; this covers the .cursor/memories/
# community convention per project:
python -m porter.import_cli --harness cursor --project-root ~/myproj \
  --token "$PORTER_TOKEN"

# Cline / Roo Code: memory-bank (+ .roo/rules as skill_ref for Roo):
python -m porter.import_cli --harness cline --project-root ~/myproj \
  --token "$PORTER_TOKEN"
python -m porter.import_cli --harness roo --project-root ~/myproj \
  --token "$PORTER_TOKEN"

# Gemini / Qwen: instruction files imported as low-confidence seeds:
python -m porter.import_cli --harness gemini --token "$PORTER_TOKEN"
python -m porter.import_cli --harness qwen --token "$PORTER_TOKEN"

# ChatGPT / Claude: official data exports (best-effort, read-only on the file):
# Point --file at conversations.json, or at the whole export .zip — the zip
# is parsed IN MEMORY and never extracted to disk (zip-slip impossible;
# zip bombs rejected: >200MB claimed uncompressed or >100k entries).
python -m porter.import_cli --harness chatgpt \
  --file ~/Downloads/conversations.json --dry-run
python -m porter.import_cli --harness claude \
  --file ~/Downloads/claude-export.zip --token "$PORTER_TOKEN"

# Zed / OpenCode / Amp / Kilo / Kimi / Copilot CLI / Aider:
# no native portable memory store — nothing to import (by design).
# Going forward they write memories to Porter via MCP (next section).
```

**What the ChatGPT/Claude export importers extract (and what they don't):**

- One `episode` memory per conversation: a summary line —
  `ChatGPT conversation '<title>' (<n> messages, <date range>)`. Full
  transcripts are deliberately **not** imported: they are long, mostly
  non-durable, and would flood the inbox.
- One `fact` memory per conversation **at most**: the *first* user message
  (truncated to 2000 chars), and only if it matches a small keyword
  heuristic for durable statements (`my name is`, `I live in`,
  `I prefer`, `my favorite`, `remember that`, `from now on`, … — the full
  list lives in `DURABLE_STATEMENT_PATTERNS` in `porter/importers.py`).
  Everything else in the conversation is skipped.
- Both land in the **pending inbox** at **confidence 0.5** with
  `source.client_id` = `chatgpt`/`claude`, `source.imported_from` = the
  export path, and `source.conversation_id` = the conversation's id.
  Secrets are scrubbed before storage, and re-imports dedupe per
  conversation (not per file).

**Limits, stated plainly:** this is best-effort migration, not a faithful
archive. The heuristic is keyword matching, not understanding — it misses
paraphrases ("call me Alex" has no "my name is") and occasionally catches
a transient sentence. Timestamps come from the export and may be missing
(shown as "dates unknown"). If a file matches neither the ChatGPT nor the
Claude `conversations.json` shape, it is skipped with a warning — never a
crash. **Review everything in the inbox before approving.**

Then open the panel's **Inbox** and approve what you want searchable.

## 4. Connect Codex (first client)

Prefer the [HTTP setup](#recommended-codex-connection-http) when the API is
already running. The following instructions are the stdio alternative.

```bash
# make the porter package importable for the stdio server:
pip install -c /path/to/porter/requirements-lock.txt -e /path/to/porter
# (or set PYTHONPATH in the config instead)
```

Merge `clients/codex-config.toml` into `~/.codex/config.toml`:
replace `{{PORTER_TOKEN}}` with your token and `/path/to/porter` with the
real repo path. Replace `{{PORTER_JWT_SECRET}}` with the SAME value from
`.env`, and copy its `DATABASE_URL` (including the generated password). On
Windows, set `command` to your Python executable or `python` instead of
`python3`. All stdio clients need these shared settings; HTTP clients need
only a bearer token. Then restart Codex and check it sees the tools:

```
# in a Codex session:
/mcp   (or ask it to list available tools — you should see
        memory_search, memory_add, memory_update)
```

## 5. Connect the rest

All harnesses below consume the same Porter MCP server — merge the
indicated block from `clients/`, replacing `{{PORTER_TOKEN}}` with your
token, the shared signing secret, matching database URL and real repository
path (as in step 4), then restart the harness:

| Harness | Config file | Merge into |
|---|---|---|
| Zed | `clients/zed-settings.json` (`context_servers`) | `~/.config/zed/settings.json` |
| Cursor | `clients/cursor-mcp.json` | `~/.cursor/mcp.json` (user) or `.cursor/mcp.json` (project) |
| Claude Code | `clients/claude-code-mcp.json` | project `.mcp.json` or `~/.claude.json` |
| OpenCode | `clients/opencode.json` (top-level **`mcp`** key, not `mcpServers`) | `~/.config/opencode/opencode.json` or `./opencode.json` |
| Gemini CLI | `clients/gemini-settings.json` | `~/.gemini/settings.json` |
| Qwen Code | `clients/qwen-settings.json` | `~/.qwen/settings.json` |
| Amp | `clients/amp-settings.json` (**`amp.mcpServers`** key) | Amp `settings.json` |
| Windsurf | `clients/windsurf-mcp_config.json` | `~/.codeium/windsurf/mcp_config.json` (**full restart** required) |
| Roo Code | `clients/roo-mcp.json` | VS Code settings JSON, Roo Code section |
| Cline | `clients/cline-mcp.json` | VS Code settings JSON, Cline section |
| Kilo Code | `clients/kilocode-mcp.json` | VS Code settings JSON, Kilo Code section |
| Kimi CLI | `clients/kimi-mcp.json` | Kimi CLI MCP config (check path in Kimi docs) |
| Copilot CLI | `clients/copilot-cli-mcp.json` | `~/.config/github-copilot/mcp.json` (**verify** on your install) |
| Aider | `clients/aider-conf.yml` (**`mcp-servers:`** key, YAML) | `~/.aider.conf.yml` or `./.aider.conf.yml` |

Config formats were validated as JSON/TOML/YAML and against each vendor's
docs (`tests/test_harness12.py`), but only your installed versions can
complete the real handshake — if a harness rejects the shape, check its
current docs and tell us.

## 6. The cilantro test (US1 — cross-harness recall)

1. In the panel (or any client), write + approve: **"user avoids cilantro"**
   tagged `dietary`.
2. In Codex, ask: *"recommend a restaurant for me"* (or ask it to call
   `memory_search` with "what foods should I avoid").
3. ✅ It recalls the cilantro preference. Check the panel **Audit** page:
   you should see the read under your Codex client's id.

## 7. The revoke test (US3 — one-click kill)

1. In the panel, go to **Clients** → revoke your Codex client.
2. Back in Codex, call `memory_search` again → it must fail.
3. Re-register/re-authorize Codex to restore access (new token).

## 8. Going forward

- New memories written in any connected harness go to the inbox; approve
  once and every harness sees them (P1 auto-sync removes even this step).
- Least privilege: when authorizing a client, prefer `memory.read.<tag>`
  scopes over blanket `memory.read`.
- Backups: `docker compose exec db pg_dump ...` — see README "Backup".

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `curl …/healthz` refuses connection | `docker compose ps` — is `api` healthy? Check `docker compose logs api`. Port 8000 already in use? Change the host port mapping in `docker-compose.yml`. |
| MCP server exits immediately in Codex | Run it by hand: `PORTER_TOKEN=… python3 -m porter.mcp_stdio` and read stderr. Usual cause: `porter` not importable (fix `pip install -e` / PYTHONPATH) or `DATABASE_URL` unreachable. |
| `porter:invalid_token` from MCP tools | Token expired (1h) or revoked. Mint a fresh one via `/oauth/token` with your refresh token, update the client config, restart the harness. |
| Embedder never becomes healthy | Needs ~2.3 GB download + ~3 GB RAM. Check `docker compose logs embedder`. On small machines set `EMBEDDER_KIND` appropriately or use the OpenAI embedding opt-in (see README — note the privacy tradeoff). |
| Import finds nothing for Codex | Codex only writes `~/.codex/memories/` when it actually saves memories. Empty dir = nothing to migrate; going forward it writes to Porter via MCP. Override with `--codex-dir`. |
| `401 invalid_token` on `/api/v1/*` | Same as MCP token above — refresh it. |
| Panel shows nothing at `/panel` | Open `/panel` from the published localhost port. If Docker forwards the peer address, unlock at `/owner/login` with the setup token. |

## What still needs YOUR machine to verify

- Real MCP handshakes for all 27 harness templates (config formats were validated
  as TOML/JSON/YAML and against each vendor's docs in
  `tests/test_harness12.py`, but only your installed versions can complete
  the handshake; Copilot CLI's config path especially needs confirming).
- Real `~/.codex/memories/`, `~/.claude/projects/`, etc. (importers were
  tested against fixture trees only).
- The cilantro + revoke tests above in your installed harness (steps 6–7).
  The equivalent independent MCP process and HTTP transport checks passed locally.

## Persistence and sync behavior

The `porter-data` volume keeps language settings, snapshots and signing keys
across container recreation. `porter-db` keeps the memory database. The
database and embedder are published on localhost only for stdio clients.
Do not run `docker compose down -v` if you want to retain this data.

`sync_delta` supplies an incremental feed; consuming it and injecting context
into a harness is the adapter/client responsibility. Merely installing an MCP
template does not make a harness capture every conversation automatically.
Sample Python/TypeScript polling loops drain all pages before waiting and
acknowledge filtered pages too. Access tokens expire after one hour; use the
refresh flow and update static client tokens as described above.

The default `stub` extractor uses keyword rules. Optional local Ollama
extraction requires an explicitly configured model and always queues facts
for review; see [extraction setup](docs/EXTRACTION.md). OpenAI extraction
is not implemented. ML triage remains a separate optional feature.


## Repeatable isolated acceptance

Install the lightweight test dependencies, then run:

```bash
python -m pip install -c requirements-lock.txt -r requirements-test.txt
python scripts/verify_deployment.py --keep-running
```

This uses the `porter-review` Compose project, generates private `.env.review`
credentials, publishes ports 55432/18001/18000 and checks the complete API/MCP
flow with deterministic hash embeddings. It does not require downloading a
model. The panel is at `http://127.0.0.1:18000/panel`; unlock using the setup
token in `.env.review`. Without `--keep-running`, containers stop after the
check and named volumes remain.

Real-model quality is a separate check. Against a running BGE-M3 embedder:

```bash
python scripts/eval_embeddings.py --embedders hash,http --http-url http://127.0.0.1:8001
```

Use a separate database for a different embedder, or regenerate every stored
vector before switching. Equal vector dimensions do not make embeddings from
different models compatible. The recorded small fixture found imperfect BGE
recall and duplicate detection; review the results before changing thresholds.


## Host validation and reverse proxies

The API accepts `localhost`, `127.0.0.1` and `[::1]` by default. Requests with
other Host headers are rejected with HTTP 400 before owner/auth routing.
For a custom domain, set exact hostnames (no scheme, port or wildcard):

```dotenv
PORTER_ALLOWED_HOSTS=localhost,127.0.0.1,[::1],memory.example.com
PORTER_PUBLIC_ORIGIN=https://memory.example.com
```

Set the public origin when an authenticated reverse proxy terminates TLS.
It must be the browser-facing scheme/host/port, without a path or credentials.
The proxy must preserve the original Host header and handle TLS. Bundled
launch commands disable forwarded-header trust; custom domains require owner
login even when the proxy connects from loopback. HTTPS public origins mark
the owner cookie Secure. Native loopback URLs retain local owner access.

`AUTH_ENABLED` and `PORTER_WIKI_ENABLED` accept 1/0, true/false, yes/no and
on/off, case-insensitively. Invalid values stop startup rather than silently
changing authentication behavior. `PORTER_REPO` accepts only memory/postgres;
a typo must not turn persistent storage into an empty temporary repository.

## Recommended Codex connection: HTTP

With the API running, prefer `clients/codex-http-config.toml`. It needs only a
scoped access token for that client; database credentials and the server JWT
signing secret stay on the server. Merge its `[mcp_servers.porter]` block into
your Codex configuration, choosing HTTP or stdio for that server name.

Set the access token in the environment used to launch Codex:

```powershell
$env:PORTER_TOKEN = '{{PORTER_TOKEN}}'
codex mcp get porter --json
```

In Bash, use `export PORTER_TOKEN='{{PORTER_TOKEN}}'`. Replace the placeholder
with an OAuth access token, then launch/restart Codex from that environment.
The template defaults to port 8000; use 18010 for the retained local BGE review
preview. Tokens still expire after one hour and require the OAuth refresh flow.

Both templates were accepted by the installed Codex CLI 0.160.0 on 2026-10-19.
`mcp get` verifies configuration parsing, not tool execution. Independent
HTTP/stdio MCP clients have passed live acceptance; a Codex conversation using
the tools remains a separate user acceptance check. See the
[official MCP configuration documentation](https://developers.openai.com/codex/mcp/).
