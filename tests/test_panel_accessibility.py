"""HTTP/HTML accessibility and CSS contrast checks, without a browser engine.

These checks do not prove keyboard behavior, layout, or visual accessibility.
They inspect actual rendered responses and composite the shipped color values.
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from porter.app import create_app
from porter.config import settings
from porter.embeddings import HashEmbedder
from porter.i18n import set_lang
from porter.oauth_store import InMemoryOAuthStore
from porter.repositories import InMemoryMemoryRepository
from porter.snapshots import SNAPSHOT_FORMAT

STATIC = Path(__file__).resolve().parents[1] / "porter" / "static"
VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
FORM_CONTROLS = {"input", "textarea", "select", "button"}


@dataclass(eq=False)
class Element:
    tag: str
    attrs: dict = field(default_factory=dict)
    parent: object = None
    children: list = field(default_factory=list)

    def text(self):
        if self.attrs.get("aria-hidden") == "true":
            return ""
        return " ".join(
            child.text() if isinstance(child, Element) else child for child in self.children
        ).strip()

    def ancestors(self):
        current = self.parent
        while current is not None:
            yield current
            current = current.parent


class Document(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.root = Element("document")
        self.stack = [self.root]
        self.elements = []
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Element(tag, dict(attrs), self.stack[-1])
        node.parent.children.append(node)
        self.elements.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for position in range(len(self.stack) - 1, 0, -1):
            if self.stack[position].tag == tag:
                self.stack = self.stack[:position]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)

    def find(self, **attrs):
        return [
            node for node in self.elements if all(node.attrs.get(k) == v for k, v in attrs.items())
        ]

    def name(self, node):
        refs = node.attrs.get("aria-labelledby", "").split()
        if refs:
            return " ".join(match.text() for ref in refs for match in self.find(id=ref)).strip()
        if node.attrs.get("aria-label", "").strip():
            return node.attrs["aria-label"].strip()
        labels = [
            label
            for label in self.elements
            if label.tag == "label"
            and (
                label.attrs.get("for") == node.attrs.get("id")
                and "id" in node.attrs
                or label in node.ancestors()
            )
        ]
        if labels:
            return " ".join(label.text() for label in labels).strip()
        if node.tag in {"button", "a", "legend"}:
            return node.text() or node.attrs.get("title", "").strip()
        if node.tag == "input" and node.attrs.get("type") in {"submit", "reset", "button"}:
            return node.attrs.get("value", "").strip()
        return node.attrs.get("title", "").strip()


@pytest.fixture(params=["en", "zh"])
def panel(request, tmp_path, monkeypatch):
    monkeypatch.setenv("PORTER_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(settings, "snapshot_dir", str(tmp_path / "snapshots"))
    set_lang(request.param)
    repo = InMemoryMemoryRepository(HashEmbedder())
    store = InMemoryOAuthStore()
    client = TestClient(
        create_app(repo, store, auth_enabled=False, enable_mcp=False), follow_redirects=False
    )

    async def seed():
        active, _ = await repo.create_active(
            content="Use this project convention",
            type="project",
            scope_tags=["work", "project:demo"],
            confidence=1,
            source={"client_id": "accessibility-test"},
        )
        pending, _ = await repo.create_pending(
            content="A pending personal preference",
            type="personal",
            scope_tags=["general"],
            confidence=1,
            source={},
        )
        return active.id, pending.id

    active, pending = asyncio.run(seed())
    return client, active, pending, request.param


PAGES = [
    "memories",
    "active_detail",
    "pending_detail",
    "drawer",
    "inbox",
    "clients/new",
    "shares/new",
    "snapshots",
    "snapshots/apply",
    "settings",
    "handoff",
    "handoff_preview",
    "client_created",
    "share_created",
    "snapshot_preview",
    "audit",
    "sync",
]


def response_for(panel, page):
    client, active, pending, _lang = panel
    if page in {"active_detail", "pending_detail", "drawer"}:
        target = pending if page == "pending_detail" else active
        return client.get(
            f"/panel/memories/{target}", headers={"HX-Request": "true"} if page == "drawer" else {}
        )
    if page == "handoff_preview":
        return client.post(
            "/panel/handoff",
            data={"project": "demo", "from_harness": "codex", "to_harness": "cursor"},
        )
    if page == "client_created":
        return client.post(
            "/panel/clients/new",
            data={"name": "Synthetic client", "redirect_uris": "http://localhost:19999/callback"},
        )
    if page == "share_created":
        return client.post(
            "/panel/shares/new",
            data={"name": "Synthetic share", "tags": ["work"], "expires_in_hours": "1"},
        )
    if page == "snapshot_preview":
        body = json.dumps(
            {
                "format": SNAPSHOT_FORMAT,
                "memories": [
                    {"content": "Synthetic imported fact", "type": "fact", "scope_tags": ["work"]}
                ],
            }
        )
        return client.post(
            "/panel/snapshots/apply", files={"file": ("synthetic.json", body, "application/json")}
        )
    return client.get("/panel/" + page)


@pytest.mark.parametrize("page", PAGES)
def test_rendered_controls_have_names_and_valid_label_references(panel, page):
    response = response_for(panel, page)
    assert response.status_code == 200, (page, response.status_code)
    document = Document(response.text)
    ids = [node.attrs["id"] for node in document.elements if "id" in node.attrs]
    assert len(ids) == len(set(ids)), f"duplicate ids on {page}"
    controls = [
        node
        for node in document.elements
        if node.tag in FORM_CONTROLS and node.attrs.get("type") != "hidden"
    ]
    assert controls, f"page {page} did not render its expected controls"
    for node in controls:
        assert document.name(node), (page, node.tag, node.attrs)
    for node in document.elements:
        if node.tag == "label" and "for" in node.attrs:
            targets = document.find(id=node.attrs["for"])
            assert len(targets) == 1 and targets[0].tag in FORM_CONTROLS, (page, node.attrs)
            assert node.text(), (page, node.attrs)
        for attribute in ("aria-labelledby", "aria-describedby", "aria-controls"):
            for reference in node.attrs.get(attribute, "").split():
                assert document.find(id=reference), (page, attribute, reference)
        if node.attrs.get("role") == "group":
            assert document.name(node), (page, "unnamed control group", node.attrs)


def test_shell_has_named_navigation_skip_link_and_initially_inert_dialog(panel):
    response = response_for(panel, "memories")
    assert response.status_code == 200
    document = Document(response.text)
    assert document.find(lang="zh-CN" if panel[3] == "zh" else "en")
    main = next(node for node in document.elements if node.tag == "main")
    assert main.attrs.get("id") and main.attrs.get("tabindex") == "-1"
    skip_links = [
        node
        for node in document.elements
        if node.tag == "a" and node.attrs.get("href") == "#" + main.attrs["id"]
    ]
    assert skip_links and all(document.name(link) for link in skip_links)
    navigation = [node for node in document.elements if node.tag == "nav"]
    assert navigation and all(document.name(node) for node in navigation)
    links = [
        node
        for node in document.elements
        if node.tag == "a" and any(ancestor in navigation for ancestor in node.ancestors())
    ]
    assert links
    # Their text spans are hidden in the tablet icon rail, so links need
    # a name that remains available when those spans are display:none.
    assert all(node.attrs.get("aria-label", "").strip() for node in links)
    assert len([link for link in links if link.attrs.get("aria-current") == "page"]) == 1
    (dialog,) = document.find(role="dialog")
    assert document.name(dialog)
    assert dialog.attrs.get("aria-modal") == "true"
    assert dialog.attrs.get("aria-hidden") == "true"
    assert "inert" in dialog.attrs and dialog.attrs.get("tabindex") == "-1"
    assert not any("shell" in node.attrs.get("class", "").split() for node in dialog.ancestors())
    close_buttons = [
        node for node in document.elements if node.tag == "button" and dialog in node.ancestors()
    ]
    assert close_buttons and all(document.name(node) for node in close_buttons)
    (toggle,) = document.find(id="sidebar-toggle")
    assert toggle.attrs.get("aria-expanded") == "false"
    assert document.find(id=toggle.attrs.get("aria-controls"))


def styles(source, selector):
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    declarations = {}
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", source):
        if selector in [part.strip() for part in selectors.split(",")]:
            declarations.update(
                {
                    key.strip(): value.strip()
                    for key, value in re.findall(r"([\w-]+)\s*:\s*([^;]+)", body)
                }
            )
    assert declarations, f"missing CSS selector {selector}"
    return declarations


def theme_tokens(theme):
    source = (STATIC / "css" / "tokens.css").read_text(encoding="utf-8")
    values = styles(source, ":root")
    if theme == "light":
        values.update(styles(source, '[data-theme="light"]'))
    return values


def color(value, tokens):
    if value.startswith("var("):
        return color(tokens[value[4:-1].strip()], tokens)
    if value == "transparent":
        return (0, 0, 0, 0)
    if value.startswith("#"):
        raw = value[1:]
        if len(raw) == 3:
            raw = "".join(ch * 2 for ch in raw)
        return tuple(int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4)) + (1,)
    numbers = [float(number) for number in re.findall(r"[\d.]+", value)]
    assert len(numbers) in {3, 4}, f"unsupported CSS color: {value}"
    return tuple(number / 255 for number in numbers[:3]) + (numbers[3] if len(numbers) == 4 else 1,)


def composite(foreground, background):
    alpha = foreground[3]
    return tuple(foreground[i] * alpha + background[i] * (1 - alpha) for i in range(3)) + (1,)


def luminance(rgba):
    linear = [
        part / 12.92 if part <= 0.04045 else ((part + 0.055) / 1.055) ** 2.4 for part in rgba[:3]
    ]
    return sum(part * weight for part, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))


def assert_contrast(foreground, background, context):
    low, high = sorted([luminance(composite(foreground, background)), luminance(background)])
    ratio = (high + 0.05) / (low + 0.05)
    assert ratio >= 4.5, f"{context}: {ratio:.3f}:1, needs 4.5:1"


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_all_text_tokens_meet_normal_text_contrast_on_panel_surfaces(theme):
    tokens = theme_tokens(theme)
    for text in ("--text", "--text-secondary", "--text-tertiary", "--accent-text"):
        for background in ("--bg", "--bg-subtle", "--surface", "--surface-raised"):
            assert_contrast(
                color(tokens[text], tokens),
                color(tokens[background], tokens),
                f"{theme} {text} on {background}",
            )


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_semantic_badge_text_and_inbox_count_contrast_after_alpha_compositing(theme):
    tokens = theme_tokens(theme)
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    for selector, backgrounds in [
        *(
            (f".badge--{kind}", ("--surface", "--bg"))
            for kind in ("success", "warning", "danger", "info", "neutral", "accent")
        ),
        (".nav__badge", ("--bg-subtle",)),
        (".danger-zone h2", ("--bg",)),
    ]:
        declarations = styles(css, selector)
        background_value = declarations.get(
            "background",
            styles(css, ".danger-zone")["background"]
            if selector == ".danger-zone h2"
            else "transparent",
        )
        for background in backgrounds:
            effective_background = composite(
                color(background_value, tokens), color(tokens[background], tokens)
            )
            assert_contrast(
                color(declarations["color"], tokens),
                effective_background,
                f"{theme} {selector} on {background}",
            )


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_action_and_danger_button_labels_meet_normal_text_contrast(theme):
    tokens = theme_tokens(theme)
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    for selector in (".btn--primary", ".btn--danger-solid"):
        declarations = styles(css, selector)
        for state in ("", ":hover"):
            computed = {**declarations, **styles(css, selector + state)}
            background = color(computed["background"], tokens)
            foreground = color(computed["color"], tokens)
            brightness = re.fullmatch(r"brightness\(([\d.]+)\)", computed.get("filter", ""))
            if brightness:
                factor = float(brightness[1])
                background = tuple(min(1, part * factor) for part in background[:3]) + (1,)
                foreground = tuple(min(1, part * factor) for part in foreground[:3]) + (1,)
            assert_contrast(foreground, background, theme + " " + selector + state)
