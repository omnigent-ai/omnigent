"""``harness: cursor`` wrap for Cursor Agent CLI."""

from __future__ import annotations

import json
import logging
import os

from fastapi import FastAPI

from omnigent.inner.cursor_executor import CursorExecutor
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

_ENV_MODEL = "HARNESS_CURSOR_MODEL"
_ENV_CWD = "HARNESS_CURSOR_CWD"
_ENV_CURSOR_PATH = "HARNESS_CURSOR_PATH"
_ENV_OS_ENV = "HARNESS_CURSOR_OS_ENV"
_ENV_AGENT_NAME = "HARNESS_CURSOR_AGENT_NAME"


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


def _build_cursor_executor() -> Executor:
    agent_name_raw = os.environ.get(_ENV_AGENT_NAME, "").strip()
    return CursorExecutor(
        cwd=os.environ.get(_ENV_CWD),
        os_env=_resolve_os_env(),
        model=os.environ.get(_ENV_MODEL) or None,
        cursor_path=os.environ.get(_ENV_CURSOR_PATH) or None,
        agent_name=agent_name_raw or None,
    )


def create_app() -> FastAPI:
    """Build the ``cursor`` harness FastAPI app."""
    adapter = ExecutorAdapter(executor_factory=_build_cursor_executor)
    return adapter.build()
