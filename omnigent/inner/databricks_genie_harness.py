"""
``harness: databricks-genie`` wrap.

Thin module exposing :func:`create_app` — the entrypoint the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent process
resolves ``"databricks-genie"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Wraps a :class:`omnigent.inner.databricks_genie_executor.DatabricksGenieExecutor`,
which converses with a remote Databricks Genie space over the Genie Conversation
API. Mirrors the cursor wrap's env-var config flow.

Env vars read at startup (set by
:func:`omnigent.runtime.workflow._build_databricks_genie_spawn_env`):

- ``HARNESS_DATABRICKS_GENIE_MODEL``: the Genie space id (carried in
  ``executor.model``). ``None`` surfaces as a turn error telling the user to set
  it.
- ``HARNESS_DATABRICKS_GENIE_PROFILE``: the Databricks profile from
  ``~/.databrickscfg`` used to authenticate the workspace client. ``None`` lets
  the SDK use its own resolution order.
- ``HARNESS_DATABRICKS_GENIE_TIMEOUT``: optional per-turn deadline (seconds,
  float) handed to Genie's blocking ``*_and_wait`` helpers. Malformed values
  fall back to the executor default.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI

from omnigent.inner.databricks_genie_executor import (
    _DEFAULT_TIMEOUT_SECONDS,
    DatabricksGenieExecutor,
)
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

_ENV_MODEL = "HARNESS_DATABRICKS_GENIE_MODEL"
_ENV_PROFILE = "HARNESS_DATABRICKS_GENIE_PROFILE"
_ENV_TIMEOUT = "HARNESS_DATABRICKS_GENIE_TIMEOUT"


def _resolve_timeout() -> float:
    """Resolve the per-turn timeout from :data:`_ENV_TIMEOUT`.

    :returns: The parsed timeout in seconds, or
        :data:`~omnigent.inner.databricks_genie_executor._DEFAULT_TIMEOUT_SECONDS`
        when the env var is unset or malformed.
    """
    raw = os.environ.get(_ENV_TIMEOUT, "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        return float(raw)
    except ValueError:
        _logger.warning(
            "%s is not a valid float (%r); falling back to %s seconds",
            _ENV_TIMEOUT,
            raw,
            _DEFAULT_TIMEOUT_SECONDS,
        )
        return _DEFAULT_TIMEOUT_SECONDS


def _build_databricks_genie_executor() -> Executor:
    """Construct a :class:`DatabricksGenieExecutor` from env-var config.

    Called lazily by the :class:`ExecutorAdapter` on the first turn, so a missing
    ``databricks-sdk`` install surfaces as a request-time error rather than an
    app-boot crash.
    """
    return DatabricksGenieExecutor(
        space_id=os.environ.get(_ENV_MODEL) or None,
        profile=os.environ.get(_ENV_PROFILE) or None,
        timeout_seconds=_resolve_timeout(),
    )


def create_app() -> FastAPI:
    """Build the databricks-genie harness's FastAPI app (required entry point)."""
    adapter = ExecutorAdapter(executor_factory=_build_databricks_genie_executor)
    return adapter.build()
