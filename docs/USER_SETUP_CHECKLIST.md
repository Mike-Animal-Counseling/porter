**English** | [中文](USER_SETUP_CHECKLIST.zh-CN.md)

# Porter Setup Checklist — Things Only You Can Do

> The core implementation has automated coverage. Full deployment, real model
> quality and individual harness connections still need acceptance checks.
> Follow the steps in order; the initial model download can take several minutes.

---

## 1. Prepare the environment (5 min)

- [ ] Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Mac / Windows) or Docker Engine (Linux)
- [ ] Install Python 3.12+ (only used to run the importer and client scripts)
- [ ] Clone the repo:
  ```bash
  git clone https://github.com/Mike-Animal-Counseling/porter.git
  cd porter
  ```

## 2. Start the services (10 min)

- [ ] One command brings everything up (API + Postgres + pgvector + panel + embedding):
  ```bash
  python scripts/setup_local.py
  docker compose up --build -d
  ```
- [ ] Read `PORTER_SETUP_TOKEN` in `.env`; the setup script also creates a
  stable JWT signing key and database password. Existing `.env` files are
  preserved, so fill in missing values before starting.
- [ ] Install the local package: `python -m pip install -c requirements-lock.txt -e .`.

## 3. Open the panel and take a look around (5 min)

- [ ] Open http://127.0.0.1:8000/panel in your browser
- [ ] If redirected to `/owner/login`, unlock with the setup token from `.env`.
- [ ] Settings page: confirm the language (中文/English), check the scope tag list
- [ ] Note: the panel only accepts local connections — that's by design. Not being reachable from the internet is expected behavior.

## 4. Connect your harnesses (2 min each — only connect the ones you use)

- [ ] Find the matching template in `clients/` (the filename is the harness name)
- [ ] Copy it to the **target path** noted in the template's header comment (e.g. `~/.codex/config.toml` for Codex)
- [ ] Create a client in the panel, then complete the OAuth consent and code
  exchange in [DEPLOY.md](../DEPLOY.md) to obtain an access token. The client
  secret shown in the panel is not the access token.
- [ ] For Codex, prefer `clients/codex-http-config.toml`: set `PORTER_TOKEN`
  in the launch environment; no database or server signing key is needed.
- [ ] For stdio templates, replace `{{PORTER_TOKEN}}`, `{{PORTER_JWT_SECRET}}`, `DATABASE_URL` and
  `PYTHONPATH` using the token, matching `.env` credentials and repository path.
  Windows users should set `command` to `python` or their Python executable.
- [ ] Restart that harness (Windsurf/Tabnine must be fully restarted for MCP to load)

> Suggested order: Codex → Claude Code → Cursor (most-used first)

## 5. Import your existing memories (10 min)

- [ ] Dry-run first to see what would be imported (read-only — your harness files are not touched):
  ```bash
  python -m porter.import_cli --harness codex --dry-run
  ```
- [ ] If it looks good, run the real import (memories go into the pending-review queue; nothing takes effect automatically):
  ```bash
  python -m porter.import_cli --harness codex
  ```
- [ ] Repeat for your other harnesses: `--harness claude-code|cursor|windsurf|cline|roo|gemini|qwen|junie|droid|tabnine|devin|openhands|chatgpt|claude`
- [ ] Go to the panel's pending-review queue and approve each item (or approve all at once)

## 6. End-to-end verification (5 min)

- [ ] In harness A, say "remember: I'm allergic to peanuts"
- [ ] Approve it in the panel's pending-review queue
- [ ] In harness B, ask "what am I allergic to" — it should answer correctly
- [ ] On the panel's "Sync" page you should see both adapters online with their cursors

Once this step works, Porter is officially managing your memory.

---

## Honest list: things I haven't verified (needs confirmation on a real machine)

| # | Item | Why unverified | What you need to do |
|---|------|-----------|---------|
| 1 | Your deployment and data | All eleven migrations and isolated Docker acceptance passed locally on 2026-10-18 | Run `python scripts/verify_deployment.py`, then check your production deployment |
| 2 | BGE-M3 quality on your memories | CPU inference and a small bilingual fixture were tested; results are imperfect | Review `docs/embedding-eval.2026-10-18.json` and test your own queries |
| 3 | Real harness tool use | Codex 0.160.0 parsed both templates; individual conversations were not run | After step 4, call `memory_search` once from your harness |
| 4 | Antigravity's Authorization header | Not verified in a live install | Confirm it can send the bearer header before relying on that template |
| 5 | ML triage model (zero-shot) latency/quality | This optional backend was not evaluated | Install `.[ml]`, set `PORTER_TRIAGE_MODEL=zeroshot`, and check triage reasons on representative data |
| 6 | Embedder A/B (qwen3/harrier vs bge-m3) | Needs GPU/CPU time + model downloads | Run `python scripts/eval_embeddings.py --embedders qwen3,bge-m3,harrier,embeddinggemma` and compare |

---

## Where to get help

- Deployment issues: see `DEPLOY.md`
- Integration development: see `docs/DEVELOPER.md`
- For errors, inspect the last 50 lines of `docker compose logs api` (redact secrets).
