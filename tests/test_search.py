"""Semantic search recall + untrusted_data marking + duplicate detection."""
from fastapi.testclient import TestClient


def _write_approve(client: TestClient, content: str, tags: list[str]) -> str:
    r = client.post("/api/v1/memories", json={
        "content": content, "type": "preference",
        "scope_tags": tags, "source": {"client_id": "test"},
    })
    assert r.status_code == 202
    mid = r.json()["id"]
    r = client.post(f"/api/v1/memories/{mid}/approve")
    assert r.status_code == 200
    return mid


def test_search_recalls_semantically_related(client: TestClient):
    mid = _write_approve(client, "User does not eat cilantro and is allergic to peanuts", ["dietary"])
    _write_approve(client, "User prefers pnpm over npm for JS projects", ["coding_style"])
    _write_approve(client, "User's favorite editor theme is Catppuccin Mocha", ["workflow"])

    r = client.post("/api/v1/memories/search",
                    json={"query": "what foods should I avoid?", "top_k": 5})
    assert r.status_code == 200
    results = r.json()["results"]
    assert results, "expected at least one hit"
    assert results[0]["id"] == mid, f"dietary memory should rank first, got {results[0]['content'][:40]}"


def test_search_tag_filter(client: TestClient):
    _write_approve(client, "User does not eat cilantro", ["dietary"])
    _write_approve(client, "User prefers pnpm over npm", ["coding_style"])

    r = client.post("/api/v1/memories/search",
                    json={"query": "preferences", "top_k": 5, "tags": ["coding_style"]})
    results = r.json()["results"]
    assert all("coding_style" in hit["scope_tags"] for hit in results)


def test_search_results_marked_untrusted(client: TestClient):
    _write_approve(client, "User does not eat cilantro", ["dietary"])
    r = client.post("/api/v1/memories/search", json={"query": "cilantro", "top_k": 5})
    for hit in r.json()["results"]:
        assert hit["untrusted_data"] is True  # PRD principle 5


def test_search_writes_audit_entry(client: TestClient):
    mid = _write_approve(client, "User does not eat cilantro", ["dietary"])
    client.post("/api/v1/memories/search", json={"query": "cilantro", "top_k": 5})
    r = client.get("/api/v1/audit-log", params={"limit": 10})
    reads = [e for e in r.json() if e["action"] == "read"]
    assert reads and mid in reads[0]["memory_ids"]


def test_duplicate_detection_on_write(client: TestClient):
    r1 = client.post("/api/v1/memories", json={
        "content": "User prefers pnpm over npm for JavaScript projects",
        "type": "preference", "scope_tags": ["coding_style"],
    })
    assert r1.status_code == 202
    assert r1.json()["duplicate_candidates"] == []

    # near-identical content -> flagged
    r2 = client.post("/api/v1/memories", json={
        "content": "User prefers pnpm over npm for JavaScript projects!",
        "type": "preference", "scope_tags": ["coding_style"],
    })
    assert r2.status_code == 202
    dups = r2.json()["duplicate_candidates"]
    assert dups, "expected duplicate candidate"
    assert dups[0]["id"] == r1.json()["id"]
    assert dups[0]["similarity"] > 0.92
