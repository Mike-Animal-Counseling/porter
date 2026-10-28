"""The local shortcut must never forward or print the long-lived owner key."""

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts import open_panel


@pytest.fixture(autouse=True)
def isolated_installation(monkeypatch, tmp_path):
    # Never consult the operator's real deployment pointer during helper tests.
    monkeypatch.setattr(open_panel, "ROOT", tmp_path)


@pytest.mark.parametrize(
    "value, normalized",
    [
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("http://LOCALHOST:18010/", "http://localhost:18010"),
        ("http://[::1]:8000", "http://[::1]:8000"),
        ("https://localhost", "https://localhost"),
    ],
)
def test_only_explicit_loopback_origins_are_supported(value, normalized):
    assert open_panel.local_url(value) == normalized


@pytest.mark.parametrize(
    "value",
    [
        "http://example.com:8000",
        "http://127.0.0.1.example.com",
        "http://127.1:8000",
        "http://0.0.0.0:8000",
        "http://192.168.1.1:8000",
        "http://user:secret@127.0.0.1:8000",
        "http://127.0.0.1:8000/panel",
        "http://127.0.0.1:8000?token=secret",
        "http://127.0.0.1:8000#secret",
        "http://127.0.0.1:",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://[invalid",
        "file:///tmp/porter",
        "http://127.0.0.1:8000\n",
    ],
)
def test_invalid_destinations_are_rejected_before_key_exchange(value):
    with pytest.raises(open_panel.OpenPanelError, match="invalid_url"):
        open_panel.local_url(value)


def test_config_file_is_literal_and_overrides_inherited_key(tmp_path):
    config = tmp_path / "compose.env"
    config.write_text(
        "\ufeffOTHER=$(never-execute)\nPORTER_SETUP_TOKEN = '$DO_NOT_EXPAND#literal' # local key\n",
        encoding="utf-8",
    )
    assert (
        open_panel.administrator_key(config, {"PORTER_SETUP_TOKEN": "old-preview-key"})
        == "$DO_NOT_EXPAND#literal"
    )


@pytest.mark.parametrize(
    "text, expected",
    [
        ('PORTER_SETUP_TOKEN="quoted-key"\n', "quoted-key"),
        ("PORTER_SETUP_TOKEN=plain-key # comment\n", "plain-key"),
        ("PORTER_SETUP_TOKEN=key#literal\n", "key#literal"),
        ("PORTER_SETUP_TOKEN=first\nPORTER_SETUP_TOKEN=last\n", "last"),
    ],
)
def test_supported_key_values(tmp_path, text, expected):
    config = tmp_path / "compose.env"
    config.write_text(text, encoding="utf-8")
    assert open_panel.administrator_key(config, {}) == expected


@pytest.mark.parametrize("text", ["OTHER=value\n", 'PORTER_SETUP_TOKEN=""\n'])
def test_existing_incomplete_file_cannot_fall_back_to_old_environment(tmp_path, text):
    config = tmp_path / "compose.env"
    config.write_text(text, encoding="utf-8")
    with pytest.raises(open_panel.OpenPanelError, match="missing_key"):
        open_panel.administrator_key(config, {"PORTER_SETUP_TOKEN": "old-preview-key"})


@pytest.mark.parametrize("text", ['PORTER_SETUP_TOKEN="unfinished', 'PORTER_SETUP_TOKEN="x"bad'])
def test_malformed_quoted_values_fail_without_exposing_them(tmp_path, text):
    config = tmp_path / "compose.env"
    config.write_text(text, encoding="utf-8")
    with pytest.raises(open_panel.OpenPanelError, match="invalid_config"):
        open_panel.administrator_key(config, {})


def test_environment_is_a_fallback_only_when_file_does_not_exist(tmp_path):
    assert (
        open_panel.administrator_key(
            tmp_path / "absent.env", {"PORTER_SETUP_TOKEN": "installed-owner-key"}
        )
        == "installed-owner-key"
    )
    with pytest.raises(open_panel.OpenPanelError, match="invalid_key"):
        open_panel.administrator_key(
            tmp_path / "absent.env", {"PORTER_SETUP_TOKEN": "secret\r\nInjected: value"}
        )


class Response(io.BytesIO):
    status = 200


