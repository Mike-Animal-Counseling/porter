"""E2E acceptance demo (PRD §7 US1/US3, §8 F7).

Runs the full loop locally against the in-memory backend and prints each
step: harness A writes via MCP -> owner approves -> harness B recalls (US1)
-> B revoked -> B blocked (US3) -> Codex fixture import (F7).

    python scripts/demo_e2e.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from porter.app import create_app  # noqa: E402
from porter.config import settings  # noqa: E402
from porter.embeddings import HashEmbedder  # noqa: E402
from porter.importers import CodexImporter, run_import  # noqa: E402
from porter.mcp_server import MCPDeps, build_mcp_server  # noqa: E402
from porter.oauth_store import InMemoryOAuthStore  # noqa: E402
from porter.repositories import InMemoryMemoryRepository  # noqa: E402

REDIRECT = "http://localhost:9999/callback"


def step(n: int, title: str) -> None:
    print(f"\n=== [{n}] {title} ===")


def mint(client: TestClient, scopes: str, admin_token=None):
    reg = client.post(
        "/api/v1/clients",
        json={"name": "demo-cli", "redirect_uris": [REDIRECT]},
        headers={
            "X-Porter-Setup-Token": settings.setup_token,
            **({"Authorization": f"Bearer {admin_token}"} if admin_token else {}),
        },
    ).json()
    client.get(
        "/oauth/authorize",
        params={
            "client_id": reg["client_id"],
            "redirect_uri": REDIRECT,
            "scope": scopes,
            "state": "d",
        },
    )
    r = client.post(
        "/oauth/authorize",
        data={
            "client_id": reg["client_id"],
            "redirect_uri": REDIRECT,
            "state": "d",
            "decision": "approve",
            "granted_scopes": scopes.split(),
            "requested_scopes": scopes.split(),
        },
        follow_redirects=False,
    )
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    tok = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": reg["client_id"],
            "client_secret": reg["client_secret"],
        },
    ).json()
    return tok["access_token"], reg["client_id"], reg["client_secret"]


def main() -> None:
    repo = InMemoryMemoryRepository(HashEmbedder())
    store = InMemoryOAuthStore()
    app = create_app(repo, store, auth_enabled=True)
    client = TestClient(app)
    admin, _, _ = mint(client, "memory.admin")
    token_a, _, _ = mint(client, "memory.read memory.write", admin)
    token_b, cid_b, sec_b = mint(client, "memory.read memory.write", admin)

    def mcp(token):
        os.environ["PORTER_TOKEN"] = token
        return build_mcp_server(
            MCPDeps(
                repo_fn=lambda: repo,
                oauth_store_fn=lambda: store,
                revocation_cache_fn=lambda: app.state.revocation_cache,
                rate_limiter_fn=lambda: app.state.rate_limiter,
                auth_enabled_fn=lambda: True,
            )
        )

    async def call(server, tool, args):
        return await server.call_tool(tool, args)

    # -- US1: cross-harness recall -------------------------------------
    step(1, "Harness A writes a dietary preference via MCP")
    res = asyncio.run(
        call(
            mcp(token_a),
            "memory_add",
            {
                "content": "user avoids cilantro and is allergic to peanuts",
                "type": "preference",
                "scope_tags": ["dietary"],
            },
        )
    )
    mid = json.loads(res.content[0].text)["id"]
    print("added ->", mid, "(pending_review)")

    step(2, "Harness B searches BEFORE approval -> nothing (inbox rule)")
    res = asyncio.run(call(mcp(token_b), "memory_search", {"query": "restaurant food", "top_k": 5}))
    print("results:", json.loads(res.content[0].text)["results"])

    step(3, "Owner approves via REST")
    r = client.post(f"/api/v1/memories/{mid}/approve", headers={"Authorization": f"Bearer {admin}"})
    print("approve ->", r.status_code)

    step(4, "Harness B recalls it (US1)")
    res = asyncio.run(
        call(mcp(token_b), "memory_search", {"query": "what foods should I avoid", "top_k": 5})
    )
    hits = json.loads(res.content[0].text)["results"]
    print(f"{len(hits)} hit(s):", [h["content"][:60] for h in hits])
    assert any("cilantro" in h["content"] for h in hits), "US1 FAILED"
    print("US1 PASS: cross-harness recall works")

    # -- US3: revoke ----------------------------------------------------
    step(5, "Owner revokes harness B's client")
    r = client.post(
        "/oauth/revoke",
        data={"token": token_b, "client_id": cid_b, "client_secret": sec_b, "revoke_all": "true"},
    )
    print("revoke ->", r.json())

    step(6, "Harness B tries again -> blocked (US3)")
    try:
        asyncio.run(call(mcp(token_b), "memory_search", {"query": "x"}))
        print("US3 FAILED: revoked token still worked!")
        sys.exit(1)
    except Exception as exc:  # ToolError porter:token_revoked
        print("blocked as expected:", str(exc)[:80])
        print("US3 PASS: revocation kills access immediately")

    # -- F7: Codex import -----------------------------------------------
    step(7, "Import Codex fixture memories (read-only, pending inbox)")
    with tempfile.TemporaryDirectory() as tmp:
        mem = Path(tmp) / ".codex" / "memories"
        mem.mkdir(parents=True)
        (mem / "food.md").write_text(
            "---\ntitle: Snack prefs\ntags: dietary\n---\n\nLikes spicy food.\n"
        )
        (mem / "leak.md").write_text("note: api_key=sk-demo00000000000000000000\n")
        cands, note = CodexImporter(mem).scan()
        print(note)

        async def _imp():
            return await run_import(repo, cands)

        summary = asyncio.run(_imp())
        print("import summary:", {k: v for k, v in summary.items() if k != "imported_ids"})
        pending = asyncio.run(repo.list(status="pending", limit=100))
        leak = next(p for p in pending if p.source.get("imported_from", "").endswith("leak.md"))
        assert "sk-demo00000000000000000000" not in leak.content
        assert "[REDACTED]" in leak.content
        print("F7 PASS: imported to inbox, secrets scrubbed, source tagged")

    print("\nALL E2E CHECKS PASSED")


if __name__ == "__main__":
    main()
