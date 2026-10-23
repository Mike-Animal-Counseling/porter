"""Postgres connection pool + migration runner (asyncpg)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
if not MIGRATIONS_DIR.is_dir():
    MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
NO_TRANSACTION_MARKER = "-- porter:no-transaction"
LEGACY_MIGRATION_NAMES = {
    "010_memory_domains.sql": "008_memory_domains.sql",
    "011_sync_visibility.sql": "009_sync_visibility.sql",
    "012_automation_audit.sql": "010_automation_audit.sql",
    "013_sync_commit_order.sql": "011_sync_commit_order.sql",
}


def json_object(value: Any) -> dict:
    """Decode asyncpg's default JSON/JSONB text and custom-codec objects."""
    if value is None:
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return dict(value)


def _split_statements(sql: str) -> list[str]:
    """Split SQL on semicolons outside $$...$$ blocks, strings and comments.

    Needed for migrations that cannot run inside a transaction block
    (e.g. ALTER TYPE ... ADD VALUE). Kept small and strict: dollar-quoting,
    single-quoted strings ('' escapes), line comments (--), block comments.
    """
    stmts, buf = [], []
    i, n = 0, len(sql)
    in_dollar = False
    in_squote = False
    in_line_comment = False
    in_block_comment = False
    has_sql = False
    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if in_line_comment:
            buf.append(c)
            if c == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            buf.append(c)
            if c == "*" and nxt == "/":
                buf.append(nxt)
                i += 2
                in_block_comment = False
                continue
            i += 1
            continue
        if in_squote:
            buf.append(c)
            if c == "'":
                if nxt == "'":
                    buf.append(nxt)
                    i += 2
                    continue
                in_squote = False
            i += 1
            continue
        if in_dollar:
            if c == "$" and nxt == "$":
                buf.append("$$")
                i += 2
                in_dollar = False
                continue
            buf.append(c)
            i += 1
            continue
        # normal mode
        if c == "$" and nxt == "$":
            buf.append("$$")
            has_sql = True
            i += 2
            in_dollar = True
            continue
        if c == "'":
            buf.append(c)
            has_sql = True
            in_squote = True
            i += 1
            continue
        if c == "-" and nxt == "-":
            buf.append("--")
            i += 2
            in_line_comment = True
            continue
        if c == "/" and nxt == "*":
            buf.append("/*")
            i += 2
            in_block_comment = True
            continue
        if c == ";":
            stmt = "".join(buf).strip()
            if stmt and has_sql:
                stmts.append(stmt)
            buf = []
            has_sql = False
            i += 1
            continue
        if not c.isspace():
            has_sql = True
        buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail and has_sql:
        stmts.append(tail)
    return stmts


async def create_pool(database_url: str, min_size: int = 2, max_size: int = 10) -> asyncpg.Pool:
    return await asyncpg.create_pool(database_url, min_size=min_size, max_size=max_size)


async def run_migrations(database_url: str) -> list[str]:
    """Apply pending *.sql migrations in order. Idempotent via schema_migrations."""
    applied: list[str] = []
    con = await asyncpg.connect(database_url)
    try:
        # API and multiple stdio processes can bootstrap simultaneously.
        # Serialize the migration runner until this connection closes.
        await con.execute("SELECT pg_advisory_lock($1)", 0x504F52544552)
        await con.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())"
        )
        done = {r["name"] for r in await con.fetch("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            legacy = LEGACY_MIGRATION_NAMES.get(path.name)
            if legacy in done:
                # Adopt pre-integration review names without rerunning SQL.
                await con.execute(
                    "UPDATE schema_migrations SET name = $1 WHERE name = $2", path.name, legacy
                )
                done.add(path.name)
                continue
            sql = path.read_text(encoding="utf-8")
            if NO_TRANSACTION_MARKER in sql.split("\n", 1)[0]:
                # Statements that Postgres forbids inside a transaction block
                # (ALTER TYPE ... ADD VALUE). Each runs in its own implicit
                # transaction (autocommit).
                for stmt in _split_statements(sql):
                    await con.execute(stmt)
                await con.execute("INSERT INTO schema_migrations (name) VALUES ($1)", path.name)
            else:
                async with con.transaction():
                    await con.execute(sql)
                    await con.execute("INSERT INTO schema_migrations (name) VALUES ($1)", path.name)
            applied.append(path.name)
    finally:
        await con.close()
    return applied
