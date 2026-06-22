"""Database utilities — engine caching, session management, helpers."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import Engine, create_engine, event, inspect, text

if TYPE_CHECKING:
    from alembic.config import Config
from sqlalchemy.orm import Session, sessionmaker

from omnigent.entities import NewConversationItem

_logger = logging.getLogger(__name__)

# A callable that returns a context manager yielding a Session.
ManagedSessionMaker = Callable[[], AbstractContextManager[Session]]

# A zero-argument callable returning a fresh database password (e.g. a
# short-lived Lakebase OAuth token). Invoked once per *new* DBAPI connection.
LakebaseTokenProvider = Callable[[], str]


# ── Lakebase token-aware connections ───────────────────
#
# Databricks Lakebase (managed Postgres) authenticates with a short-lived
# OAuth token (~1h TTL, rotated) used as the Postgres *password* — there is no
# static password to bake into the URL. To stay connected we must mint a fresh
# token for every new physical connection instead of pinning one at engine
# construction. This is OPT-IN: it activates only when a token provider is
# resolvable (``OMNIGENT_LAKEBASE_INSTANCE`` is set, or a provider was injected
# via :func:`set_lakebase_token_provider`). When it is not active, engine
# creation is byte-for-byte the legacy static-URI path (SQLite or
# static-password Postgres) — see :func:`_create_engine`.

# Env var naming the Lakebase database *instance* whose OAuth token should be
# minted per connection. Its presence is what flips a Postgres engine into
# token-refresh mode.
_LAKEBASE_INSTANCE_ENV = "OMNIGENT_LAKEBASE_INSTANCE"

# Recycle (close + reopen) pooled connections older than this many seconds.
# Static deployments use 30 min (stale-connection hygiene). Lakebase lowers it
# to 10 min so a connection is rebuilt — and its OAuth token re-minted via the
# ``do_connect`` hook — comfortably before the ~1h token lifetime lapses, even
# for connections that sit idle in the pool across a rotation.
_SERVER_POOL_RECYCLE_SECONDS = 1800
_LAKEBASE_POOL_RECYCLE_SECONDS = 600

# Process-wide override, primarily for tests and for callers that want to plug
# in their own token source (e.g. a non-default Databricks auth flow) without
# the env-var path. ``None`` means "not overridden".
_lakebase_token_provider_override: LakebaseTokenProvider | None = None


def set_lakebase_token_provider(provider: LakebaseTokenProvider | None) -> None:
    """
    Install (or clear) a process-wide Lakebase token provider.

    When set, every Postgres engine subsequently created by
    :func:`get_or_create_engine` mints its connection password by calling
    *provider* once per new DBAPI connection, and uses the shorter
    Lakebase pool-recycle window. Pass ``None`` to clear the override and
    fall back to the ``OMNIGENT_LAKEBASE_INSTANCE`` env-var path.

    This is the documented seam for swapping the token source: the default
    env-var path mints tokens via the Databricks SDK
    (:func:`_databricks_lakebase_token_provider`), but a deployment with a
    bespoke credential flow can inject its own zero-arg ``() -> str`` here.

    :param provider: A zero-arg callable returning a fresh password string,
        or ``None`` to clear a previously installed override.
    """
    global _lakebase_token_provider_override
    _lakebase_token_provider_override = provider


def _databricks_lakebase_token_provider(instance_name: str) -> str:
    """
    Mint a fresh short-lived Lakebase OAuth token via the Databricks SDK.

    Uses ambient Databricks authentication (the workspace's app identity /
    service principal when running inside a Databricks App, or a configured
    profile / env credentials elsewhere). The returned token is used as the
    Postgres password for a single connection; it expires in roughly an hour,
    which is why it is re-minted per connection rather than cached.

    :param instance_name: The Lakebase database instance name, e.g.
        ``"omnigent-db"`` (the value of ``OMNIGENT_LAKEBASE_INSTANCE``).
    :returns: A short-lived OAuth token string to use as the DB password.
    :raises ImportError: If the ``databricks-sdk`` (the ``databricks`` extra)
        is not installed.
    """
    from databricks.sdk import WorkspaceClient

    workspace_client = WorkspaceClient()
    credential = workspace_client.database.generate_database_credential(
        request_id=str(uuid.uuid4()),
        instance_names=[instance_name],
    )
    if not credential.token:
        raise RuntimeError(
            f"Databricks returned no Lakebase credential token for instance "
            f"{instance_name!r}. Verify the instance name and that this identity "
            f"has access to it."
        )
    return credential.token


def _resolve_lakebase_token_provider() -> LakebaseTokenProvider | None:
    """
    Return the active Lakebase token provider, or ``None`` if not configured.

    Resolution order:

    1. A provider installed via :func:`set_lakebase_token_provider` (override).
    2. The Databricks SDK provider, bound to the instance named by
       ``OMNIGENT_LAKEBASE_INSTANCE``.
    3. ``None`` — no token path; engines use the static-URI behavior.

    :returns: A zero-arg ``() -> str`` token provider, or ``None``.
    """
    if _lakebase_token_provider_override is not None:
        return _lakebase_token_provider_override
    instance_name = os.environ.get(_LAKEBASE_INSTANCE_ENV)
    if instance_name:
        return lambda: _databricks_lakebase_token_provider(instance_name)
    return None


def _install_lakebase_token_refresh(
    engine: Engine,
    token_provider: LakebaseTokenProvider,
) -> Callable[[object, object, list[object], dict[str, object]], None]:
    """
    Wire *engine* to refresh its connection password on every new connection.

    Registers a SQLAlchemy ``do_connect`` listener that overwrites the
    ``password`` connection parameter with a freshly minted token immediately
    before each physical DBAPI connection is opened. ``do_connect`` fires once
    per *new* connection (not per pool checkout), so pooled connections reuse
    their token until recycled — which is why :func:`_create_engine` pairs this
    with the shorter ``_LAKEBASE_POOL_RECYCLE_SECONDS`` window.

    :param engine: The SQLAlchemy engine to attach the listener to.
    :param token_provider: Zero-arg callable returning a fresh password.
    :returns: The registered listener (returned so callers/tests can assert it
        is wired and exercise it directly).
    """

    def _provide_fresh_token(
        _dialect: object,
        _conn_rec: object,
        _cargs: list[object],
        cparams: dict[str, object],
    ) -> None:
        # do_connect lets us mutate the connection params psycopg receives.
        # Overwriting ``password`` here means the token is read fresh for each
        # new connection — never baked into the cached engine's URL.
        cparams["password"] = token_provider()

    event.listen(engine, "do_connect", _provide_fresh_token)
    return _provide_fresh_token


# ── URL normalization ──────────────────────────────────


def normalize_database_url(url: str) -> str:
    """Rewrite a PaaS ``postgres://`` / ``postgresql://`` URL to the
    ``postgresql+psycopg://`` form SQLAlchemy needs; other URLs pass through.

    :param url: A SQLAlchemy-compatible database URL.
    :returns: The URL with the psycopg3 dialect specifier applied when needed.
    """
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


# ── Engine caching ─────────────────────────────────────

_engine_cache: dict[str, Engine] = {}
_engine_lock = threading.Lock()


def _create_engine(db_uri: str) -> Engine:
    """
    Create a SQLAlchemy engine with connection pool configuration.

    SQLite engines enable WAL journal mode and a 20s
    ``busy_timeout`` on every connection (not just sessions
    created via :func:`make_managed_session_maker`). Without WAL,
    multi-process workloads — REPL + Omnigent server + runner subprocess
    all hitting the same ``chat.db`` — surface as spurious
    ``disk I/O error`` and ``database is locked`` failures because
    the default ``journal_mode=DELETE`` only permits one writer at
    a time and synchronous-write contention propagates immediately.
    WAL also lets readers proceed concurrently with a writer.

    Non-SQLite databases use connection pooling with
    ``pool_pre_ping`` to verify connections before use. When a Lakebase
    token provider is active (see :func:`_resolve_lakebase_token_provider`),
    the engine additionally re-mints its OAuth token per new connection and
    uses a shorter ``pool_recycle`` window; otherwise the static URI (and its
    baked-in password, if any) is used unchanged.

    :param db_uri: SQLAlchemy database connection string, e.g.
        ``"sqlite:///mydb.db"`` or
        ``"postgresql://user:pass@host/dbname"``.
    :returns: A configured :class:`~sqlalchemy.engine.Engine`.
    """
    is_sqlite = db_uri.startswith("sqlite")
    if is_sqlite:
        # ``check_same_thread=False`` lets SQLAlchemy's pool hand a
        # connection to whichever worker thread asks for it (FastAPI,
        # asyncio.to_thread). The library still serializes access via
        # the pool, so this isn't a footgun — it just removes the
        # legacy single-thread restriction.
        engine = create_engine(
            db_uri,
            connect_args={"check_same_thread": False, "timeout": 20.0},
        )

        # Apply WAL + busy_timeout on every fresh DBAPI connection
        # so AsyncSession instances and any other consumer all
        # benefit — the per-session PRAGMA in
        # :func:`make_managed_session_maker` only fires for code
        # paths that go through that helper.
        import sqlite3

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn: sqlite3.Connection, _conn_record: object) -> None:
            cur = dbapi_conn.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=20000")  # 20s
                cur.execute("PRAGMA synchronous=NORMAL")  # WAL-safe + fast
                cur.execute("PRAGMA foreign_keys=ON")
            finally:
                cur.close()

        return engine
    # Lakebase (managed Postgres) authenticates with a short-lived OAuth token
    # re-minted per connection; everything else uses the static URI as-is. The
    # token path is OPT-IN — ``_resolve_lakebase_token_provider`` returns
    # ``None`` unless ``OMNIGENT_LAKEBASE_INSTANCE`` is set or a provider was
    # injected — so a static-password Postgres URI is byte-for-byte unchanged.
    token_provider = _resolve_lakebase_token_provider()
    pool_recycle = (
        _LAKEBASE_POOL_RECYCLE_SECONDS if token_provider else _SERVER_POOL_RECYCLE_SECONDS
    )
    engine = create_engine(
        db_uri,
        # Verify connections are alive before checking them out
        # from the pool. Prevents "server has gone away" errors
        # after idle periods.
        pool_pre_ping=True,
        # Recycle connections older than this window. Prevents stale
        # connections when the database server restarts or closes idle
        # connections; in Lakebase token mode the shorter window also keeps
        # each connection's OAuth token refreshed ahead of its ~1h expiry.
        pool_recycle=pool_recycle,
        # Aligned with the AnyIO thread limiter in
        # ``server/app.py:_lifespan``. Every DB call runs via
        # ``asyncio.to_thread``, so connections beyond the thread
        # token count just sit idle. Overflow covers boot-time
        # bursts (e.g. migrations). Lakebase per-instance cap: 1000.
        pool_size=200,
        max_overflow=20,
        # Bound the wait when the pool is exhausted instead of
        # blocking indefinitely; surfaces real saturation as an
        # error rather than a hang.
        pool_timeout=10,
    )
    if token_provider:
        _install_lakebase_token_refresh(engine, token_provider)
    return engine


def get_or_create_engine(db_uri: str) -> Engine:
    """
    Return a cached engine for the given URI, creating one if needed.

    On first creation, initializes or upgrades the database schema
    by running migrations to head. See
    :func:`_initialize_or_verify_schema`.

    :param db_uri: SQLAlchemy database connection string, e.g.
        ``"sqlite:///mydb.db"`` or
        ``"postgresql://user:pass@host/dbname"``.
    :returns: A :class:`~sqlalchemy.engine.Engine` for the given URI.
    :raises RuntimeError: If automatic schema migration fails.
    """
    if db_uri not in _engine_cache:
        with _engine_lock:
            if db_uri not in _engine_cache:
                engine = _create_engine(db_uri)
                _initialize_or_verify_schema(engine, db_uri)
                _engine_cache[db_uri] = engine
    return _engine_cache[db_uri]


def _build_alembic_config(db_uri: str) -> Config:
    """
    Build an Alembic ``Config`` pointed at our migrations directory.

    Centralized so :func:`_run_migrations` and the
    :func:`omnigent debug db-upgrade` CLI command share the same
    config (URL, script location). The script_location in
    ``alembic.ini`` is relative — resolve it against the ini
    file's parent so the config works from any working directory.

    :param db_uri: SQLAlchemy database URL, e.g.
        ``"sqlite:///mydb.db"`` or ``"postgresql://..."``.
    :returns: A populated ``alembic.config.Config`` ready to hand
        to ``alembic.command.upgrade``.
    """
    from alembic.config import Config

    alembic_ini = Path(__file__).parent / "alembic.ini"
    config = Config(str(alembic_ini))
    config.set_main_option("sqlalchemy.url", db_uri)
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    return config


def _run_migrations(engine: Engine, db_uri: str) -> None:
    """
    Bring the database schema up to head.

    Always invokes ``alembic.command.upgrade("head")`` regardless
    of whether application tables already exist. Alembic is
    idempotent — when the database is already at head this is a
    fast no-op (one ``SELECT`` on ``alembic_version``) — so the
    extra call is cheap, and it's the only way for column-level
    follow-up migrations (e.g. an ``ALTER TABLE ... ADD COLUMN``)
    to land on databases that were initialized at an earlier
    revision. The previous ``if expected_tables.issubset(...): return``
    short-circuit silently skipped those migrations, leaving
    existing DBs missing columns that the runtime expects.

    :param engine: The SQLAlchemy engine bound to the target
        database.
    :param db_uri: Database connection string forwarded to
        Alembic's ``sqlalchemy.url`` config option, e.g.
        ``"sqlite:///mydb.db"``.
    """
    from alembic import command

    from omnigent.db.db_models import Base

    _logger.info("Running database migrations...")
    config = _build_alembic_config(db_uri)
    # Pass a shared connection so Alembic operates within the same
    # engine (required for SQLite in-memory databases, and avoids
    # creating a second connection pool).
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    # Belt-and-suspenders: if a future migration is added but a
    # caller forgets to wire it into the chain, ``create_all`` will
    # at least create any missing tables from ORM metadata so the
    # server still boots. Cannot rescue missing COLUMNS on existing
    # tables — those need a real migration, which is why the
    # short-circuit above was removed.
    Base.metadata.create_all(bind=engine, checkfirst=True)


def _get_current_db_revision(engine: Engine) -> str | None:
    """
    Return the database's current Alembic revision, or ``None``.

    ``None`` means the database has no ``alembic_version`` table at
    all — i.e. nothing has ever been migrated against this database.
    A database that exists at some revision (even if not head) returns
    that revision string.

    :param engine: SQLAlchemy engine bound to the target database.
    :returns: The current revision hash (e.g. ``"c9d3a1f2e4b5"``) or
        ``None`` if the ``alembic_version`` table is absent.
    """
    from alembic.runtime.migration import MigrationContext

    inspector = inspect(engine)
    if "alembic_version" not in inspector.get_table_names():
        return None
    with engine.connect() as connection:
        ctx = MigrationContext.configure(connection)
        return ctx.get_current_revision()


def _get_head_db_revision(db_uri: str) -> str:
    """
    Return the head Alembic revision for our migrations directory.

    Reads the migration scripts on disk (not the database). Raises
    if the migrations directory is empty or otherwise has no head —
    that would indicate a packaging bug.

    :param db_uri: Database URL — only used to build an Alembic
        ``Config`` pointing at our scripts directory; the database
        itself is not contacted.
    :returns: The head revision hash, e.g. ``"c9d3a1f2e4b5"``.
    :raises RuntimeError: If no head revision is defined.
    """
    from alembic.script import ScriptDirectory

    config = _build_alembic_config(db_uri)
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    if head is None:
        raise RuntimeError(
            "No Alembic head revision found — the migrations directory appears to be empty."
        )
    return head


def _initialize_or_verify_schema(engine: Engine, db_uri: str) -> None:
    """
    Bring a fresh or stale database to head before the server starts.

    Three cases:

    - **Fresh DB** (no ``alembic_version`` table) — run migrations to
      head. This covers brand-new SQLite files and freshly created
      Postgres schemas.
    - **At head** — no-op.
    - **Behind head** — log a warning, attempt an automatic Alembic
      upgrade to head, then verify that the database reached head.
      If the migration fails, re-raise with context so the server
      still terminates with an actionable error instead of continuing
      against an incompatible schema.

    :param engine: SQLAlchemy engine bound to the target database.
    :param db_uri: Database URL, used both for Alembic config and in
        any migration-failure error message.
    :raises RuntimeError: If automatic schema migration fails or does
        not bring the database to head.
    """
    head = _get_head_db_revision(db_uri)
    current = _get_current_db_revision(engine)

    if current is None:
        _run_migrations(engine, db_uri)
        return

    if current != head:
        _logger.warning(
            "Omnigent database schema is out of date "
            "(found revision %r, expected %r); attempting automatic migration.",
            current,
            head,
        )
        try:
            _run_migrations(engine, db_uri)
        except Exception as exc:
            raise RuntimeError(
                f"Omnigent database schema is out of date "
                f"(found revision {current!r}, expected {head!r}) "
                f"and automatic migration failed. Take a backup of your database, then run\n"
                f"\n"
                f"    omnigent debug db-upgrade {db_uri!r}\n"
                f"\n"
                f"to inspect or retry the migration manually."
            ) from exc

        migrated = _get_current_db_revision(engine)
        if migrated != head:
            raise RuntimeError(
                f"Omnigent automatic database migration did not reach head "
                f"(started at {current!r}, now at {migrated!r}, expected {head!r}). "
                f"Take a backup of your database, then run\n"
                f"\n"
                f"    omnigent debug db-upgrade {db_uri!r}\n"
                f"\n"
                f"to inspect or retry the migration manually."
            )


def clear_engine_cache() -> None:
    """
    Dispose of all cached engines and clear the engine cache.

    Intended for test teardown to ensure a fresh database state
    between test runs.
    """
    with _engine_lock:
        for engine in _engine_cache.values():
            engine.dispose()
        _engine_cache.clear()


# ── Managed session ────────────────────────────────────


def make_managed_session_maker(
    engine: Engine,
    *,
    immediate: bool = False,
) -> ManagedSessionMaker:
    """
    Create a context-manager factory for database sessions.

    Sessions auto-commit on success and auto-rollback on failure.
    When the underlying dialect is SQLite, each session additionally
    enables ``PRAGMA foreign_keys`` and sets a 20-second
    ``busy_timeout``.

    :param engine: The SQLAlchemy engine to bind sessions to.
    :param immediate: When ``True`` and the dialect is SQLite, starts
        the transaction with ``BEGIN IMMEDIATE`` to acquire the write
        lock before any read, preventing check-then-insert races.
        No-op on PostgreSQL (``SELECT ... FOR UPDATE`` is used there).
    :returns: A callable that, when invoked, returns a context
        manager yielding a :class:`~sqlalchemy.orm.Session`.
    """
    factory = sessionmaker(bind=engine)
    is_sqlite = engine.dialect.name == "sqlite"

    @contextmanager
    def managed_session() -> Iterator[Session]:
        """
        Yield a managed :class:`~sqlalchemy.orm.Session`.

        Commits on clean exit, rolls back on exception. For SQLite
        backends, enables foreign key enforcement and sets a
        busy timeout before yielding.
        """
        with factory() as session:
            try:
                if is_sqlite:
                    # PRAGMAs must run before BEGIN IMMEDIATE: foreign_keys is
                    # a no-op inside a transaction, and busy_timeout must be
                    # set before lock acquisition or it doesn't apply.
                    session.execute(text("PRAGMA foreign_keys = ON"))
                    session.execute(text("PRAGMA busy_timeout = 20000"))  # 20s
                    if immediate:
                        # Acquire write lock before any read to prevent
                        # concurrent check-then-insert races on SQLite.
                        session.execute(text("BEGIN IMMEDIATE"))
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    return managed_session


# ── ID generation ──────────────────────────────────────

_ITEM_TYPE_PREFIX: dict[str, str] = {
    "message": "msg_",
    "function_call": "fc_",
    "function_call_output": "fco_",
    "error": "err_",
    "reasoning": "rs_",
    "compaction": "cmp_",
    "native_tool": "nt_",
    "resource_event": "rse_",
    "slash_command": "sc_",
    "terminal_command": "tc_",
}


def generate_agent_id() -> str:
    """
    Generate a unique agent identifier.

    :returns: A string of the form ``"ag_<32-char hex>"``,
        e.g. ``"ag_0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c"``.
    """
    return f"ag_{uuid.uuid4().hex}"


def builtin_agent_id(name: str) -> str:
    """
    Deterministic agent id for a built-in agent, derived from its name.

    Same shape and length as :func:`generate_agent_id` (``ag_`` + 32 hex), but
    stable across processes: a multi-tenant deployment reseeds the built-ins into
    an ephemeral per-pod store, where a random id would change each boot and
    dangle a persisted ``conversation.agent_id``. Do NOT revert built-in seeding
    to :func:`generate_agent_id` (guarded by the ``builtin_agent_id`` tests).

    :param name: The built-in agent's unique name, e.g. ``"polly"``.
    :returns: A deterministic id of the form ``"ag_<32-char hex>"``.
    """
    digest = hashlib.sha256(f"builtin:{name}".encode()).hexdigest()
    return f"ag_{digest[:32]}"


def generate_file_id() -> str:
    """
    Generate a unique file identifier.

    :returns: A string of the form ``"file_<32-char hex>"``,
        e.g. ``"file_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"``.
    """
    return f"file_{uuid.uuid4().hex}"


def generate_conversation_id() -> str:
    """
    Generate a unique conversation identifier.

    :returns: A string of the form ``"conv_<32-char hex>"``,
        e.g. ``"conv_e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9"``.
    """
    return f"conv_{uuid.uuid4().hex}"


def generate_task_id() -> str:
    """
    Generate a unique task (response) identifier.

    :returns: A string of the form ``"resp_<32-char hex>"``,
        e.g. ``"resp_d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3"``.
    """
    return f"resp_{uuid.uuid4().hex}"


def generate_item_id(item_type: str) -> str:
    """
    Generate a unique conversation-item identifier.

    The prefix is determined by the item type:

    - ``"message"`` -> ``"msg_"``
    - ``"function_call"`` -> ``"fc_"``
    - ``"function_call_output"`` -> ``"fco_"``
    - ``"error"`` -> ``"err_"``
    - ``"reasoning"`` -> ``"rs_"``
    - ``"compaction"`` -> ``"cmp_"``
    - ``"native_tool"`` -> ``"nt_"``
    - ``"slash_command"`` -> ``"sc_"``

    :param item_type: One of the keys in :data:`_ITEM_TYPE_PREFIX`.
    :returns: A prefixed identifier, e.g. ``"msg_a1b2c3d4..."``.
    :raises ValueError: If *item_type* is not a recognised type.
    """
    prefix = _ITEM_TYPE_PREFIX.get(item_type)
    if prefix is None:
        raise ValueError(f"unknown item type: {item_type!r}")
    return f"{prefix}{uuid.uuid4().hex}"


# ── FTS (SQLite FTS5) ─────────────────────────────────

_FTS_TABLE = "conversation_items_fts"

_CREATE_FTS = text(
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE} USING fts5("
    "item_id UNINDEXED, conversation_id UNINDEXED, search_text)"
)

# Dialects that support SQLite's FTS5 extension. Cloudflare D1 is SQLite
# served over HTTP, so it gets full-text search too — gate FTS on the dialect
# *family*, not the literal name "sqlite". (The engine-level WAL/PRAGMA path in
# ``_create_engine`` stays sqlite-only: those are local-file concerns that D1
# neither needs nor supports over the wire.)
_FTS5_DIALECTS = frozenset({"sqlite", "cloudflare_d1"})


def _supports_fts5(dialect_name: str) -> bool:
    """
    Whether *dialect_name* is a SQLite-family dialect that supports FTS5.

    :param dialect_name: A SQLAlchemy ``dialect.name``, e.g. ``"sqlite"``,
        ``"cloudflare_d1"``, or ``"postgresql"``.
    :returns: ``True`` for SQLite and SQLite-over-the-wire dialects (D1),
        ``False`` otherwise.
    """
    return dialect_name in _FTS5_DIALECTS


def ensure_fts_table(engine: Engine) -> None:
    """
    Create the FTS5 virtual table on SQLite-family dialects. Idempotent.

    On dialects without FTS5 (e.g. PostgreSQL) this is a no-op.

    :param engine: The SQLAlchemy engine whose dialect is inspected.
        On a SQLite-family dialect (SQLite or Cloudflare D1) the
        ``conversation_items_fts`` virtual table is created if absent.
    """
    if _supports_fts5(engine.dialect.name):
        with engine.connect() as conn:
            conn.execute(_CREATE_FTS)
            conn.commit()


def insert_fts(
    session: Session,
    item_id: str,
    conversation_id: str,
    search_text: str,
) -> None:
    """
    Dual-write a row into the FTS5 table (SQLite-family dialects only).

    On dialects without FTS5 this is a no-op.

    :param session: An active SQLAlchemy session. Its bound engine's
        dialect is checked to decide whether to write.
    :param item_id: The conversation-item ID to index, e.g.
        ``"msg_a1b2c3d4..."``.
    :param conversation_id: The parent conversation ID, e.g.
        ``"conv_e4f5a6b7..."``.
    :param search_text: Plain-text content to store in the FTS
        index for this item.
    """
    if session.bind and _supports_fts5(session.bind.dialect.name):
        session.execute(
            text(
                f"INSERT INTO {_FTS_TABLE}"
                "(item_id, conversation_id, search_text) "
                "VALUES (:item_id, :cid, :st)"
            ),
            {"item_id": item_id, "cid": conversation_id, "st": search_text},
        )


def delete_fts_by_conversation(session: Session, conversation_id: str) -> None:
    """
    Remove all FTS rows for a conversation (SQLite-family dialects only).

    On dialects without FTS5 this is a no-op.

    :param session: An active SQLAlchemy session. Its bound engine's
        dialect is checked to decide whether to delete.
    :param conversation_id: The conversation whose FTS rows should be
        removed, e.g. ``"conv_e4f5a6b7..."``.
    """
    if session.bind and _supports_fts5(session.bind.dialect.name):
        session.execute(
            text(f"DELETE FROM {_FTS_TABLE} WHERE conversation_id = :cid"),
            {"cid": conversation_id},
        )


# ── Search text extraction ─────────────────────────────


def extract_search_text(item: NewConversationItem) -> str:
    """
    Extract plain text for FTS from an item's data, per DBSPEC.

    The item has already been Pydantic-validated, so required fields
    (content, name, arguments, output, summary) are guaranteed
    present. We use direct dict access to fail loud if that
    assumption is ever violated.

    Content/summary blocks are heterogeneous (text, image, etc.)
    so we filter to only text-bearing blocks via ``.get("text")``.

    :param item: A Pydantic-validated conversation item whose
        ``type`` is one of ``"message"``, ``"function_call"``,
        ``"function_call_output"``, ``"reasoning"``,
        ``"compaction"``, ``"native_tool"``, ``"resource_event"``,
        ``"slash_command"``, or ``"terminal_command"``.
    :returns: A single plain-text string suitable for FTS indexing.
    :raises ValueError: If *item.type* is not a recognised type.
    """
    from omnigent.entities.conversation import CompactionData

    data = item.data.model_dump()
    if item.type == "message":
        return " ".join(
            block["text"]
            for block in data["content"]
            if isinstance(block, dict) and block.get("text")
        )
    if item.type == "function_call":
        return f"{data['name']} {data['arguments']}"
    if item.type == "function_call_output":
        return str(data["output"])
    if item.type == "error":
        return " ".join(part for part in (data["source"], data["code"], data["message"]) if part)
    if item.type == "reasoning":
        return " ".join(
            block["text"]
            for block in data["summary"]
            if isinstance(block, dict) and block.get("text")
        )
    if item.type == "compaction":
        assert isinstance(item.data, CompactionData)
        return item.data.summary
    if item.type == "native_tool":
        # Native tool items are opaque provider dicts — no
        # meaningful text to index for search.
        return ""
    if item.type == "resource_event":
        # Resource lifecycle records are metadata. Index only the stable
        # identifiers so persistence succeeds and basic resource lookup can
        # find the event, without dumping opaque metadata into FTS.
        return " ".join(
            part
            for part in (data["event_type"], data["resource_id"], data["resource_type"])
            if part
        )
    if item.type == "slash_command":
        # Index command name + args + stdout so FTS can find a
        # historical Skill invocation by what the operator typed
        # or what the command echoed. ``output`` may be absent
        # (skills with no inline stdout); coerce to "" for join.
        return " ".join(
            part for part in (data["name"], data["arguments"], data.get("output") or "") if part
        )
    if item.type == "terminal_command":
        # Index the command input + stdout so FTS can find historical
        # !cmd executions by what was typed or what was printed.
        return " ".join(
            part for part in (data.get("input") or "", data.get("stdout") or "") if part
        )
    raise ValueError(f"unknown item type: {item.type!r}")


def strip_nul_bytes(value: str) -> str:
    """
    Remove NUL (``0x00``) bytes from a string bound for a text column.

    PostgreSQL ``text``/``varchar`` columns reject NUL bytes outright
    (``psycopg.DataError: PostgreSQL text fields cannot contain NUL
    (0x00) bytes``), so any tool output, message, or search text that
    embeds a NUL — e.g. a tool that returns the contents of a binary
    file — would otherwise abort the whole ``INSERT``. SQLite tolerates
    NUL, so stripping uniformly here also keeps the two backends
    behaving identically. NUL carries no textual or full-text-search
    meaning, so removing it is lossless for our purposes.

    :param value: The string about to be persisted to a text column,
        e.g. a JSON-serialized item payload or an FTS search string.
    :returns: The same string with every ``"\\x00"`` removed; returned
        unchanged when no NUL bytes are present.
    """
    return value.replace("\x00", "")


# ── Timestamp ──────────────────────────────────────────


def now_epoch() -> int:
    """
    Return the current time as Unix epoch seconds (integer).

    :returns: Seconds since 1970-01-01 00:00:00 UTC, truncated to
        an integer.
    """
    return int(time.time())


def now_epoch_us() -> int:
    """
    Return the current time as Unix epoch microseconds (integer).

    Used for change-detection timestamps (``comments.updated_at``)
    where consecutive writes inside the same second must still produce
    distinct, ordered values — second-granularity ``now_epoch`` would
    make back-to-back mutations indistinguishable to diff-based
    consumers like the ``WS /v1/sessions/updates`` fingerprint.
    Microseconds rather than nanoseconds because epoch-µs stays below
    JavaScript's ``Number.MAX_SAFE_INTEGER`` (until ~2255), so web
    clients read the JSON value exactly.

    :returns: Microseconds since 1970-01-01 00:00:00 UTC.
    """
    return time.time_ns() // 1_000


def utc_day(epoch_seconds: int) -> str:
    """
    Return the UTC calendar day for a Unix epoch timestamp.

    The day key used to bucket per-user daily cost: a session spanning
    midnight UTC splits its spend across both days. Always UTC so the
    bucket is unambiguous across deployments.

    :param epoch_seconds: Unix epoch seconds, e.g. ``1749081600``.
    :returns: The UTC date as ``"YYYY-MM-DD"``, e.g. ``"2026-06-05"``.
    """
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).date().isoformat()
