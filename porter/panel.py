"""Porter owner panel (PRD §8 F4, §17). Server-rendered HTML, HTMX + hand-written CSS.

Security model (read before touching):
- Mounted at /panel. The auth middleware treats /panel as a public-path
  prefix (no bearer token required) — see auth.PUBLIC_PATH_PREFIXES.
- Every route carries the panel_owner dependency. Native loopback IPs on
  a loopback hostname are trusted; container peers and custom domains need
  a signed owner session. An outer host allowlist rejects unknown domains.
  The bundled server disables proxy headers; they cannot grant ownership.
- CSRF: panel_owner also requires Origin/Referer to match the request's
  origin (or the explicitly configured public origin for TLS termination).
- The panel IS the owner interface: it implicitly holds memory.admin and
  mirrors the same repository/store calls + audit writes as the REST
  endpoints (each mutating handler notes which endpoint it mirrors). There is
  no separate unauthenticated data path — no backdoors.
- i18n: all user-facing panel strings go through porter.i18n.t(key, lang).
  The language preference is persisted via i18n.get_lang/set_lang (English
  default). API error strings are intentionally NOT translated.
- XSS: every interpolated value goes through esc(). No inline event
  handlers with interpolated data — destructive confirms use hx-confirm,
  dynamic behavior lives in /panel/static/js/panel.js.
"""

from __future__ import annotations

import csv
import html
import io
import json
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

from . import oauth as oauth_logic
from . import snapshots as snapshots_logic
from .config import settings
from .errors import InvalidInput, NotFound
from .exporter import build_export_payload
from .i18n import (
    ACTION_LABEL,
    STATUS_LABEL,
    TYPE_LABEL,
    get_lang,
    set_lang,
    t,
)
from .origin import OWNER_COOKIE, owner_only
from .repositories import DUPLICATE_THRESHOLD
from .schemas import MAX_CIPHERTEXT_LEN, MAX_CONTENT_LEN, _validate_tags

router = APIRouter(prefix="/panel")
# NOTE: the localhost gate lives on every route via _=Depends(panel_owner)
# (verified by test). It is deliberately per-route, not router-level, because
# panel_owner is defined below; moving it above would allow a router-level
# dependency instead.


# ------------------------------------------------------------------ helpers
def esc(v) -> str:
    return html.escape(str(v), quote=True)


def _is_hx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _hx_toast(msg: str, type_: str = "success") -> Response:
    """Empty 200 that pops a toast via the panel.js HX-Trigger listener."""
    return Response(
        "",
        headers={"HX-Trigger": json.dumps({"toast": {"type": type_, "msg": msg}})},
    )


async def panel_owner(request: Request) -> None:
    """Owner access + CSRF guard (see porter/origin.py)."""
    await owner_only(request)


def _deps(request: Request):
    repo = request.app.state.repo
    store = request.app.state.oauth_store
    snap = request.app.state.snapshot_store
    if repo is None or store is None or snap is None:
        from .errors import ServiceNotReady

        raise ServiceNotReady("panel backend is still starting up")
    return repo, store, snap


log = logging.getLogger("porter.panel")


async def _wiki_index(request: Request, rec) -> None:
    """Best-effort LLM-wiki indexing after an approval.

    Flag-gated (PORTER_WIKI_ENABLED=1): with the flag off, wiki_service is
    None and this is a no-op. A wiki failure must never fail the approval.
    """
    wiki = getattr(request.app.state, "wiki_service", None)
    if wiki is not None and getattr(wiki, "enabled", False) and rec is not None:
        try:
            await wiki.on_approved(rec)
        except Exception:  # noqa: BLE001 - prototype best-effort hook
            log.exception("wiki on_approved failed for %s", rec.id)


def _dt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    return esc(str(v)[:16])


