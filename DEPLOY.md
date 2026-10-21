**English** | [中文](DEPLOY.zh-CN.md)

# Deploy and connect Porter

This guide deploys the single-owner alpha on localhost. Start with one client,
then run the [short user acceptance checklist](docs/USER_SETUP_CHECKLIST.md).
The [acceptance record](docs/ACCEPTANCE.md) distinguishes automated service
checks from real client and browser checks. A template is not a compatibility
certification.

## 1. Prerequisites and startup

- Git, Python 3.12+ and Docker with Compose 2.24.4+.
- Free localhost ports 8000 (API/panel), 8001 (embedder) and 5432 (database),
  or adjusted Compose mappings.
- Network access for image/package/model downloads on first start. BGE-M3's
  measured model cache was about 3.8 GB, plus Docker image space. CPU is supported;
  the embedder container has a 4 GB memory limit. The API, database and Docker
  need additional memory; this is not a guaranteed whole-machine minimum.

```bash
git clone https://github.com/Mike-Animal-Counseling/porter.git
cd porter
python scripts/setup_local.py
docker compose up --build -d
docker compose ps
docker compose logs --tail 50 embedder api
```

Use the candidate PR's head branch if accepting unmerged fixes. The setup helper
creates a private `.env` with stable random credentials; existing files are
preserved, so compare missing settings with [`.env.example`](.env.example).
The Docker API uses persistent PostgreSQL even though the example's native
development repository setting is `memory`.

The embedder uses CPU-only PyTorch and tested constraints in
`requirements-embedder-lock.txt`. Its first model download can take time. The
API waits for the embedder healthcheck. Once ready, opening
**http://127.0.0.1:8000/healthz** must return 200. Do not remove data volumes to
repair a startup error.

Open **http://127.0.0.1:8000/panel**. Docker-forwarded browsers unlock at
`/owner/login` with `PORTER_SETUP_TOKEN` from `.env`. The signed owner session
lasts 12 hours and also gates OAuth consent. Native loopback URLs retain local
owner access; custom domains require owner login. The default panel language is
English; switch between English and Chinese in Settings. Docker stores that
preference in the `porter-data` volume.

## 2. Create a limited client and obtain a token

Use **Clients → New client** in the owner panel. Give each tool its own client
and save the returned `client_id` and one-time `client_secret` privately.
For this manual OAuth example, register the exact redirect URI
`http://127.0.0.1:18765/callback`.

Request only the scopes you need:

| Use | Scopes |
|---|---|
| Work-context writer | `memory.read.work memory.write` |
| Work-context reader | `memory.read.work` |
| Read all approved memories | `memory.read` (broader access; deliberate choice) |
| Owner administration | `memory.admin` (implies all permissions; keep separate from ordinary tools) |
| Snapshot API operations | `state.export` / `state.import` as needed |

`memory.write` permits new writes; tiered approval can activate low-risk
content. It does not grant read access. Updating an existing memory requires
both write permission and read access to that target. Explicit merge/resolve,
actual server-side writeback and device key-envelope management require admin.
The owner panel performs approval without giving your coding tool admin.

### Authorization code exchange

Open this URL in the owner browser, replacing the client ID and using a fresh,
unpredictable state value:

```text
http://127.0.0.1:8000/oauth/authorize?client_id=YOUR_CLIENT_ID&redirect_uri=http%3A%2F%2F127.0.0.1%3A18765%2Fcallback&scope=memory.read.work%20memory.write&state=YOUR_RANDOM_STATE
```

Review the requested scopes and approve. No callback server is required for
this manual flow: the browser may show a connection error. Read `code` and
`state` from its address bar; confirm state matches your request. Exchange the
short-lived, one-use code with the same exact redirect URI.

Bash example (replace all placeholder values locally):

```bash
PORTER_CLIENT_ID='YOUR_CLIENT_ID'
PORTER_CLIENT_SECRET='YOUR_CLIENT_SECRET'
PORTER_AUTH_CODE='YOUR_ONE_TIME_CODE'
curl --fail-with-body -sS http://127.0.0.1:8000/oauth/token \
  --data-urlencode 'grant_type=authorization_code' \
  --data-urlencode "code=$PORTER_AUTH_CODE" \
  --data-urlencode 'redirect_uri=http://127.0.0.1:18765/callback' \
  --data-urlencode "client_id=$PORTER_CLIENT_ID" \
  --data-urlencode "client_secret=$PORTER_CLIENT_SECRET"
# The response contains access_token, refresh_token, expires_in and scope.
export PORTER_TOKEN='ACCESS_TOKEN_FROM_RESPONSE'
```

