# Porter Panel UI Redesign Spec

**Status:** research complete, ready for implementation
**Scope:** visual redesign of the owner panel only (`porter/panel.py` + new `porter/static/`). No route/logic changes unless noted. Server-rendered HTML + HTMX stays — no SPA framework.
**Target feel:** "2026 AI infra product" (Linear / Vercel / Supabase / Langfuse register), not "Bootstrap 2015".

Reference products studied: Linear (dark-first, LCH colors, inverted-L chrome, dense lists), Vercel (zero-saturation, Geist, status dots inline), Railway (layered dark surfaces, service cards with status dots), Supabase (flat dark, warm grays, single mint accent, mono for IDs, dense 32px table rows, hairline dividers), Langfuse/LangSmith (observability IA: sidebar nav, master-detail traces, dense observation tables, global time-range filter), shadcn (CSS-variable token theming: background/foreground/card/primary/muted/border/radius pairs), HTMX+server-rendered dashboard patterns (HTML fragments over the wire, no client state store).

---

## 1. Design language

### 1.1 Color system — dark-first, single accent

Dark is the default (Linear/Supabase consensus: infra tools are dark-first; our users are developers in dark editors all day). Light mode supported via token swap, toggle in header.

**Primary accent: indigo-violet `#6E6BF2`** (Linear family `#5E6AD2`, brightened slightly for dark backgrounds). Used sparingly: primary buttons, active nav indicator, focus rings, key links. One accent only — status colors are semantic, never decorative.

**Status colors** (always dot + text, never color alone):
- success/healthy/active: `#34D399` (green)
- pending/attention: `#FBBF24` (amber)
- danger/revoked/error: `#F87171` (red)
- info/sync transit: `#60A5FA` (blue)

### 1.2 Dark tokens (`:root`)

```css
--bg:            #0B0C0E;  /* app canvas — near-black, NOT pure #000 */
--bg-subtle:     #101214;  /* sidebar */
--surface:       #14171B;  /* cards */
--surface-raised:#1B1F24;  /* popovers, dialogs, hover states */
--border:        #232A31;  /* hairline dividers */
--border-strong: #313A43;  /* input borders, card outlines on hover */
--text:          #EDEFF2;  /* primary */
--text-secondary:#9BA3AD;  /* secondary */
--text-tertiary: #646D78;  /* placeholders, disabled */
--accent:        #6E6BF2;
--accent-hover:  #8280F5;
--accent-muted:  rgba(110,107,242,.14);  /* tinted backgrounds, use color-mix where possible */
--accent-text:   #C7C5FF;  /* accent-colored text on dark */
--success: #34D399; --warning: #FBBF24; --danger: #F87171; --info: #60A5FA;
--shadow: 0 1px 2px rgba(0,0,0,.35);
```

### 1.3 Light tokens (`[data-theme="light"]`)

```css
--bg: #FAFAF9; --bg-subtle: #F4F4F2; --surface: #FFFFFF; --surface-raised: #FFFFFF;
--border: #E8E6E3; --border-strong: #D6D3CF;
--text: #1B1D21; --text-secondary: #5B616B; --text-tertiary: #9AA0A8;
--accent: #5A58E8;  /* deepened for contrast on light */
--accent-hover: #4A48D8;
--accent-muted: rgba(90,88,232,.10);
--accent-text: #4A48D8;
--shadow: 0 1px 2px rgba(20,20,30,.06);
```

### 1.4 Typography

- **UI font:** system stack (offline, correct CJK rendering — critical since zh is default):
  `-apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans SC", Inter, "Segoe UI", sans-serif`
- **Mono** (IDs, hashes, cursors, timestamps, tokens, code): `ui-monospace, "SF Mono", "JetBrains Mono", "Cascadia Code", Consolas, monospace`
- **Scale:** 12 (meta/labels) / 13 (table body, secondary) / 14 (base body) / 16 (card titles) / 20 (page titles) / 24 (stat numbers) / 32 (hero numbers only)
- Page titles: 20px, weight 600, letter-spacing -0.01em. Never heavy bold in UI chrome (cap at 600).
- Labels/eyebrows: 12px, uppercase, letter-spacing .06em, `--text-tertiary` (e.g. "SYNC STATUS", "DANGER ZONE").

