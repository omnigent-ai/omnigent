"""Databricks Apps entry point for omnigent.

Starts omnigent with Lakebase (managed PostgreSQL) as the
database and UC Volumes as the artifact store.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback

logging.basicConfig(level=logging.INFO, stream=sys.stderr, force=True)
logger = logging.getLogger("omnigent-app")

# ── Lakebase token cache ──────────────────────────────────
#
# Lakebase tokens are valid for ~60 minutes. The previous design
# minted a fresh token on every new physical Postgres connection
# inside SQLAlchemy's ``do_connect`` event hook — a synchronous
# Databricks SDK HTTPS round-trip costing 100–300 ms per call.
# Under the 200-runner load test that meant ~20 mints/minute as
# the pool churned overflow connections, with each mint blocking
# whatever thread (sometimes the asyncio event-loop thread) was
# establishing the connection.
#
# This cache mints once per endpoint and reuses the token across
# all subsequent ``do_connect`` calls until the TTL expires. 50
# minutes leaves a 10-minute safety margin before Lakebase rejects
# the token. Concurrent first-time mints are NOT serialized — we
# release the lock around the SDK call so a thundering herd of
# initial connections all mints once each (worst case) rather than
# waiting on a single in-flight mint. The cache is then populated
# atomically with whichever mint finishes first; the late losers
# just overwrite with their own (equally-valid) token.
_TOKEN_TTL_SECONDS = 50 * 60
_token_cache: dict[str, tuple[str, float]] = {}
_token_cache_lock = threading.Lock()

try:
    import sqlalchemy
    from databricks.sdk import WorkspaceClient

    # ── Configuration ──────────────────────────────────────────

    # Required env vars — injected by Databricks Apps runtime from
    # the resources declared in databricks.yml / app.yaml.
    LAKEBASE_ENDPOINT = os.environ["AP_LAKEBASE_ENDPOINT"]
    VOLUME_PATH = os.environ["AP_ARTIFACT_VOLUME_PATH"]
    PGHOST = os.environ["PGHOST"]
    PGDATABASE = os.environ["PGDATABASE"]
    PGUSER = os.environ["PGUSER"]

    # Optional with documented defaults.
    # Databricks Apps expects the app to listen on DATABRICKS_APP_PORT
    # (8000 by convention) — deliberately decoupled from the CLI's
    # local-server default (6767 in host/local_server.py).
    PORT = int(os.environ.get("DATABRICKS_APP_PORT", "8000"))
    PGPORT = os.environ.get("PGPORT", "5432")
    PGSSLMODE = os.environ.get("PGSSLMODE", "require")
    # Recycle DB connections before Lakebase 60-min token expiry.
    # 300s (5 min) is conservative — well under the 60-min token TTL.
    POOL_RECYCLE_SECONDS = int(os.environ.get("AP_POOL_RECYCLE_SECONDS", "300"))
    logger.info(
        "Config: PGHOST=%s PGDATABASE=%s PGUSER=%s VOLUME=%s PORT=%d",
        PGHOST,
        PGDATABASE,
        PGUSER,
        VOLUME_PATH,
        PORT,
    )

    # ── Lakebase token injection ──────────────────────────────

    _workspace_client = WorkspaceClient()

    def _get_cached_token(endpoint: str) -> str:
        """Return a cached Lakebase token for ``endpoint``, minting if needed.

        Fast path: cached token whose expiry is in the future is returned
        without contacting the workspace. Slow path: mint a new token via
        the Databricks SDK (synchronous HTTPS). The mint runs OUTSIDE the
        cache lock so multiple concurrent first-time mints don't serialize
        behind one another — the last winner writes the cache, which is
        safe since every minted token is independently valid for ~60 min.

        :param endpoint: Lakebase endpoint resource name, e.g.
            ``"projects/foo/branches/production/endpoints/primary"``.
        :returns: A Lakebase database credential token.
        :raises RuntimeError: If the SDK returns a credential with no token.
        """
        now = time.monotonic()
        with _token_cache_lock:
            cached = _token_cache.get(endpoint)
            if cached is not None and cached[1] > now:
                return cached[0]
        credential = _workspace_client.postgres.generate_database_credential(
            endpoint=endpoint,
        )
        if credential.token is None:
            raise RuntimeError("Lakebase credential response did not include a token")
        with _token_cache_lock:
            _token_cache[endpoint] = (credential.token, now + _TOKEN_TTL_SECONDS)
        return credential.token

    # SQLAlchemy fixes the signature of do_connect; the dialect /
    # conn_rec / cargs args aren't used here, but they have to be
    # named so the hook accepts them. Underscore prefix tells the
    # linter we know they're unused.
    @sqlalchemy.event.listens_for(sqlalchemy.engine.Engine, "do_connect")
    def _inject_lakebase_credentials(_dialect, _conn_rec, _cargs, cparams):
        if cparams.get("host") != PGHOST:
            return
        cparams["password"] = _get_cached_token(LAKEBASE_ENDPOINT)
        cparams["sslmode"] = PGSSLMODE

    # ── Start omnigent ─────────────────────────────────────

    import tempfile
    from pathlib import Path

    import uvicorn

    from omnigent.runtime import init as init_runtime
    from omnigent.runtime import telemetry
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.runtime.caps import RuntimeCaps
    from omnigent.server.app import create_app
    from omnigent.server.auth import create_auth_provider

    # OTel: the Databricks Apps platform auto-injects
    # OTEL_EXPORTER_OTLP_ENDPOINT when `telemetry_export_destinations`
    # is set on the app — telemetry.init() picks that up and routes
    # OTLP to the platform collector, which writes to the configured
    # UC tables. No-op if neither OTEL nor MLflow env vars are set.
    telemetry.init()
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.databricks_volumes import (
        DatabricksVolumesArtifactStore,
    )
    from omnigent.stores.comment_store.sqlalchemy_store import (
        SqlAlchemyCommentStore,
    )
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore
    from omnigent.stores.permission_store.sqlalchemy_store import (
        SqlAlchemyPermissionStore,
    )
    from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore

    DB_URI = f"postgresql+psycopg://{PGUSER}@{PGHOST}:{PGPORT}/{PGDATABASE}"
    ARTIFACT_URI = f"dbfs:{VOLUME_PATH}"
    CACHE_DIR = Path(tempfile.mkdtemp(prefix="ap_cache_"))

    logger.info("DB_URI: %s", DB_URI[:80])
    logger.info("ARTIFACT_URI: %s", ARTIFACT_URI)

    # The app SP owns the tables — run any pending Alembic upgrades
    # before the stores boot, since the verify-schema check refuses
    # to start a stale DB. Idempotent: a no-op when the DB is at head.
    from omnigent.db.utils import _run_migrations as _run_alembic_upgrade

    _migration_engine = sqlalchemy.create_engine(DB_URI)
    try:
        _run_alembic_upgrade(_migration_engine, DB_URI)
    finally:
        _migration_engine.dispose()

    agent_store = SqlAlchemyAgentStore(DB_URI)
    file_store = SqlAlchemyFileStore(DB_URI)
    conversation_store = SqlAlchemyConversationStore(DB_URI)
    artifact_store = DatabricksVolumesArtifactStore(ARTIFACT_URI)
    file_comment_store = SqlAlchemyCommentStore(DB_URI)
    permission_store = SqlAlchemyPermissionStore(DB_URI)
    policy_store = SqlAlchemyPolicyStore(DB_URI)
    host_store = HostStore(DB_URI)

    agent_cache = AgentCache(artifact_store=artifact_store, cache_dir=CACHE_DIR)

    init_runtime(
        agent_cache=agent_cache,
        caps=RuntimeCaps(),
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        comment_store=file_comment_store,
        policy_store=policy_store,
    )

    # The Databricks Apps proxy injects ``X-Forwarded-Email`` on
    # every request, so we run in header mode. Header is the
    # framework default, but pin it explicitly so the hosted product
    # keeps its existing behavior regardless of any ambient
    # OMNIGENT_AUTH_ENABLED in the deploy env (an explicit
    # provider always wins over the enable switch).
    os.environ.setdefault("OMNIGENT_AUTH_PROVIDER", "header")
    auth_provider = create_auth_provider()
    app = create_app(
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        agent_cache=agent_cache,
        comment_store=file_comment_store,
        permission_store=permission_store,
        policy_store=policy_store,
        host_store=host_store,
        auth_provider=auth_provider,
    )

    if __name__ == "__main__":
        logger.info("Starting omnigent on 0.0.0.0:%d", PORT)
        uvicorn.run(app, host="0.0.0.0", port=PORT)

except Exception:  # noqa: BLE001 — startup catch-all; we want every failure logged
    logger.error("FATAL: omnigent failed to start:\n%s", traceback.format_exc())
    # Keep the process alive briefly so logs can be captured
    import time

    time.sleep(30)
    sys.exit(1)
