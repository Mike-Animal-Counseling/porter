"""Shared origin checks: localhost gate + CSRF same-origin guard.

Used by the owner panel AND the OAuth consent endpoint. Rationale: the
localhost gate is IP-based, which a victim's *browser* satisfies
automatically — so without an Origin/Referer check, a malicious website
could forge cross-origin form POSTs (approve OAuth scopes, delete
memories, revoke clients...) while the user simply has the page open.
Browsers attach Origin/Referer on cross-origin navigations and form
POSTs, so we require those headers to match the request's origin exactly.
Absent headers (curl, direct navigation, same-origin HTMX without
Origin) are allowed: the localhost gate remains the primary defense,
this is the second layer.
"""

from __future__ import annotations

import hashlib
import time
from hmac import compare_digest
from hmac import new as hmac_new
from urllib.parse import urlsplit

from fastapi import Request

from .config import settings
from .errors import Unauthorized

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
LOCAL_URL_HOSTS = {"127.0.0.1", "::1", "localhost"}
OWNER_COOKIE = "porter_owner"
OWNER_SESSION_SECONDS = 12 * 60 * 60


def owner_session() -> str:
    expires = str(int(time.time()) + OWNER_SESSION_SECONDS)
    digest = hmac_new(
        settings.setup_token.encode(), ("owner:" + expires).encode(), hashlib.sha256
    ).hexdigest()
    return expires + "." + digest


def has_owner_access(request: Request) -> bool:
    host = request.client.host if request.client else ""
    if (host in LOCAL_HOSTS and request.url.hostname in LOCAL_URL_HOSTS) or (
        host == "testclient" and request.url.hostname == "testserver"
    ):
        return True
    # Docker port forwarding changes the TCP peer from loopback to a
    # container gateway. Authenticate that browser instead of trusting the
    # gateway or X-Forwarded-For headers.
    cookie = request.cookies.get(OWNER_COOKIE, "")
    try:
        expires, signature = cookie.split(".", 1)
        if int(expires) <= time.time():
            return False
        digest = hmac_new(
            settings.setup_token.encode(), ("owner:" + expires).encode(), hashlib.sha256
        ).hexdigest()
        return compare_digest(signature, digest)
    except (ValueError, TypeError):
        return False


def check_same_origin(request: Request) -> None:
    """Reject a different scheme, host or port in Origin/Referer."""
    for header in ("origin", "referer"):
        val = request.headers.get(header)
        if not val:
            continue
        try:
            origin = urlsplit(val)
            target = urlsplit(settings.public_origin or str(request.url))
            if (
                origin.scheme not in {"http", "https"}
                or not origin.hostname
                or origin.username is not None
                or origin.password is not None
            ):
                raise ValueError("invalid origin")
            origin_port = (
                origin.port
                if origin.port is not None
                else (443 if origin.scheme == "https" else 80)
            )
            target_port = (
                target.port
                if target.port is not None
                else (443 if target.scheme == "https" else 80)
            )
        except ValueError:
            raise Unauthorized("malformed request origin") from None
        if (origin.scheme, origin.hostname, origin_port) != (
            target.scheme,
            target.hostname,
            target_port,
        ):
            raise Unauthorized("cross-origin request rejected")


async def owner_only(request: Request) -> None:
    """Require native loopback ownership or a signed browser session."""
    if not has_owner_access(request):
        raise Unauthorized("owner access required; open /owner/login")
    check_same_origin(request)
