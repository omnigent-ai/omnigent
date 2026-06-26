"""``harness: cursor-cloud`` wrap.

Thin module exposing :func:`create_app` — the entrypoint the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent process
resolves ``"cursor-cloud"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Wraps a :class:`omnigent.inner.cursor_cloud_executor.CursorCloudExecutor`,
which launches Cursor Cloud / Background Agent runs over the ``cursor-sdk``.
Mirrors the ``cursor`` wrap's env-var config flow; like ``cursor`` it has NO
gateway / Databricks-profile env vars (the SDK talks only to Cursor's backend).

Env vars read at startup:

- ``HARNESS_CURSOR_CLOUD_MODEL``: Cursor cloud model id (e.g. ``composer-2.5``,
  ``claude-4.6-sonnet-thinking``). ``None`` / a ``databricks-*`` id resolves to
  the cloud default.
- ``HARNESS_CURSOR_CLOUD_API_KEY``: Cursor API key (the same ``crsr_`` key the
  local ``cursor`` harness uses). The runner-side spawn-env builder
  (``_build_cursor_cloud_spawn_env``) resolves the key (spec api-key > stored
  cursor key > ambient ``CURSOR_API_KEY``) before spawn; this wrap simply reads
  the already-resolved value, passing ``None`` when it is unset.
- ``HARNESS_CURSOR_CLOUD_REPO`` / ``HARNESS_CURSOR_CLOUD_REF``: the GitHub repo
  URL + starting ref the cloud agent clones. Resolved by the spawn-env builder
  from the cwd ``origin`` remote (or an override).
- ``HARNESS_CURSOR_CLOUD_CWD``: working directory (used only to seed defaults;
  the cloud run does not touch it).
- ``HARNESS_CURSOR_CLOUD_OS_ENV``: JSON-encoded :class:`OSEnvSpec` (its ``cwd``
  is used when ``HARNESS_CURSOR_CLOUD_CWD`` is unset).
- ``HARNESS_CURSOR_CLOUD_AGENT_NAME``: optional display name for the run.
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import FastAPI

from omnigent.inner.cursor_cloud_executor import CursorCloudExecutor
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

_ENV_MODEL = "HARNESS_CURSOR_CLOUD_MODEL"
_ENV_API_KEY = "HARNESS_CURSOR_CLOUD_API_KEY"
_ENV_REPO = "HARNESS_CURSOR_CLOUD_REPO"
_ENV_REF = "HARNESS_CURSOR_CLOUD_REF"
_ENV_CWD = "HARNESS_CURSOR_CLOUD_CWD"
_ENV_OS_ENV = "HARNESS_CURSOR_CLOUD_OS_ENV"
_ENV_AGENT_NAME = "HARNESS_CURSOR_CLOUD_AGENT_NAME"


def _resolve_os_env() -> OSEnvSpec:
    """Resolve the inner-executor :class:`OSEnvSpec` from :data:`_ENV_OS_ENV`.

    Mirrors the ``cursor`` wrap: decodes the JSON-encoded dict, falling back to
    ``caller_process + sandbox=none`` when the env var is missing or malformed.
    """
    raw = os.environ.get(_ENV_OS_ENV, "").strip()
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            _logger.warning(
                "%s is not valid JSON (%s); falling back to default os_env", _ENV_OS_ENV, exc
            )
            payload = None
        if isinstance(payload, dict):
            sandbox_payload = payload.get("sandbox")
            sandbox = (
                OSEnvSandboxSpec(**sandbox_payload) if isinstance(sandbox_payload, dict) else None
            )
            return OSEnvSpec(
                type=str(payload.get("type", "caller_process")),
                cwd=payload.get("cwd"),
                sandbox=sandbox,
                fork=bool(payload.get("fork", False)),
            )
    return OSEnvSpec(
        type="caller_process",
        cwd=None,
        sandbox=OSEnvSandboxSpec(type="none"),
        fork=False,
    )


def _build_cursor_cloud_executor() -> Executor:
    """Construct a :class:`CursorCloudExecutor` from env-var config.

    Called lazily by the :class:`ExecutorAdapter` on the first turn, so a
    missing ``cursor-sdk`` install surfaces as a request-time error rather than
    an app-boot crash.
    """
    return CursorCloudExecutor(
        cwd=os.environ.get(_ENV_CWD) or None,
        os_env=_resolve_os_env(),
        model=os.environ.get(_ENV_MODEL) or None,
        api_key=os.environ.get(_ENV_API_KEY) or None,
        repo_url=os.environ.get(_ENV_REPO) or None,
        ref=os.environ.get(_ENV_REF) or None,
        agent_name=os.environ.get(_ENV_AGENT_NAME, "").strip() or None,
    )


def create_app() -> FastAPI:
    """Build the cursor-cloud harness's FastAPI app (required entry point)."""
    adapter = ExecutorAdapter(
        executor_factory=_build_cursor_cloud_executor, harness_label="Cursor Cloud"
    )
    return adapter.build()
