"""SQL splitting regressions found during real container startup."""

from pathlib import Path

import pytest

from porter.db import _split_statements


@pytest.mark.parametrize(
    "comment", ["-- trailing comment", "/* trailing; comment */", "-- first\n/* second */"]
)
def test_comment_only_tail_is_not_executed(comment):
    assert _split_statements("SELECT 1; " + comment) == ["SELECT 1"]
    assert _split_statements(comment) == []


def test_quoted_comments_and_semicolons_are_preserved():
    sql = "SELECT 'a;--b'; SELECT $$/*c;d*/$$; -- trailing"
    assert _split_statements(sql) == ["SELECT 'a;--b'", "SELECT $$/*c;d*/$$"]


def test_oauth_migration_ends_with_sql_not_a_comment():
    root = Path(__file__).resolve().parents[1]
    statements = _split_statements((root / "migrations/002_oauth.sql").read_text(encoding="utf-8"))
    assert len(statements) == 7
    assert "ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'security'" in statements[-1]


def test_migration_numbers_are_unique_and_legacy_names_are_not_reused():
    from porter.db import LEGACY_MIGRATION_NAMES, MIGRATIONS_DIR

    names = [path.name for path in MIGRATIONS_DIR.glob("*.sql")]
    numbers = [name.split("_", 1)[0] for name in names]
    assert len(numbers) == len(set(numbers))
    assert set(LEGACY_MIGRATION_NAMES).issubset(names)
    assert not set(LEGACY_MIGRATION_NAMES.values()).intersection(names)
