**English** | [中文](USER_SETUP_CHECKLIST.zh-CN.md)

# Porter Setup Checklist — Things Only You Can Do

> The code is 100% done (all 273 tests green), but a few things have to happen on **your machine** — I can't do those for you.
> Follow the steps in order and Porter will be up and running. Estimated 30–60 minutes.

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
  docker compose up -d
  ```
- [ ] Grab the one-time setup token from the logs (shown only once):
  ```bash
  docker compose logs porter | grep PORTER_SETUP_TOKEN
  ```
- [ ] Set the JWT signing key for production (without it, all tokens are invalidated on restart):
  ```bash
  # Add this line to your .env file (.env is already in .gitignore, so it won't end up in git)
  PORTER_JWT_SECRET=<output of openssl rand -hex 32>
  ```
  Then restart with `docker compose up -d`.

## 3. Open the panel and take a look around (5 min)

- [ ] Open http://127.0.0.1:8000/panel in your browser
- [ ] Settings page: confirm the language (中文/English), check the scope tag list
- [ ] Note: the panel only accepts local connections — that's by design. Not being reachable from the internet is expected behavior.

## 4. Connect your harnesses (2 min each — only connect the ones you use)

- [ ] Find the matching template in `clients/` (the filename is the harness name)
- [ ] Copy it to the **target path** noted in the template's header comment (e.g. `~/.codex/config.toml` for Codex)
- [ ] Replace `{{PORTER_TOKEN}}` with a real token (Panel → Authorization page → create a new client)
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
| 1 | Real Postgres migration (004/005) | My environment has no Docker/PG | After `docker compose up`, check the migration logs for errors |
| 2 | Real BGE-M3 embedding quality | The small VM can't run big models; tests used HashEmbedder | Search a semantically similar term and check whether recall is accurate |
| 3 | Real harness MCP handshake | Codex/Cursor aren't installed in this environment | After step 4, call `memory_search` once from your harness |
| 4 | Antigravity's Authorization header | The docs are unclear on it | If you can't connect, tell me and I'll build a passwordless local mode for it |
| 5 | ML triage model (zero-shot) latency/quality | Needs `pip install transformers torch` + ~1.1 GB download; VM can't do it | After deploy: `pip install transformers torch`, set `PORTER_TRIAGE_MODEL=zeroshot`, write a test memory and check the triage reason |
| 6 | Embedder A/B (qwen3/harrier vs bge-m3) | Needs GPU/CPU time + model downloads | Run `python scripts/eval_embeddings.py --embedders qwen3,bge-m3,harrier,embeddinggemma` and compare |

---

## Where to get help

- Deployment issues: see `DEPLOY.md`
- Integration development: see `docs/DEVELOPER.md`
- For errors, paste me the last 50 lines of `docker compose logs porter`
