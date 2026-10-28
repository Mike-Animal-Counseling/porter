"""Owner handoff workflow: privacy, escaping, preview and audit semantics."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from porter.app import create_app
from porter.embeddings import HashEmbedder
from porter.i18n import set_lang
from porter.oauth_store import InMemoryOAuthStore
from porter.repositories import InMemoryMemoryRepository


@pytest.fixture()
def panel(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTER_SETTINGS_PATH", str(tmp_path / "settings.json"))
    repo = InMemoryMemoryRepository(HashEmbedder())
    client = TestClient(create_app(repo, InMemoryOAuthStore(), auth_enabled=True))

    async def seed():
        for content, kind, tags, active in [
            ("Use pytest for demo", "project", ["project:demo"], True),
            ("Different project secret", "project", ["project:other"], True),
            ("Pending decision secret", "project", ["project:demo"], False),
            ("I prefer dark mode", "preference", ["coding_style"], True),
            ("</textarea><script>alert('memory')</script>", "project", ["project:demo"], True),
        ]:
            record, _ = await repo.create_pending(
                content=content, type=kind, scope_tags=tags, confidence=1, source={}
            )
            if active:
                await repo.approve(record.id)

    asyncio.run(seed())
    return client, repo


FORM = {"project": "demo", "from_harness": "codex", "to_harness": "cursor"}


def test_preview_filters_and_escapes_without_recording_a_switch(panel):
    client, repo = panel
    page = client.get("/panel/handoff")
    assert page.status_code == 200 and 'value="demo"' in page.text
    preview = client.post("/panel/handoff", data=FORM)
    assert preview.status_code == 200
    assert preview.headers["cache-control"] == "no-store"
    assert "Use pytest for demo" in preview.text and "I prefer dark mode" in preview.text
    assert "Different project secret" not in preview.text
    assert "Pending decision secret" not in preview.text
    assert "<script>alert" not in preview.text
    assert "&lt;/textarea&gt;" in preview.text
    assert not [
        event for event in asyncio.run(repo.audit_list()) if event.action == "harness_switch"
    ]


@pytest.mark.parametrize("language,marker", [("en", "Handoff briefing"), ("zh", "交接简报")])
def test_download_uses_language_and_records_switch(panel, language, marker):
    client, repo = panel
    set_lang(language)
    response = client.post("/panel/handoff/download", data=FORM)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["cache-control"] == "no-store"
    assert 'filename="porter-handoff.txt"' in response.headers["content-disposition"]
    assert marker in response.text
    assert (
        len([event for event in asyncio.run(repo.audit_list()) if event.action == "harness_switch"])
        == 1
    )


@pytest.mark.parametrize("path", ["/panel/handoff", "/panel/handoff/download"])
def test_handoff_rejects_cross_origin_and_invalid_inputs(panel, path):
    client, _ = panel
    assert (
        client.post(path, data=FORM, headers={"Origin": "https://evil.example"}).status_code == 401
    )
    assert client.post(path, data={**FORM, "project": " "}).status_code == 422
    assert client.post(path, data={**FORM, "project": "x" * 129}).status_code == 422


def test_handoff_requires_owner_when_accessed_remotely(panel):
    original, _ = panel
    remote = TestClient(original.app, client=("203.0.113.5", 4242))
    assert remote.get("/panel/handoff").status_code == 401
    assert remote.post("/panel/handoff/download", data=FORM).status_code == 401
