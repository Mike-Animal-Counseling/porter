"""Opt-in live model check; uses only the synthetic transcript below."""

import asyncio
import json
import os

import pytest

from porter.auth import RevocationCache
from porter.mcp_server import MCPDeps, build_mcp_server
from porter.oauth_store import InMemoryOAuthStore
from porter.ratelimit import TokenBucket


def test_live_ollama_capture_stays_pending(repo, monkeypatch):
    url, model = os.getenv("PORTER_TEST_OLLAMA_URL"), os.getenv("PORTER_TEST_OLLAMA_MODEL")
    if not url or not model:
        pytest.skip("local Ollama acceptance model is not configured")
    monkeypatch.setenv("PORTER_OLLAMA_URL", url)
    monkeypatch.setenv("PORTER_OLLAMA_MODEL", model)
    monkeypatch.setenv("PORTER_LLM_TIMEOUT", "90")
    server = build_mcp_server(
        MCPDeps(
            repo_fn=lambda: repo,
            oauth_store_fn=lambda: InMemoryOAuthStore(),
            revocation_cache_fn=lambda: RevocationCache(),
            rate_limiter_fn=lambda: TokenBucket(100),
            auth_enabled_fn=lambda: False,
            llm_extractor_kind_fn=lambda: "ollama",
        )
    )

    async def run():
        result = await server.call_tool(
            "memory_capture",
            {
                "conversation_text": "User: I prefer dark mode. Assistant: Understood.",
                "source_tags": ["project:synthetic-review"],
            },
        )
        payload = json.loads(result.content[0].text)
        assert payload["facts"] and all(f["status"] == "pending_review" for f in payload["facts"])
        records = await repo.list(status="pending")
        assert records and all(
            r.confidence == 0.7 and r.source["extractor"] == "ollama" for r in records
        )
        assert await repo.list(status="active") == []

    asyncio.run(run())
