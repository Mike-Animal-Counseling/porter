"""Create a short-lived local panel link without exposing the administrator key.

Uses only Python's standard library. No browser is opened; paste the printed
one-time link into an existing browser. It expires within two minutes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
TOKEN_KEY = "PORTER_SETUP_TOKEN"
DEFAULT_URL = "http://127.0.0.1:8000"
MAX_RESPONSE_BYTES = 8192


class OpenPanelError(Exception):
    """A safe error identifier; never include an input secret or HTTP body."""


MESSAGES = {
    "zh": {
        "invalid_url": "请使用本机 Porter 地址，例如 http://127.0.0.1:8000。",
        "unreadable_config": "无法读取本机配置文件，请检查文件位置和读取权限。",
        "missing_key": "未找到本机管理密钥。请先完成 Porter 安装配置，或指定该服务的配置文件。",
        "invalid_config": "配置文件中的管理密钥格式不完整，请检查引号和换行。",
        "invalid_key": "管理密钥格式不受支持；仍可在解锁页面手动输入。",
        "wrong_key": "配置文件与正在运行的 Porter 不匹配，请使用启动该服务时的配置文件。",
        "redirect": "服务返回了跳转。为保护管理密钥，请核对本机地址后重试。",
        "not_supported": "此服务尚不支持便捷解锁。请更新并重启 Porter，或在页面手动解锁。",
        "rate_limited": "请求过于频繁，请稍后再试。",
        "unavailable": "Porter 尚未就绪。请先启动 Docker 和 Porter，再重新打开此入口。",
        "invalid_response": "未收到有效的解锁入口，请确认这个地址运行的是 Porter。",
        "interrupted": "已取消，没有打开浏览器。",
        "prompt": "把下面的一次性链接粘贴到你正在使用的浏览器，即可解锁 Porter：",
        "expiry": "链接有效期 {seconds} 秒，仅可使用一次，请勿分享。没有打开任何浏览器。",
    },
    "en": {
        "invalid_url": "Use a local Porter address, such as http://127.0.0.1:8000.",
        "unreadable_config": "Cannot read the local configuration. Check its location and permissions.",
        "missing_key": "No administrator key was found. Complete Porter setup or select this service's configuration file.",
        "invalid_config": "The administrator key has an incomplete value. Check its quotes and line breaks.",
        "invalid_key": "This key format is unsupported by the shortcut; use the manual unlock form.",
        "wrong_key": "The configuration does not match this Porter service. Select the file used to start it.",
        "redirect": "The service redirected the request. Check the local address before retrying.",
        "not_supported": "This service does not support quick unlock yet. Update and restart Porter, or unlock manually.",
        "rate_limited": "Too many requests. Please try again shortly.",
        "unavailable": "Porter is not ready. Start Docker and Porter, then run this shortcut again.",
        "invalid_response": "No valid unlock link was returned. Check that this address runs Porter.",
        "interrupted": "Cancelled. No browser was opened.",
        "prompt": "Paste this one-time link into the browser you already use to unlock Porter:",
        "expiry": "Valid for {seconds} seconds and one use. Keep it private. No browser was opened.",
    },
}


def local_url(value: str) -> str:
    """Accept one explicit loopback origin, never a credential-bearing URL."""
    try:
        if any(ord(character) <= 32 or ord(character) == 127 for character in value):
            raise ValueError
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.netloc.endswith(":")
        ):
            raise ValueError
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        host = "[::1]" if parsed.hostname == "::1" else parsed.hostname
        return f"{parsed.scheme}://{host}" + (f":{port}" if port is not None else "")
    except (ValueError, TypeError):
        raise OpenPanelError("invalid_url") from None


def _key_value(raw: str) -> str:
    value = raw.strip()
    if value.startswith(("'", '"')):
        quote = value[0]
        closing = value.find(quote, 1)
        if closing == -1:
            raise OpenPanelError("invalid_config")
        tail = value[closing + 1 :].strip()
        if tail and not tail.startswith("#"):
            raise OpenPanelError("invalid_config")
        return value[1:closing]
    return re.split(r"\s+#", value, maxsplit=1)[0].rstrip()


def administrator_key(env_file: Path, environment: Mapping[str, str] | None = None) -> str:
    """Read literal key=value data. An existing file wins over inherited state."""
    environment = os.environ if environment is None else environment
    try:
        content = env_file.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        value = environment.get(TOKEN_KEY, "")
    except (OSError, UnicodeError):
        raise OpenPanelError("unreadable_config") from None
    else:
        value = ""
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, raw = line.partition("=")
            if separator and key.strip() == TOKEN_KEY:
                value = _key_value(raw)
    if not value:
        raise OpenPanelError("missing_key")
    if (
        len(value) > 4096
        or not value.isascii()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise OpenPanelError("invalid_key")
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def browser_link(base_url: str, key: str, language: str) -> tuple[str, int]:
    """Exchange a local key for a disposable fragment, without proxying it."""
    base_url = local_url(base_url)
    request = Request(
        base_url + "/owner/browser-link",
        data=b"",
        headers={"X-Porter-Setup-Token": key, "Accept": "application/json"},
        method="POST",
    )
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=10) as response:
            if response.status != 200:
                raise OpenPanelError("invalid_response")
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        if error.code in {401, 403}:
            kind = "wrong_key"
        elif error.code == 404:
            kind = "not_supported"
        elif error.code == 429:
            kind = "rate_limited"
        elif 300 <= error.code < 400:
            kind = "redirect"
        else:
            kind = "unavailable"
        raise OpenPanelError(kind) from None
    except (URLError, OSError, UnicodeError, ValueError):
        raise OpenPanelError("unavailable") from None
    try:
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ValueError
        parsed = json.loads(payload)
        code, seconds = parsed["code"], parsed["expires_in"]
        if (
            not isinstance(code, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", code) is None
            or isinstance(seconds, bool)
            or not isinstance(seconds, int)
            or not 1 <= seconds <= 120
        ):
            raise ValueError
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise OpenPanelError("invalid_response") from None
    return base_url + "/owner/login?" + urlencode({"lang": language}) + "#" + urlencode(
        {"code": code}
    ), seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--url", help="local Porter origin (default: port 8000)")
    parser.add_argument("--lang", choices=["zh", "en"], default="zh")
    parser.add_argument(
        "--no-open", action="store_true", help="print the link without opening a browser (default)"
    )
    args = parser.parse_args(argv)
    messages = MESSAGES[args.lang]
    try:
        local_config = {}
        config_path = ROOT / ".porter-panel.local.json"
        if config_path.exists() and not (args.env_file and args.url):
            try:
                local_config = json.loads(config_path.read_text("utf-8-sig"))
                if not isinstance(local_config, dict) or any(
                    not isinstance(local_config.get(key, ""), str) for key in ("env_file", "url")
                ):
                    raise ValueError
            except (OSError, ValueError, UnicodeError):
                raise OpenPanelError("unreadable_config") from None
        base_url = local_url(args.url or local_config.get("url") or DEFAULT_URL)
        env_file = args.env_file or Path(local_config.get("env_file") or ROOT / ".env")
        if not env_file.is_absolute():
            env_file = ROOT / env_file
        key = administrator_key(env_file)
        link, seconds = browser_link(base_url, key, args.lang)
    except OpenPanelError as error:
        print(messages[str(error)], file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(messages["interrupted"], file=sys.stderr)
        return 130
    print(messages["prompt"])
    print(link)
    print(messages["expiry"].format(seconds=seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
