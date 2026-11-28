"""Dialect-family checks shared by the stores and Alembic migrations.

Cloudflare D1 serves SQLite over HTTP under the ``cloudflare_d1`` dialect name,
so code that branches on SQLite behavior must test the family, not the literal
``"sqlite"`` name. ``tests/db/test_migrations_sqlite_safe.py`` enforces this for
migrations.
"""

from __future__ import annotations

from sqlalchemy.engine import Connection, Dialect, Engine

SQLITE_DIALECT = "sqlite"
CLOUDFLARE_D1_DIALECT = "cloudflare_d1"
SQLITE_FAMILY_DIALECTS = frozenset({SQLITE_DIALECT, CLOUDFLARE_D1_DIALECT})

DialectSource = str | Dialect | Connection | Engine


def dialect_name(source: DialectSource) -> str:
    """
    Return the SQLAlchemy dialect name for *source*.

    :param source: A dialect name such as ``"sqlite"``, a ``Dialect``, or a
        ``Connection`` / ``Engine`` (e.g. ``op.get_bind()``).
    :returns: The dialect name, e.g. ``"cloudflare_d1"``.
    """
    if isinstance(source, str):
        return source
    if isinstance(source, Dialect):
        return source.name
    return source.dialect.name


def is_sqlite_family(source: DialectSource) -> bool:
    """
    Whether *source* runs SQLite, locally or as Cloudflare D1.

    :param source: Anything :func:`dialect_name` accepts.
    :returns: ``True`` for ``sqlite`` and ``cloudflare_d1``.
    """
    return dialect_name(source) in SQLITE_FAMILY_DIALECTS


def is_cloudflare_d1(source: DialectSource) -> bool:
    """
    Whether *source* is Cloudflare D1, which has no SQL transactions or savepoints.

    :param source: Anything :func:`dialect_name` accepts.
    :returns: ``True`` only for ``cloudflare_d1``.
    """
    return dialect_name(source) == CLOUDFLARE_D1_DIALECT