PowerShell equivalent:

```powershell
$porterBase = 'http://127.0.0.1:8000'
$porterClientId = Read-Host 'Client ID'
$porterClientSecret = Read-Host 'Client secret'
$porterPair = Invoke-RestMethod -Method Post -Uri "$porterBase/oauth/token" -Body @{
    grant_type = 'authorization_code'
    code = (Read-Host 'One-time code')
    redirect_uri = 'http://127.0.0.1:18765/callback'
    client_id = $porterClientId
    client_secret = $porterClientSecret
}
$env:PORTER_TOKEN = $porterPair.access_token
```

The **access token** goes in the client configuration, not the client secret.
The owner setup token, JWT signing key and database password stay on the server
for HTTP connections. Tokens are credentials; do not include these responses
in screenshots, issues or committed files.

### Refresh and revoke

Access tokens expire after **one hour**. Send `grant_type=refresh_token`,
`refresh_token`, `client_id` and `client_secret` to the same `/oauth/token`
endpoint. In PowerShell, continuing the example:

```powershell
$porterPair = Invoke-RestMethod -Method Post -Uri "$porterBase/oauth/token" -Body @{
    grant_type = 'refresh_token'
    refresh_token = $porterPair.refresh_token
    client_id = $porterClientId
    client_secret = $porterClientSecret
}
$env:PORTER_TOKEN = $porterPair.access_token
```

Save the newly returned refresh token: refresh tokens rotate. Update the
configured access token and reconnect/restart the tool. Static MCP templates
do not refresh automatically. Use **Clients → Revoke** to stop that client;
authorize again to restore it with new credentials.

## 3. Connect MCP: HTTP first

For Codex, merge the block in
[`clients/codex-http-config.toml`](clients/codex-http-config.toml) into your
Codex configuration; do not replace the whole file:

```toml
[mcp_servers.porter]
url = "http://127.0.0.1:8000/mcp"
bearer_token_env_var = "PORTER_TOKEN"
startup_timeout_sec = 30
tool_timeout_sec = 60
```

Start or restart the tool from the environment where `PORTER_TOKEN` is set.
Choose HTTP or stdio for the same server name, not both. For the retained
maintainer BGE review environment use port **18010**; standard Compose uses
**8000**. The review address is local to that machine.

