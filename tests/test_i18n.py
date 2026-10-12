"""Panel i18n (zh/en) tests: key completeness, persistence, both-language render."""
from __future__ import annotations

import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

from porter import i18n
from porter.app import create_app
from porter.embeddings import HashEmbedder
from porter.i18n import (
    ACTION_LABEL,
    STATUS_LABEL,
    TYPE_LABEL,
    get_lang,
    set_lang,
    t,
)
from porter.oauth_store import InMemoryOAuthStore
from porter.repositories import InMemoryMemoryRepository
from porter.snapshots import InMemorySnapshotStore


@pytest.fixture()
def langfile(tmp_path, monkeypatch):
    """Hermetic settings path for language persistence tests."""
    p = tmp_path / "porter-settings.json"
    monkeypatch.setenv("PORTER_SETTINGS_PATH", str(p))
    return p


@pytest.fixture()
def pclient(langfile):
    repo = InMemoryMemoryRepository(HashEmbedder())
    store = InMemoryOAuthStore()
    snap = InMemorySnapshotStore()
    app = create_app(repo, store, auth_enabled=False, snapshot_store=snap)
    return TestClient(app)


# ------------------------------------------------------------------ key completeness
def test_every_key_exists_in_both_languages():
    zh, en = i18n.STRINGS["zh"], i18n.STRINGS["en"]
    missing_in_en = [k for k in zh if k not in en]
    missing_in_zh = [k for k in en if k not in zh]
    assert not missing_in_en, f"missing in en: {missing_in_en}"
    assert not missing_in_zh, f"missing in zh: {missing_in_zh}"
    # No empty translations.
    for lang, table in i18n.STRINGS.items():
        for k, v in table.items():
            assert isinstance(v, str) and v.strip(), f"{lang}.{k} is empty"


def test_label_maps_cover_same_keys_in_both_languages():
    for name, table in (
        ("TYPE", TYPE_LABEL),
        ("STATUS", STATUS_LABEL),
        ("ACTION", ACTION_LABEL),
    ):
        assert set(table["zh"]) == set(table["en"]), f"{name} key mismatch"
        for lang in ("zh", "en"):
            for k, v in table[lang].items():
                assert v.strip(), f"{name}.{lang}.{k} is empty"


def test_t_falls_back_to_zh_and_raises_on_unknown():
    # t() never 500s the panel on a missing key: falls back to zh...
    assert t("nav_memories", "xx") == i18n.STRINGS["zh"]["nav_memories"]
    # ...but a totally unknown key is a loud programmer error.
    with pytest.raises(KeyError):
        t("no_such_key_xyz", "en")


# ------------------------------------------------------------------ persistence
def test_default_is_chinese(langfile):
    assert get_lang() == "zh"  # file absent -> default


def test_set_get_roundtrip(langfile):
    assert set_lang("en") == "en"
    assert get_lang() == "en"
    assert set_lang("zh") == "zh"
    assert get_lang() == "zh"
    data = json.loads(langfile.read_text(encoding="utf-8"))
    assert data == {"lang": "zh"}


def test_settings_file_is_not_world_readable(langfile):
    set_lang("en")
    mode = stat.S_IMODE(os.stat(langfile).st_mode)
    assert mode == 0o600, f"settings file mode is {oct(mode)}, want 0600"


def test_set_lang_rejects_unknown(langfile):
    with pytest.raises(ValueError):
        set_lang("fr")
    with pytest.raises(ValueError):
        set_lang("")


def test_corrupt_settings_file_falls_back_to_zh(langfile):
    langfile.write_text("{not json", encoding="utf-8")
    assert get_lang() == "zh"
    langfile.write_text('{"lang": "de"}', encoding="utf-8")
    assert get_lang() == "zh"  # unknown value -> default


# ------------------------------------------------------------------ panel rendering
PAGES = [
    "/panel/memories",
    "/panel/inbox",
    "/panel/clients",
    "/panel/audit",
    "/panel/snapshots",
    "/panel/settings",
]