### 1.5 Spacing & radius (8px base)

- Page padding: 24px (32px on ≥1400px). Section gap: 24px. Card padding: 20px. Dense control padding: 8–12px.
- Radius: `--radius-sm: 6px` (inputs, small buttons), `--radius-md: 8px` (buttons, table containers), `--radius-lg: 12px` (cards, dialogs), `--radius-full: 999px` (pills/badges).
- Borders: 1px hairlines only; **no heavy drop shadows on cards** — separate surfaces by background tone (`bg` → `surface` → `surface-raised`), like Supabase.

### 1.6 Sidebar vs top-nav — decision: sidebar

**Rationale:** 7 pages is sidebar territory (Vercel moved *to* a sidebar in 2026; Supabase, Langfuse, Linear all use left nav). Top-nav-only doesn't scale past ~5 items and wastes vertical space. Sidebar also gives a permanent home for the Inbox count badge (highest-traffic signal) and a collapse affordance for focus mode.

---

## 2. Layout shell

```
+--------+------------------------------------------------------+
|        |  header: breadcrumb · search ⌘K · theme · lang · ver   |
| side   +------------------------------------------------------+
| bar    |                                                      |
| 248px  |  page content (max-width 1200px, 24px padding)         |
|        |                                                      |
+--------+------------------------------------------------------+
```

**Sidebar** (`<aside>`, `--bg-subtle`, no right border — separation by tone, not line):
- Top: Porter wordmark (text "Porter" 15px/600 + small "MEMORY LAYER" eyebrow or a minimal SVG mark). No logo image needed.
- Nav items (icon + label, 14px, 8px radius rows, full-width click targets):
  1. Memories
  2. Inbox — with amber count badge (pending N)
  3. Sync — with status dot (green=all online / amber=lagging)
  4. Clients
  5. Audit
  6. Snapshots
  7. Settings (separated group at bottom, or a "SYSTEM" section label)
- Active item: `--accent-muted` background + `--accent-text` label + 2px accent bar at left edge (or a subtle left indicator). Inactive: `--text-secondary`.
- Bottom: version pill (`v0.1.0`, mono 11px, muted) + theme toggle is in header, not sidebar.
- Collapsible to 64px icon rail at <1100px (persist in localStorage); <720px becomes overlay drawer via hamburger in header.

**Header** (sticky, `--bg` with blur, 56px):
- Left: hamburger (mobile only) + breadcrumb (`Porter / Memories`) or page title.
- Center/right: search input (⌘K style, 200px, filters current page via HTMX; full command palette is overkill for v1), theme toggle (sun/moon SVG), language switcher (compact `中文/EN` segmented control — replaces the settings-page-only dropdown as the primary control; settings page keeps the full card), version pill.
- No user avatar/menu — single-owner product, no login. Don't invent account chrome.

**Responsive:** ≥1100px full sidebar; 720–1100px icon rail; <720px drawer + tables become horizontally scrollable (never reflow table columns — Supabase rule).

**i18n note:** sidebar labels and all chrome go through existing `t()`; CJK font stack already covers rendering. `lang` attribute already set on `<html>`.

---

## 3. Per-page wireframes

### 3.1 Memories (记忆) — the library

Dense, searchable table. Supabase table-editor register.

```
+---------------------------------------------------------------+
| Memories                          128 memories   [+ New]      |
| [search memories...        ] [Type ▾] [Tag ▾] [Status ▾]      |
+---------------------------------------------------------------+
| CONTENT              | TYPE   | TAGS        | SOURCE | UPDATED |
| user avoids cilantro…| pref   | ● dietary   | codex  | 2h ago  |
| API returns snake_…  | pref   | ● code_style| cursor | 1d ago  |
+---------------------------------------------------------------+
| Semantic search only returns ACTIVE memories… (footnote)       |
+---------------------------------------------------------------+
```

