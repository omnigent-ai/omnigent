"""``harness: forge`` wrap.

Thin module exposing :func:`create_app`, the entrypoint used by the
shared harness runner after the parent process resolves ``"forge"`` to
this module via the harness registry.

Wraps :class:`omnigent.inner.forge_executor.ForgeExecutor`, which drives
ForgeCode (https://forgecode.dev) headlessly via one ``forge -p``
subprocess per turn.
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import FastAPI

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.inner.forge_executor import ForgeExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

_ENV_MODEL = "HARNESS_FORGE_MODEL"
_ENV_CWD = "HARNESS_FORGE_CWD"
_ENV_BIN = "HARNESS_FORGE_PATH"
_ENV_OS_ENV = "HARNESS_FORGE_OS_ENV"
_ENV_AGENT = "HARNESS_FORGE_AGENT"
_ENV_CONFIG_DIR = "HARNESS_FORGE_CONFIG_DIR"


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


def _build_forge_executor() -> Executor:
    return ForgeExecutor(
        cwd=os.environ.get(_ENV_CWD) or os.environ.get("OMNIGENT_RUNNER_WORKSPACE") or None,
        os_env=_resolve_os_env(),
        model=os.environ.get(_ENV_MODEL) or None,
        binary_path=os.environ.get(_ENV_BIN) or None,
        agent=os.environ.get(_ENV_AGENT) or None,
        config_dir=os.environ.get(_ENV_CONFIG_DIR) or None,
    )


def create_app() -> FastAPI:
    """Build the forge harness's FastAPI app."""
    adapter = ExecutorAdapter(executor_factory=_build_forge_executor)
    return adapter.build()
