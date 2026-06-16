"""``harness: agy`` wrap for Antigravity CLI.

Exposes :func:`create_app` for the shared harness runner and wraps
:class:`omnigent.inner.agy_executor.AgyExecutor`, which drives
``agy --print`` (one subprocess per turn; no ACP session).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI

from omnigent.inner.agy_executor import _DEFAULT_PRINT_TIMEOUT, AGY_DEFAULT_MODEL, AgyExecutor
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

_ENV_PATH = "HARNESS_AGY_PATH"
_ENV_MODEL = "HARNESS_AGY_MODEL"
_ENV_PRINT_TIMEOUT = "HARNESS_AGY_PRINT_TIMEOUT"
_ENV_CWD = "HARNESS_AGY_CWD"
_ENV_OS_ENV = "HARNESS_AGY_OS_ENV"
_ENV_SKILLS_FILTER = "HARNESS_AGY_SKILLS_FILTER"
_ENV_BUNDLE_DIR = "HARNESS_AGY_BUNDLE_DIR"
_ENV_AGENT_NAME = "HARNESS_AGY_AGENT_NAME"


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


def _resolve_skills_filter() -> str | list[str]:
    raw = os.environ.get(_ENV_SKILLS_FILTER, "").strip()
    if not raw:
        return "all"
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        _logger.warning(
            "%s is not valid JSON (%s); falling back to 'all'", _ENV_SKILLS_FILTER, exc
        )
        return "all"
    if isinstance(decoded, str) and decoded in ("all", "none"):
        return decoded
    if isinstance(decoded, list) and all(isinstance(s, str) for s in decoded):
        return decoded
    _logger.warning(
        "%s decoded to unsupported shape %r; falling back to 'all'",
        _ENV_SKILLS_FILTER,
        decoded,
    )
    return "all"


def _build_agy_executor() -> Executor:
    """Construct an :class:`AgyExecutor` from env-var config."""
    bundle_dir_raw = os.environ.get(_ENV_BUNDLE_DIR, "").strip()
    bundle_dir = Path(bundle_dir_raw) if bundle_dir_raw else None
    return AgyExecutor(
        cwd=os.environ.get(_ENV_CWD) or None,
        os_env=_resolve_os_env(),
        model=os.environ.get(_ENV_MODEL, "").strip() or AGY_DEFAULT_MODEL,
        agy_path=os.environ.get(_ENV_PATH) or None,
        print_timeout=os.environ.get(_ENV_PRINT_TIMEOUT, "").strip() or _DEFAULT_PRINT_TIMEOUT,
        bundle_dir=bundle_dir,
        agent_name=os.environ.get(_ENV_AGENT_NAME, "").strip() or None,
        skills_filter=_resolve_skills_filter(),
    )


def create_app() -> FastAPI:
    """Build the agy harness's FastAPI app (required entry point)."""
    adapter = ExecutorAdapter(executor_factory=_build_agy_executor)
    return adapter.build()
