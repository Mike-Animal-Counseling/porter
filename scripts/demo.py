"""End-to-end demo: write -> approve -> semantic search recalls it.

Runs fully in-process (in-memory repository + HashEmbedder stub), so it works
without Docker or Postgres:

    cd ~/workspace/porter && .venv/bin/python scripts/demo.py
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from porter.app import create_app
from porter.embeddings import HashEmbedder
from porter.repositories import InMemoryMemoryRepository


def main() -> None:
    # W1 owner-context demo: auth disabled (pre-OAuth behavior).
    client = TestClient(create_app(
        InMemoryMemoryRepository(HashEmbedder()), auth_enabled=False))

    print("== 1. write memory (goes to pending review) ==")
    r = client.post("/api/v1/memories", json={
        "content": "User does not eat cilantro and is allergic to peanuts",
        "type": "preference",
        "scope_tags": ["dietary"],
        "confidence": 0.95,
        "source": {"client_id": "demo"},
    })
    print(r.status_code, json.dumps(r.json(), indent=2)[:300])
    mid = r.json()["id"]

    print("\n== 2. search BEFORE approve (pending is invisible) ==")
    r = client.post("/api/v1/memories/search",
                    json={"query": "what foods should I avoid?", "top_k": 3})
    print("hits:", len(r.json()["results"]))

    print("\n== 3. approve ==")
    r = client.post(f"/api/v1/memories/{mid}/approve")
    print(r.status_code, r.json()["status"])

    print("\n== 4. search AFTER approve (semantic recall) ==")
    r = client.post("/api/v1/memories/search",
                    json={"query": "what foods should I avoid?", "top_k": 3})
    for hit in r.json()["results"]:
        print(f"  score={hit['score']:.3f} untrusted={hit['untrusted_data']}")
        print(f"  content: {hit['content']}")

    print("\n== 5. duplicate detection ==")
    r = client.post("/api/v1/memories", json={
        "content": "User does not eat cilantro and is allergic to peanuts!",
        "type": "preference", "scope_tags": ["dietary"],
    })
    print("duplicate_candidates:", json.dumps(r.json()["duplicate_candidates"], indent=2))

    print("\n== 6. audit log ==")
    r = client.get("/api/v1/audit-log", params={"limit": 5})
    for e in r.json():
        print(f"  {e['action']:6s} memories={e['memory_ids']} scopes={e['scopes_used']}")

    print("\nDEMO OK")


if __name__ == "__main__":
    main()
