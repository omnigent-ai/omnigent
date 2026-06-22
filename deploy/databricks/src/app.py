"""Databricks Apps entrypoint for the Omnigent server.

A thin shim over the generic Docker entrypoint
(``deploy/docker/entrypoint.py``): it bridges the Databricks-Apps runtime
contract onto the environment-variable contract that entrypoint already
speaks, then reuses its ``_resolve_config`` / ``build_app`` to wire and serve
the exact same FastAPI app every other platform runs. Nothing here forks the
server's boot logic — only the *inputs* to it.

Two Databricks-specific bridges happen in :func:`configure_databricks_env`:

* **Port.** Databricks Apps tell the app which port to bind via
  ``DATABRICKS_APP_PORT`` (defaults to 8000). We surface it as ``PORT`` — the
  variable the generic entrypoint reads.

* **Lakebase database URL.** Binding a Lakebase (managed Postgres) resource
  injects ``PGHOST`` / ``PGUSER`` / ``PGDATABASE`` / ``PGPORT`` (and a
  short-lived ``PGPASSWORD`` OAuth token). We compose a ``DATABASE_URL`` from
  those parts **without** the password and rely on the token-aware engine in
  :mod:`omnigent.db.utils` to mint a fresh OAuth token per connection — keyed
  on ``OMNIGENT_LAKEBASE_INSTANCE`` (set in ``app.yaml``). Baking the injected
  ``PGPASSWORD`` into the URL would break the server after the token's ~1h
  expiry; minting per connection keeps a long-lived server connected across
  rotations.

Migrations run through the token-aware :func:`get_or_create_engine` rather
than the generic entrypoint's raw ``sqlalchemy.create_engine`` migration
engine, so the Alembic upgrade also authenticates with a freshly minted token.

Single replica by design: the runner registry lives in server process memory,
so all traffic must land on one instance. Databricks Apps run a single
container per app — do not add any scale-out. See README.md.

Importing this module has no side effects; all work lives in ``main()``.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys
import traceback
from collections.abc import MutableMapping
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from omnigent.stores.artifact_store import ArtifactStore  # noqa: F401

logging.basicConfig(level=logging.INFO, stream=sys.stderr, force=True)
logger = logging.getLogger("omnigent-databricks")

# Databricks Apps inject the bind port here (defaults to 8000); the app MUST
# listen on it. Mirrors the generic entrypoint's PORT default.
_APP_PORT_ENV = "DATABRICKS_APP_PORT"
# Lakebase resource binding injects these standard libpq variables.
_PG_HOST_ENV = "PGHOST"
_PG_USER_ENV = "PGUSER"
_PG_DATABASE_ENV = "PGDATABASE"
_PG_PORT_ENV = "PGPORT"
# Names the Lakebase instance whose OAuth token omnigent.db.utils mints per
# connection. Its presence also flips the engine into token-refresh mode.
_LAKEBASE_INSTANCE_ENV = "OMNIGENT_LAKEBASE_INSTANCE"


def configure_databricks_env(env: MutableMapping[str, str]) -> None:
    """Translate the Databricks-Apps runtime contract into Omnigent's env vars.

    Idempotent and non-destructive: anything the operator set explicitly
    (``PORT``, ``DATABASE_URL``, ``OMNIGENT_AUTH_PROVIDER``) is left untouched,
    so this is safe to run unconditionally at boot.

    :param env: The mutable process environment (``os.environ``), modified
        in place.
    :raises RuntimeError: If a passwordless Lakebase ``DATABASE_URL`` is
        composed but ``OMNIGENT_LAKEBASE_INSTANCE`` is unset — that would leave
        the server with no way to authenticate to Postgres.
    """
    # ── Port ──────────────────────────────────────────────────
    app_port = env.get(_APP_PORT_ENV)
    if app_port and not env.get("PORT"):
        env["PORT"] = app_port

    # ── Lakebase DATABASE_URL (password-less; token minted per connection) ──
    if not env.get("DATABASE_URL"):
        host = env.get(_PG_HOST_ENV)
        user = env.get(_PG_USER_ENV)
        database = env.get(_PG_DATABASE_ENV)
        port = env.get(_PG_PORT_ENV, "5432")
        if host and user and database:
            # No password in the URL: omnigent.db.utils injects a fresh OAuth
            # token as the password on every new connection. sslmode=require is
            # mandatory for Lakebase.
            env["DATABASE_URL"] = (
                f"postgresql+psycopg://{quote(user, safe='')}@{host}:{port}"
                f"/{database}?sslmode=require"
            )
            if not env.get(_LAKEBASE_INSTANCE_ENV):
                raise RuntimeError(
                    "A Lakebase DATABASE_URL was composed from the injected PG* "
                    "variables, but OMNIGENT_LAKEBASE_INSTANCE is not set. Set it "
                    "to the Lakebase database instance name (in app.yaml) so the "
                    "server can mint a per-connection OAuth token; otherwise the "
                    "password-less URL cannot authenticate."
                )

    # ── Auth ──────────────────────────────────────────────────
    # Databricks fronts the app with an identity-aware proxy that forwards the
    # authenticated user in X-Forwarded-Email — exactly what header auth reads.
    env.setdefault("OMNIGENT_AUTH_PROVIDER", "header")


def _load_server_entrypoint() -> ModuleType:
    """Import the generic Docker entrypoint module.

    Prefers a copy vendored next to this file by ``deploy.py`` (the deployed
    app source only contains this directory), then falls back to importing the
    canonical ``deploy.docker.entrypoint`` from a repo checkout (used by the
    test suite and local dev).

    :returns: The imported entrypoint module exposing ``_resolve_config`` and
        ``build_app``.
    """
    here = Path(__file__).resolve().parent
    vendored = here / "_omnigent_entrypoint.py"
    if vendored.exists():
        spec = importlib.util.spec_from_file_location("_omnigent_entrypoint", vendored)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    # Dev / test fallback: the repo root is deploy/databricks/src → up 3.
    repo_root = here.parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    return importlib.import_module("deploy.docker.entrypoint")


def main() -> None:
    """Boot the Omnigent server on Databricks Apps.

    Wraps the whole boot in a catch-all so any failure lands in the app logs
    (``databricks apps logs``) and the process lingers briefly for log capture
    before exiting non-zero.
    """
    try:
        configure_databricks_env(os.environ)

        entrypoint = _load_server_entrypoint()
        resolved = entrypoint._resolve_config()

        # Run migrations through the TOKEN-AWARE engine builder (the generic
        # entrypoint's run_migrations uses a raw engine with no token listener,
        # which cannot authenticate to Lakebase). get_or_create_engine runs
        # _initialize_or_verify_schema on first creation and caches the engine,
        # so the stores built by build_app reuse this very engine.
        from omnigent.db.utils import get_or_create_engine

        logger.info("Running database migrations (token-aware engine)...")
        get_or_create_engine(resolved.database_url)

        built = entrypoint.build_app(resolved)

        import uvicorn

        from omnigent.runner.transports.ws_tunnel.limits import (
            RUNNER_TUNNEL_MAX_MESSAGE_BYTES,
        )

        logger.info("Starting omnigent server on %s:%d", built.host, built.port)
        uvicorn.run(
            built.app,
            host=built.host,
            port=built.port,
            ws_max_size=RUNNER_TUNNEL_MAX_MESSAGE_BYTES,
        )
    except Exception:  # noqa: BLE001 — startup catch-all so failures land in logs
        logger.error("FATAL: omnigent server failed to start:\n%s", traceback.format_exc())
        import time  # deferred — keeps module inert

        time.sleep(30)
        sys.exit(1)


if __name__ == "__main__":
    main()
