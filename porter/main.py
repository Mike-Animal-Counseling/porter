"""API entrypoint. `uvicorn porter.main:app` or `python -m porter.main`.

W1: in-memory repository by default (no Postgres needed for local dev).
Set PORTER_REPO=postgres + DATABASE_URL for the real Postgres+pgvector backend.

The Postgres pool is created inside the FastAPI lifespan (startup), never at
import time: creating an asyncpg pool with asyncio.run() and then serving from
uvicorn's event loop binds the pool to a dead loop and breaks all queries.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from .app import create_app
from .config import settings
from .embeddings import build_embedder
from .oauth_store import InMemoryOAuthStore, PostgresOAuthStore
from .repositories import InMemoryMemoryRepository, PostgresMemoryRepository
from .snapshots import InMemorySnapshotStore, PostgresSnapshotStore


def _use_postgres() -> bool:
    return os.getenv("PORTER_REPO", "memory") == "postgres"


def build_embedder_from_settings():
    # PORTER_EMBEDDER is canonical; EMBEDDER_KIND kept as a legacy alias.
    kind = os.getenv(
        "PORTER_EMBEDDER", os.getenv("EMBEDDER_KIND", settings.embedder_kind)
    )
    kwargs: dict = {}
    if kind == "http":
        kwargs["base_url"] = settings.embedder_url
    elif kind == "openai":
        kwargs["api_key"] = settings.openai_api_key
    return build_embedder(kind, **kwargs)


def build_wiki_from_settings(pool=None):
    """Wiki service for the LLM entity-graph layer (default OFF).

    Returns None unless PORTER_WIKI_ENABLED=1. In-memory repo mode gets an
    InMemoryWikiStore; postgres mode gets PostgresWikiStore over the pool.
    """
    if not settings.wiki_enabled:
        return None
    from .wiki import build_wiki_service

    return build_wiki_service(
        "postgres" if pool is not None else "memory",
        pool,
        enabled=True,
        extractor_kind=settings.llm_extractor_kind,
    )


@asynccontextmanager
async def lifespan(app):
    """Startup: migrations + pool (postgres) or nothing (in-memory)."""
    if _use_postgres():
        from .db import create_pool, run_migrations

        await run_migrations(settings.database_url)
        pool = await create_pool(settings.database_url)
        app.state.repo = PostgresMemoryRepository(build_embedder_from_settings(), pool)
        app.state.oauth_store = PostgresOAuthStore(pool)
        app.state.snapshot_store = PostgresSnapshotStore(pool)
        app.state.wiki_service = build_wiki_from_settings(pool)
        try:
            yield
        finally:
            await pool.close()
    else:
        yield


def build_app():
    repo = None if _use_postgres() else InMemoryMemoryRepository(
        build_embedder_from_settings()
    )
    oauth_store = None if _use_postgres() else InMemoryOAuthStore()
    snapshot_store = None if _use_postgres() else InMemorySnapshotStore()
    wiki_service = None if _use_postgres() else build_wiki_from_settings()
    return create_app(repo, oauth_store, lifespan=lifespan,
                      snapshot_store=snapshot_store,
                      wiki_service=wiki_service)


app = build_app()


if __name__ == "__main__":
    import uvicorn

    # P0: localhost only by default (see docker-compose.yml comment).
    uvicorn.run("porter.main:app", host="127.0.0.1", port=8000)
