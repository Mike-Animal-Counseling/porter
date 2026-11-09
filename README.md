# Porter

**Carry project context between AI coding tools.**

[中文](README.zh-CN.md) · Alpha · MIT

Porter is a self-hosted memory service for AI coding assistants. Save a project decision in one tool and retrieve it in another through MCP. You own the database and control what each client can read or write.

## What it looks like

**Codex saves a project decision → Porter stores it → OpenCode retrieves it in a fresh conversation.** The [native-client test](docs/NATIVE_CLIENT_ACCEPTANCE.2026-10-22.md) verified that both tools saw the same memory ID.

- **Review writes.** Sensitive or conflicting candidates go to the review inbox. Strict mode queues every new write.
- **Limit access.** Give clients separate OAuth credentials with read, write, or tag-restricted read permissions. Revoke them individually.
- **Keep data local.** PostgreSQL and pgvector power search. The owner panel also exports handoff briefings and supports backups.

This alpha is designed for a single owner. Assistants call MCP tools explicitly. Standard memories are stored in plaintext on your server. See the [security policy](SECURITY.md) for deployment boundaries.

## Quickstart

Requires Git, Python 3.12+, and Docker with Compose 2.24.4+. The first launch downloads images and a model whose measured cache is about 3.8 GB.

```bash
git clone https://github.com/Mike-Animal-Counseling/porter.git
cd porter
python scripts/setup_local.py
docker compose up --build -d
docker compose ps
```

Open [the local panel](http://127.0.0.1:8000/panel) once the services are healthy. If prompted to unlock, use `Open-Porter.cmd` on Windows or run `python scripts/open_panel.py --lang en` on macOS/Linux.

Follow the [deployment guide](DEPLOY.md) to connect your first HTTP MCP client. The [connection checklist](docs/USER_SETUP_CHECKLIST.md) walks through a save and retrieval test.

## Documentation

- [Developer guide](docs/DEVELOPER.md) and [protocol specification](SPECS.md)
- [Acceptance criteria and test evidence](docs/ACCEPTANCE.md)
- [Contributing](CONTRIBUTING.md) and [maintainer guide](docs/MAINTAINERS.md)

[MIT license](LICENSE) · [Third-party components](THIRD_PARTY.md)
