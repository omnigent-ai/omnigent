"""Auto-loaded shim that makes Omnigent work against Cloudflare D1.

Python imports ``sitecustomize`` at interpreter startup, so any process
launched with this directory on the path (it is dropped into site-packages by
the Dockerfile) applies these fixes before Omnigent builds an engine or runs
migrations.

D1 is SQLite-over-HTTP, but the third-party ``sqlalchemy-cloudflare-d1``
dialect subclasses ``DefaultDialect`` (not ``SQLiteDialect``), so several
things break. Every fix below is "treat cloudflare_d1 exactly like sqlite,"
which is correct because D1 *is* SQLite. The clean long-term fix is to make the
dialect subclass ``SQLiteDialect`` upstream; until then this shim carries it.

  1. Alembic's ddl-impl registry (``alembic.ddl.impl._impls``) is keyed by
     dialect name with no fallback -> ``KeyError('cloudflare_d1')`` in
     ``MigrationContext.__init__`` (online AND offline). Register a SQLite-
     behavior impl under that name.
  2. The dialect uses generic (DefaultDialect) SQL/DDL compilers, so e.g. a
     composite primary key emits BOTH an inline column ``PRIMARY KEY`` and a
     table-level ``PRIMARY KEY (...)`` -> D1 rejects it ("more than one primary
     key"), and reserved words like ``key`` go unquoted. Point the dialect at
     SQLite's compilers so every table compiles byte-identical to native sqlite.
  3. ``get_foreign_keys()`` omits ``referred_schema`` / ``name`` keys that
     SQLAlchemy reflection accesses with ``dict[...]`` -> ``KeyError`` during
     the table reflection ``batch_alter_table`` performs. D1 has no schemas.
  4. ``get_unique_constraints()`` is unimplemented (base ``Dialect`` raises
     ``NotImplementedError``), but migrations call it directly. Implement it the
     SQLite way (``PRAGMA index_list`` with origin ``'u'``).
"""
import sys

try:
    from alembic.ddl.sqlite import SQLiteImpl

    class CloudflareD1Impl(SQLiteImpl):  # auto-registers via __dialect__
        __dialect__ = "cloudflare_d1"
except Exception as exc:  # pragma: no cover - never block startup
    print(f"[d1-shim] could not register Alembic impl: {exc}", file=sys.stderr)

try:
    from sqlalchemy.dialects.sqlite.base import (
        SQLiteCompiler,
        SQLiteDDLCompiler,
        SQLiteIdentifierPreparer,
        SQLiteTypeCompiler,
    )
    from sqlalchemy_cloudflare_d1.dialect import CloudflareD1Dialect

    # D1 == SQLite: borrow SQLite's full compilation machinery.
    CloudflareD1Dialect.ddl_compiler = SQLiteDDLCompiler
    CloudflareD1Dialect.statement_compiler = SQLiteCompiler
    CloudflareD1Dialect.type_compiler = SQLiteTypeCompiler
    CloudflareD1Dialect.preparer = SQLiteIdentifierPreparer

    # Reflection: inject the keys SQLAlchemy requires (D1 has no schemas).
    _orig_get_fks = CloudflareD1Dialect.get_foreign_keys

    def _get_fks_with_schema(self, connection, table_name, schema=None, **kw):
        fks = _orig_get_fks(self, connection, table_name, schema=schema, **kw)
        for fk in fks:
            fk.setdefault("referred_schema", None)
            fk.setdefault("name", None)
        return fks

    CloudflareD1Dialect.get_foreign_keys = _get_fks_with_schema

    # Reflection: implement get_unique_constraints the SQLite way.
    if not getattr(CloudflareD1Dialect, "_d1_unique_patched", False):
        from sqlalchemy import text as _text

        def _get_unique_constraints(self, connection, table_name, schema=None, **kw):
            prep = self.identifier_preparer.quote_identifier
            out = []
            for row in connection.execute(_text(f"PRAGMA index_list({prep(table_name)})")):
                _, idx_name, unique, origin = row[0], row[1], row[2], row[3]
                if not unique or origin != "u":  # 'u' == a UNIQUE constraint
                    continue
                cols = [r[2] for r in connection.execute(
                    _text(f"PRAGMA index_info({prep(idx_name)})"))]
                name = None if idx_name.startswith("sqlite_autoindex_") else idx_name
                out.append({"name": name, "column_names": cols})
            return out

        CloudflareD1Dialect.get_unique_constraints = _get_unique_constraints
        CloudflareD1Dialect._d1_unique_patched = True
except Exception as exc:  # pragma: no cover - never block startup
    print(f"[d1-shim] could not patch D1 compilers: {exc}", file=sys.stderr)
