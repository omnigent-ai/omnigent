"""
``harness: databricks-genie`` wrap.

Thin module exposing :func:`create_app` — the entrypoint the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent process
resolves ``"databricks-genie"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Wraps a :class:`omnigent.inner.databricks_genie_executor.DatabricksGenieExecutor`,
which converses with a remote Databricks Genie space in Agent mode over the
Genie Responses API. Mirrors the cursor wrap's env-var config flow.

Env vars read when the adapter builds the executor — lazily, on the first
turn. Three are spec-driven — set by
:func:`omnigent.runtime.workflow._build_databricks_genie_spawn_env`:

- ``HARNESS_DATABRICKS_GENIE_MODEL``: the Genie space id (carried in
  ``executor.model``). ``None`` surfaces as a turn error telling the user to set
  it.
- ``HARNESS_DATABRICKS_GENIE_PROFILE``: the Databricks profile from
  ``~/.databrickscfg`` used to authenticate. ``None`` lets the SDK use its own
  resolution order.
- ``HARNESS_DATABRICKS_GENIE_ENABLE_VIZ``: optional opt-in asking Genie to
  attach visualizations to its answer, from ``executor.config["enable_viz"]``.
  Off unless set (case-insensitively) to ``"true"``/``"True"``/``"1"`` — the
  spawn-env builder exports a YAML ``true`` as the string ``"True"``.

The remaining one has no spec surface — the spawn-env builder never exports it,
so it is read from the ambient environment the harness subprocess inherits:

- ``HARNESS_DATABRICKS_GENIE_TIMEOUT``: optional idle timeout (seconds, float)
  bounding each silent gap in the streamed response — not the turn's total
  length. Values that are not a positive, finite number fall back to the
  executor default.
"""

from __future__ import annotations

import logging
import math
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
_ENV_ENABLE_VIZ = "HARNESS_DATABRICKS_GENIE_ENABLE_VIZ"

# ``executor.config`` scalars reach the harness stringified, so a YAML ``true``
# arrives as ``"True"``; accept the spellings a user could plausibly write.
_TRUTHY = frozenset({"true", "1"})


def _resolve_timeout() -> float:
    """Resolve the stream idle timeout from :data:`_ENV_TIMEOUT`.

    Only a positive, finite number of seconds is a usable HTTP deadline —
    ``0``, a negative, ``nan``, and ``inf`` all parse as floats but would either
    fail every request or never bound a wedged stream.

    :returns: The parsed timeout in seconds, or
        :data:`~omnigent.inner.databricks_genie_executor._DEFAULT_TIMEOUT_SECONDS`
        when the env var is unset or does not name a positive, finite number.
    """
    raw = os.environ.get(_ENV_TIMEOUT, "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        timeout = float(raw)
    except ValueError:
        _logger.warning(
            "%s is not a valid float (%r); falling back to %s seconds",
            _ENV_TIMEOUT,
            raw,
            _DEFAULT_TIMEOUT_SECONDS,
        )
        return _DEFAULT_TIMEOUT_SECONDS
    if not math.isfinite(timeout) or timeout <= 0:
        _logger.warning(
            "%s must be a positive, finite number of seconds (%r); falling back to %s seconds",
            _ENV_TIMEOUT,
            raw,
            _DEFAULT_TIMEOUT_SECONDS,
        )
        return _DEFAULT_TIMEOUT_SECONDS
    return timeout


def _resolve_enable_viz() -> bool:
    """Resolve the visualization opt-in from :data:`_ENV_ENABLE_VIZ`.

    :returns: ``True`` for ``"true"``/``"True"``/``"1"``; ``False`` when the
        env var is unset or holds anything else.
    """
    return os.environ.get(_ENV_ENABLE_VIZ, "").strip().lower() in _TRUTHY


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
        enable_viz=_resolve_enable_viz(),
    )


def create_app() -> FastAPI:
    """Build the databricks-genie harness's FastAPI app (required entry point)."""
    adapter = ExecutorAdapter(executor_factory=_build_databricks_genie_executor)
    return adapter.build()
