"""Check local documentation and packaging contracts without network or writes.

Run from any working directory. Markdown checks cover local file destinations
in inline/reference links and HTML href/src attributes, plus EN/zh-CN pairs.
Fragments and external URLs are deliberately not validated here.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tomllib
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = (
    "README.md",
    "README.zh-CN.md",
    "CHANGELOG.md",
    "CHANGELOG.zh-CN.md",
    "CONTRIBUTING.md",
    "CONTRIBUTING.zh-CN.md",
    "SECURITY.md",
    "SECURITY.zh-CN.md",
    "CODE_OF_CONDUCT.md",
    "CODE_OF_CONDUCT.zh-CN.md",
    "LICENSE",
    "VERSION",
    "pyproject.toml",
    ".env.example",
    "docs/MAINTAINERS.md",
    "docs/MAINTAINERS.zh-CN.md",
    "docs/ACCEPTANCE.md",
    "docs/ACCEPTANCE.zh-CN.md",
)
INLINE_LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^\s)]+))")
REFERENCE_LINK = re.compile(r"^\s{0,3}\[[^\]\n]+\]:\s*(?:<([^>\n]+)>|(\S+))", re.M)
HTML_LINK = re.compile(r"\b(?:href|src)\s*=\s*[\"']([^\"']+)[\"']", re.I)


def prose_only(markdown: str) -> str:
    """Remove fenced examples while retaining line positions for diagnostics."""
    result: list[str] = []
    fence: tuple[str, int] | None = None
    for line in markdown.splitlines(keepends=True):
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fence is None and marker:
            fence = (marker[1][0], len(marker[1]))
            result.append("\n")
        elif fence is not None:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1]:
                fence = None
            result.append("\n")
        else:
            result.append(line)
    return "".join(result)


def destinations(markdown: str) -> list[tuple[int, str]]:
    prose = prose_only(markdown)
    links: list[tuple[int, str]] = []
    for pattern in (INLINE_LINK, REFERENCE_LINK, HTML_LINK):
        for match in pattern.finditer(prose):
            destination = next(value for value in match.groups() if value is not None)
            links.append((prose.count("\n", 0, match.start()) + 1, destination))
    return links


def exact_case_exists(path: Path, root: Path) -> bool:
    """Catch Linux-breaking case errors even when checks run on Windows."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    for segment in relative.parts:
        if not current.is_dir() or segment not in {entry.name for entry in current.iterdir()}:
            return False
        current /= segment
    return current.exists()


def check_repository(root: Path) -> tuple[list[str], int, int]:
    errors: list[str] = []
    for filename in REQUIRED_FILES:
        if not (root / filename).is_file():
            errors.append(f"missing required file: {filename}")

    markdown_paths = sorted(
        {
            *root.glob("*.md"),
            *(root / "docs").rglob("*.md"),
            *(root / "assets").rglob("*.md"),
            *(root / ".github").rglob("*.md"),
        }
    )
    link_count = 0
    for path in markdown_paths:
        display = path.relative_to(root).as_posix()
        try:
            markdown = path.read_text(encoding="utf-8")
        except UnicodeError:
            errors.append(f"{display}: documentation must be UTF-8")
            continue
        links = destinations(markdown)
        for line, destination in links:
            try:
                parts = urlsplit(destination)
            except ValueError:
                errors.append(f"{display}:{line}: malformed link: {destination}")
                continue
            if parts.scheme or parts.netloc or not parts.path:
                continue
            link_count += 1
            # resolve() canonicalizes existing filename case on Windows and
            # would hide a link spelling that breaks on a Linux checkout.
            target = Path(os.path.abspath(path.parent / unquote(parts.path)))
            if not target.resolve().is_relative_to(root) or not exact_case_exists(target, root):
                errors.append(f"{display}:{line}: missing local destination: {destination}")
        if ".github" not in path.relative_to(root).parts:
            sibling_name = (
                path.name.replace(".zh-CN.md", ".md")
                if path.name.endswith(".zh-CN.md")
                else path.stem + ".zh-CN.md"
            )
            if not path.with_name(sibling_name).is_file():
                errors.append(f"{display}: missing bilingual sibling: {sibling_name}")
            elif not any(destination == sibling_name for _, destination in links):
                errors.append(f"{display}: add a link to bilingual sibling: {sibling_name}")

    metadata_path = root / "pyproject.toml"
    if metadata_path.is_file():
        try:
            metadata = tomllib.loads(metadata_path.read_text(encoding="utf-8"))
            project = metadata["project"]
            if not project.get("description", "").strip():
                errors.append("pyproject.toml: project.description must describe the package")
            if project.get("readme") != "README.md" or project.get("license") != "MIT":
                errors.append("pyproject.toml: package README/license must match the repository")
            version_source = metadata["tool"]["setuptools"]["dynamic"]["version"]["file"]
            if version_source != ["VERSION"]:
                errors.append("pyproject.toml: VERSION must be the single package version source")
        except (tomllib.TOMLDecodeError, KeyError, UnicodeError) as exc:
            errors.append(f"pyproject.toml: invalid package metadata: {exc}")

    migration_ids = Counter(
        path.name.split("_", 1)[0] for path in (root / "migrations").glob("*.sql")
    )
    for number, count in migration_ids.items():
        if count > 1:
            errors.append(f"migrations: duplicate numeric prefix: {number}")
    if not migration_ids:
        errors.append("migrations: no SQL migrations found")
    return errors, len(markdown_paths), link_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT, help="repository checkout")
    args = parser.parse_args()
    errors, pages, links = check_repository(args.root.resolve())
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"Repository checks failed: {len(errors)} error(s).", file=sys.stderr)
        return 1
    print(
        f"Repository checks passed: {pages} Markdown files, {links} local links, metadata and migrations."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