- Filter bar: sticky under header; search is HTMX (`hx-get=/panel/memories/rows`, `hx-trigger="input changed delay:300ms"`, `hx-target="#mem-table-body"`, `hx-indicator` skeleton shimmer on the table).
- Rows: 48px, 13px, hairline dividers, hover `--surface-raised`. Content column: 2-line clamp, link-styled → opens **slide-over detail panel** (right drawer, 480px): full content, metadata grid (id mono + copy button, type, tags, confidence bar, source path, created/updated), **history chain** (superseded_by timeline — "edited from v3 → v4"), actions (Edit / Archive / Delete).
- Status pill: active=green, pending=amber, archived=gray.
- Tags: small tinted chips (neutral tint, not rainbow — one tint, text distinguishes).
- Empty state: centered icon + "No memories yet" + "Import from a harness or add your first memory" + [Import] button.

### 3.2 Inbox (待确认队列) — highest-traffic page, triage-first

This is the page the owner visits most. Design like an email triage / Linear inbox: **one decision per card, minimal chrome, fast approve**.

```
+---------------------------------------------------------------+
| Inbox (3)                      [Select all] [Approve all (3)] |
| ┌ Card ──────────────────────────────────────────────────┐   |
| │ ● code_style · from claude-code · conf 0.70 · 10:42    │   |
| │                                                        │   |
| │   User prefers Tailwind for frontend styling           │   |
| │                                                        │   |
| │                    [Approve] [Edit & approve] [Reject]  │   |
| └────────────────────────────────────────────────────────┘   |
| ┌ Card ──────────────────────────────────────────────────┐   |
| │ ...                                                    │   |
+---------------------------------------------------------------+
```

- Cards (not table rows): content is the hero (15px), metadata is a quiet 12px eyebrow line (tag chip · source client · confidence · time).
- Actions right-aligned: **Approve** (primary accent button), **Edit & approve** (secondary), **Reject** (ghost, turns red on hover). All HTMX POST → card swaps out with a brief success flash; remaining count updates; when empty → celebratory empty state ("Inbox zero — all caught up").
- Confidence shown as a thin progress bar under metadata (not a number alone).
- "Approve all" with `hx-confirm` ("Approve all 3 pending memories?").
- Keyboard: `a`/`e`/`r` shortcuts are nice-to-have; skip for v1, keep buttons large and fast.

### 3.3 Clients (授权)

```
+---------------------------------------------------------------+
| Clients                                   [+ Register client] |
+---------------------------------------------------------------+
| ┌ codex ───────────────● active──────────────[Revoke] ┐        |
| │ porter_8f3k… (mono, copy btn)                       │        |
| │ scopes: [memory.read] [memory.write]  last used 2h  │        |
| └────────────────────────────────────────────────────┘        |
| ┌ cursor ──────────────● revoked──────────[deleted]   ┐        |
| │ ...                                                 │        |
+---------------------------------------------------------------+
```

- Card per client: name (16px/600) + status dot/pill, client_id in mono with copy button, scope badges (tinted chips), "last used" relative time.
- Revoke: destructive outline button → `hx-confirm` dialog ("Revoke all tokens for 'codex'? Active sessions will be terminated immediately.").
- Register → modal (`<dialog>`): form (name, scopes checkboxes) → response shows **secret once** in a mono block with copy button + "I saved it" confirm. This is the one place a modal is correct (secret ceremony).

### 3.4 Audit (审计)

Langfuse Observations register: dense, filterable event stream.

