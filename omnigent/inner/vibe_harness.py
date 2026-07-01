"""``harness: vibe`` wrap.

Thin module exposing :func:`create_app` — the entrypoint the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent
process resolves ``"vibe"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Wraps a :class:`omnigent.inner.vibe_executor.VibeExecutor` that drives
the upstream Mistral Vibe CLI headlessly via
``vibe -p <prompt> --output streaming`` per turn.
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import FastAPI

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.inner.vibe_executor import VibeExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

_ENV_AGENT = "HARNESS_VIBE_AGENT"
_ENV_CWD = "HARNESS_VIBE_CWD"
_ENV_BIN = "HARNESS_VIBE_PATH"
_ENV_OS_ENV = "HARNESS_VIBE_OS_ENV"


def _resolve_os_env() -> OSEnvSpec:
    raw = os.environ.get(_ENV_OS_ENV, "").strip()
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            _logger.warning(
                "%s is not valid JSON (%s); falling back to default os_env",
                _ENV_OS_ENV,
                exc,
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


def _build_vibe_executor() -> Executor:
    return VibeExecutor(
        cwd=os.environ.get(_ENV_CWD) or None,
        os_env=_resolve_os_env(),
        agent=os.environ.get(_ENV_AGENT) or None,
        binary_path=os.environ.get(_ENV_BIN) or None,
    )


def create_app() -> FastAPI:
    adapter = ExecutorAdapter(executor_factory=_build_vibe_executor)
    return adapter.build()
