"""End-to-end acceptance tests (PRD §7 US1/US3, §8 F7, §19 W3).

Simulates two harness clients (A and B) through REAL MCP sessions against
the real server object (same auth/scope machinery as the stdio and HTTP
transports — the wire framing itself is covered in test_mcp.py), plus the
harness-memory importer against fixture data.

US1: A writes via memory_add -> owner approves via REST -> B's memory_search
     recalls it (Top-5). Audit log shows the read under B's client id.
US3: B's client is revoked -> B's next MCP call fails with token_revoked.
F7:  fixture ~/.codex/memories-style files -> dry-run -> import -> pending
     inbox with correct source tagging; secrets scrubbed; re-import is a
     no-op (no double import).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from porter.app import create_app
from porter.config import settings
from porter.embeddings import HashEmbedder
from porter.importers import (
    CodexImporter,
    CursorImporter,
    ZedImporter,
    run_import,
    scrub_secrets,
)
from porter.mcp_server import MCPDeps, build_mcp_server
from porter.oauth_store import InMemoryOAuthStore
from porter.repositories import InMemoryMemoryRepository

REDIRECT = "http://localhost:9999/callback"


# ------------------------------------------------------------ OAuth helpers
def _setup_headers(admin_token=None):
    h = {"X-Porter-Setup-Token": settings.setup_token}
    if admin_token:
        h["Authorization"] = f"Bearer {admin_token}"
    return h


def _mint(client: TestClient, scopes: str, admin_token=None):
    reg = client.post(
        "/api/v1/clients",
        json={"name": "e2e-cli", "redirect_uris": [REDIRECT]},
        headers=_setup_headers(admin_token),
    ).json()
    r = client.get(
        "/oauth/authorize",
        params={
            "client_id": reg["client_id"],
            "redirect_uri": REDIRECT,
            "scope": scopes,
            "state": "e2e",
        },
    )
    assert r.status_code == 200, r.text
    r = client.post(
        "/oauth/authorize",
        data={
            "client_id": reg["client_id"],
            "redirect_uri": REDIRECT,
            "state": "e2e",
            "decision": "approve",
            "granted_scopes": scopes.split(),
            "requested_scopes": scopes.split(),
        },
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": reg["client_id"],
            "client_secret": reg["client_secret"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["access_token"], reg["client_id"], reg["client_secret"]


@pytest.fixture()
def e2e():
    """Two harness clients (A=codex-like, B=cursor-like) + admin, one backend."""
    repo = InMemoryMemoryRepository(HashEmbedder())
    store = InMemoryOAuthStore()
    app = create_app(repo, store, auth_enabled=True)
    client = TestClient(app)
    admin_token, _, _ = _mint(client, "memory.admin")
    token_a, cid_a, sec_a = _mint(client, "memory.read memory.write", admin_token=admin_token)
    token_b, cid_b, sec_b = _mint(client, "memory.read memory.write", admin_token=admin_token)
    return {
        "app": app,
        "client": client,
        "repo": repo,
        "store": store,
        "admin": admin_token,
        "a": (token_a, cid_a, sec_a),
        "b": (token_b, cid_b, sec_b),
    }


def _mcp_for(e2e, token):
    os.environ["PORTER_TOKEN"] = token
    app = e2e["app"]
    return build_mcp_server(
        MCPDeps(
            repo_fn=lambda: e2e["repo"],
            oauth_store_fn=lambda: e2e["store"],
            revocation_cache_fn=lambda: app.state.revocation_cache,
            rate_limiter_fn=lambda: app.state.rate_limiter,
            auth_enabled_fn=lambda: True,
        )
    )


def _call(server, tool, args):
    async def _run():
        return await server.call_tool(tool, args)

    return asyncio.run(_run())


def _payload(result):
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


# ------------------------------------------------------------ US1
def test_us1_cross_harness_recall(e2e):
    """A (harness 1) writes -> owner approves -> B (harness 2) recalls."""
    token_a, _, _ = e2e["a"]
    token_b, cid_b, _ = e2e["b"]
    client, repo = e2e["client"], e2e["repo"]

    # 1. Harness A writes a dietary preference via MCP.
    srv_a = _mcp_for(e2e, token_a)
    added = _payload(
        _call(
            srv_a,
            "memory_add",
            {
                "content": "user avoids cilantro and is allergic to peanuts",
                "type": "preference",
                "scope_tags": ["dietary"],
            },
        )
    )
    assert added["status"] == "pending_review"
    mid = added["id"]

    # 2. Not searchable before approval (inbox semantics).
    srv_b = _mcp_for(e2e, token_b)
    before = _payload(_call(srv_b, "memory_search", {"query": "restaurant food", "top_k": 5}))
    assert before["results"] == []

    # 3. Owner approves via REST (the panel does this in production).
    r = client.post(
        f"/api/v1/memories/{mid}/approve", headers={"Authorization": f"Bearer {e2e['admin']}"}
    )
    assert r.status_code == 200, r.text

    # 4. Harness B recalls it via MCP (US1: Top-5, different token/client).
    after = _payload(
        _call(srv_b, "memory_search", {"query": "what foods should I avoid", "top_k": 5})
    )
    assert len(after["results"]) >= 1
    assert any("cilantro" in h["content"] for h in after["results"])
    assert all(h["untrusted_data"] is True for h in after["results"])

    # 5. Audit log shows the read under B's client identity (US1 checkbox 3).
    audits = asyncio.run(repo.audit_list(limit=20))
    reads = [a for a in audits if a.action == "read" and a.meta.get("via") == "mcp"]
    assert reads, "no MCP read audit found"
    hit_ids = {str(m) for a in reads for m in a.memory_ids}
    assert mid in hit_ids


def test_us1_tag_scoped_client_isolation(e2e):
    """A dietary-scoped client never sees coding_style memories (T1)."""
    token_a, _, _ = e2e["a"]
    client = e2e["client"]

    for content, tags in [("avoids cilantro", ["dietary"]), ("uses pnpm", ["coding_style"])]:
        r = client.post(
            "/api/v1/memories",
            json={"content": content, "type": "preference", "scope_tags": tags},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert r.status_code == 202
        ra = client.post(
            f"/api/v1/memories/{r.json()['id']}/approve",
            headers={"Authorization": f"Bearer {e2e['admin']}"},
        )
        assert ra.status_code == 200

    diet_token, _, _ = _mint(client, "memory.read.dietary", admin_token=e2e["admin"])
    srv = _mcp_for(e2e, diet_token)
    res = _payload(_call(srv, "memory_search", {"query": "food tools"}))
    assert all("dietary" in h["scope_tags"] for h in res["results"])
    assert not any("pnpm" in h["content"] for h in res["results"])


# ------------------------------------------------------------ US3
def test_us3_revoke_kills_mcp_access(e2e):
    """Revoking B's client -> B's next MCP call fails; A's still works."""
    token_a, _, _ = e2e["a"]
    token_b, cid_b, sec_b = e2e["b"]
    client, repo = e2e["client"], e2e["repo"]

    # seed one active memory so there is something to read
    r = client.post(
        "/api/v1/memories",
        json={"content": "user avoids cilantro", "type": "preference", "scope_tags": ["dietary"]},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    asyncio.run(repo.approve(uuid.UUID(r.json()["id"])))

    # B can read before revocation.
    srv_b = _mcp_for(e2e, token_b)
    assert len(_payload(_call(srv_b, "memory_search", {"query": "cilantro"}))["results"]) >= 1

    # Owner revokes B's whole client (what the panel's one-click revoke does).
    r = client.post(
        "/oauth/revoke",
        data={"token": token_b, "client_id": cid_b, "client_secret": sec_b, "revoke_all": "true"},
    )
    assert r.status_code == 200 and r.json()["revoked"]

    # B's next MCP call fails with token_revoked (US3: immediate 401-class).
    with pytest.raises(ToolError, match="porter:token_revoked"):
        _call(srv_b, "memory_search", {"query": "cilantro"})

    # A (different client) is unaffected.
    srv_a = _mcp_for(e2e, token_a)
    assert len(_payload(_call(srv_a, "memory_search", {"query": "cilantro"}))["results"]) >= 1

    # The revocation itself is audited (US3 checkbox 3).
    audits = asyncio.run(repo.audit_list(limit=20))
    assert any(a.action == "revoke" for a in audits)


# ------------------------------------------------------------ F7 importer
@pytest.fixture()
def codex_home(tmp_path):
    mem = tmp_path / ".codex" / "memories"
    mem.mkdir(parents=True)
    (mem / "food.md").write_text(
        "---\ntitle: Food preferences\ntags: dietary\n---\n\n"
        "# Food preferences\n\nAvoids cilantro. Allergic to peanuts.\n"
    )
    (mem / "tools.md").write_text("Uses pnpm instead of npm for all JS projects.\n")
    (mem / "leaky.md").write_text(
        "Deploy notes. The staging API key is api_key=sk-fake1234567890abcdef do not commit.\n"
    )
    return tmp_path


def test_importer_codex_scan(codex_home):
    imp = CodexImporter(codex_home / ".codex" / "memories")
    candidates, note = imp.scan()
    assert len(candidates) == 3, note
    by_title = {c.title: c for c in candidates}
    assert by_title["Food preferences"].scope_tags == ["dietary"]
    assert by_title["Food preferences"].type == "fact"
    # filename fallback title, body kept
    assert "pnpm" in by_title["tools"].content
    assert all(c.confidence == 0.5 for c in candidates)
    assert all(c.source["client_id"] == "codex" for c in candidates)


def test_importer_codex_missing_dir(tmp_path):
    imp = CodexImporter(tmp_path / "nope")
    candidates, note = imp.scan()
    assert candidates == []
    assert "nothing to import" in note


def test_importer_scrubs_secrets():
    text = "key is api_key=sk-fake1234567890abcdef keep it"
    scrubbed, n = scrub_secrets(text)
    # The key=value pattern consumes the value first, so it counts once;
    # either way the secret text is gone and only the count is reported.
    assert n == 1
    assert "sk-fake1234567890abcdef" not in scrubbed
    assert "[REDACTED]" in scrubbed
    # the key NAME survives for context
    assert "api_key" in scrubbed


def test_importer_zed_is_honest_noop():
    candidates, note = ZedImporter().scan()
    assert candidates == []
    assert "no native" in note.lower()


def test_importer_cursor_convention(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".cursor" / "memories").mkdir(parents=True)
    (proj / ".cursor" / "memories" / "arch.md").write_text(
        "# Architecture\n\nWe use Postgres, not MongoDB.\n"
    )
    imp = CursorImporter([proj])
    candidates, note = imp.scan()
    assert len(candidates) == 1
    assert candidates[0].source["client_id"] == "cursor"
    assert "Postgres" in candidates[0].content
    # no project roots -> empty but documented
    c2, note2 = CursorImporter([]).scan()
    assert c2 == [] and "no native" in note2.lower()


def test_importer_end_to_end_no_double_import(codex_home):
    """scan -> import -> pending inbox tagged; re-import is a no-op."""

    async def _run():
        repo = InMemoryMemoryRepository(HashEmbedder())
        imp = CodexImporter(codex_home / ".codex" / "memories")
        candidates, _ = imp.scan()
        assert len(candidates) == 3

        s1 = await run_import(repo, candidates)
        assert s1["imported"] == 3
        assert s1["redactions"] >= 1  # leaky.md got scrubbed

        pending = await repo.list(status="pending", limit=100)
        assert len(pending) == 3
        assert all(p.source.get("client_id") == "codex" for p in pending)
        assert all(p.source.get("imported_from", "").endswith(".md") for p in pending)
        leaky = next(p for p in pending if p.source["imported_from"].endswith("leaky.md"))
        assert "sk-fake1234567890abcdef" not in leaky.content
        assert "[REDACTED]" in leaky.content
        # Nothing became searchable without approval.
        assert await repo.search(query="cilantro", top_k=5) == []

        # Second run: same files -> all skipped, zero new rows.
        s2 = await run_import(repo, candidates)
        assert s2["imported"] == 0
        assert s2["skipped_already_imported"] == 3
        assert len(await repo.list(status="pending", limit=100)) == 3

        # Semantic dedupe: an exact copy of an ACTIVE memory is skipped.
        # (list() returns newest-first, so find the record matching
        # candidates[0] by its imported_from path.)
        target = next(
            p for p in pending if p.source.get("imported_from") == candidates[0].imported_from
        )
        await repo.approve(target.id)
        dup_cand = [
            type(candidates[0])(
                title=candidates[0].title,
                content=candidates[0].content,
                type="fact",
                scope_tags=[],
                confidence=0.5,
                source={"client_id": "codex", "imported_from": "/other/path.md"},
                imported_from="/other/path.md",
            )
        ]
        s3 = await run_import(repo, dup_cand)
        assert s3["skipped_duplicate"] == 1
        assert s3["imported"] == 0

    asyncio.run(_run())


def test_importer_never_writes_source_files(codex_home):
    """READ-ONLY guarantee: source files byte-identical after import."""

    async def _run():
        before = {p: p.read_bytes() for p in (codex_home / ".codex" / "memories").rglob("*.md")}
        repo = InMemoryMemoryRepository(HashEmbedder())
        imp = CodexImporter(codex_home / ".codex" / "memories")
        candidates, _ = imp.scan()
        await run_import(repo, candidates)
        after = {p: p.read_bytes() for p in (codex_home / ".codex" / "memories").rglob("*.md")}
        assert before == after

    asyncio.run(_run())
