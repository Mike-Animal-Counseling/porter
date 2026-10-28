"""Owner sign-in presentation and short-lived, single-use browser links."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections.abc import Callable
from html import escape

from fastapi import Request
from fastapi.responses import HTMLResponse

LINK_SECONDS = 120
MAX_LINKS = 16
PRIVATE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; script-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
}


class OwnerBrowserLinks:
    """Bounded process-local codes; restarting the service invalidates them."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._codes: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _digest(code: str, secret: str) -> str:
        return hmac.new(secret.encode(), code.encode(), hashlib.sha256).hexdigest()

    def issue(self, secret: str) -> str:
        code = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            self._codes = {key: end for key, end in self._codes.items() if end > now}
            if len(self._codes) >= MAX_LINKS:
                self._codes.pop(next(iter(self._codes)))
            self._codes[self._digest(code, secret)] = now + LINK_SECONDS
        return code

    def consume(self, code: str, secret: str) -> bool:
        if not code or len(code) > 128:
            return False
        with self._lock:
            end = self._codes.pop(self._digest(code, secret), 0)
        return end > self._clock()


def login_language(request: Request, choice: str = "") -> str:
    selected = choice or request.query_params.get("lang", "")
    if selected in {"en", "zh"}:
        return selected
    return "zh" if request.headers.get("accept-language", "").lower().startswith("zh") else "en"


def render_login(lang: str, *, error: str = "", status_code: int = 200) -> HTMLResponse:
    lang = "zh" if lang == "zh" else "en"
    zh = lang == "zh"
    copy = {
        "title": "打开你的记忆库" if zh else "Open your memory library",
        "intro": "保存的项目约定和偏好，都在这里。解锁后即可查看和管理。"
        if zh
        else "Your project decisions and preferences live here. Unlock to view and manage them.",
        "local": "在运行 Porter 的电脑上" if zh else "On the computer running Porter",
        "open": "在 Porter 文件夹双击" if zh else "In the Porter folder, double-click",
        "link": "再打开窗口里的一次性链接。"
        if zh
        else "Then open the one-time link shown in its window.",
        "explain": "本机入口会读取已有设置，生成 2 分钟有效的安全链接。"
        if zh
        else "The local helper reads your saved settings and creates a secure link valid for 2 minutes.",
        "other": "macOS 或 Linux" if zh else "macOS or Linux",
        "manual": "我已有管理员密钥" if zh else "I have an administrator key",
        "key": "管理员密钥" if zh else "Administrator key",
        "help": "使用部署 Porter 时生成的管理密钥；可向部署这套服务的人索取。"
        if zh
        else "Use the administrator key created when Porter was set up, or ask the person who runs this service.",
        "unlock": "解锁记忆库" if zh else "Unlock memory library",
        "session": "此浏览器的解锁状态最多保留 12 小时。只在你信任的设备上解锁。"
        if zh
        else "This browser stays unlocked for up to 12 hours. Use a device you trust.",
        "opening": "正在安全打开记忆库…" if zh else "Opening your memory library…",
        "noscript": "一次性链接需要 JavaScript。你也可以使用管理员密钥解锁。"
        if zh
        else "One-time links need JavaScript. You can also unlock with your administrator key.",
    }
    errors = {
        "key": "密钥不匹配。请重新复制完整密钥，或运行本机入口生成新链接。"
        if zh
        else "That key did not match. Copy the complete key again, or create a new link with the local helper.",
        "link": "这个入口已过期、已使用或失效。请重新运行本机入口，打开新生成的链接。"
        if zh
        else "This link has expired, was already used or is invalid. Run the local helper again and open the new link.",
        "rate": "尝试次数过多。请稍后重试。"
        if zh
        else "Too many attempts. Please wait a little and try again.",
        "origin": "这个请求来自不同地址。请回到当前 Porter 页面后重试。"
        if zh
        else "This request came from a different address. Return to this Porter page and try again.",
    }
    alert = (
        f'<p id="owner-error" class="owner-error" role="alert">{escape(errors[error])}</p>'
        if error in errors
        else ""
    )
    en_current = ' aria-current="page"' if not zh else ""
    zh_current = ' aria-current="page"' if zh else ""
    manual_open = " open" if error == "key" else ""
    return HTMLResponse(
        f'''<!doctype html><html lang="{lang}"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light"><title>{copy["title"]} · Porter</title>
<link rel="stylesheet" href="/panel/static/css/tokens.css">
<link rel="stylesheet" href="/panel/static/css/app.css">
<link rel="stylesheet" href="/panel/static/css/owner-login.css?v=1">
<script src="/panel/static/js/owner-login.js?v=1" defer></script></head><body>
<main class="owner-login" data-ui>
<header class="owner-header"><span class="owner-brand">Porter</span>
<nav aria-label="Language"><a href="?lang=zh" lang="zh"{zh_current}>中文</a>
<a href="?lang=en" lang="en"{en_current}>English</a></nav></header>
<h1>{copy["title"]}</h1><p class="owner-intro">{copy["intro"]}</p>
{alert}<p id="owner-opening" role="status" hidden>{copy["opening"]}</p>
<section class="owner-local" aria-labelledby="owner-local-title">
<h2 id="owner-local-title">{copy["local"]}</h2>
<p>{copy["open"]} <strong>Open-Porter.cmd</strong>{"，" if zh else ". "}{copy["link"]}</p>
<p class="owner-help">{copy["explain"]}</p>
<details class="owner-platform"><summary>{copy["other"]}</summary>
<pre><code>python scripts/open_panel.py</code></pre></details></section>
<details class="owner-manual"{manual_open}><summary>{copy["manual"]}</summary>
<form method="post" action="/owner/login">
<input type="hidden" name="lang" value="{lang}">
<label class="f" for="owner-token">{copy["key"]}</label>
<input id="owner-token" name="token" type="password" autocomplete="current-password"
required aria-describedby="owner-key-help{" owner-error" if error == "key" else ""}" spellcheck="false"{" aria-invalid='true'" if error == "key" else ""}>
<p id="owner-key-help" class="owner-help">{copy["help"]}</p>
<button class="btn btn--primary" type="submit">{copy["unlock"]}</button></form></details>
<p class="owner-session">{copy["session"]}</p>
<noscript><p>{copy["noscript"]}</p></noscript>
<form id="owner-link-form" method="post" action="/owner/login" hidden>
<input type="hidden" name="lang" value="{lang}"><input id="owner-code" type="hidden" name="code"></form>
</main></body></html>''',
        status_code=status_code,
        headers=PRIVATE_HEADERS,
    )
