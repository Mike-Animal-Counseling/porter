<p align="center">
  <a href="README.md"><b>English</b></a> &nbsp;|&nbsp; <a href="README.zh-CN.md">中文</a>
</p>

<h1 align="center">Porter</h1>
<p align="center"><i>Your memory. Your harnesses. You hold the keys.</i></p>
<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT"></a>
  <img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/docker-ready-blue.svg" alt="Docker ready">
</p>

**Porter** is a self-hosted memory and state layer for AI coding assistants. You approve every memory once — then any of your harnesses (Codex, Cursor, Claude Code, and 24 more) can semantically search it. Scoped OAuth, a human approval inbox, optional end-to-end encryption. Your data never leaves your machine.

[![Watch the demo](assets/poster.png)](assets/porter-promo.mp4)

## Quickstart

```bash
git clone https://github.com/Mike-Animal-Counseling/porter.git && cd porter
docker compose up -d          # API + Postgres/pgvector + web panel
# open http://127.0.0.1:8000/panel
```

Connect a harness: copy its template from [`clients/`](clients/), paste in your token, restart the harness. Full walkthrough: [`docs/USER_SETUP_CHECKLIST.md`](docs/USER_SETUP_CHECKLIST.md).

## Features

- **Approval inbox** — AI-written memories land in a queue; nothing goes live without your OK
- **MCP-native, 27 harnesses** — stdio + Streamable HTTP transports
- **Semantic search + dedup** — pgvector recall; near-duplicates flagged, never silently merged
- **Scoped OAuth 2.0** — per-harness tokens, per-tag read scopes, one-click revoke
- **Cross-harness sync** — write in A, readable in B within 60s; conflicts go to the inbox, never dropped
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

**Two memory domains.** Porter keeps your *personal* memories (your
preferences, health, finance, PII — sensitivity-gated, human-reviewed)
separate from each *project's* working memory (conventions, decisions,
codebase knowledge, task state — the luggage that follows you when you
switch harnesses mid-project). Project memories auto-approve under a
`project:<name>` scope; personal ones always queue. See SPECS.md §7.1.1.

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

v0.1.0 · 294 tests passing. A few items (real-Postgres migrations, embedding quality on real hardware, physical harness handshakes) still await verification on a real machine — tracked in [DEPLOY.md](DEPLOY.md).

## License

MIT — see [LICENSE](LICENSE).