```
+---------------------------------------------------------------+
| Audit                                    [1h|24h|7d|30d] [CSV] |
| [actor ▾] [action ▾] [search...                      ]        |
+---------------------------------------------------------------+
| TIME     | ACTOR  | ACTION          | DETAIL                    |
| 10:42:11 | owner  | memory.approve  | 13236885… → active        |
| 10:41:03 | codex  | memory.add      | pending_review (2 tags)     |
+---------------------------------------------------------------+
```

- 40px rows, mono timestamps, action as tinted chip by category (write=blue, approve=green, revoke/delete=red, auth=gray).
- Row click → expandable inline JSON detail (HTMX swap, no navigation).
- Time-range segmented control wired to all queries (Langfuse pattern). Export CSV button.
- "Immutable" note: small lock icon + "Append-only — entries cannot be edited or deleted" caption. Trust signal.

### 3.5 Snapshots (快照)

```
+---------------------------------------------------------------+
| Snapshots                              [+ Create snapshot]    |
+---------------------------------------------------------------+
| ┌ porter-2026-10-11.porter ──────────── ✓ signed ┐             |
| │ 128 memories · 2.1 MB · AES-256-GCM · 10:42   │             |
| │ [Download] [Inspect] [Apply]                  │             |
| └───────────────────────────────────────────────┘             |
+---------------------------------------------------------------+
```

- Cards with: filename (mono), signature badge (green ✓ signed / amber "legacy unsigned"), meta line, actions.
- Create → modal: name, encrypt toggle (default on), note about Ed25519 signing. 
- Apply → confirm dialog with **dry-run preview first** (counts: "will add 12, update 3, skip 113") — destructive, double confirm.
- Inspect → drawer with JSON tree of contents.

### 3.6 Sync (同步) — the differentiator page

This page sells the product ("automatic follow"). Railway service-card register + status dashboard.

```
+---------------------------------------------------------------+
| Sync                                        head seq #4821    |
| [stat: 3 online] [stat: 0 lagging] [stat: median lag 4s]      |
+---------------------------------------------------------------+
| ┌ ● codex ───────── online ───────────── cursor #4819 ┐       |
| │ lag: 2 changes · ▂▅▂▄▆ (sparkline) · last seen 12s │       |
| └────────────────────────────────────────────────────┘       |
| ┌ ● cursor ───────── online ───────────── cursor #4821 ┐      |
| │ lag: 0 · up to date · last seen 5s                  │      |
| └────────────────────────────────────────────────────┘       |
| ┌ ○ windsurf ─────── offline ────────────────────────┐       |
| │ last seen 2h ago · cursor #4790 (31 behind)         │       |
| └────────────────────────────────────────────────────┘       |
| How it works: adapters poll GET /sync/delta?since=<cursor>…   |
+---------------------------------------------------------------+
```

- Header stats: adapters online, lagging count, median lag — 24px numbers, quiet labels.
- Adapter cards: status dot (green <5min, amber 5–60min, gray >60min/offline), name, cursor in mono, lag line ("2 changes behind" or "up to date"), last-seen relative time.
- **Lag sparkline:** pure-CSS bar sparkline (last 20 lag samples as inline-block divs, 3px wide, height scaled) — no chart library. Render server-side from recent samples.
- Footer explainer card: "How sync works" — 3 steps (adapter writes → hub assigns sequence → others poll `/sync/delta`), mono endpoint, link to DEVELOPER.md. This teaches the mental model.
- Auto-refresh: `hx-get` poll every 15s on the stats/cards container (`hx-trigger="every 15s"`), swapped silently.

### 3.7 Settings (设置)

Grouped cards, max-width 720px (forms read better narrow):

1. **Language** card — segmented control 中文/EN (also in header; this is the full setting).
2. **Scope tags** — table of preset + custom tags (existing), add-tag inline form.
3. **Backup** — export button (porter-export/v1), snapshot shortcut link.
4. **Danger zone** — red-tinted card (`rgba(248,113,113,.06)` bg, red border): "Delete all memories", "Reset instance" — each with `hx-confirm` + type-to-confirm for reset.

---