ZH_MARKERS = {
    "/panel/memories": "记忆",
    "/panel/inbox": "待确认队列",
    "/panel/clients": "授权",
    "/panel/audit": "审计",
    "/panel/snapshots": "快照",
    "/panel/settings": "设置",
}
EN_MARKERS = {
    "/panel/memories": "Memories",
    "/panel/inbox": "Pending inbox",
    "/panel/clients": "Clients",
    "/panel/audit": "Audit",
    "/panel/snapshots": "Snapshots",
    "/panel/settings": "Settings",
}


def _assert_lang_page(client, path, lang, markers):
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    html = r.text
    assert f'<html lang="{"zh-CN" if lang == "zh" else "en"}">' in html
    assert markers[path] in html, f"{path} missing {lang} marker {markers[path]!r}"


@pytest.mark.parametrize("path", PAGES)
def test_pages_render_in_chinese_by_default(pclient, path):
    set_lang("zh")
    _assert_lang_page(pclient, path, "zh", ZH_MARKERS)


@pytest.mark.parametrize("path", PAGES)
def test_pages_render_in_english(pclient, path):
    set_lang("en")
    _assert_lang_page(pclient, path, "en", EN_MARKERS)


def test_settings_toggle_persists_across_restart(langfile, tmp_path):
    """POST the language form, then re-create the app: the new language sticks."""
    set_lang("zh")

    def make_client():
        repo = InMemoryMemoryRepository(HashEmbedder())
        store = InMemoryOAuthStore()
        snap = InMemorySnapshotStore()
        app = create_app(repo, store, auth_enabled=False, snapshot_store=snap)
        return TestClient(app)

    c1 = make_client()
    r = c1.post("/panel/settings/language", data={"lang": "en"},
                follow_redirects=False)
    assert r.status_code == 302, r.text
    assert get_lang() == "en"

    c2 = make_client()  # "restart": brand-new app objects, same settings file
    r = c2.get("/panel/settings")
    assert r.status_code == 200
    assert '<html lang="en">' in r.text
    assert "Tag management" in r.text
    assert "语言" not in r.text  # nav no longer Chinese

    # And back to Chinese.
    r = c2.post("/panel/settings/language", data={"lang": "zh"},
                follow_redirects=False)
    assert r.status_code == 302
    c3 = make_client()
    r = c3.get("/panel/settings")
    assert '<html lang="zh-CN">' in r.text
    assert "标签管理" in r.text


def test_settings_language_rejects_bad_value(pclient):
    set_lang("zh")
    r = pclient.post("/panel/settings/language", data={"lang": "fr"})
    # InvalidInput -> 4xx, and the stored language is untouched.
    assert 400 <= r.status_code < 500
    assert get_lang() == "zh"


def test_settings_page_shows_language_card(pclient):
    set_lang("zh")
    r = pclient.get("/panel/settings")
    assert r.status_code == 200
    assert "语言" in r.text
    assert 'data-set-lang="zh"' in r.text  # segmented control, not a select
    set_lang("en")
    r = pclient.get("/panel/settings")
    assert "Language" in r.text


def test_redirect_messages_are_translated(pclient):
    """Settings tag-add flash messages follow the language setting.

    Redesign: flash messages travel in the ?msg= query param and are rendered
    as toasts by panel.js (no inline banner), so assert on the redirect
    Location the browser would read.
    """
    from urllib.parse import unquote

    set_lang("en")
    r = pclient.post("/panel/settings/tags/add",
                     data={"tag": "parenting", "label": "Parenting"},
                     follow_redirects=False)
    assert r.status_code == 302
    assert "Tag added: parenting" in unquote(r.headers["location"])

    set_lang("zh")
    r = pclient.post("/panel/settings/tags/add",
                     data={"tag": "gaming", "label": "游戏"},
                     follow_redirects=False)
    assert r.status_code == 302
    assert "已添加标签 gaming" in unquote(r.headers["location"])
