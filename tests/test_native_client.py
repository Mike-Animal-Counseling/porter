"""Synthetic native configuration and OAuth lifecycle regression cases."""

import asyncio
import json
import time
from dataclasses import replace

import httpx2
import pytest

from porter import native_client, native_credentials
from porter.auto_memory import _project


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_install_uninstall_preserves_existing_handlers(tmp_path, client):
    settings_path = tmp_path / "client settings.json"
    config = tmp_path / "porter's config.json"
    existing = {
        "hooks": {
            "UserPromptSubmit": [
                {"matcher": "", "hooks": [{"type": "command", "command": "keep"}]}
            ],
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "other"}]}],
        },
        "otherSetting": True,
    }
    settings_path.write_text(json.dumps(existing), encoding="utf-8")
    assert native_client._edit_hooks(settings_path, client, config, install=True)
    assert native_client._has_hooks(settings_path, client, config)
    assert not native_client._edit_hooks(settings_path, client, config, install=True)
    assert native_client._edit_hooks(settings_path, client, config, install=False)
    assert json.loads(settings_path.read_text(encoding="utf-8")) == existing


def test_managed_binding_rejects_other_clients_projects_and_origins(tmp_path):
    config = tmp_path / "porter.json"
    origin = "http://127.0.0.1:8100"
    key = native_credentials.credential_key(config, "codex", tmp_path, origin, "synthetic")
    item = dict(
        root=str(tmp_path),
        client="codex",
        tag="demo",
        base_url=origin,
        client_id="synthetic",
        credential_key=key,
        recall=True,
    )

    def write(value):
        config.write_text(json.dumps({"projects": [value]}), encoding="utf-8")

    write(item)
    assert _project(config, str(tmp_path), "codex", managed=True).credential_key == key
    assert _project(config, str(tmp_path), "claude", managed=True) is None
    assert _project(config, str(tmp_path.parent), "codex", managed=True) is None
    write(item | {"paused": True})
    assert _project(config, str(tmp_path), "codex", managed=True) is None
    write(item | {"base_url": "https://example.com"})
    with pytest.raises(ValueError, match="metadata"):
        _project(config, str(tmp_path), "codex", managed=True)


def test_dsh_overlay_is_separate_and_contains_no_credential(tmp_path):
    profile = tmp_path / "profile.yml"
    profile.write_text("- id: already-installed\n", encoding="utf-8")
    overlay = tmp_path / "porter.patch.yml"
    config = tmp_path / "projects.json"
    assert native_client._edit_dsh(overlay, config, install=True)
    assert native_client._has_hooks(overlay, "dsh", config)
    assert not native_client._edit_dsh(overlay, config, install=True)
    command = json.loads(overlay.read_text(encoding="utf-8"))[0]["insert"][0]["config"]
    assert command["transport"] == "stdio"
    assert command["args"][-2:] == ["proxy", "dsh"]
    assert "headers" not in command and "env" not in command
    assert native_client._edit_dsh(overlay, config, install=False)
    assert not overlay.exists()
    with pytest.raises(ValueError):
        native_client._edit_dsh(profile, config, install=True)
    assert profile.read_text(encoding="utf-8") == "- id: already-installed\n"


def test_expired_credentials_rotate_and_survive_connect_failure(tmp_path, monkeypatch):
    config = tmp_path / "porter.json"
    pair = native_credentials.Credentials(
        "synthetic", "secret", "refresh-1", "access-1", 0, "memory.read.demo"
    )
    entries = {"key": pair}
    monkeypatch.setattr(native_credentials, "get_credentials", entries.__getitem__)
    monkeypatch.setattr(native_credentials, "save_credentials", entries.__setitem__)

    class Endpoint:
        online = False

        async def post(self, url, data):
            assert url == "http://127.0.0.1:8100/oauth/token"
            assert data == {
                "client_id": "synthetic",
                "client_secret": "secret",
                "grant_type": "refresh_token",
                "refresh_token": "refresh-1",
            }
            assert entries["key"].refresh_token == ""
            if not self.online:
                raise httpx2.ConnectError("offline")
            return httpx2.Response(
                200,
                request=httpx2.Request("POST", url),
                json={
                    "access_token": "access-2",
                    "refresh_token": "refresh-2",
                    "expires_in": 60,
                    "scope": "memory.read.demo",
                },
            )

    endpoint = Endpoint()

    async def exercise():
        with pytest.raises(httpx2.ConnectError):
            await native_credentials.access_token(endpoint, config, "key", "http://127.0.0.1:8100")
        assert entries["key"] == pair
        endpoint.online = True
        assert (
            await native_credentials.access_token(endpoint, config, "key", "http://127.0.0.1:8100")
            == "access-2"
        )
        assert entries["key"].refresh_token == "refresh-2"
        assert entries["key"].expires_at > time.time()
        assert (
            await native_credentials.access_token(endpoint, config, "key", "http://127.0.0.1:8100")
            == "access-2"
        )
        entries["key"] = replace(pair, refresh_token="")
        with pytest.raises(native_credentials.ConnectionError, match="interrupted"):
            await native_credentials.access_token(endpoint, config, "key", "http://127.0.0.1:8100")

    asyncio.run(exercise())
