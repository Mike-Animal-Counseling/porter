"""stdio entrypoint for the Porter MCP server: `python -m porter.mcp_stdio`.

Transport for local CLIs (Codex, Cursor, ...). Auth: PORTER_TOKEN env var
holding a Porter access token (same tokens as the REST API; revoked tokens
are rejected identically).

Backend selection mirrors the REST API:
  PORTER_REPO=memory    in-memory repo (default; ephemeral — for tests/dev)
  PORTER_REPO=postgres  shared Postgres+pgvector (production; same DB as the
                        API, so memories are shared across transports)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

log = logging.getLogger("porter.mcp_stdio")


async def _build_deps():
    from .auth import RevocationCache
    from .config import env_bool, repository_kind, settings
    from .embeddings import build_embedder
    from .llm import build_extractor
    from .mcp_server import MCPDeps
    from .oauth_store import InMemoryOAuthStore, PostgresOAuthStore
    from .ratelimit import TokenBucket
    from .repositories import InMemoryMemoryRepository, PostgresMemoryRepository

    kind = repository_kind()
    auth_enabled = env_bool("AUTH_ENABLED", True)
    build_extractor(os.getenv("PORTER_LLM_EXTRACTOR", "stub"))

    embed_kind = os.getenv("PORTER_EMBEDDER", os.getenv("EMBEDDER_KIND", "hash"))
    kwargs = {}
    if embed_kind == "http":
        kwargs["base_url"] = settings.embedder_url
    elif embed_kind == "openai":
        kwargs["api_key"] = settings.openai_api_key
    embedder = build_embedder(embed_kind, **kwargs)
    pool = None
    if kind == "postgres":
        from .db import create_pool, run_migrations

        await run_migrations(settings.database_url)
        pool = await create_pool(settings.database_url)
        repo = PostgresMemoryRepository(embedder, pool)
        store = PostgresOAuthStore(pool)
    else:
        repo, store = InMemoryMemoryRepository(embedder), InMemoryOAuthStore()

    rate_limiter = TokenBucket(int(os.getenv("PORTER_RATE_LIMIT_PER_MINUTE", "60")))
    revocation_cache = RevocationCache(ttl_seconds=30)

    from .teams import InMemoryTeamStore, PostgresTeamStore

    team_store = PostgresTeamStore(pool) if pool is not None else InMemoryTeamStore()

    deps = MCPDeps(
        repo_fn=lambda: repo,
        oauth_store_fn=lambda: store,
        revocation_cache_fn=lambda: revocation_cache,
        rate_limiter_fn=lambda: rate_limiter,
        auth_enabled_fn=lambda: auth_enabled,
        team_store_fn=lambda: team_store,
        llm_extractor_kind_fn=lambda: os.getenv("PORTER_LLM_EXTRACTOR", "stub"),
    )
    return deps, pool


async def amain() -> int:
    from .mcp_server import build_mcp_server

    deps, pool = await _build_deps()
    server = build_mcp_server(deps)
    try:
        await server.run_stdio_async()
    finally:
        if pool is not None:
            await pool.close()
    return 0


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
