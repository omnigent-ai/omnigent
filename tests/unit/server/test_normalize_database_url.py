import pytest

from omnigent.db.utils import normalize_database_url


@pytest.mark.parametrize(
    "input_url, expected",
    [
        # PaaS providers emit postgres:// (shorthand form)
        (
            "postgres://user:pw@host:5432/db",
            "postgresql+psycopg://user:pw@host:5432/db",
        ),
        # Standard form without driver specifier
        (
            "postgresql://user:pw@host:5432/db",
            "postgresql+psycopg://user:pw@host:5432/db",
        ),
        # Already correct — pass through unchanged
        (
            "postgresql+psycopg://user:pw@host:5432/db",
            "postgresql+psycopg://user:pw@host:5432/db",
        ),
        # SQLite (local dev) — pass through unchanged
        (
            "sqlite:///./omnigent.db",
            "sqlite:///./omnigent.db",
        ),
        # Credentials with special characters survive the prefix rewrite
        (
            "postgres://user:p%40ssword@host/db",
            "postgresql+psycopg://user:p%40ssword@host/db",
        ),
    ],
)
def test_normalize_database_url(input_url: str, expected: str) -> None:
    assert normalize_database_url(input_url) == expected
