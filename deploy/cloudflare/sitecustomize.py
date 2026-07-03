"""Auto-loaded shim that makes Omnigent work against Cloudflare D1.

D1 is SQLite reached over an HTTP REST API. The third-party
``sqlalchemy-cloudflare-d1`` dialect subclasses the *generic* ``DefaultDialect``
and then hand-reimplements SQLite's SQL compilation and schema reflection —
incompletely. That breaks DDL (a composite primary key emits two ``PRIMARY
KEY`` clauses, which D1 rejects; reserved words like ``key`` go unquoted) and
migrations (``get_unique_constraints`` is unimplemented; ``get_foreign_keys``
drops keys reflection needs).

The fix is to subclass SQLAlchemy's real ``SQLiteDialect`` and keep only the
*transport*: the HTTP DBAPI, the URL parser, and the D1 type processors (which
base64-encode blobs and ISO-format dates for the JSON REST API — SQLite's file
DBAPI does this natively, D1's API does not). Everything above the transport —
DDL compiler, type compiler, identifier quoting, and full reflection — is then
inherited from SQLite, correctly. This is the change that belongs upstream in
the dialect package (just change its base class); until it ships, sitecustomize
re-registers a corrected dialect here. Python imports ``sitecustomize`` at
interpreter startup, so it runs before Omnigent builds an engine.

Two small adaptations remain, both expressing facts about D1 rather than SQLite
shortcomings:

  * **Alembic.** Its DDL-impl registry (``alembic.ddl.impl._impls``) is keyed by
    ``dialect.name`` with no inheritance fallback, so the (correctly named)
    ``cloudflare_d1`` dialect ``KeyError``s in ``MigrationContext.__init__``.
    Register SQLite's impl under that name.
  * **No ``temp`` schema.** D1 exposes a single ``main`` schema and forbids the
    ``temp`` schema (``SQLITE_AUTH``). SQLite's reflection probes ``temp``
    (``PRAGMA temp.*``, ``sqlite_temp_master``, ``PRAGMA database_list``); the
    three touchpoints are overridden to read only ``main``.
"""

import sys

# ── Alembic: register a DDL impl for the cloudflare_d1 name ──────────────
try:
    from alembic.ddl.sqlite import SQLiteImpl

    class CloudflareD1Impl(SQLiteImpl):  # auto-registers via __dialect__
        __dialect__ = "cloudflare_d1"
except Exception as exc:  # noqa: BLE001 -- defensive: never block server startup
    print(f"[d1-shim] could not register Alembic impl: {exc}", file=sys.stderr)

# ── Dialect: re-register cloudflare_d1 as a real SQLite subclass ─────────
try:
    from sqlalchemy import exc as _sa_exc
    from sqlalchemy.dialects import registry
    from sqlalchemy.dialects.sqlite.base import SQLiteDialect
    from sqlalchemy_cloudflare_d1.dialect import CloudflareD1Dialect as _UpstreamD1

    # SQLiteDialect.__init__ reads self.dbapi.sqlite_version_info to gate
    # features. D1 runs a modern SQLite; advertise it (the live version is also
    # read in _get_server_version_info below).
    _D1_SQLITE_VERSION = (3, 45, 0)
    _dbapi = _UpstreamD1.import_dbapi()
    if not hasattr(_dbapi, "sqlite_version_info"):
        _dbapi.sqlite_version_info = _D1_SQLITE_VERSION
        _dbapi.sqlite_version = ".".join(str(p) for p in _D1_SQLITE_VERSION)

    class CloudflareD1Dialect(SQLiteDialect):
        """The cloudflare_d1 dialect: SQLite behavior over D1's HTTP transport."""

        name = "cloudflare_d1"
        driver = "httpx"
        default_paramstyle = "qmark"
        supports_statement_cache = True

        # D1's JSON REST API needs the transport type processors (blob->base64,
        # date/time->ISO); layer them over SQLite's defaults.
        colspecs = {**SQLiteDialect.colspecs, **_UpstreamD1.colspecs}  # noqa: RUF012

        # ── transport (from the upstream dialect) ──
        @classmethod
        def import_dbapi(cls):
            return _UpstreamD1.import_dbapi()

        create_connect_args = _UpstreamD1.create_connect_args

        # D1 has no isolation levels — keep these no-ops so SQLite's
        # PRAGMA read_uncommitted machinery never runs over the REST API.
        def get_isolation_level(self, dbapi_connection):  # noqa: ARG002
            return None

        def set_isolation_level(self, dbapi_connection, level):
            pass

        def _get_server_version_info(self, connection):
            try:
                v = connection.exec_driver_sql("SELECT sqlite_version()").scalar()
                return tuple(int(x) for x in str(v).split("."))
            except Exception:  # noqa: BLE001
                return _D1_SQLITE_VERSION

        # ── D1 has a single "main" schema and forbids "temp" ──
        def get_schema_names(self, connection, **kw):  # noqa: ARG002
            return ["main"]

        def _get_table_sql(self, connection, table_name, schema=None, **kw):  # noqa: ARG002
            schema_expr = f"{self.identifier_preparer.quote_identifier(schema)}." if schema else ""
            s = (
                f"SELECT sql FROM {schema_expr}sqlite_master "
                "WHERE name = ? AND type in ('table', 'view')"
            )
            value = connection.exec_driver_sql(s, (table_name,)).scalar()
            if value is None and not self._is_sys_table(table_name):
                raise _sa_exc.NoSuchTableError(f"{schema_expr}{table_name}")
            return value

        def _get_table_pragma(self, connection, pragma, table_name, schema=None):
            quote = self.identifier_preparer.quote_identifier
            prefix = f"{quote(schema)}." if schema is not None else "main."
            cursor = connection.exec_driver_sql(f"PRAGMA {prefix}{pragma}({quote(table_name)})")
            return cursor.fetchall() if not cursor._soft_closed else []

    registry.register("cloudflare_d1", __name__, "CloudflareD1Dialect")
except Exception as exc:  # noqa: BLE001 -- defensive: never block server startup
    print(f"[d1-shim] could not register D1 dialect: {exc}", file=sys.stderr)