## 4. Component patterns (build once, reuse)

| Pattern | Spec |
|---|---|
| **Buttons** | 4 variants: `primary` (accent bg, white text, hover darken), `secondary` (surface-raised bg, border), `ghost` (transparent, hover surface), `danger` (transparent, red text; hover red tint). Height 36px, padding 0 16px, radius 8px, 14px/500. Focus: 2px accent ring offset 2px. |
| **Inputs** | 36px, `--surface-raised` bg, 1px `--border-strong`, radius 8px, 14px. Focus: accent border + ring. Placeholder `--text-tertiary`. |
| **Tables** | 13px body, 12px uppercase header labels (`--text-tertiary`), 48px rows (40px for audit), hairline row dividers, sticky header on scroll, row hover `--surface-raised`. Mono for IDs/hashes/timestamps. |
| **Badges/pills** | 12px/500, `radius-full`, padding 2px 10px, tinted bg via `color-mix(in srgb, <color> 14%, transparent)` + colored text + 6px dot for status. Never color-only. |
| **Cards** | `--surface` bg, 1px `--border`, radius 12px, 20px padding. No shadows (tone separation). Hover: `--border-strong` for clickable cards. |
| **Empty states** | Centered: 40px muted SVG icon, 15px/600 title, 13px secondary description, primary CTA button. Generous vertical padding (64px). |
| **Toasts** | Bottom-right stack, `--surface-raised` bg, `--border-strong`, radius 12px, auto-dismiss 4s, types: success(green bar)/error(red bar)/info. Trigger: server sends `HX-Trigger: {"toast": {"type":"success","msg":"..."}}` → 10-line JS listener. |
| **Confirm dialogs** | HTMX `hx-confirm` for simple cases (revoke, approve-all). Native `<dialog>` element (styled) for ceremonies (client secret display, snapshot apply with dry-run preview). No JS modal library. |
| **Filter bars** | Sticky (`position: sticky; top: 56px`), `--bg` blur backdrop, 12px vertical padding, consistent anatomy: search input + selects + result count right-aligned. |
| **Skeletons** | For HTMX swaps: `.htmx-indicator` shimmer blocks (pulsing `--surface-raised` bars matching row shape). No spinner-only loading with layout shift. |
| **Copy buttons** | Small ghost icon button next to any mono ID/secret; click → clipboard + "Copied" toast. ~15 lines JS total. |
| **Drawers** | Right slide-over 480px for detail views (memory detail, snapshot inspect). `position: fixed`, `--surface` bg, `--border-strong` left border, overlay `rgba(0,0,0,.5)`. Close on Esc / overlay click. Can be HTMX-swapped content. |

**Icons:** single set, inline SVG, 16px default, `stroke="currentColor"`, stroke-width 2, Lucide/Feather path vocabulary (search, plus, check, x, clock, shield, key, database, refresh-cw, download, upload, settings, globe, moon, sun, copy, chevron-down, trash, edit, zap, activity). Implement as Python `icon(name, size=16)` helper returning the SVG string — no emoji icons anywhere.

---

## 5. CSS approach — recommendation

**Recommended: hand-written CSS with shadcn-style CSS variables. No Tailwind, no CDN.**

Rationale for this codebase:
1. **Offline-first is a product value** ("数据默认不出本地"). Tailwind Play CDN phones home on every page load — philosophically wrong here and breaks air-gapped self-hosting. A build-step Tailwind adds Node to a Python project's release story.
2. **Single source of truth:** one `porter/static/css/app.css` (~600–900 lines) + `tokens.css`. Every template references semantic classes (`.btn-primary`, `.card`, `.badge-success`) — the Python stays readable, and zh/en i18n is untouched (CSS is language-agnostic).
3. **Dark/light theming** falls out of the variable system: `:root` = dark, `[data-theme="light"]` overrides. No class duplication.
4. **HTMX already vendored** — keep the "zero network dependency" property: serve `/static` via FastAPI `StaticFiles` with immutable cache headers.