def test_exchange_uses_header_and_prints_only_disposable_fragment(monkeypatch, tmp_path, capsys):
    config = tmp_path / "compose.env"
    secret = "private-owner-sentinel"
    config.write_text("PORTER_SETUP_TOKEN=" + secret, encoding="utf-8")
    disposable = "R" * 43
    captured = {}

    class Opener:
        def open(self, request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response(json.dumps({"code": disposable, "expires_in": 120}).encode())

    def build(*handlers):
        captured["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(open_panel, "build_opener", build)
    assert open_panel.main(["--env-file", str(config), "--no-open", "--lang", "en"]) == 0
    out = capsys.readouterr()
    assert secret not in out.out + out.err
    request = captured["request"]
    assert request.full_url == "http://127.0.0.1:8000/owner/browser-link"
    assert request.get_method() == "POST"
    assert request.get_header("X-porter-setup-token") == secret
    assert secret.encode() not in request.data
    proxy = next(
        handler for handler in captured["handlers"] if isinstance(handler, open_panel.ProxyHandler)
    )
    assert proxy.proxies == {}
    link = next(line for line in out.out.splitlines() if line.startswith("http"))
    parts = urlsplit(link)
    assert parts.path == "/owner/login"
    assert parse_qs(parts.query) == {"lang": ["en"]}
    assert parse_qs(parts.fragment) == {"code": [disposable]}
    assert disposable not in parts.query


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"[]",
        b"null",
        b"{}",
        json.dumps({"code": "https://remote.example", "expires_in": 120}).encode(),
        json.dumps({"code": "x" * 43, "expires_in": 121}).encode(),
        json.dumps({"code": "x" * 43, "expires_in": True}).encode(),
        json.dumps({"code": "x" * 43, "expires_in": 0}).encode(),
        b"x" * (open_panel.MAX_RESPONSE_BYTES + 1),
    ],
)
def test_malformed_server_response_is_not_a_browser_destination(monkeypatch, payload):
    class Opener:
        def open(self, request, timeout):
            return Response(payload)

    monkeypatch.setattr(open_panel, "build_opener", lambda *args: Opener())
    with pytest.raises(open_panel.OpenPanelError, match="invalid_response"):
        open_panel.browser_link(open_panel.DEFAULT_URL, "owner-key", "zh")


@pytest.mark.parametrize(
    "error, expected",
    [
        (HTTPError("secret-url", 401, "private-owner-sentinel", {}, None), "wrong_key"),
        (HTTPError("secret-url", 404, "private-owner-sentinel", {}, None), "not_supported"),
        (HTTPError("secret-url", 429, "private-owner-sentinel", {}, None), "rate_limited"),
        (HTTPError("secret-url", 302, "private-owner-sentinel", {}, None), "redirect"),
        (HTTPError("secret-url", 500, "private-owner-sentinel", {}, None), "unavailable"),
        (URLError("private-owner-sentinel"), "unavailable"),
    ],
)
def test_connection_errors_never_echo_secrets_or_tracebacks(
    monkeypatch, tmp_path, capsys, error, expected
):
    config = tmp_path / "compose.env"
    config.write_text("PORTER_SETUP_TOKEN=private-owner-sentinel", encoding="utf-8")

    class Opener:
        def open(self, request, timeout):
            raise error

    monkeypatch.setattr(open_panel, "build_opener", lambda *args: Opener())
    assert open_panel.main(["--env-file", str(config), "--lang", "en"]) == 1
    output = capsys.readouterr()
    assert "private-owner-sentinel" not in output.err + output.out
    assert "secret-url" not in output.err + output.out
    assert "Traceback" not in output.err
    assert open_panel.MESSAGES["en"][expected] in output.err


def test_redirect_is_not_followed_even_if_environment_has_a_proxy(monkeypatch):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append((self.path, self.headers.get("X-Porter-Setup-Token")))
            self.send_response(302)
            self.send_header("Location", "/should-not-receive-key")
            self.end_headers()

        def do_GET(self):
            calls.append((self.path, self.headers.get("X-Porter-Setup-Token")))
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    try:
        with pytest.raises(open_panel.OpenPanelError, match="redirect"):
            open_panel.browser_link(
                "http://127.0.0.1:" + str(server.server_port), "local-only-secret", "zh"
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert calls == [("/owner/browser-link", "local-only-secret")]


def test_local_install_pointer_is_used_without_exposing_key(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(open_panel, "ROOT", tmp_path)
    (tmp_path / "custom.env").write_text("PORTER_SETUP_TOKEN=private-local-key", "utf-8")
    (tmp_path / ".porter-panel.local.json").write_text(
        json.dumps({"env_file": "custom.env", "url": "http://127.0.0.1:18020"}), "utf-8"
    )

    def exchange(base, key, language):
        assert base == "http://127.0.0.1:18020" and key == "private-local-key"
        return base + "/owner/login#code=" + "x" * 43, 120

    monkeypatch.setattr(open_panel, "browser_link", exchange)
    assert open_panel.main([]) == 0
    assert "private-local-key" not in capsys.readouterr().out


def test_explicit_configuration_overrides_local_pointer(monkeypatch, tmp_path):
    monkeypatch.setattr(open_panel, "ROOT", tmp_path)
    (tmp_path / ".porter-panel.local.json").write_text("invalid-json", "utf-8")
    config = tmp_path / "selected.env"
    config.write_text("PORTER_SETUP_TOKEN=selected-key", "utf-8")
    monkeypatch.setattr(open_panel, "browser_link", lambda *args: ("http://127.0.0.1/#code=x", 120))
    assert open_panel.main(["--env-file", str(config), "--url", "http://127.0.0.1:8000"]) == 0
