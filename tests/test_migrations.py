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
