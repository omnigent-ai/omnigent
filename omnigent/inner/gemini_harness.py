"""
``harness: gemini`` wrap.

Thin module exposing :func:`create_app` — the entrypoint the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent process
resolves ``"gemini"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Wraps a :class:`omnigent.inner.gemini_executor.GeminiExecutor`, which drives a
``gemini --acp`` session. Mirrors the cursor / mimo wraps' env-var config flow.

This harness is pinned to a single model
(:data:`omnigent.inner.gemini_executor.GEMINI_PINNED_MODEL`), so there is NO
``HARNESS_GEMINI_MODEL`` env var — the model is fixed in the executor and any
spec / ``/model`` override is ignored.

Like cursor, gemini talks only to Google's own backend (``GEMINI_API_KEY`` /
``gemini`` login) and has no custom API base-URL override, so there is nothing
for the workflow layer to route through the Databricks AI gateway.

Env vars read at startup:

- ``HARNESS_GEMINI_PATH``: absolute path to a ``gemini`` CLI binary. ``None``
  searches ``PATH``.
- ``HARNESS_GEMINI_CWD``: working directory the session operates in. ``None``
  falls back to ``os_env.cwd`` then the process cwd.
- ``HARNESS_GEMINI_API_KEY``: Gemini API key, injected as ``GEMINI_API_KEY``.
  ``None`` falls back to an inherited ``GEMINI_API_KEY`` or a prior login.
- ``HARNESS_GEMINI_OS_ENV``: JSON-encoded :class:`OSEnvSpec` (its ``cwd`` is
  used when ``HARNESS_GEMINI_CWD`` is unset). Defaults to
  ``caller_process + sandbox=none``.
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import FastAPI

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.inner.gemini_executor import GeminiExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

_ENV_PATH = "HARNESS_GEMINI_PATH"
_ENV_CWD = "HARNESS_GEMINI_CWD"
_ENV_API_KEY = "HARNESS_GEMINI_API_KEY"
_ENV_OS_ENV = "HARNESS_GEMINI_OS_ENV"


def _resolve_os_env() -> OSEnvSpec:
    """Resolve the inner-executor :class:`OSEnvSpec` from :data:`_ENV_OS_ENV`.

    Decodes the JSON-encoded dict Omnigent serialized via
    :func:`dataclasses.asdict`. When the env var is missing or malformed, falls
    back to ``caller_process + sandbox=none`` — matches the cursor / mimo wraps'
    default for specs without an ``os_env:`` block.
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


def _build_gemini_executor() -> Executor:
    """Construct a :class:`GeminiExecutor` from env-var config.

    Called lazily by the :class:`ExecutorAdapter` on the first turn, so an
    absent ``gemini`` surfaces as a request-time error rather than an app-boot
    crash.

    :raises ImportError: If ``gemini`` isn't on PATH and ``HARNESS_GEMINI_PATH``
        isn't set.
    """
    return GeminiExecutor(
        cwd=os.environ.get(_ENV_CWD) or None,
        os_env=_resolve_os_env(),
        gemini_path=os.environ.get(_ENV_PATH) or None,
        api_key=os.environ.get(_ENV_API_KEY) or None,
    )


def create_app() -> FastAPI:
    """Build the gemini harness's FastAPI app (required entry point)."""
    adapter = ExecutorAdapter(executor_factory=_build_gemini_executor)
    return adapter.build()