The installed Codex CLI 0.160.0 parsed both templates on 2026-10-19;
`codex mcp get porter --json` checks parsing, not tool execution. Independent
HTTP/stdio MCP clients passed deployed acceptance; actual tool conversations
remain a separate check. For the configuration format, see the
[official Codex MCP reference](https://developers.openai.com/codex/mcp/).

### Trusted local stdio alternative

Install the package in the Python environment that will launch the server:

```bash
python -m pip install -c requirements-lock.txt -e .
```

Merge the matching stdio template, such as
[`clients/codex-config.toml`](clients/codex-config.toml), and replace:

- `PORTER_TOKEN`: that tool's scoped access token.
- `PORTER_REPO=postgres` and `DATABASE_URL`: the same persistent database as the
  API, including the generated password and the host-published port.
- `PORTER_JWT_SECRET`: the same signing secret as the API.
- `PORTER_EMBEDDER=http` and `EMBEDDER_URL=http://127.0.0.1:8001`: the same
  embedder used for stored vectors.
- `PYTHONPATH`: the absolute checkout path; `command`: a Python executable
  with Porter installed (often `python` on Windows, or a virtualenv path).

Do not copy `EMBEDDER_URL=http://embedder:8001` from the Docker environment
into a host process; that hostname is internal to Compose. All these settings
must be shared correctly. Otherwise a stdio process may use a separate
ephemeral repository or fail authentication. Only use direct database/signing
credentials with tools trusted as much as the server.

## 4. Importers and configuration templates

**This matrix lists repository implementations, not live compatibility
certifications.** “Importer” means a parser exists for that path/format, tested
with fixtures. “Template” means a configuration file is included. Native stores,
configuration paths and transport support can vary by installed version.
No row claims that a real conversation in that product has passed acceptance.

| Tool / source | Importer reads | Included MCP template | Import argument |
|---|---|---|---|
| Codex CLI | `~/.codex/memories/**/*.md` | `codex-http-config.toml` / `codex-config.toml` | `codex` |
| Claude Code | `~/.claude/projects/*/memory/*.md`, `MEMORY.md`, `CLAUDE.md` seed | `claude-code-mcp.json` | `claude-code` |
| Windsurf | `~/.codeium/windsurf/memories/*.md`, if present | `windsurf-mcp_config.json` | `windsurf` |
| Cline | Project `memory-bank/*.md` | `cline-mcp.json` | `cline --project-root PATH` |
| Roo Code | Project `memory-bank/*.md`, `.roo/rules/*.md` | `roo-mcp.json` | `roo --project-root PATH` |
| Gemini CLI | User/project `GEMINI.md` seeds | `gemini-settings.json` | `gemini` |
| Qwen Code | User/project `QWEN.md` seeds | `qwen-settings.json` | `qwen` |
| Cursor | Project `.cursor/memories/` community convention | `cursor-mcp.json` | `cursor --project-root PATH` |
| Junie | User/project `.junie/guidelines.md` seeds | `junie-mcp.json` | `junie` |
| Factory Droid | User/project `AGENTS.md` seeds | `droid-mcp.json` | `droid` |
| Tabnine | Project `TABNINE.md` seed | `tabnine-settings.json` | `tabnine --project-root PATH` |
| Devin | Project `.devin/**/*.md` seeds | `devin-mcp_config.json` | `devin --project-root PATH` |
| OpenHands | Project `.agents/skills/*/SKILL.md` references | `openhands-mcp.json` | `openhands --project-root PATH` |
| ChatGPT export | `conversations.json` or export ZIP | None | `chatgpt --file PATH` |
| Claude export | `conversations.json` or export ZIP | None | `claude --file PATH` |
| Zed | No native-store importer | `zed-settings.json` | — |
| OpenCode | No native-store importer | `opencode.json` | — |
| Amp | No native-store importer | `amp-settings.json` | — |
| Kilo Code | No native-store importer | `kilocode-mcp.json` | — |
| Kimi CLI | No native-store importer | `kimi-mcp.json` | — |
| Copilot CLI | No native-store importer | `copilot-cli-mcp.json` | — |
| Aider | No native-store importer | `aider-conf.yml` | — |
| Continue.dev | No native-store importer | `continue-config.json` | — |
| Zencoder | No native-store importer | `zencoder-settings.json` | — |
| Neovim | No native-store importer | `neovim-mcphub-servers.json` | — |
| Emacs / gptel | No native-store importer | `emacs-gptel-mcp.el` | — |
| Google Antigravity | No native-store importer | `antigravity-mcp_config.json`; bearer header unverified | — |
| Kiro | No native-store importer | `kiro-mcp.json` | — |
| Augment Code | No native-store importer | `augment-mcp.json` | — |

All template filenames above are under [`clients/`](clients/). Merge only the
required block into your existing tool configuration, using the target path
noted in that template; confirm your installed version accepts it. Format tests
do not fetch and revalidate every vendor's current schema or prove a handshake.

### Optional import

Importing is not required for first acceptance. It reads files without changing
them. Inspect a dry run before submitting anything:

```bash
python -m pip install -c requirements-lock.txt -e .
python -m porter.import_cli --harness codex --dry-run
python -m porter.import_cli --harness codex --api-url http://127.0.0.1:8000
# The actual import reads PORTER_TOKEN from the environment.
```

For project sources, add `--project-root /path/to/project`. Use
`--harness all --dry-run` to inspect all supported file importers; export
sources need their own `--harness chatgpt|claude --file PATH` invocation.
Use the correct `--api-url` for a non-default port. The token needs write
permission and only the read scopes you deliberately grant.

ChatGPT/Claude exports are best-effort migration, not complete transcript
archives: an episode summary and at most one keyword-selected fact per
conversation, at confidence 0.5. ZIPs are read in memory with size/entry limits.
Seed files can be low confidence. The server scrubs recognized secret patterns,
but that does not guarantee every secret is detected. Review the imported
records in Inbox before relying on them; native file shapes may differ.

## 5. Confirm use and understand automation

Run the synthetic [two-tool checklist](docs/USER_SETUP_CHECKLIST.md).
Ask for actual `memory_add` / `memory_search` calls and compare returned IDs.
A natural-language answer from chat history does not prove cross-tool recall.

Default `PORTER_APPROVAL_MODE=tiered` can activate low-risk writes or merge
readable near-duplicates with the same type and tags. Personal, sensitive,
conflicting or low-confidence candidates queue. Use `strict` to queue all new
writes. Approval does not widen a token's scope.

`sync_delta` provides an incremental feed. The adapter/client must drain its
pages, acknowledge progress, poll again and inject context. Installing a
template does not perform those operations or capture every conversation.
The Sync page cannot show a polling client that was never implemented.

The default extractor is a regex stub. Optional Ollama requires an explicitly
configured local model; its `memory_capture` facts always queue for review.
It is not needed for ordinary saves or handoffs. OpenAI extraction remains
unimplemented; ML triage and the wiki graph are separate optional features.
See [extraction setup](docs/EXTRACTION.md).

## 6. Persistence and backups

- `porter-db`: PostgreSQL memories, OAuth and audit data.
- `porter-data`: settings, snapshots and signing keys.
- `porter-models`: downloaded model cache.

Keep `.env` private and stable. To stop without erasing data, use
`docker compose down`; **`docker compose down -v` deletes named volumes**.

For a portable memory backup, create a password-encrypted snapshot in the owner
panel and download it to your backup location. Keep the password separately and
test restoration in an isolated instance. Snapshots export selected active
memories; they do not back up the entire database, OAuth clients or deployment.

Full disaster recovery also needs a PostgreSQL backup, the `porter-data` volume
and protected configuration/keys. One binary-safe database export is:

```bash
docker compose exec -T db pg_dump -U porter -d porter -Fc -f /tmp/porter-backup.dump
docker compose cp db:/tmp/porter-backup.dump ../porter-backup.dump
```

This copies the backup outside the source checkout; choose a private destination
and protect it as private data. Use a new filename for subsequent backups.
Avoid piping binary `pg_dump -Fc` through PowerShell text redirection. A full
database restore changes instance state; rehearse against a separate database
before doing it on your working service.

## Troubleshooting

| Symptom | First check |
|---|---|
| API not ready | `docker compose ps`, logs for embedder/API, port collisions and first model download |
| Embedder unhealthy | Model download/network/cache and actual memory use; measured cache is about 3.8 GB, container limit 4 GB plus other services |
| 401 or expired token | Refresh for this server, update the token, restart/reconnect the tool |
| 403 | Check consented scopes and target visibility; do not bypass by handing out admin |
| No memory results | Active status, tags, correct server/port, same embedder and database |
| stdio exits | Python environment, package import, database URL and shared signing key |
| Import finds nothing | No matching native files is a valid result; inspect dry-run and path overrides |
| Panel login loops | Correct localhost port, setup token and browser cookie support |
| Host rejected (400) | Use an allowed localhost address or configure exact host/origin below |

Logs can contain paths and identifiers. Redact credentials and memory content
before sharing. Do not solve data or model errors by silently using an empty
in-memory repository.

## Host validation and reverse proxies

Defaults accept `localhost`, `127.0.0.1` and `[::1]`; other Host headers fail
before owner/auth routing. For a custom domain:

```dotenv
PORTER_ALLOWED_HOSTS=localhost,127.0.0.1,[::1],memory.example.com
PORTER_PUBLIC_ORIGIN=https://memory.example.com
```

Use exact hostnames without scheme, port or wildcard in the host list.
The public origin is the browser-facing scheme/host/optional port, without a
path or credentials. An authenticated TLS proxy must preserve Host. Bundled
commands disable forwarded-header trust; custom-domain requests need owner
login even through a loopback proxy. HTTPS origins mark owner cookies Secure.
The bundled setup is not a public multi-tenant hosting solution.

Native boolean settings such as `AUTH_ENABLED` and `PORTER_WIKI_ENABLED`
accept 1/0, true/false, yes/no and on/off; invalid values fail startup.
`PORTER_REPO` accepts only memory/postgres. Keep production authentication
enabled; use the explicit PostgreSQL backend.

## Repeatable maintainer acceptance

The [review report](docs/REVIEW.md) records the dated 589-test baseline and all
14 migrations. To rerun isolated service acceptance after a change:

```bash
python -m pip install -c requirements-lock.txt -r requirements-test.txt
python scripts/verify_deployment.py --keep-running
```

This uses Compose project `porter-review`, private `.env.review`, ports
55432/18001/18000 and deterministic hash embeddings. It verifies infrastructure,
not semantic model quality; no model download is required. Without
`--keep-running`, its containers stop and named volumes remain.

Real BGE-M3 model acceptance is separate. Optional fixture evaluation against
the normal embedder:

```bash
python scripts/eval_embeddings.py --embedders hash,http --http-url http://127.0.0.1:8001
```

Use a separate database or regenerate **every stored vector** before switching
embedding models. Equal dimensions do not make different models' vectors
compatible. Broader model comparisons are not an onboarding or alpha sign-off
requirement. Installed-tool conversations and browser acceptance remain the
human checks in [ACCEPTANCE.md](docs/ACCEPTANCE.md).