def _rel(ts, lang: str) -> str:
    """Relative time for sync/last-seen. ts may be naive (UTC) or aware."""
    if ts is None:
        return t("sync_never", lang)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    s = (datetime.now(UTC) - ts).total_seconds()
    if s < 60:
        return t("sync_last_seen_now", lang)
    if s < 3600:
        return t("sync_last_seen_m", lang).format(n=int(s // 60))
    if s < 86400:
        return t("sync_last_seen_h", lang).format(n=int(s // 3600))
    return t("sync_last_seen_d", lang).format(n=int(s // 86400))


# ------------------------------------------------------------------ icons
# Inline SVG, 24x24 viewBox, stroke=currentColor, feather/lucide vocabulary.
# No emoji icons anywhere.
_ICONS: dict[str, str] = {
    "database": '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>',
    "inbox": '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    "refresh": '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
    "key": '<circle cx="7.5" cy="15.5" r="4.5"/><path d="M11 12 21 2"/><path d="M16 7l3 3"/>',
    "share": '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    "camera": '<path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    "search": '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
    "sun": '<circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>',
    "moon": '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
    "plus": '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
    "check": '<polyline points="20 6 9 17 4 12"/>',
    "x": '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    "copy": '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "trash": '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    "pencil": '<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
    "upload": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    "menu": '<line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/>',
    "alert": '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
    "lock": '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "zap": '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
    "activity": '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
    "file": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>',
    "chevron-down": '<polyline points="6 9 12 15 18 9"/>',
}


def icon(name: str, size: int = 16) -> str:
    """Inline SVG icon. Unknown names render nothing (never raw input)."""
    path = _ICONS.get(name)
    if path is None:
        return ""
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{path}</svg>'
    )


# ------------------------------------------------------------------ layout
_NAV = [
    ("memories", "nav_memories", "database", "/panel/memories"),
    ("inbox", "nav_inbox", "inbox", "/panel/inbox"),
    ("sync", "nav_sync", "refresh", "/panel/sync"),
    ("handoff", "nav_handoff", "share", "/panel/handoff"),
    ("clients", "nav_clients", "key", "/panel/clients"),
    ("shares", "nav_shares", "share", "/panel/shares"),
    ("audit", "nav_audit", "file", "/panel/audit"),
    ("snapshots", "nav_snapshots", "camera", "/panel/snapshots"),
    ("settings", "nav_settings", "settings", "/panel/settings"),
]


async def _layout(
    request: Request, active: str, title: str, body: str, lang: str, *, sub: str = ""
) -> str:
    from . import __version__

    repo, _store, _snap = _deps(request)
    # Inbox badge: pending count (cap the scan; the panel is localhost-only).
    pendings = await repo.list(status="pending", limit=1000)
    inbox_n = len(pendings)
    # Sync nav dot: amber if any adapter lags, green if adapters are fresh.
    sync_dot = ""
    try:
        cursors = await repo.list_cursors()
        _changes, head = await repo.get_changes(0, 1)
        now = datetime.now(UTC)
        lagging = False
        fresh = False
        for c in cursors:
            ls = c.get("last_seen")
            if ls is not None and ls.tzinfo is None:
                ls = ls.replace(tzinfo=UTC)
            if ls is not None and (now - ls).total_seconds() < 300:
                fresh = True
            if head - c.get("cursor", 0) > 0:
                lagging = True
        if lagging:
            sync_dot = '<span class="nav__dot nav__dot--warn"></span>'
        elif fresh:
            sync_dot = '<span class="nav__dot nav__dot--ok"></span>'
    except Exception:
        pass

    items = []
    for key, label_key, ic, url in _NAV:
        extra = ""
        if key == "inbox" and inbox_n:
            extra = f'<span class="nav__badge" id="nav-inbox-badge">{inbox_n}</span>'
        if key == "sync" and sync_dot:
            extra = sync_dot
        cls = "nav__item nav__item--active" if key == active else "nav__item"
        items.append(
            f'<a href="{url}" class="{cls}">{icon(ic)}<span>{esc(t(label_key, lang))}</span>{extra}</a>'
        )
    # Settings gets a separated group label.
    nav_html = "".join(items[:-1])
    nav_html += (
        f'<div class="nav__group-label">{esc(t("nav_group_system", lang))}</div>' + items[-1]
    )
    html_lang = "zh-CN" if lang == "zh" else "en"
    sub_html = f'<div class="page-head__meta">{sub}</div>' if sub else ""
    sign_out = (
        f'<form method="post" action="/owner/logout"><button class="btn btn--secondary" '
        f'type="submit">{esc(t("btn_sign_out", lang))}</button></form>'
        if request.cookies.get(OWNER_COOKIE)
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="{html_lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} — Porter</title>
<script>try{{var th=localStorage.getItem("porter-theme");if(!th&&matchMedia("(prefers-color-scheme: light)").matches)th="light";if(th==="light")document.documentElement.setAttribute("data-theme","light");}}catch(e){{}}</script>
<link rel="stylesheet" href="/panel/static/css/tokens.css">
<link rel="stylesheet" href="/panel/static/css/app.css">
<script src="/panel/static/htmx.min.js" defer></script>
<script src="/panel/static/js/panel.js" defer></script>
</head>
<body>
<div class="shell">
<aside class="sidebar" id="sidebar">
  <div class="brand"><div class="brand__name">Porter</div><div class="brand__sub">memory layer</div></div>
  <nav class="nav">{nav_html}</nav>
  <div class="sidebar__foot"><span class="version-pill">v{esc(__version__)}</span>{sign_out}</div>
</aside>
<div class="main">
  <header class="topbar">
    <button class="icon-btn sidebar-toggle" id="sidebar-toggle" aria-label="menu">{icon("menu")}</button>
    <div class="topbar__crumb">Porter <span class="muted">/</span> <strong>{esc(title)}</strong></div>
    <div class="topbar__spacer"></div>
    <form class="topbar__search" method="get" action="/panel/memories" role="search">
      {icon("search", 14)}
      <input type="search" name="q" placeholder="{esc(t("header_search_ph", lang))}" aria-label="{esc(t("header_search_ph", lang))}">
    </form>
    <div class="seg" role="group" aria-label="{esc(t("header_lang", lang))}">
      <button type="button" data-set-lang="zh" aria-pressed="{str(lang == "zh").lower()}">中文</button>
      <button type="button" data-set-lang="en" aria-pressed="{str(lang == "en").lower()}">EN</button>
    </div>
    <button class="icon-btn" id="theme-toggle" aria-label="{esc(t("header_theme", lang))}" aria-pressed="false">{icon("moon")}</button>
  </header>
  <main class="page">
    <div class="page-head"><h1>{esc(title)}</h1>{sub_html}</div>
    {body}
  </main>
  <div class="footer">Porter v{esc(__version__)} · {esc(t("footer_tagline", lang))}</div>
</div>
</div>
<div class="drawer-overlay" id="drawer-overlay"></div>
<aside class="drawer" id="drawer" aria-label="{esc(t("drawer_close", lang))}">
  <div class="drawer__head"><strong>{esc(title)}</strong>
    <button class="icon-btn" data-close-drawer aria-label="{esc(t("drawer_close", lang))}">{icon("x")}</button>
  </div>
  <div class="drawer__body" id="drawer-body"></div>
</aside>
<div id="toasts" aria-live="polite"></div>
</body>
</html>"""


# ------------------------------------------------------- shared fragments
def _tag_chips(tags) -> str:
    return "".join(f'<span class="tag-chip">{esc(t)}</span>' for t in (tags or []))


def _enc_badge(m, lang: str) -> str:
    """E2EE (P2): 'encrypted' badge. The panel never attempts decryption."""
    if not getattr(m, "encrypted", False):
        return ""
    return f' <span class="badge badge--neutral">{icon("lock", 12)} {esc(t("badge_encrypted", lang))}</span>'


def _type_label(tp: str, lang: str) -> str:
    return TYPE_LABEL[lang].get(tp, tp)


def _status_label(st: str, lang: str) -> str:
    return STATUS_LABEL[lang].get(st, st)


def _action_label(a: str, lang: str) -> str:
    return ACTION_LABEL[lang].get(a, a)


def _status_badge(status: str, lang: str) -> str:
    kind = {"active": "success", "pending": "warning", "archived": "neutral"}.get(status, "neutral")
    return (
        f'<span class="badge badge--{kind}"><span class="badge__dot"></span>'
        f"{esc(_status_label(status, lang))}</span>"
    )


def _copy_btn(value: str, lang: str) -> str:
    return (
        f'<button class="copy-btn" data-copy="{esc(value)}" '
        f'data-copied-msg="{esc(t("toast_copied", lang))}" '
        f'title="{esc(t("toast_copied", lang))}">{icon("copy", 13)}</button>'
    )


def _mem_row(m, lang: str, dup_note: str = "") -> str:
    src = (m.source or {}).get("client_id", "—")
    snippet = esc(m.content[:90]) + ("…" if len(m.content) > 90 else "")
    return (
        f"<tr><td style='max-width:340px'>"
        f"<a href='/panel/memories/{m.id}' hx-get='/panel/memories/{m.id}' "
        f"hx-target='#drawer-body' hx-swap='innerHTML' class='mem-snippet'>{snippet}</a>{dup_note}"
        f"<div style='margin-top:4px'>{_tag_chips(m.scope_tags)}</div></td>"
        f"<td>{esc(_type_label(m.type, lang))}</td>"
        f"<td>{_status_badge(m.status, lang)}{_enc_badge(m, lang)}</td>"
        f"<td class='mono muted'>{esc(src)}</td>"
        f"<td class='muted small mono'>{_dt(m.created_at)}</td></tr>"
    )


def _empty(icon_name: str, title: str, desc: str, cta: str = "") -> str:
    return f"""
<div class="empty">{icon(icon_name, 40)}
<div class="empty__title">{esc(title)}</div>
<div class="empty__desc">{esc(desc)}</div>{cta}</div>"""


async def _all_tags(repo, store) -> tuple[list[str], dict[str, str]]:
    """(all tag ids sorted, {tag: label})."""
    preset = dict(oauth_logic.PRESET_SCOPE_TAGS)
    custom = {t["tag"]: t["label"] for t in await store.list_custom_tags()}
    labels = {**preset, **custom}
    return sorted(labels), labels


# ------------------------------------------------------------------ memories
@router.get("/", response_class=HTMLResponse)
async def panel_index(request: Request, _=Depends(panel_owner)):
    return RedirectResponse("/panel/memories", status_code=302)


@router.get("/memories", response_class=HTMLResponse)
async def memories_page(
    request: Request,
    q: str = Query(default=""),
    mode: str = Query(default="text"),
    tag: str = Query(default=""),
    type: str = Query(default=""),
    client: str = Query(default=""),
    status: str = Query(default="active"),
    domain: str = Query(default=""),
    _=Depends(panel_owner),
):
    lang = get_lang()
    if domain not in ("", "personal", "project"):
        domain = ""
    repo, store, _snap = _deps(request)
    tags, _labels = await _all_tags(repo, store)
    tag_opts = "".join(
        f"<option value='{esc(tg)}' {'selected' if tg == tag else ''}>{esc(tg)}</option>"
        for tg in [""] + tags
    )
    type_opts = f"<option value=''>{esc(t('opt_all_types', lang))}</option>" + "".join(
        f"<option value='{tp}' {'selected' if tp == type else ''}>{esc(_type_label(tp, lang))}</option>"
        for tp in TYPE_LABEL[lang]
    )
    rows_html = await _mem_rows_html(
        repo,
        lang,
        q=q,
        mode=mode,
        tag=tag,
        type=type,
        client=client,
        status=status,
        domain=domain,
    )
    status_opts = "".join(
        f"<option value='{s}' {'selected' if s == status else ''}>{esc(_status_label(s, lang))}</option>"
        for s in ("active", "pending", "archived")
    )
    # Constellation data: up to 60 memories under the current filters.
    # Only id + short label + tags are embedded; labels are drawn with
    # canvas fillText (never innerHTML). Clicks reuse the HTMX drawer.
    graph_mems = await repo.list(
        status=status or None,
        tags=(tag,) if tag else (),
        type=(domain or type) or None,
        limit=60,
        query=q or None,
    )
    nodes = [
        {
            "id": str(m.id),
            "label": m.content[:24],
            "tags": list(m.scope_tags or []),
            "confidence": m.confidence,
        }
        for m in graph_mems
    ]
    nodes_json = esc(json.dumps(nodes, ensure_ascii=False))
    graph_empty = (
        ""
        if nodes
        else (
            f"<div class='empty'><div class='empty__desc'>"
            f"{esc(t('constel_empty', lang))}</div></div>"
        )
    )
    body = f"""
<div class="row" style="margin-bottom:16px;justify-content:space-between">
<div class="seg" role="group" aria-label="{esc(t("domain_toggle_label", lang))}">
<button type="button" data-domain-filter="" aria-pressed="{str(domain == "").lower()}">{esc(t("domain_all", lang))}</button>
<button type="button" data-domain-filter="personal" aria-pressed="{str(domain == "personal").lower()}">{esc(t("domain_personal", lang))}</button>
<button type="button" data-domain-filter="project" aria-pressed="{str(domain == "project").lower()}">{esc(t("domain_project", lang))}</button>
</div>
<div class="seg view-toggle" role="group" aria-label="{esc(t("view_toggle_label", lang))}">
<button type="button" data-mem-view="table" aria-pressed="true">{esc(t("view_table", lang))}</button>
<button type="button" data-mem-view="graph" aria-pressed="false">{esc(t("view_graph", lang))}</button>
</div></div>
<div id="mem-table-wrap">
<div class="card">
<form method="get" action="/panel/memories" id="mem-filter-form">
<div class="toolbar" id="mem-filters">
<input type="search" name="q" value="{esc(q)}" placeholder="{esc(t("search_placeholder", lang))}"
  hx-get="/panel/memories/rows" hx-trigger="input changed delay:300ms"
  hx-target="#mem-rows" hx-swap="innerHTML" hx-include="#mem-filter-form"
  hx-indicator="#mem-skel">
<select name="mode" hx-get="/panel/memories/rows" hx-trigger="change"
  hx-target="#mem-rows" hx-swap="innerHTML" hx-include="#mem-filter-form" aria-label="{esc(t("search_text", lang))}">
<option value="text" {"selected" if mode != "semantic" else ""}>{esc(t("search_text", lang))}</option>
<option value="semantic" {"selected" if mode == "semantic" else ""}>{esc(t("search_semantic", lang))}</option>
</select>
<select name="tag" hx-get="/panel/memories/rows" hx-trigger="change"
  hx-target="#mem-rows" hx-swap="innerHTML" hx-include="#mem-filter-form" aria-label="{esc(t("opt_all_tags", lang))}">
<option value="">{esc(t("opt_all_tags", lang))}</option>{tag_opts}</select>
<select name="type" hx-get="/panel/memories/rows" hx-trigger="change"
  hx-target="#mem-rows" hx-swap="innerHTML" hx-include="#mem-filter-form" aria-label="{esc(t("opt_all_types", lang))}">
<option value="">{esc(t("opt_all_types", lang))}</option>{type_opts}</select>
<select name="status" hx-get="/panel/memories/rows" hx-trigger="change"
  hx-target="#mem-rows" hx-swap="innerHTML" hx-include="#mem-filter-form" aria-label="{esc(t("th_status", lang))}">
{status_opts}
</select>
<input type="hidden" name="client" value="{esc(client)}">
<input type="hidden" name="domain" id="domain-input" value="{esc(domain)}">
</div></form>
<div class="tbl-wrap"><table class="tbl"><thead><tr>
<th>{esc(t("th_content", lang))}</th><th>{esc(t("th_type", lang))}</th><th>{esc(t("th_status", lang))}</th>
<th>{esc(t("th_source", lang))}</th><th>{esc(t("th_created", lang))}</th>
</tr></thead>
<tbody id="mem-rows">{rows_html}</tbody></table></div>
<div id="mem-skel" class="htmx-indicator" style="padding:12px"><div class="skel" style="height:44px;margin-bottom:8px"></div><div class="skel" style="height:44px;margin-bottom:8px"></div><div class="skel" style="height:44px"></div></div>
<div class="small muted" style="margin-top:10px">{esc(t("mem_note", lang))}</div>
</div></div>
<div id="mem-graph-wrap" hidden>
<div class="card constel-card constel-card--large">
<canvas id="constel" data-nodes="{nodes_json}"></canvas>
{graph_empty}
<div class="constel-hint">{esc(t("constel_hint", lang))}</div>
</div></div>"""
    return HTMLResponse(await _layout(request, "memories", t("title_memories", lang), body, lang))


async def _mem_rows_html(
    repo, lang: str, *, q, mode, tag, type, client, status, domain: str = ""
) -> str:
    # Domain toggle (SPECS §7.1.1): filter by DECLARED domain. The ML-resolved
    # domain is a triage-time signal, not a stored field — the toggle filters
    # on the stored type, which harnesses set explicitly for personal/project.
    if domain in ("personal", "project"):
        type = domain
    if mode == "semantic" and q.strip():
        tags = [tag] if tag else []
        scored = await repo.search(
            query=q.strip(),
            top_k=50,
            tags=tags,
            types=[type] if type else (),
            active_only=True,
        )
        recs = [s.record for s in scored]
        if client:
            recs = [m for m in recs if (m.source or {}).get("client_id") == client]
    else:
        recs = await repo.list(
            status=status or None,
            tags=[tag] if tag else (),
            type=type or None,
            query=q.strip() or None,
            source_client=client or None,
            limit=200,
        )
    if not recs:
        return f"<tr><td colspan='5'>{_empty('database', t('mem_empty_title', lang), t('mem_empty', lang))}</td></tr>"
    return "".join(_mem_row(m, lang) for m in recs)


@router.get("/memories/rows", response_class=HTMLResponse)
async def memories_rows(
    request: Request,
    q: str = Query(default=""),
    mode: str = Query(default="text"),
    tag: str = Query(default=""),
    type: str = Query(default=""),
    client: str = Query(default=""),
    status: str = Query(default="active"),
    domain: str = Query(default=""),
    _=Depends(panel_owner),
):
    lang = get_lang()
    if domain not in ("", "personal", "project"):
        domain = ""
    repo, _store, _snap = _deps(request)
    return HTMLResponse(
        await _mem_rows_html(
            repo,
            lang,
            q=q,
            mode=mode,
            tag=tag,
            type=type,
            client=client,
            status=status,
            domain=domain,
        )
    )


def _memory_detail_body(m, lang: str) -> str:
    """Detail card + edit card. Shared by the full page and the HTMX drawer."""
    src = m.source or {}
    src_rows = "".join(
        f"<div class='meta-grid' style='margin-bottom:4px'><dt>{esc(k)}</dt><dd class='mono small'>{esc(v)}</dd></div>"
        for k, v in src.items()
    )
    sup = (
        f"<a href='/panel/memories/{m.superseded_by}'>{esc(t('link_new_version', lang))}</a>"
        if m.superseded_by
        else "—"
    )
    approve_btn = ""
    if m.status == "pending":
        approve_btn = f"""
<form method="post" action="/panel/inbox/{m.id}/approve" style="display:inline">
<button class="btn btn--primary btn--sm" type="submit">{icon("check", 14)} {esc(t("btn_approve", lang))}</button></form>
<form method="post" action="/panel/inbox/{m.id}/reject" style="display:inline"
  hx-post="/panel/inbox/{m.id}/reject" hx-confirm="{esc(t("confirm_reject", lang))}">
<button class="btn btn--ghost btn--sm" type="submit">{esc(t("btn_reject", lang))}</button></form>"""
    and_approve_box = (
        f"<label class='f'><input type='checkbox' name='and_approve' value='1' checked> "
        f"{esc(t('f_and_approve', lang))}</label>"
        if m.status == "pending"
        else ""
    )
    type_opts = "".join(
        f"<option value='{tp}' {'selected' if tp == m.type else ''}>{esc(_type_label(tp, lang))}</option>"
        for tp in TYPE_LABEL[lang]
    )
    enc_rows = ""
    if getattr(m, "encrypted", False):
        enc_rows = (
            f"<div class='meta-grid' style='margin-bottom:4px'><dt>{esc(t('row_key_id', lang))}</dt>"
            f"<dd><code>{esc(m.key_id)}</code></dd></div>"
            f"<div class='small muted' style='margin-bottom:8px'>{esc(t('enc_server_note', lang))}</div>"
        )
    return f"""
<div class="row row--between" style="margin-bottom:14px">
<div class="row">{_status_badge(m.status, lang)}{_enc_badge(m, lang)}{_tag_chips(m.scope_tags)}</div>
<div class="row">{approve_btn}
<button class="btn btn--danger btn--sm" hx-post="/panel/memories/{m.id}/delete"
  hx-confirm="{esc(t("confirm_delete", lang))}">{icon("trash", 14)} {esc(t("btn_delete", lang))}</button></div>
</div>
<h2 style="font-size:16px;margin:0 0 8px">{esc(t("h_content", lang))}</h2>
<p style="white-space:pre-wrap;font-size:15px;margin:0 0 16px">{esc(m.content)}</p>
<dl>
<div class="meta-grid" style="margin-bottom:4px"><dt>{esc(t("row_type", lang))}</dt><dd>{esc(_type_label(m.type, lang))}</dd></div>
<div class="meta-grid" style="margin-bottom:4px"><dt>{esc(t("row_confidence", lang))}</dt><dd>{m.confidence:.2f}<div class="conf-bar"><i style="width:{int(m.confidence * 100)}%"></i></div></dd></div>
{enc_rows}
<div class="meta-grid" style="margin-bottom:4px"><dt>ID</dt><dd><code class="small">{esc(str(m.id)[:13])}…</code> {_copy_btn(str(m.id), lang)}</dd></div>
<div class="meta-grid" style="margin-bottom:4px"><dt>{esc(t("row_created", lang))}</dt><dd class="small muted">{_dt(m.created_at)}</dd></div>
<div class="meta-grid" style="margin-bottom:4px"><dt>{esc(t("row_updated", lang))}</dt><dd class="small muted">{_dt(m.updated_at)}</dd></div>
<div class="meta-grid" style="margin-bottom:4px"><dt>{esc(t("row_superseded", lang))}</dt><dd>{sup}</dd></div>
{src_rows}
</dl>
<hr style="border:0;border-top:1px solid var(--border);margin:20px 0">
<h2 style="font-size:16px;margin:0 0 6px">{esc(t("h_edit", lang))}</h2>
<div class="small muted" style="margin-bottom:8px">{esc(t("edit_sub", lang))}</div>
<form method="post" action="/panel/memories/{m.id}/edit">
<label class="f">{esc(t("f_content", lang))}</label>
<textarea name="content">{esc(m.content)}</textarea>
<div class="row">
<div style="flex:1;min-width:160px"><label class="f">{esc(t("f_type", lang))}</label>
<select name="type">{type_opts}</select></div>
<div style="flex:1;min-width:160px"><label class="f">{esc(t("f_confidence", lang))}</label>
<input type="number" name="confidence" min="0" max="1" step="0.05" value="{m.confidence}"></div>
</div>
<label class="f">{esc(t("f_tags", lang))}</label>
<input type="text" name="scope_tags" value="{esc(",".join(m.scope_tags))}">
{and_approve_box}
<div class="row mt"><button class="btn btn--primary" type="submit">{esc(t("btn_save", lang))}</button>
<a class="btn btn--secondary" href="/panel/memories">{esc(t("btn_back", lang))}</a></div>
</form>"""


@router.get("/memories/{memory_id}", response_class=HTMLResponse)
async def memory_detail(
    request: Request,
    memory_id: UUID,
    _=Depends(panel_owner),
):
    lang = get_lang()
    repo, _store, _snap = _deps(request)
    m = await repo.get(memory_id)
    if m is None:
        raise NotFound(f"memory {memory_id} not found")
    body = _memory_detail_body(m, lang)
    if _is_hx(request):
        # HTMX drawer fragment — no layout shell.
        return HTMLResponse(body)
    return HTMLResponse(
        await _layout(
            request,
            "memories",
            t("title_memory_detail", lang),
            f'<div class="card">{body}</div>',
            lang,
        )
    )


@router.post("/memories/new", response_class=HTMLResponse)
async def memory_new(
    request: Request,
    content: str = Form(...),
    type: str = Form(...),
    scope_tags: str = Form(default=""),
    _=Depends(panel_owner),
):
    # Owner-created memories go through the same triage as harness writes
    # (automation layer): low-risk auto-approves, the rest queues.
    lang = get_lang()
    repo, _store, _snap = _deps(request)
    if type not in TYPE_LABEL[lang]:
        raise InvalidInput(f"unknown type: {type}")
    if not content or len(content) > MAX_CONTENT_LEN:
        raise InvalidInput(f"content must be 1..{MAX_CONTENT_LEN} chars")
    try:
        tags = _validate_tags([tg.strip() for tg in scope_tags.split(",") if tg.strip()])
    except ValueError as exc:
        raise InvalidInput(str(exc))
    from .triage import create_with_triage

    rec, decision = await create_with_triage(
        repo,
        content=content,
        type=type,
        scope_tags=tags,
        confidence=1.0,
        source={"client_id": "owner-panel"},
        via="panel",
        scopes_used=["memory.admin"],
    )
    if decision.action == "auto_merge" and decision.merge_target_id is not None:
        return RedirectResponse(
            f"/panel/memories/{decision.merge_target_id}?msg={quote(t('msg_saved', lang))}",
            status_code=302,
        )
    if rec is None:  # defensive: auto_merge without a target id
        raise InvalidInput("merge target unavailable")
    if decision.action == "auto_approve":
        return RedirectResponse(
            f"/panel/memories/{rec.id}?msg={quote(t('msg_saved', lang))}",
            status_code=302,
        )
    return RedirectResponse(
        f"/panel/inbox?msg={quote(t('msg_memory_created', lang))}", status_code=302
    )


@router.post("/memories/{memory_id}/edit")
async def memory_edit(
    request: Request,
    memory_id: UUID,
    content: str = Form(...),
    type: str = Form(...),
    scope_tags: str = Form(default=""),
    confidence: float = Form(default=1.0),
    and_approve: str = Form(default=""),
    _=Depends(panel_owner),
):
    # Mirrors PATCH /api/v1/memories/{id} (memory.admin) + audit.
    # Same input contract as the REST endpoint: the panel must not be a
    # backdoor around the content caps or tag validation. Encrypted
    # records carry ciphertext (cap 40k), not plaintext (cap 20k).
    lang = get_lang()
    repo, _store, _snap = _deps(request)
    existing = await repo.get(memory_id)
    cap = MAX_CIPHERTEXT_LEN if (existing and existing.encrypted) else MAX_CONTENT_LEN
    if type not in TYPE_LABEL[lang]:
        raise InvalidInput(f"unknown type: {type}")
    if not content or len(content) > cap:
        raise InvalidInput(f"content must be 1..{cap} chars")
    try:
        tags = _validate_tags([tg.strip() for tg in scope_tags.split(",") if tg.strip()])
    except ValueError as exc:
        raise InvalidInput(str(exc))
    if not (0.0 <= confidence <= 1.0):
        raise InvalidInput("confidence must be 0..1")
    rec = await repo.revise(
        memory_id,
        content=content,
        type=type,
        scope_tags=tags,
        confidence=confidence,
    )
    if rec is None:
        raise NotFound(f"memory {memory_id} not found")
    await repo.audit(
        action="write",
        memory_ids=[rec.id],
        scopes_used=["memory.admin"],
        meta={"via": "panel", "revised_from": str(memory_id)},
    )
    if and_approve and rec.status == "pending":
        await repo.approve(rec.id)
        await repo.audit(
            action="write",
            memory_ids=[rec.id],
            scopes_used=["memory.admin"],
            meta={"via": "panel", "transition": "pending->active"},
        )
    return RedirectResponse(
        f"/panel/memories/{rec.id}?msg={quote(t('msg_saved', lang))}", status_code=302
    )


@router.post("/memories/{memory_id}/delete")
async def memory_delete(request: Request, memory_id: UUID, _=Depends(panel_owner)):
    # Mirrors DELETE /api/v1/memories/{id} (memory.admin) + audit.
    lang = get_lang()
    repo, _store, _snap = _deps(request)
    ok = await repo.delete(memory_id)
    if not ok:
        raise NotFound(f"memory {memory_id} not found")
    await repo.audit(
        action="delete",
        memory_ids=[memory_id],
        scopes_used=["memory.admin"],
        meta={"via": "panel"},
    )
    if _is_hx(request):
        # Drawer context: close the drawer by navigating to the list.
        return Response("", headers={"HX-Redirect": "/panel/memories"})
    return RedirectResponse(f"/panel/memories?msg={quote(t('msg_deleted', lang))}", status_code=302)


# ------------------------------------------------------------------ inbox
def _triage_chips(m, lang: str) -> str:
    """Triage reason chips for inbox cards (automation layer). Unknown
    reason codes fall back to a generic label — never render raw codes."""
    reasons = getattr(m, "triage_reasons", None) or ["unknown"]
    chips = []
    for r in reasons:
        try:
            label = t(f"triage_{r}", lang)
        except KeyError:
            label = t("triage_unknown", lang)
        chips.append(f"<span class='chip chip--warn'>{esc(label)}</span>")
    return "".join(chips)


def _inbox_card(m, lang: str, dups_html: str = "") -> str:
    src = (m.source or {}).get("client_id", "—")
    pct = int((m.confidence or 0) * 100)
    return f"""
<div class="card inbox-card" id="inbox-card-{m.id}">
<div class="inbox-card__eyebrow">
{_tag_chips(m.scope_tags)}{_enc_badge(m, lang)}{_triage_chips(m, lang)}
<span>{esc(_type_label(m.type, lang))} · {esc(t("inbox_from", lang))} <span class="mono">{esc(src)}</span> · {_dt(m.created_at)}</span>
</div>
<p class="inbox-card__content">{esc(m.content)}</p>
<div class="conf-bar" title="{pct}%"><i style="width:{pct}%"></i></div>
{dups_html}
<div class="inbox-card__actions" style="margin-top:12px">
<button class="btn btn--primary btn--sm"
  hx-post="/panel/inbox/{m.id}/approve" hx-target="#inbox-card-{m.id}" hx-swap="outerHTML">{icon("check", 14)} {esc(t("btn_approve", lang))}</button>
<a class="btn btn--secondary btn--sm" href="/panel/memories/{m.id}">{esc(t("btn_edit_approve", lang))}</a>
<button class="btn btn--ghost btn--sm"
  hx-post="/panel/inbox/{m.id}/reject" hx-target="#inbox-card-{m.id}" hx-swap="outerHTML"
  hx-confirm="{esc(t("confirm_reject", lang))}">{esc(t("btn_reject", lang))}</button>
</div></div>"""


async def _inbox_list_html(repo, lang: str) -> str:
    pendings = await repo.list(status="pending", limit=200)
    cards = []
    for m in pendings:
        dups = []
        if not m.encrypted and m.embedding is not None:
            dups = [
                s
                for s in await repo.find_similar_active(
                    m.embedding, threshold=DUPLICATE_THRESHOLD, limit=5
                )
                if s.score > DUPLICATE_THRESHOLD and s.record.id != m.id
            ][:3]
        dup_html = ""
        if dups:
            items = "".join(
                f"<li><a href='/panel/memories/{d.record.id}'>{esc(d.record.content[:60])}…</a> "
                f"<span class='muted small'>({esc(t('dup_similarity', lang))} {d.score:.2f})</span></li>"
                for d in dups
            )
            dup_html = (
                f"<div class='callout callout--warn' style='margin:12px 0 0'>{icon('alert', 14)}"
                f"<div><strong>{esc(t('dup_title', lang))}</strong>"
                f"<ul style='margin:6px 0 0;padding-left:18px'>{items}</ul></div></div>"
            )
        cards.append(_inbox_card(m, lang, dup_html))
    if not cards:
        return _empty("inbox", t("inbox_zero_title", lang), t("inbox_zero_desc", lang))
    return "".join(cards)


@router.get("/inbox", response_class=HTMLResponse)
async def inbox_page(request: Request, _=Depends(panel_owner)):
    lang = get_lang()
    repo, _store, _snap = _deps(request)
    pendings = await repo.list(status="pending", limit=200)
    n = len(pendings)
    approve_all = ""
    if n:
        approve_all = f"""
<button class="btn btn--secondary btn--sm"
  hx-post="/panel/inbox/approve-all" hx-target="#inbox-list" hx-swap="innerHTML"
  hx-confirm="{esc(t("confirm_approve_all", lang).format(n=n))}">{esc(t("btn_approve_all", lang).format(n=n))}</button>"""
    body = f"""
<div class="row row--between" style="margin-bottom:16px">
<div class="muted small">{esc(t("inbox_sub", lang))}</div>
<div>{approve_all}</div></div>
<div id="inbox-list">{await _inbox_list_html(repo, lang)}</div>"""
    title = t("title_inbox_n", lang).format(n=n)
    return HTMLResponse(await _layout(request, "inbox", title, body, lang))


@router.post("/inbox/approve-all")
async def inbox_approve_all(request: Request, _=Depends(panel_owner)):
    # Mirrors POST /api/v1/memories/{id}/approve per item + audit.
    lang = get_lang()
    repo, _store, _snap = _deps(request)
    pendings = await repo.list(status="pending", limit=1000)
    n = 0
    for m in pendings:
        rec = await repo.approve(m.id)
        if rec is not None:
            n += 1
            await _wiki_index(request, rec)
    await repo.audit(
        action="write",
        memory_ids=[m.id for m in pendings],
        scopes_used=["memory.admin"],
        meta={"via": "panel", "transition": "pending->active", "bulk": True},
    )
    if _is_hx(request):
        trig = json.dumps(
            {
                "toast": {"type": "success", "msg": t("msg_approved_all", lang).format(n=n)},
                "inboxCount": {"n": 0},
            }
        )
        return HTMLResponse(await _inbox_list_html(repo, lang), headers={"HX-Trigger": trig})
    return RedirectResponse(
        f"/panel/inbox?msg={quote(t('msg_approved_all', lang).format(n=n))}",
        status_code=302,
    )


@router.post("/inbox/{memory_id}/approve")
async def inbox_approve(request: Request, memory_id: UUID, _=Depends(panel_owner)):
    # Mirrors POST /api/v1/memories/{id}/approve (memory.admin) + audit.
    lang = get_lang()
    repo, _store, _snap = _deps(request)
    rec = await repo.approve(memory_id)
    if rec is None:
        raise NotFound(f"memory {memory_id} not found")
    await _wiki_index(request, rec)
    await repo.audit(
        action="write",
        memory_ids=[rec.id],
        scopes_used=["memory.admin"],
        meta={"via": "panel", "transition": "pending->active"},
    )
    if _is_hx(request):
        remaining = len(await repo.list(status="pending", limit=1000))
        trig = json.dumps(
            {
                "toast": {"type": "success", "msg": t("msg_approved", lang)},
                "inboxCount": {"n": remaining},
            }
        )
        return Response("", headers={"HX-Trigger": trig})
    return RedirectResponse(f"/panel/inbox?msg={quote(t('msg_approved', lang))}", status_code=302)


@router.post("/inbox/{memory_id}/reject")
async def inbox_reject(request: Request, memory_id: UUID, _=Depends(panel_owner)):
    lang = get_lang()
    repo, _store, _snap = _deps(request)
    m = await repo.get(memory_id)
    if m is None or m.status != "pending":
        raise NotFound(f"pending memory {memory_id} not found")
    await repo.delete(memory_id)
    await repo.audit(
        action="delete",
        memory_ids=[memory_id],
        scopes_used=["memory.admin"],
        meta={"via": "panel", "reason": "rejected_from_inbox"},
    )
    if _is_hx(request):
        remaining = len(await repo.list(status="pending", limit=1000))
        trig = json.dumps(
            {
                "toast": {"type": "info", "msg": t("msg_rejected", lang)},
                "inboxCount": {"n": remaining},
            }
        )
        return Response("", headers={"HX-Trigger": trig})
    return RedirectResponse(f"/panel/inbox?msg={quote(t('msg_rejected', lang))}", status_code=302)


# ------------------------------------------------------------------ clients
def _scope_list_html(scopes, custom: dict[str, str], lang: str) -> str:
    items = []
    for s in sorted(scopes):
        desc = oauth_logic.describe_scope(s, custom)
        label = desc[0] if desc else s
        warn = (
            f' <span class="badge badge--danger">{esc(t("scope_admin_warn", lang))}</span>'
            if s == "memory.admin"
            else ""
        )
        items.append(
            f"<div class='small' style='margin-bottom:4px'><code>{esc(s)}</code> <span class='muted'>— {esc(label)}</span>{warn}</div>"
        )
    return "".join(items) or f"<span class='muted'>{esc(t('scope_none', lang))}</span>"


@router.get("/clients", response_class=HTMLResponse)
async def clients_page(request: Request, _=Depends(panel_owner)):
    lang = get_lang()
    repo, store, _snap = _deps(request)
    clients = await store.list_clients()
    custom = {t["tag"]: t["label"] for t in await store.list_custom_tags()}
    last_active: dict[str, str] = {}
    for r in await repo.audit_list(limit=500):
        if r.client_id and str(r.client_id) not in last_active:
            last_active[str(r.client_id)] = _rel(r.created_at, lang)
    grants: dict[str, list[str]] = {}
    if hasattr(store, "_grants"):  # in-memory impl exposes grants
        for g in store._grants:  # type: ignore[attr-defined]
            grants[str(g["client_id"])] = list(g["scopes"])
    cards = []
    for c in clients:
        scopes = grants.get(str(c.id), [])
        cards.append(f"""
<div class="card" id="client-card-{c.id}">
<div class="row row--between" style="margin-bottom:10px">
<div class="row"><strong style="font-size:16px">{esc(c.name)}</strong>
<span class="badge badge--success"><span class="badge__dot"></span>{esc(t("client_status_active", lang))}</span></div>
<button class="btn btn--danger btn--sm"
  hx-post="/panel/clients/{c.id}/revoke" hx-target="#client-card-{c.id}" hx-swap="outerHTML"
  hx-confirm="{esc(t("revoke_warn_short", lang).format(name=c.name))}">{icon("x", 14)} {esc(t("btn_revoke", lang))}</button>
</div>
<div class="row" style="margin-bottom:8px"><code class="small">{esc(c.client_id)}</code> {_copy_btn(c.client_id, lang)}</div>
{_scope_list_html(scopes, custom, lang)}
<div class="small muted" style="margin-top:8px">{icon("clock", 12)} {esc(t("th_last_active", lang))}: {esc(last_active.get(str(c.id), "—"))}</div>
</div>""")
    body = f"""
<div class="row row--between" style="margin-bottom:16px">
<div class="muted small">{esc(t("clients_summary", lang).format(n=len(clients)))}</div>
<a class="btn btn--primary btn--sm" href="/panel/clients/new">{icon("plus", 14)} {
        esc(t("btn_new_client", lang))
    }</a></div>
{
        "".join(cards)
        if cards
        else _empty(
            "key",
            t("clients_empty_title", lang),
            t("clients_empty", lang),
            f'<a class="btn btn--primary" href="/panel/clients/new">{esc(t("btn_new_client", lang))}</a>',
        )
    }"""
    return HTMLResponse(await _layout(request, "clients", t("title_clients", lang), body, lang))


@router.get("/clients/new", response_class=HTMLResponse)
async def client_new_form(request: Request, _=Depends(panel_owner)):
    lang = get_lang()
    body = f"""
<div class="card page--narrow"><h2>{esc(t("title_client_new", lang))}</h2>
<div class="card__sub">{esc(t("client_new_sub", lang))}</div>
<form method="post" action="/panel/clients/new">
<label class="f">{esc(t("f_name", lang))}</label>
<input type="text" name="name" maxlength="100" required>
<label class="f">{esc(t("f_redirect_uris", lang))}</label>
<textarea name="redirect_uris" style="min-height:70px" placeholder="http://localhost:9999/callback"></textarea>
<div class="row mt"><button class="btn btn--primary" type="submit">{esc(t("btn_register", lang))}</button>
<a class="btn btn--secondary" href="/panel/clients">{esc(t("btn_cancel", lang))}</a></div>
</form></div>"""
    return HTMLResponse(await _layout(request, "clients", t("title_client_new", lang), body, lang))


@router.post("/clients/new", response_class=HTMLResponse)
async def client_new(
    request: Request,
    name: str = Form(...),
    redirect_uris: str = Form(default=""),
    _=Depends(panel_owner),
):
    # Mirrors POST /api/v1/clients (owner context: no setup token needed —
    # the panel itself is the owner gate).
    lang = get_lang()
    _repo, store, _snap = _deps(request)
    uris = [u.strip() for u in redirect_uris.splitlines() if u.strip()]
    for u in uris:
        if not (u.startswith("http://") or u.startswith("https://")) or "#" in u:
            raise InvalidInput(f"invalid redirect_uri: {u}")
    secret = oauth_logic.new_client_secret()
    rec = await store.create_client(
        name=name.strip()[:100],
        client_id=oauth_logic.new_client_id(),
        secret_hash=oauth_logic.hash_secret(secret),
        redirect_uris=uris,
    )
    # Secret ceremony: shown exactly once, never stored.
    body = f"""
<div class="card page--narrow">
<div class="callout callout--warn">{icon("alert", 14)}<div><strong>{esc(t("client_new_done_warn", lang))}</strong></div></div>
<dl>
<div class="meta-grid" style="margin-bottom:4px"><dt>{esc(t("row_name", lang))}</dt><dd>{esc(rec.name)}</dd></div>
<div class="meta-grid"><dt>client_id</dt><dd><code class="small">{esc(rec.client_id)}</code> {_copy_btn(rec.client_id, lang)}</dd></div>
</dl>
<label class="f">client_secret</label>
<pre class="secret"><span>{esc(secret)}</span>{_copy_btn(secret, lang)}</pre>
<div class="row mt"><a class="btn btn--primary" href="/panel/clients">{esc(t("btn_done", lang))}</a></div>
</div>"""
    return HTMLResponse(
        await _layout(request, "clients", t("title_client_new_done", lang), body, lang)
    )


@router.post("/clients/{client_pk}/revoke")
async def client_revoke(request: Request, client_pk: UUID, _=Depends(panel_owner)):
    # Mirrors POST /oauth/revoke with revoke_all=true (T2): same store call,
    # same revocation-cache invalidation, same audit write.
    lang = get_lang()
    repo, store, _snap = _deps(request)
    c = await store.get_client_by_id(client_pk)
    if c is None:
        raise NotFound(f"client {client_pk} not found")
    n = await store.revoke_all_for_client(client_pk)
    request.app.state.revocation_cache.invalidate()
    await repo.audit(
        action="revoke",
        client_id=client_pk,
        scopes_used=[],
        meta={"via": "panel", "all": True, "tokens_revoked": n},
    )
    msg = t("msg_revoked", lang).format(name=c.name, n=n)
    if _is_hx(request):
        return _hx_toast(msg)
    return RedirectResponse(f"/panel/clients?msg={quote(msg)}", status_code=302)


# ------------------------------------------------------------------ shares (P2, memory.share)
def _share_tags(scopes) -> list[str]:
    """Tags behind a share's scopes (always memory.read.<tag>, nothing else)."""
    return sorted(s.split(".", 2)[2] for s in (scopes or []) if s.startswith("memory.read."))


def _share_status_badge(s, now, lang: str) -> str:
    if s.revoked:
        return f'<span class="badge badge--neutral"><span class="badge__dot"></span>{esc(t("share_revoked", lang))}</span>'
    if s.expires_at <= now:
        return f'<span class="badge badge--warning"><span class="badge__dot"></span>{esc(t("share_expired", lang))}</span>'
    return f'<span class="badge badge--success"><span class="badge__dot"></span>{esc(t("share_active", lang))}</span>'


@router.get("/shares", response_class=HTMLResponse)
async def shares_page(request: Request, _=Depends(panel_owner)):
    lang = get_lang()
    _repo, store, _snap = _deps(request)
    shares = await store.list_shares()
    now = datetime.now(UTC)
    cards = []
    for s in shares:
        active = not s.revoked and s.expires_at > now
        revoke_btn = ""
        if active:
            revoke_btn = (
                f"<button class='btn btn--danger btn--sm' hx-post='/panel/shares/{s.id}/revoke' "
                f"hx-target='#share-card-{s.id}' hx-swap='outerHTML' "
                f'hx-confirm="{esc(t("confirm_revoke_share", lang))}">'
                f"{esc(t('btn_revoke', lang))}</button>"
            )
        cards.append(f"""
<div class="card" id="share-card-{s.id}">
<div class="row row--between" style="margin-bottom:10px">
<div class="row"><strong style="font-size:16px">{esc(s.name)}</strong>{_share_status_badge(s, now, lang)}</div>
{revoke_btn}</div>
<div class="row" style="margin-bottom:8px"><code class="small">share:{s.id}</code> {_copy_btn(f"share:{s.id}", lang)}</div>
<div style="margin-bottom:8px">{_tag_chips(_share_tags(s.scopes))}</div>
<div class="small muted">{icon("clock", 12)} {esc(t("th_share_expires", lang))}: {_dt(s.expires_at)}</div>
</div>""")
    body = f"""
<div class="row row--between" style="margin-bottom:16px">
<div class="muted small">{esc(t("shares_sub", lang))}</div>
<a class="btn btn--primary btn--sm" href="/panel/shares/new">{icon("plus", 14)} {
        esc(t("btn_new_share", lang))
    }</a></div>
{
        "".join(cards)
        if cards
        else _empty(
            "share",
            t("shares_empty_title", lang),
            t("shares_empty", lang),
            f'<a class="btn btn--primary" href="/panel/shares/new">{esc(t("btn_new_share", lang))}</a>',
        )
    }"""
    return HTMLResponse(await _layout(request, "shares", t("title_shares", lang), body, lang))


@router.get("/shares/new", response_class=HTMLResponse)
async def share_new_form(request: Request, _=Depends(panel_owner)):
    lang = get_lang()
    _repo, store, _snap = _deps(request)
    preset = dict(oauth_logic.PRESET_SCOPE_TAGS)
    custom = {t["tag"]: t["label"] for t in await store.list_custom_tags()}
    labels = {**preset, **custom}
    checks = "".join(
        f"<label><input type='checkbox' name='tags' value='{esc(tag)}'> "
        f"<code class='small'>{esc(tag)}</code> <span class='muted small'>({esc(labels[tag])})</span></label>"
        for tag in sorted(labels)
    )
    body = f"""
<div class="card page--narrow"><h2>{esc(t("title_share_new", lang))}</h2>
<div class="card__sub">{esc(t("share_new_sub", lang))}</div>
<form method="post" action="/panel/shares/new">
<label class="f">{esc(t("f_share_name", lang))}</label>
<input type="text" name="name" maxlength="64" required>
<label class="f">{esc(t("f_share_tags", lang))}</label>
<div class="checks">{checks}</div>
<label class="f">{esc(t("f_share_expires", lang))}</label>
<input type="number" name="expires_in_hours" min="1" max="2160" value="168" style="max-width:200px">
<div class="row mt"><button class="btn btn--primary" type="submit">{esc(t("btn_mint_share", lang))}</button>
<a class="btn btn--secondary" href="/panel/shares">{esc(t("btn_cancel", lang))}</a></div>
</form></div>"""
    return HTMLResponse(await _layout(request, "shares", t("title_share_new", lang), body, lang))


@router.post("/shares/new", response_class=HTMLResponse)
async def share_new(
    request: Request,
    name: str = Form(...),
    tags: list[str] = Form(default=[]),
    expires_in_hours: int = Form(default=168),
    _=Depends(panel_owner),
):
    # Mirrors POST /api/v1/shares (owner context: the panel itself is the
    # owner gate). The token is shown exactly once below — never stored,
    # never logged, never returned again.
    lang = get_lang()
    repo, store, _snap = _deps(request)
    clean = name.strip()
    if not clean:
        raise InvalidInput("name must not be blank")
    if not (1 <= expires_in_hours <= 2160):
        raise InvalidInput("expires_in_hours must be 1..2160")
    share, token = await oauth_logic.mint_share_token(
        store,
        name=clean[:64],
        tags=list(tags),
        expires_in_hours=expires_in_hours,
    )
    await repo.audit(
        action="grant",
        scopes_used=["memory.admin"],
        meta={
            "via": "panel",
            "share_id": str(share.id),
            "share_name": share.name,
            "scopes": share.scopes,
            "expires_at": share.expires_at.isoformat(),
        },
    )
    body = f"""
<div class="card page--narrow">
<div class="callout callout--warn">{icon("alert", 14)}<div><strong>{esc(t("share_new_done_warn", lang))}</strong></div></div>
<dl>
<div class="meta-grid" style="margin-bottom:4px"><dt>{esc(t("row_name", lang))}</dt><dd>{esc(share.name)}</dd></div>
<div class="meta-grid" style="margin-bottom:4px"><dt>{esc(t("th_share_tags", lang))}</dt><dd>{_tag_chips(_share_tags(share.scopes))}</dd></div>
<div class="meta-grid"><dt>{esc(t("th_share_expires", lang))}</dt><dd class="small muted">{_dt(share.expires_at)}</dd></div>
</dl>
<label class="f">token</label>
<pre class="secret"><span>{esc(token)}</span>{_copy_btn(token, lang)}</pre>
<div class="row mt"><a class="btn btn--primary" href="/panel/shares">{esc(t("btn_done", lang))}</a></div>
</div>"""
    return HTMLResponse(
        await _layout(request, "shares", t("title_share_new_done", lang), body, lang)
    )


@router.post("/shares/{share_id}/revoke")
async def share_revoke(request: Request, share_id: UUID, _=Depends(panel_owner)):
    # Mirrors DELETE /api/v1/shares/{id} (owner context): same store call,
    # same revocation-cache invalidation, same audit write.
    lang = get_lang()
    repo, store, _snap = _deps(request)
    share = await store.get_share(share_id)
    if share is None:
        raise NotFound(f"share {share_id} not found")
    await store.revoke_share(share_id)
    request.app.state.revocation_cache.invalidate(str(share_id))
    await repo.audit(
        action="revoke",
        scopes_used=["memory.admin"],
        meta={
            "via": "panel",
            "share_id": str(share_id),
            "share_name": share.name,
        },
    )
    msg = t("msg_share_revoked", lang).format(name=share.name)
    if _is_hx(request):
        return _hx_toast(msg)
    return RedirectResponse(f"/panel/shares?msg={quote(msg)}", status_code=302)


# ------------------------------------------------------------------ audit
_AUDIT_RANGES = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
_ACTION_BADGE = {
    "write": "info",
    "read": "neutral",
    "delete": "danger",
    "export": "info",
    "snapshot_apply": "warning",
    "revoke": "danger",
    "grant": "success",
    "refresh": "neutral",
    "security": "danger",
}


def _audit_filters(repo, lang, *, client_id, action, rng, clients, name_of):
    copts = "".join(
        f"<option value='{c.id}' {'selected' if str(c.id) == client_id else ''}>{esc(c.name)}</option>"
        for c in clients
    )
    aopts = "".join(
        f"<option value='{a}' {'selected' if a == action else ''}>{esc(_action_label(a, lang))}</option>"
        for a in [""] + sorted(ACTION_LABEL[lang])
    )
    ropts = "".join(
        f"<option value='{r}' {'selected' if r == rng else ''}>{r}</option>" for r in _AUDIT_RANGES
    )
    return (copts, aopts, ropts)


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request,
    client_id: str = Query(default=""),
    action: str = Query(default=""),
    range: str = Query(default="7d"),
    _=Depends(panel_owner),
):
    lang = get_lang()
    repo, store, _snap = _deps(request)
    clients = await store.list_clients()
    name_of = {str(c.id): c.name for c in clients}
    rng = range if range in _AUDIT_RANGES else "7d"
    since = datetime.now(UTC) - _AUDIT_RANGES[rng]
    cid = UUID(client_id) if client_id else None
    rows = await repo.audit_list(limit=500, client_id=cid, since=since)
    if action:
        rows = [r for r in rows if r.action == action]
    copts, aopts, ropts = _audit_filters(
        repo,
        lang,
        client_id=client_id,
        action=action,
        rng=rng,
        clients=clients,
        name_of=name_of,
    )
    trs = []
    for r in rows:
        who = (
            name_of.get(str(r.client_id), t("audit_owner", lang))
            if r.client_id
            else t("audit_owner", lang)
        )
        badge = _ACTION_BADGE.get(r.action, "neutral")
        trs.append(f"""
<tr><td class="mono small muted">{_dt(r.created_at)}</td>
<td>{esc(who)}</td>
<td><span class="badge badge--{badge}">{esc(_action_label(r.action, lang))}</span></td>
<td class="muted small">{esc(t("audit_count", lang).format(n=len(r.memory_ids)))}</td>
<td class="mono small muted">{esc(", ".join(r.scopes_used)) or "—"}</td></tr>""")
    qs = f"client_id={quote(client_id)}&action={quote(action)}&range={rng}"
    body = f"""
<div class="callout callout--info">{icon("lock", 14)}<div>{t("audit_integrity", lang)}</div></div>
<div class="card">
<form method="get" action="/panel/audit"><div class="toolbar" style="position:static;padding:0 0 12px">
<select name="range" aria-label="{esc(t("audit_range_label", lang))}">{ropts}</select>
<select name="client_id"><option value="">{esc(t("opt_all_sources", lang))}</option>{copts}</select>
<select name="action"><option value="">{esc(t("opt_all_actions", lang))}</option>{aopts}</select>
<button class="btn btn--secondary btn--sm" type="submit">{esc(t("btn_filter", lang))}</button>
<a class="btn btn--ghost btn--sm" href="/panel/audit.csv?{qs}">{icon("download", 14)} {esc(t("btn_export_csv", lang))}</a>
</div></form>
<div class="tbl-wrap"><table class="tbl tbl--dense"><thead><tr>
<th>{esc(t("th_time", lang))}</th><th>{esc(t("th_who", lang))}</th><th>{esc(t("th_action", lang))}</th>
<th>{esc(t("th_memories", lang))}</th><th>{esc(t("th_scope", lang))}</th>
</tr></thead>
<tbody>{"".join(trs) if trs else f"<tr><td colspan='5'>{_empty('file', t('audit_empty_title', lang), t('audit_empty', lang))}</td></tr>"}</tbody></table></div>
</div>"""
    return HTMLResponse(await _layout(request, "audit", t("title_audit", lang), body, lang))


@router.get("/audit.csv")
async def audit_csv(
    request: Request,
    client_id: str = Query(default=""),
    action: str = Query(default=""),
    range: str = Query(default="7d"),
    _=Depends(panel_owner),
):
    repo, _store, _snap = _deps(request)
    rng = range if range in _AUDIT_RANGES else "7d"
    since = datetime.now(UTC) - _AUDIT_RANGES[rng]
    cid = UUID(client_id) if client_id else None
    rows = await repo.audit_list(limit=5000, client_id=cid, since=since)
    if action:
        rows = [r for r in rows if r.action == action]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "actor_client_id", "action", "memory_ids", "scopes"])
    for r in rows:
        w.writerow(
            [
                r.created_at.isoformat() if r.created_at else "",
                str(r.client_id) if r.client_id else "owner",
                r.action,
                ";".join(str(m) for m in r.memory_ids),
                ";".join(r.scopes_used),
            ]
        )
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=porter-audit.csv"},
    )


# ------------------------------------------------------------------ snapshots
@router.get("/snapshots", response_class=HTMLResponse)
async def snapshots_page(request: Request, _=Depends(panel_owner)):
    lang = get_lang()
    repo, store, snap_store = _deps(request)
    _tags, labels = await _all_tags(repo, store)
    checks = "".join(
        f"<label><input type='checkbox' name='tags' value='{esc(tg)}'><code class='small'>{esc(tg)}</code> <span class='muted small'>({esc(labels[tg])})</span></label>"
        for tg in sorted(labels)
    )
    recs = await snap_store.list(limit=50)
    cards = []
    for r in recs:
        m = r.manifest or {}
        counts = m.get("counts") or {}
        enc = (
            f'<span class="badge badge--neutral">{icon("lock", 12)} {esc(t("snap_encrypted", lang))}</span>'
            if m.get("encrypted")
            else f'<span class="badge badge--warning"><span class="badge__dot"></span>{esc(t("snap_plain", lang))}</span>'
        )
        sig = (
            f'<span class="badge badge--success">{icon("check", 12)} {esc(t("snap_sig_ok", lang))}</span>'
            if m.get("signature_ok")
            else f'<span class="badge badge--warning"><span class="badge__dot"></span>{esc(t("snap_sig_missing", lang))}</span>'
        )
        cards.append(f"""
<div class="card">
<div class="row row--between" style="margin-bottom:8px">
<div class="row"><code class="small">{esc(r.id)}</code> {_copy_btn(str(r.id), lang)}</div>
<div class="row">{sig} {enc}</div></div>
<div class="muted small" style="margin-bottom:12px">
{esc(t("snap_memories", lang).format(n=counts.get("memories", "?")))} · {esc(",".join(m.get("filter_tags", [])) or t("snap_all_tags", lang))} · {_dt(r.created_at)}
</div>
<div class="row"><a class="btn btn--secondary btn--sm" href="/panel/snapshots/{r.id}/download">{icon("download", 14)} {esc(t("btn_download", lang))}</a></div>
</div>""")
    body = f"""
<div class="card"><h2>{esc(t("h_create_snapshot", lang))}</h2>
<div class="card__sub">{esc(t("snap_create_sub", lang))}</div>
<form method="post" action="/panel/snapshots/create">
<label class="f">{esc(t("f_snap_tags", lang))}</label>
<div class="checks">{checks}</div>
<label class="f">{esc(t("f_snap_password", lang))}</label>
<input type="password" name="password" maxlength="128" autocomplete="new-password" style="max-width:320px">
<div class="row mt"><button class="btn btn--primary" type="submit">{esc(t("btn_create_snapshot", lang))}</button></div>
</form></div>
<div class="row row--between" style="margin:20px 0 12px">
<h2 style="font-size:16px;margin:0">{esc(t("h_snap_history", lang))}</h2>
<a class="btn btn--secondary btn--sm" href="/panel/snapshots/apply">{icon("upload", 14)} {esc(t("btn_apply_snapshot", lang))}</a></div>
{"".join(cards) if cards else _empty("camera", t("snap_empty_title", lang), t("snap_empty", lang))}
<div class="small muted">{esc(t("snap_sign_note", lang))}</div>"""
    return HTMLResponse(await _layout(request, "snapshots", t("title_snapshots", lang), body, lang))


@router.post("/snapshots/create")
async def snapshots_create(
    request: Request,
    password: str = Form(default=""),
    tags: list[str] = Form(default=[]),
    _=Depends(panel_owner),
):
    # Mirrors POST /api/v1/snapshots (state.export).
    lang = get_lang()
    repo, _store, snap_store = _deps(request)
    rec, path = await snapshots_logic.create_snapshot(
        repo,
        snap_store,
        tags=tags,
        password=password or None,
        storage_dir=settings.snapshot_dir,
    )
    fname = os.path.basename(path)
    return RedirectResponse(
        f"/panel/snapshots?msg={quote(t('msg_snapshot_created', lang).format(fname=fname))}",
        status_code=302,
    )


@router.get("/snapshots/{snapshot_id}/download")
async def snapshots_download(request: Request, snapshot_id: UUID, _=Depends(panel_owner)):
    _repo, _store, snap_store = _deps(request)
    rec = await snap_store.get(snapshot_id)
    if rec is None or not os.path.isfile(rec.storage_path):
        raise NotFound(f"snapshot {snapshot_id} not found")
    return FileResponse(
        rec.storage_path,
        filename=os.path.basename(rec.storage_path),
        media_type="application/octet-stream",
    )


@router.get("/snapshots/apply", response_class=HTMLResponse)
async def snapshots_apply_form(request: Request, _=Depends(panel_owner)):
    lang = get_lang()
    body = f"""
<div class="card page--narrow"><h2>{esc(t("h_apply_snapshot", lang))}</h2>
<div class="card__sub">{esc(t("apply_sub", lang))}</div>
<form method="post" action="/panel/snapshots/apply" enctype="multipart/form-data">
<label class="f">{esc(t("f_snap_file", lang))}</label>
<input type="file" name="file" required accept=".porter,.json">
<label class="f">{esc(t("f_snap_file_password", lang))}</label>
<input type="password" name="password" maxlength="128" style="max-width:320px">
<div class="row mt"><button class="btn btn--primary" type="submit">{esc(t("btn_preview", lang))}</button></div>
</form></div>"""
    return HTMLResponse(
        await _layout(request, "snapshots", t("title_snapshot_apply", lang), body, lang)
    )


def _upload_path(filename: str) -> str:
    """Confine an upload filename inside the snapshot dir (no traversal)."""
    base = os.path.basename(filename)[-64:] or "upload.porter"
    target = os.path.abspath(os.path.join(settings.snapshot_dir, "uploads", base))
    root = os.path.abspath(os.path.join(settings.snapshot_dir, "uploads"))
    if not target.startswith(root + os.sep):
        raise InvalidInput("invalid upload filename")
    return target


@router.post("/snapshots/apply", response_class=HTMLResponse)
async def snapshots_apply_dryrun(
    request: Request,
    file: UploadFile = File(...),
    password: str = Form(default=""),
    _=Depends(panel_owner),
):
    lang = get_lang()
    os.makedirs(os.path.join(settings.snapshot_dir, "uploads"), exist_ok=True)
    token = secrets.token_hex(8)
    path = _upload_path(f"{token}-{file.filename or 'upload.porter'}")
    with open(path, "wb") as f:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    try:
        summary = snapshots_logic.inspect_snapshot_file(path, password or None)
    except ValueError as exc:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise InvalidInput(str(exc))
    tag_rows = "".join(
        f"<tr><td><code class='small'>{esc(tag)}</code></td><td>{esc(t('snap_memories', lang).format(n=n))}</td></tr>"
        for tag, n in sorted(summary["tag_counts"].items())
    )
    samples = "".join(f"<li class='muted small'>{esc(s)}…</li>" for s in summary["sample"])
    rel = os.path.basename(path)
    sig_badge = (
        f'<span class="badge badge--success">{icon("check", 12)} {esc(t("dryrun_sig_ok", lang))}</span>'
        if summary.get("signature_ok")
        else f'<span class="badge badge--warning"><span class="badge__dot"></span>{esc(t("dryrun_sig_missing", lang))}</span>'
    )
    body = f"""
<div class="card page--narrow"><h2>{esc(t("h_dryrun", lang))}</h2>
<dl>
<div class="meta-grid" style="margin-bottom:4px"><dt>{esc(t("dryrun_count", lang))}</dt>
<dd><strong>{summary["memory_count"]}</strong> <span class="muted small">{esc(t("dryrun_inbox_note", lang))}</span></dd></div>
<div class="meta-grid" style="margin-bottom:4px"><dt>{esc(t("dryrun_sig", lang))}</dt><dd>{sig_badge}</dd></div>
<div class="meta-grid"><dt>{esc(t("dryrun_fingerprint", lang))}</dt>
<dd><code class="small">{esc(summary["content_sha256"][:32])}…</code> {_copy_btn(summary["content_sha256"], lang)}</dd></div>
</dl>
<h3 style="font-size:14px;margin:16px 0 8px">{esc(t("dryrun_tagdist", lang))}</h3>
<div class="tbl-wrap"><table class="tbl tbl--dense"><tbody>{tag_rows or f"<tr><td class='muted'>{esc(t('dryrun_no_tags', lang))}</td></tr>"}</tbody></table></div>
<h3 style="font-size:14px;margin:16px 0 8px">{esc(t("dryrun_samples", lang))}</h3><ul style="padding-left:18px">{samples}</ul>
<form method="post" action="/panel/snapshots/apply/confirm">
<input type="hidden" name="upload" value="{esc(rel)}">
<label class="f">{esc(t("f_snap_file_password", lang))}</label>
<input type="password" name="password" maxlength="128" style="max-width:320px">
<div class="row mt">
<button class="btn btn--primary" hx-post="/panel/snapshots/apply/confirm" hx-confirm="{esc(t("confirm_import", lang))}">{esc(t("btn_confirm_import", lang))}</button>
<a class="btn btn--secondary" href="/panel/snapshots">{esc(t("btn_cancel", lang))}</a></div>
</form></div>"""
    return HTMLResponse(
        await _layout(request, "snapshots", t("title_snapshot_preview", lang), body, lang)
    )


@router.post("/snapshots/apply/confirm")
async def snapshots_apply_confirm(
    request: Request,
    upload: str = Form(...),
    password: str = Form(default=""),
    _=Depends(panel_owner),
):
    lang = get_lang()
    repo, _store, _snap = _deps(request)
    path = _upload_path(upload)
    if not os.path.isfile(path):
        raise NotFound("uploaded snapshot expired; please upload again")
    try:
        report = await snapshots_logic.apply_snapshot_file(
            repo,
            repo.audit,
            path=path,
            password=password or None,
            source_label="panel-upload",
        )
    except ValueError as exc:
        raise InvalidInput(str(exc))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    msg = t("msg_imported", lang).format(n=report["imported"])
    if report.get("warning"):
        msg += t("msg_imported_warn", lang).format(w=report["warning"])
    if _is_hx(request):
        return Response(
            "",
            headers={
                "HX-Redirect": f"/panel/inbox?msg={quote(msg)}",
            },
        )
    return RedirectResponse(f"/panel/inbox?msg={quote(msg)}", status_code=302)


# ------------------------------------------------------------------ sync (P1, PRD §9.5 / US7)
def _pulse_svg(live: bool, lang: str) -> str:
    """Thin gold heartbeat strip. Live = animated crawl; idle = calm flat line."""
    # EKG-like polyline, generated once (purely decorative; liveness comes from
    # real adapter last_seen, decided by the caller).
    pts = []
    x = 0
    base_y = 22
    beat = [0, 0, -9, 14, -4, 0, 0, 0, 6, -6, 0, 0]  # one QRS-ish complex
    while x < 1200:
        for dy in beat:
            pts.append(f"{x},{base_y + dy}")
            x += 14
        # flat gap between beats
        for _ in range(6):
            pts.append(f"{x},{base_y}")
            x += 14
    if not live:
        pts = [f"{i * 14},{base_y}" for i in range(86)]
    path = "M" + " L".join(pts)
    cls = "pulse-strip--live" if live else "pulse-strip--idle"
    label = esc(t("pulse_live", lang)) if live else esc(t("pulse_idle", lang))
    return (
        f'<div class="pulse-strip {cls}" aria-hidden="true">'
        f'<svg viewBox="0 0 1200 44" preserveAspectRatio="none">'
        f'<path class="pulse-strip__line" d="{path}"/></svg>'
        f'<span class="pulse-strip__label">{label}</span></div>'
    )


def _sync_adapter_cards(adapter_ids, cursors, name_of, head, now, lang: str) -> str:
    cards = []
    for aid in adapter_ids:
        cur = cursors.get(aid)
        label = name_of.get(aid, aid)
        if cur is None:
            cards.append(f"""
<div class="card"><div class="adapter-card__top">
<span class="badge badge--neutral"><span class="badge__dot"></span>{esc(t("sync_offline", lang))}</span>
<span class="adapter-card__name">{esc(label)}</span></div>
<div class="adapter-card__meta"><span class="mono small">{esc(aid)}</span><span>{esc(t("sync_never", lang))}</span></div>
</div>""")
            continue
        last_seen = cur["last_seen"]
        if last_seen is not None and last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=UTC)
        online = last_seen is not None and (now - last_seen).total_seconds() < 300
        lag = head - cur["cursor"]
        status = (
            f'<span class="badge badge--success"><span class="badge__dot"></span>{esc(t("sync_online", lang))}</span>'
            if online
            else f'<span class="badge badge--neutral"><span class="badge__dot"></span>{esc(t("sync_offline", lang))}</span>'
        )
        lag_txt = (
            esc(t("sync_up_to_date", lang))
            if lag <= 0
            else esc(t("sync_lag_n", lang).format(n=lag))
        )
        meter_cls = "" if lag <= 0 else ("lag-meter--warn" if lag < 20 else "lag-meter--bad")
        meter_w = 4 if lag <= 0 else min(100, 8 + lag * 4)
        cards.append(f"""
<div class="card"><div class="adapter-card__top">{status}
<span class="adapter-card__name">{esc(label)}</span></div>
<div class="adapter-card__meta">
<span>{esc(t("th_cursor", lang))}: <code class="mono">#{cur["cursor"]}</code></span>
<span>{lag_txt}</span>
<span>{icon("clock", 12)} {_rel(last_seen, lang)}</span>
</div>
<div class="lag-meter {meter_cls}"><i style="width:{meter_w}%"></i></div>
</div>""")
    return "".join(cards)


@router.get("/sync", response_class=HTMLResponse)
async def sync_page(request: Request, _=Depends(panel_owner)):
    lang = get_lang()
    repo, store, _snap = _deps(request)
    clients = await store.list_clients()
    cursors = {c["adapter_id"]: c for c in await repo.list_cursors()}
    _changes, head = await repo.get_changes(0, 1)  # head seq only
    name_of = {c.client_id: c.name for c in clients}
    adapter_ids = sorted(set(name_of) | set(cursors))
    now = datetime.now(UTC)
    online_n = 0
    lagging_n = 0
    lags = []
    for aid in adapter_ids:
        cur = cursors.get(aid)
        if not cur:
            continue
        ls = cur["last_seen"]
        if ls is not None and ls.tzinfo is None:
            ls = ls.replace(tzinfo=UTC)
        if ls is not None and (now - ls).total_seconds() < 300:
            online_n += 1
        lag = head - cur["cursor"]
        lags.append(max(0, lag))
        if lag > 0:
            lagging_n += 1
    median_lag = sorted(lags)[len(lags) // 2] if lags else 0
    live = f"""
<div id="sync-live" hx-get="/panel/sync" hx-trigger="every 15s" hx-select="#sync-live" hx-swap="outerHTML">
<div class="stat-row">
<div class="stat"><div class="stat__num">{online_n}</div><div class="stat__label">{esc(t("sync_stat_online", lang))}</div></div>
<div class="stat"><div class="stat__num">{lagging_n}</div><div class="stat__label">{esc(t("sync_stat_lagging", lang))}</div></div>
<div class="stat"><div class="stat__num">{median_lag}</div><div class="stat__label">{esc(t("sync_stat_median_lag", lang))}</div></div>
</div>
{_sync_adapter_cards(adapter_ids, cursors, name_of, head, now, lang) or f"<div class='card'><div class='muted'>{esc(t('sync_empty', lang))}</div></div>"}
</div>"""
    body = f"""
{_pulse_svg(online_n > 0, lang)}
<div class="muted small" style="margin-bottom:16px">head seq <code class="mono">#{head}</code> · {esc(t("sync_sub", lang))}</div>
{live}
<div class="card" style="margin-top:16px"><h2>{esc(t("sync_how_title", lang))}</h2>
<div class="card__sub" style="margin-bottom:0">
<ol style="margin:0;padding-left:20px;display:grid;gap:8px">
<li>{esc(t("sync_how_1", lang))}</li>
<li>{esc(t("sync_how_2", lang))}</li>
<li>{esc(t("sync_how_3", lang))}</li>
</ol></div></div>"""
    return HTMLResponse(await _layout(request, "sync", t("title_sync", lang), body, lang))


# ------------------------------------------------------------------ settings
@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, _=Depends(panel_owner)):
    lang = get_lang()
    repo, store, _snap = _deps(request)
    preset_rows = "".join(
        f"<tr><td><code class='small'>{esc(tag)}</code></td><td>{esc(label)}</td>"
        f"<td><span class='badge badge--neutral'>{esc(t('tag_preset', lang))}</span></td><td></td></tr>"
        for tag, label in sorted(oauth_logic.PRESET_SCOPE_TAGS.items())
    )
    customs = await store.list_custom_tags()
    custom_rows = "".join(
        f"<tr><td><code class='small'>{esc(c['tag'])}</code></td><td>{esc(c['label'])}</td>"
        f"<td><span class='badge badge--accent'>{esc(t('tag_custom', lang))}</span></td>"
        f"<td><button class='btn btn--ghost btn--sm' hx-post='/panel/settings/tags/{esc(c['tag'])}/delete' "
        f'hx-confirm="{esc(t("confirm_del_tag", lang).format(tag=c["tag"]))}">'
        f"{esc(t('btn_del_tag', lang))}</button></td></tr>"
        for c in customs
    )
    phrase = esc(t("danger_phrase", lang))
    body = f"""
<div class="card page--narrow"><h2>{esc(t("h_language", lang))}</h2>
<div class="card__sub">{esc(t("lang_sub", lang))}</div>
<div class="seg" role="group">
<button type="button" data-set-lang="zh" aria-pressed="{str(lang == "zh").lower()}">中文</button>
<button type="button" data-set-lang="en" aria-pressed="{str(lang == "en").lower()}">EN</button>
</div></div>
<div class="card"><h2>{esc(t("h_tag_mgmt", lang))}</h2>
<div class="card__sub">{t("tags_sub", lang)}</div>
<div class="tbl-wrap"><table class="tbl"><thead><tr>
<th>{esc(t("th_tag", lang))}</th><th>{esc(t("th_tag_label", lang))}</th><th>{esc(t("th_tag_kind", lang))}</th><th></th>
</tr></thead>
<tbody>{preset_rows}{custom_rows}</tbody></table></div>
<form method="post" action="/panel/settings/tags/add" class="mt">
<div class="row">
<input type="text" name="tag" placeholder="{esc(t("ph_new_tag", lang))}" pattern="[a-z0-9_]{{1,32}}"
  required style="max-width:260px">
<input type="text" name="label" placeholder="{esc(t("ph_new_label", lang))}" maxlength="64" required style="max-width:200px">
<button class="btn btn--secondary btn--sm" type="submit">{esc(t("btn_add", lang))}</button></div></form></div>
<div class="card"><h2>{esc(t("h_data", lang))}</h2>
<div class="row">
<a class="btn btn--secondary btn--sm" href="/panel/export">{icon("download", 14)} {esc(t("btn_export_all", lang))}</a></div>
<div class="small muted mt">{t("data_note", lang)}</div>
</div>
<div class="card danger-zone page--narrow"><h2>{icon("alert", 16)} {esc(t("h_danger", lang))}</h2>
<div class="card__sub">{esc(t("danger_sub", lang))}</div>
<h3 style="font-size:14px;margin:0 0 4px">{esc(t("danger_delete_all_t", lang))}</h3>
<p class="small muted" style="margin:0 0 10px">{esc(t("danger_delete_all_d", lang))}</p>
<form method="post" action="/panel/settings/danger/delete-all-memories">
<div class="row">
<input type="text" name="confirm" required autocomplete="off"
  placeholder="{esc(t("danger_confirm_ph", lang).format(phrase=phrase))}" style="max-width:260px">
<button class="btn btn--danger-solid btn--sm" type="submit">{esc(t("danger_delete_all_t", lang))}</button></div></form>
<h3 style="font-size:14px;margin:18px 0 4px">{esc(t("danger_reset_t", lang))}</h3>
<p class="small muted" style="margin:0 0 10px">{esc(t("danger_reset_d", lang))}</p>
<form method="post" action="/panel/settings/danger/reset-instance">
<div class="row">
<input type="text" name="confirm" required autocomplete="off"
  placeholder="{esc(t("danger_confirm_ph", lang).format(phrase=phrase))}" style="max-width:260px">
<button class="btn btn--danger-solid btn--sm" type="submit">{esc(t("danger_reset_t", lang))}</button></div></form>
</div>"""
    return HTMLResponse(await _layout(request, "settings", t("title_settings", lang), body, lang))


@router.post("/settings/language")
async def settings_language(request: Request, lang: str = Form(...), _=Depends(panel_owner)):
    try:
        new_lang = set_lang(lang.strip())
    except ValueError:
        raise InvalidInput("lang must be zh or en")
    # The header segmented control uses fetch() + reload; a plain form POST
    # lands here too. Either way the preference is persisted server-side.
    if _is_hx(request) or "fetch" in request.headers.get("user-agent", "").lower():
        return JSONResponse({"ok": True, "lang": new_lang})
    return RedirectResponse(
        f"/panel/settings?msg={quote(t('msg_lang_changed', new_lang))}",
        status_code=302,
    )


@router.post("/settings/tags/add")
async def settings_tag_add(
    request: Request,
    tag: str = Form(...),
    label: str = Form(...),
    _=Depends(panel_owner),
):
    # Mirrors POST /api/v1/scope-tags (memory.admin).
    lang = get_lang()
    _repo, store, _snap = _deps(request)
    tag = tag.strip().lower()
    import re

    if not re.fullmatch(r"[a-z0-9_]{1,32}", tag):
        raise InvalidInput("tag must match [a-z0-9_]{1,32}")
    if tag in oauth_logic.PRESET_SCOPE_TAGS:
        raise InvalidInput(f"'{tag}' is a preset tag and cannot be redefined")
    await store.add_custom_tag(tag=tag, label=label.strip()[:64])
    return RedirectResponse(
        f"/panel/settings?msg={quote(t('msg_tag_added', lang).format(tag=tag))}", status_code=302
    )


@router.post("/settings/tags/{tag}/delete")
async def settings_tag_delete(request: Request, tag: str, _=Depends(panel_owner)):
    # Mirrors DELETE /api/v1/scope-tags/{tag} (memory.admin).
    lang = get_lang()
    _repo, store, _snap = _deps(request)
    if tag in oauth_logic.PRESET_SCOPE_TAGS:
        raise InvalidInput(f"'{tag}' is a preset tag and cannot be removed")
    ok = await store.remove_custom_tag(tag)
    if not ok:
        raise NotFound(f"custom tag '{tag}' not found")
    msg = t("msg_tag_deleted", lang)
    if _is_hx(request):
        return _hx_toast(msg)
    return RedirectResponse(f"/panel/settings?msg={quote(msg)}", status_code=302)


@router.post("/settings/danger/delete-all-memories")
async def danger_delete_all_memories(
    request: Request, confirm: str = Form(default=""), _=Depends(panel_owner)
):
    # Type-to-confirm bulk hard delete. Every deletion is individually
    # audited so the trail shows exactly what was destroyed.
    lang = get_lang()
    repo, _store, _snap = _deps(request)
    if confirm.strip() != t("danger_phrase", lang):
        raise InvalidInput(t("danger_phrase_wrong", lang))
    n = 0
    while True:
        batch = await repo.list(status=None, limit=200)
        if not batch:
            break
        for m in batch:
            if await repo.delete(m.id):
                n += 1
                await repo.audit(
                    action="delete",
                    memory_ids=[m.id],
                    scopes_used=["memory.admin"],
                    meta={"via": "panel", "bulk": "delete-all"},
                )
    msg = t("msg_deleted_all", lang).format(n=n)
    return RedirectResponse(f"/panel/settings?msg={quote(msg)}", status_code=302)


@router.post("/settings/danger/reset-instance")
async def danger_reset_instance(
    request: Request, confirm: str = Form(default=""), _=Depends(panel_owner)
):
    # Type-to-confirm full reset: memories + all client tokens + shares.
    lang = get_lang()
    repo, store, _snap = _deps(request)
    if confirm.strip() != t("danger_phrase", lang):
        raise InvalidInput(t("danger_phrase_wrong", lang))
    n = 0
    while True:
        batch = await repo.list(status=None, limit=200)
        if not batch:
            break
        for m in batch:
            if await repo.delete(m.id):
                n += 1
    c = 0
    for cl in await store.list_clients():
        await store.revoke_all_for_client(cl.id)
        c += 1
    try:
        request.app.state.revocation_cache.invalidate()
    except Exception:
        pass
    s = 0
    for sh in await store.list_shares():
        await store.revoke_share(sh.id)
        s += 1
    await repo.audit(
        action="delete",
        memory_ids=[],
        scopes_used=["memory.admin"],
        meta={
            "via": "panel",
            "bulk": "reset-instance",
            "memories": n,
            "clients_revoked": c,
            "shares_revoked": s,
        },
    )
    msg = t("msg_reset_done", lang).format(n=n, c=c, s=s)
    return RedirectResponse(f"/panel/settings?msg={quote(msg)}", status_code=302)


@router.get("/export")
async def panel_export(request: Request, _=Depends(panel_owner)):
    # Mirrors GET /api/v1/export (memory.admin) + audit.

    repo, _store, _snap = _deps(request)
    payload = await build_export_payload(repo)
    await repo.audit(
        action="export",
        memory_ids=[],
        scopes_used=["memory.admin"],
        meta={
            "via": "panel",
            "format": "porter-export/v1",
            "memory_count": payload["memory_count"],
        },
    )
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": "attachment; filename=porter-export-v1.json"},
    )
