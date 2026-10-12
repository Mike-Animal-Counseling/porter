"""Shared origin checks: localhost gate + CSRF same-origin guard.

Used by the owner panel AND the OAuth consent endpoint. Rationale: the
localhost gate is IP-based, which a victim's *browser* satisfies
automatically — so without an Origin/Referer check, a malicious website
could forge cross-origin form POSTs (approve OAuth scopes, delete
memories, revoke clients...) while the user simply has the page open.
Browsers attach Origin/Referer on cross-origin navigations and form
POSTs, so we require those headers to be localhost when present.
Absent headers (curl, direct navigation, same-origin HTMX without
Origin) are allowed: the localhost gate remains the primary defense,
this is the second layer.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Request

from .errors import Unauthorized

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def check_same_origin(request: Request) -> None:
    """Raise Unauthorized if Origin/Referer is present and non-local."""
    for header in ("origin", "referer"):
        val = request.headers.get(header)
        if not val:
            continue
        if (urlsplit(val).hostname or "") not in LOCAL_HOSTS:
            raise Unauthorized("cross-origin request rejected")


async def owner_only(request: Request) -> None:
    """Localhost-only gate (defense in depth)."""
    host = request.client.host if request.client else ""
    if host not in LOCAL_HOSTS:
        raise Unauthorized("localhost only")
    check_same_origin(request)
