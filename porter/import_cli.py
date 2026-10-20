"""CLI: import harness memories into Porter's pending inbox.

Usage:
    python -m porter.import_cli --harness codex --dry-run
    python -m porter.import_cli --harness all --api-url http://localhost:8000 \\
        --token "$PORTER_TOKEN"
    python -m porter.import_cli --harness cursor --project-root ~/myproj --dry-run
    python -m porter.import_cli --harness chatgpt --file ~/export/conversations.json --dry-run
    python -m porter.import_cli --harness claude --file ~/export.zip --token "$PORTER_TOKEN"

READ-ONLY on harness files: the CLI never writes to ~/.codex, ~/.cursor,
or ~/.config/zed. Imports go through the REST API into the pending-review
inbox; nothing becomes searchable until the user approves it in the panel.

Exit codes: 0 ok, 1 usage/config error, 2 API error.
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx

from .importers import (
    NO_NATIVE_STORE,
    ChatGPTExportImporter,
    ClaudeCodeImporter,
    ClaudeExportImporter,
    ClineImporter,
    CodexImporter,
    CursorImporter,
    DevinImporter,
    DroidImporter,
    GeminiImporter,
    JunieImporter,
    OpenHandsImporter,
    QwenImporter,
    RooImporter,
    TabnineImporter,
    WindsurfImporter,
    ZedImporter,
    import_dedupe_key,
    scrub_secrets,
)

DUPLICATE_THRESHOLD = 0.92

_ALL = [
    "codex",
    "cursor",
    "zed",
    "claude-code",
    "windsurf",
    "cline",
    "roo",
    "gemini",
    "qwen",
    "junie",
    "droid",
    "tabnine",
    "devin",
    "openhands",
]

# Official data exports: need --file, intentionally not part of "all".
_EXPORTS = ["chatgpt", "claude"]


def _build_importers(args) -> list:
    out = []
    want = _ALL if args.harness == "all" else [args.harness]
    for h in want:
        if h == "codex":
            out.append(CodexImporter(args.codex_dir))
        elif h == "cursor":
            out.append(CursorImporter(args.project_root))
        elif h == "zed":
            out.append(ZedImporter())
        elif h == "claude-code":
            out.append(ClaudeCodeImporter(args.claude_home))
        elif h == "windsurf":
            out.append(WindsurfImporter(args.windsurf_dir))
        elif h == "cline":
            out.append(ClineImporter(args.project_root))
        elif h == "roo":
            out.append(RooImporter(args.project_root))
        elif h == "gemini":
            out.append(GeminiImporter(args.gemini_dir, args.project_root))
        elif h == "qwen":
            out.append(QwenImporter(args.qwen_dir, args.project_root))
        elif h == "junie":
            out.append(JunieImporter(args.junie_dir, args.project_root))
        elif h == "droid":
            out.append(DroidImporter(args.factory_dir, args.project_root))
        elif h == "tabnine":
            out.append(TabnineImporter(None, args.project_root))
        elif h == "devin":
            out.append(DevinImporter(args.project_root))
        elif h == "openhands":
            out.append(OpenHandsImporter(args.project_root))
        elif h == "chatgpt":
            out.append(ChatGPTExportImporter(args.file))
        elif h == "claude":
            out.append(ClaudeExportImporter(args.file))
    return out


def _preview(text: str, n: int = 80) -> str:
    t = " ".join(text.split())
    return t[:n] + ("…" if len(t) > n else "")


def _api(api_url: str, token: str) -> httpx.Client:
    return httpx.Client(
        base_url=api_url.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )


def _existing_paths(client: httpx.Client) -> set[str]:
    """Layer-1 dedupe: import keys already in Porter (any status).

    The key is import_dedupe_key() over each stored source dict — the
    plain imported_from path for most importers, plus conversation_id/kind
    for export importers (several memories per file).
    """
    keys: set[str] = set()
    offset = 0
    while True:
        r = client.get("/api/v1/memories", params={"limit": 200, "offset": offset})
        # Non-admin tokens only see active memories; that is fine — a path
        # imported before is at least active, otherwise it is still pending
        # and the admin/panel flow will surface it. We still try everything.
        if r.status_code != 200:
            print(f"warning: list failed: {r.status_code} {r.text[:120]}", file=sys.stderr)
            break
        rows = r.json()
        if not rows:
            break
        for row in rows:
            keys.add(import_dedupe_key(row.get("source") or {}))
        if len(rows) < 200:
            break
        offset += 200
    return keys


def _is_duplicate(client: httpx.Client, candidate_text: str) -> str | None:
    """Layer-2 dedupe: semantic similarity > threshold to an active memory."""
    r = client.post("/api/v1/memories/search", json={"query": candidate_text[:500], "top_k": 3})
    if r.status_code != 200:
        return None
    results = r.json().get("results", [])
    if results and results[0].get("score", 0) > DUPLICATE_THRESHOLD:
        return results[0]["id"]
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Import coding-harness memories into Porter (read-only on "
        "harness files; imports land in the pending inbox)."
    )
    ap.add_argument(
        "--harness",
        default="all",
        choices=["all"] + _ALL + _EXPORTS,
        help="which harness to import from "
        "('all' = every importer with a native store; "
        "chatgpt/claude need --file)",
    )
    ap.add_argument(
        "--file",
        default=None,
        help="export file for --harness chatgpt/claude: a "
        "conversations.json path or a .zip containing it "
        "(read in memory, never extracted)",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="list what would be imported; change nothing"
    )
    ap.add_argument(
        "--project-root",
        action="append",
        default=[],
        help="project root to scan (cline/roo memory-bank, "
        "cursor .cursor/memories, gemini/qwen GEMINI.md, "
        "junie .junie/guidelines.md, droid/tabnine AGENTS.md/"
        "TABNINE.md, devin .devin/, openhands .agents/skills; "
        "repeatable)",
    )
    ap.add_argument("--codex-dir", default=None, help="override ~/.codex/memories")
    ap.add_argument("--claude-home", default=None, help="override ~/.claude")
    ap.add_argument("--windsurf-dir", default=None, help="override ~/.codeium/windsurf/memories")
    ap.add_argument("--gemini-dir", default=None, help="override ~/.gemini")
    ap.add_argument("--qwen-dir", default=None, help="override ~/.qwen")
    ap.add_argument("--junie-dir", default=None, help="override ~/.junie")
    ap.add_argument("--factory-dir", default=None, help="override ~/.factory (Factory Droid)")
    ap.add_argument("--api-url", default="http://localhost:8000")
    ap.add_argument(
        "--token",
        default=os.environ.get("PORTER_TOKEN", ""),
        help="Bearer token with memory.write scope (or PORTER_TOKEN env)",
    )
    ap.add_argument("--limit", type=int, default=500)
    ap.epilog = (
        "Harnesses with no native portable memory store "
        "(MCP implant only, nothing to import): " + ", ".join(sorted(NO_NATIVE_STORE))
    )
    args = ap.parse_args(argv)

    if args.harness in _EXPORTS and not args.file:
        print(
            f"error: --harness {args.harness} requires --file "
            "(path to conversations.json or a .zip containing it)",
            file=sys.stderr,
        )
        return 1

    importers = _build_importers(args)
    all_candidates = []
    for imp in importers:
        candidates, note = imp.scan()
        print(f"[{imp.harness}] {note}")
        all_candidates.extend([(imp.harness, c) for c in candidates[: args.limit]])

    if not all_candidates:
        print("nothing to import.")
        return 0

    if args.dry_run:
        print(
            f"\n--dry-run: {len(all_candidates)} memories would be imported "
            "into the pending inbox:\n"
        )
        for harness, c in all_candidates:
            # Preview the SCRUBBED text: dry-run must never print raw
            # secrets to the terminal.
            scrubbed, n_redact = scrub_secrets(c.content)
            scrubbed_title, _ = scrub_secrets(c.title)
            print(f"  [{harness}] {scrubbed_title}")
            print(f"    from: {c.imported_from}")
            print(f"    preview: {_preview(scrubbed)}")
            if n_redact:
                print(f"    secrets scrubbed: {n_redact} (values never shown)")
        print("\nRe-run without --dry-run to import (needs --token).")
        return 0

    if not args.token:
        print("error: --token (or PORTER_TOKEN env) is required for import", file=sys.stderr)
        return 1

    client = _api(args.api_url, args.token)
    try:
        existing = _existing_paths(client)
    except httpx.HTTPError as exc:
        print(f"error: cannot reach Porter API at {args.api_url}: {exc}", file=sys.stderr)
        return 2

    imported = skipped_path = skipped_dup = redactions = 0
    for harness, c in all_candidates:
        key = import_dedupe_key({**c.source, "imported_from": c.imported_from})
        if key in existing:
            skipped_path += 1
            continue
        scrubbed, n_redact = scrub_secrets(c.content)
        redactions += n_redact
        dup_of = _is_duplicate(client, c.title + "\n" + scrubbed)
        if dup_of:
            skipped_dup += 1
            print(f"  skip (duplicate of {dup_of}): {c.title}")
            continue
        r = client.post(
            "/api/v1/memories",
            json={
                "content": scrubbed,
                "type": c.type,
                "scope_tags": c.scope_tags,
                "confidence": c.confidence,
                "source": {**c.source, "redactions": n_redact},
            },
        )
        if r.status_code != 202:
            print(
                f"error: import failed for {c.imported_from}: {r.status_code} {r.text[:200]}",
                file=sys.stderr,
            )
            return 2
        imported += 1
        print(f"  imported: [{harness}] {c.title}")

    print(
        f"\ndone: imported={imported} skipped_already_imported={skipped_path} "
        f"skipped_duplicate={skipped_dup} secrets_redacted={redactions}"
    )
    print(
        "All imports are PENDING — approve them in the Porter panel before they become searchable."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
