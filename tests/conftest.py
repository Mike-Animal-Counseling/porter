"""Shared fixtures: FastAPI TestClient over the in-memory repository.

W1 tests run with auth DISABLED (owner context, pre-OAuth behavior).
OAuth tests (test_oauth.py) define their own fixtures with auth enabled.
"""

import pytest
from fastapi.testclient import TestClient

from porter.app import create_app
from porter.config import settings
from porter.embeddings import HashEmbedder
from porter.oauth_store import InMemoryOAuthStore
from porter.repositories import InMemoryMemoryRepository


@pytest.fixture(autouse=True)
def trusted_test_host(monkeypatch):
    # TestClient's synthetic hostname is not trusted in production.
    monkeypatch.setattr(settings, "allowed_hosts", [*settings.allowed_hosts, "testserver"])


@pytest.fixture()
def repo() -> InMemoryMemoryRepository:
    return InMemoryMemoryRepository(HashEmbedder())


@pytest.fixture()
def oauth_store() -> InMemoryOAuthStore:
    return InMemoryOAuthStore()


@pytest.fixture()
def client(repo: InMemoryMemoryRepository, oauth_store: InMemoryOAuthStore) -> TestClient:
    return TestClient(create_app(repo, oauth_store, auth_enabled=False))