**File layout:**
```
porter/static/
  css/tokens.css      # :root + [data-theme=light] variables, fonts, base reset
  css/app.css         # layout shell, components, per-page styles
  js/htmx.min.js      # vendored (already in use — move local, drop CDN)
  js/panel.js         # ~60 lines: theme toggle, lang segmented control,
                      #   toast listener (HX-Trigger), copy buttons,
                      #   dialog helpers, sidebar collapse
porter/panel.py       # add: layout() shell fn, icon() helper, page fns use classes
```

**Rules for the implementation agent:**
- Zero `style=""` attributes in templates; zero hardcoded hex outside `tokens.css`.
- Class naming: BEM-lite (`.mem-table`, `.mem-table__row`, `.btn`, `.btn--primary`, `.badge`, `.badge--success`).
- Theme default: dark; toggle writes `localStorage("porter-theme")` + sets `data-theme` on `<html>`; respect `prefers-color-scheme` only on first visit.
- All interactive elements keyboard-reachable; `:focus-visible` accent ring everywhere.
- Contrast: body text ≥ 7:1 on surfaces in both themes (check the token pairs when implementing).

**What NOT to do:** don't add React/Vue/Svelte; don't add a CSS build step; don't pull icon fonts (inline SVG only); don't use `<table>` for card layouts or divs for data tables (semantics matter for a11y).

---

## 6. Don'ts (what makes infra dashboards look dated)

1. **Pure `#000` backgrounds** — use tinted near-black (`#0B0C0E`); pure black looks cheap and vibrates against white text.
2. **More than one accent color** — pick indigo, remove the rest. Status colors are semantic, not decorative.
3. **Bootstrap tells:** default blue `#0d6efd`, 4px radius everywhere, gray `#6c757d` secondary text, generic card shadows. Instant 2015.
4. **Gradients without purpose** — especially purple/blue "AI gradients" on chrome. Flat tinted surfaces read more premium.
5. **Card-in-card-in-card** — max one nesting level; group with spacing, not boxes.
6. **Emoji as icons** — single SVG icon family, consistent stroke weight.
7. **Color as the only signal** — every status gets dot + text label (a11y + zh/en clarity).
8. **Modal-first** — drawers and inline expansion beat modals for detail views; reserve modals for ceremonies (secret display) and destructive confirms.
9. **Spinner-only loading with layout shift** — skeleton rows matching the content shape.
10. **Tables that reflow on mobile** — horizontal scroll with sticky first column instead.
11. **Wall of equal-weight cards** — establish hierarchy: one hero number/stat per page, supporting info quieter (the Sync page stats row is the template).
12. **Random dark sections in light mode** (or vice versa) — one continuous tonal system per theme.
13. **Oversaturated accents** — keep saturation <80%; the accent should recede, not scream (Linear: "don't compete for attention you haven't earned").

---

## 7. Implementation order (for the follow-up agent)

1. `porter/static/` scaffold: `tokens.css` (exact values from §1), `app.css` shell + buttons/inputs/badges/cards/tables, `panel.js` (~60 lines), vendored `htmx.min.js`; wire `StaticFiles` in app.
2. `panel.py`: `layout()` shell (sidebar/header per §2) + `icon()` helper; convert all 7 pages to the shell.
3. Per-page builds in traffic order: **Inbox → Memories → Sync → Clients → Audit → Snapshots → Settings** (Inbox first — it's the highest-traffic page and the triage-card pattern sets the visual tone).
4. Toast system (`HX-Trigger`) + `hx-confirm` on destructive actions + copy buttons.
5. Theme toggle + lang segmented control in header; responsive breakpoints.
6. Full test suite green + visual screenshot pass (Playwright already available in the venv from the demo work) + contrast spot-checks.

**Acceptance bar:** open any page next to Linear/Raycast/Supabase screenshots — it should look like it belongs in that family. If it looks like Bootstrap, it's wrong.
