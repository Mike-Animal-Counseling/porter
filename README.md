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

[![Watch the demo](assets/poster.png)](assets/porter-promo.mp4)

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
docker compose up -d          # API + Postgres/pgvector + web panel
# open http://127.0.0.1:8000/panel
```

Connect a harness: copy its template from [`clients/`](clients/), paste in your token, restart the harness. Full walkthrough: [`docs/USER_SETUP_CHECKLIST.md`](docs/USER_SETUP_CHECKLIST.md).

## Features

- **Tiered approval** — low-risk memories auto-approve (audited, reversible); sensitive/conflicting ones queue for you
- **MCP-native, 27 harnesses** — stdio + Streamable HTTP transports
- **Cross-harness sync** — write in A, readable in B within 60s; pull one project's memory with `sync_delta(project="…")`
- **Memory importers** — one-time ingest of each harness's native memory files (AGENTS.md, CLAUDE.md, `.cursorrules`, …)
- **Semantic search + dedup** — pgvector recall; near-duplicates flagged, never silently merged
- **Scoped OAuth 2.0** — per-harness tokens, per-tag read scopes, one-click revoke
- **E2EE memories** — XChaCha20-Poly1305; the server only ever sees ciphertext
- **Signed snapshots** — Ed25519-signed, AES-256-GCM-encrypted backup & restore
- **Bilingual panel** — English / 中文 toggle in Settings

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

## Status

v0.1.0 · 372 tests passing. A few items (real-Postgres migrations, embedding quality on real hardware, physical harness handshakes) still await verification on a real machine — tracked in [DEPLOY.md](DEPLOY.md).

## License

MIT — see [LICENSE](LICENSE).
