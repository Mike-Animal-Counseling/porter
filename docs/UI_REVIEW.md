**English** | [中文](UI_REVIEW.zh-CN.md)

# Panel interface review — 2026-10-20

This pass preserves the existing dark/light panel, indigo accent and working
layouts. It improves control naming, focus management, feedback and readability;
it does not replace the visual design. The relevant implementation is
[`panel.py`](../porter/panel.py), [`panel.js`](../porter/static/js/panel.js),
[`app.css`](../porter/static/css/app.css) and
[`tokens.css`](../porter/static/css/tokens.css).

## Changes made

- **Names and landmarks:** navigation has a name and current-page state; links
  keep accessible names in the tablet icon rail. Form labels reference controls,
  grouped switches have names, and a skip link targets the main content.
- **Memory drawer:** the closed dialog is inert and hidden from the accessibility
  tree. Opening it enables the dialog, moves focus inside, makes the surrounding
  shell inert and locks background scroll. Tab/Shift+Tab wrap within its controls;
  Escape closes it and returns focus to the connected opener.
- **Mobile navigation:** the menu button exposes expanded state and its target;
  closed narrow-screen navigation is inert and hidden. Breakpoint changes reset
  the state; Escape closes an open menu and returns focus to the toggle.
- **Phone controls:** icon/copy buttons have minimum 44 × 44 CSS pixel targets;
  buttons, segmented switches and navigation have at least 44 pixel height.
  Phone inputs, textareas and selects use 16 pixel text.
- **Failures and copying:** HTTP/HTMX response, network and timeout failures show
  localized feedback. Language changes reject unsuccessful responses. Copy has
  a fallback, reports failure and restores the previous focus; the toast region
  announces feedback politely.
- **Contrast tokens:** tertiary text is `#626973` in light mode and `#8B94A1`
  in dark mode. Action-button fills and their text use separate tokens from
  semantic success/warning/error text, so improving one does not break another.
  Source-based tests calculate a minimum 4.5:1 contrast for the checked normal
  text/surface, badge and action-button combinations, including alpha compositing
  and the checked hover states.
- **Motion and canvas work:** reduced-motion preferences disable drawer/sidebar
  transitions and toast entry animation. Canvas click/resize handlers are bound
  once per canvas; resize calculations are debounced by 150 ms.

## Evidence and limits

[`tests/test_panel_accessibility.py`](../tests/test_panel_accessibility.py)
passed **42 cases** in this pass. They inspect rendered English/Chinese HTTP
responses for names, IDs, label/ARIA references, landmarks and the initial dialog
state, and calculate contrast from the shipped CSS. They do not execute browser
JavaScript, measure actual layout, test touch interaction or prove focus behavior.

The Impeccable source detector ran once. Its one warning was the existing
3 pixel semantic left border on `.toast` (`app.css:422` at the time of the run).
It distinguishes success/error/info feedback and was retained as that status
structure; no new decorative card treatment was introduced for this pass.
The detector is a source heuristic, not a browser accessibility audit.

Browser connection attempts returned **“No browser is available.”** No screenshots
or browser interaction results were captured. These changes and passing tests
are **not a WCAG certification or a completed visual acceptance**. The final
browser checks remain open in [ACCEPTANCE.md](ACCEPTANCE.md).

## Human checks still required

Use synthetic records at desktop width, **768 px** and **390 px**, in
**English and Chinese**, with **light and dark themes**. Record the actual browser,
viewport and candidate commit; screenshots are optional and must exclude secrets.

- Navigate by Tab from the skip link through controls. Open a memory drawer;
  check Tab/Shift+Tab containment, Escape, scroll lock and focus restoration.
- At 768 px confirm icon-rail navigation remains understandable; at 390 px open
  and close the sidebar, test Escape and resize back to desktop. Hidden controls
  must not receive focus.
- Review a pending synthetic memory, search/filter and switch language/theme.
  Confirm normal and 200% zoom remain readable without blocking key actions.
- In browser development tools, temporarily fail an HTMX request or use an
  offline connection. Check localized failure feedback rather than a false
  success. Restore connectivity afterward.
- Preview a handoff; test copy with normal clipboard permission and with
  clipboard access denied, then test download. Verify actual copied/file content.
- Enable reduced motion and resize the memory graph repeatedly; check that
  transitions respect the preference and one click triggers one drawer request.

Record failures as reproducible issues. A response status of 200, static color
calculation or detector result cannot close these browser checks.
