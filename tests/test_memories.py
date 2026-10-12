"""CRUD + pending->approve lifecycle (PRD §8 F1, US2, US5)."""
from fastapi.testclient import TestClient

MEM = {
    "content": "User does not eat cilantro and is allergic to peanuts",
    "type": "preference",
    "scope_tags": ["dietary"],
    "confidence": 0.95,
    "source": {"client_id": "test"},
}


def _write(client: TestClient, body: dict | None = None) -> dict:
    r = client.post("/api/v1/memories", json=body or MEM)
    assert r.status_code == 202, r.text
    data = r.json()
    assert data["status"] == "pending_review"
    assert "id" in data
    return data


def test_write_goes_to_pending(client: TestClient):
    data = _write(client)
    r = client.get("/api/v1/memories", params={"status": "pending"})
    assert r.status_code == 200
    assert any(m["id"] == data["id"] for m in r.json())


def test_pending_not_searchable_until_approved(client: TestClient):
    _write(client)
    r = client.post("/api/v1/memories/search", json={"query": "cilantro peanuts", "top_k": 5})
    assert r.status_code == 200
    assert r.json()["results"] == []


def test_approve_flow(client: TestClient):
    data = _write(client)
    r = client.post(f"/api/v1/memories/{data['id']}/approve")
    assert r.status_code == 200
    assert r.json()["status"] == "active"
    # now searchable
    r = client.post("/api/v1/memories/search", json={"query": "cilantro peanuts", "top_k": 5})
    assert any(hit["id"] == data["id"] for hit in r.json()["results"])


def test_approve_missing_returns_404_json(client: TestClient):
    r = client.post("/api/v1/memories/00000000-0000-0000-0000-000000000000/approve")
    assert r.status_code == 404
    body = r.json()
    assert set(body.keys()) == {"error"}
    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"]


def test_hard_delete_removes_row_and_vector(client: TestClient):
    data = _write(client)
    client.post(f"/api/v1/memories/{data['id']}/approve")
    r = client.delete(f"/api/v1/memories/{data['id']}")
    assert r.status_code == 204
    # gone from list
    r = client.get("/api/v1/memories")
    assert all(m["id"] != data["id"] for m in r.json())
    # gone from search
    r = client.post("/api/v1/memories/search", json={"query": "cilantro peanuts", "top_k": 5})
    assert all(hit["id"] != data["id"] for hit in r.json()["results"])
    # delete twice -> 404
    r = client.delete(f"/api/v1/memories/{data['id']}")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_list_filters(client: TestClient):
    _write(client, {**MEM, "scope_tags": ["dietary"], "type": "preference"})
    _write(client, {**MEM, "content": "User prefers pnpm over npm",
                    "scope_tags": ["coding_style"], "type": "fact"})
    r = client.get("/api/v1/memories", params={"tag": "dietary"})
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["scope_tags"] == ["dietary"]
    r = client.get("/api/v1/memories", params={"type": "fact"})
    assert len(r.json()) == 1
