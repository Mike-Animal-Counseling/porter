<p align="center">
  <a href="README.md"><b>English</b></a> &nbsp;|&nbsp; <a href="README.zh-CN.md">中文</a>
</p>

<h1 align="center">Porter</h1>
<p align="center"><i>Your memory, packed. Carry it to any harness.</i></p>
<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT"></a>
  <img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/docker-ready-blue.svg" alt="Docker ready">
</p>

**Porter** is a self-hosted memory layer for AI coding assistants — the
porter's luggage for your AI tools. Every coding harness builds up memory:
your preferences, plus the working memory of each project (conventions,
decisions, codebase knowledge, AGENTS.md/CLAUDE.md-style instructions).
Today that memory is locked inside each tool. Switch from Claude Code to
Codex mid-project and you start from zero. Porter packs it all and carries
it with you.


## Two pieces of luggage

- **Personal** — your own memories: preferences, health, finance, PII.
  Sensitivity-gated, human-reviewed. Yours, everywhere.
- **Project** — a project's entire working memory: conventions, decisions,
  task state, codebase knowledge. Tagged `project:<name>`, auto-approved,
  and pulled by whichever harness you're in. This is the luggage that
  follows you across tools.

Approval keeps junk out, not you out: low-risk memories flow automatically,
only sensitive or conflicting ones wait for your OK. See SPECS.md §7.1.1.

## Quickstart

```bash
git clone https://github.com/Mike-Animal-Counseling/porter.git && cd porter
python scripts/setup_local.py # create stable random credentials
docker compose up --build -d  # API + Postgres/pgvector + web panel
# open http://127.0.0.1:8000/panel
```

Connect a harness using a template from [`clients/`](clients/). HTTP MCP needs
the server URL and a scoped token; stdio also needs the shared database,
signing secret, embedding service and repository path. Restart the tool after
merging its configuration. See [`docs/USER_SETUP_CHECKLIST.md`](docs/USER_SETUP_CHECKLIST.md).
Templates cover 27 harnesses; individual live handshakes still need verification.

## Features

- **Tiered approval** — low-risk memories auto-approve (audited, reversible); sensitive/conflicting ones queue for you
- **MCP-native, 27 harnesses** — stdio + Streamable HTTP transports
- **Cross-harness memory** — shared storage and project deltas via `sync_delta(project="…")`; adapters handle polling and context injection
- **Memory importers** — one-time ingest of each harness's native memory files (AGENTS.md, CLAUDE.md, `.cursorrules`, …)
- **Semantic search + dedup** — pgvector recall; near-duplicates flagged, never silently merged
- **Scoped OAuth 2.0** — per-harness tokens, per-tag read scopes, one-click revoke
- **E2EE memories** — XChaCha20-Poly1305; the server only ever sees ciphertext
- **Signed snapshots** — Ed25519-signed, AES-256-GCM-encrypted backup & restore
- **Bilingual panel** — English / 中文 toggle in Settings
- **Handoff briefing** — preview, copy or download project context at `/panel/handoff`

## How it works

```
┌─────────┐   MCP (scoped token)   ┌────────┐
│ harness │ ─────────────────────▶ │ Porter │ ──▶ Postgres + pgvector
│ Codex…  │ ◀───────────────────── │  API   │      (your machine)
└─────────┘   approved memories    └────────┘
```

## Docs

| Doc | What's inside |
|---|---|
| [DEPLOY.md](DEPLOY.md) | Docker setup, first boot, production notes |
| [docs/DEVELOPER.md](docs/DEVELOPER.md) | OAuth flow, MCP tools, Python/TypeScript clients |
| [SPECS.md](SPECS.md) | Full protocol spec, PRD §14 threat model |
| [SECURITY.md](SECURITY.md) | Trust assumptions, disclosure policy |
| [docs/USER_SETUP_CHECKLIST.md](docs/USER_SETUP_CHECKLIST.md) | Step-by-step: what you do on your machine |
| [CHANGELOG.md](CHANGELOG.md) | Release history |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Lightweight development setup and required checks |
| [docs/REPOSITORY_STANDARDS.md](docs/REPOSITORY_STANDARDS.md) | Packaging, Ruff, CI and dependency maintenance |

## Status

v0.1.0 · 589 tests passed with real Postgres and the running Docker acceptance stack (2026-10-20). OAuth, HTTP/stdio MCP, encrypted restore and container recreation were verified. BGE-M3 passed live service checks and a small bilingual retrieval evaluation. Codex CLI 0.160.0 parsed both connection templates; individual harness conversations and visual QA remain — see [DEPLOY.md](DEPLOY.md).

## License

MIT — see [LICENSE](LICENSE).
