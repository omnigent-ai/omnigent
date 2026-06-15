"""
``harness: mimo`` wrap.

Thin module exposing :func:`create_app` for the shared harness runner. It
wraps :class:`omnigent.inner.mimo_executor.MimoExecutor`, which drives
``mimo acp``.

Env vars read at startup:

- ``HARNESS_MIMO_MODEL``: Mimo model id, typically ``provider/model``.
- ``HARNESS_MIMO_PATH``: absolute path to a ``mimo`` CLI binary. ``None``
  searches ``PATH``.
- ``HARNESS_MIMO_CWD``: working directory the session operates in. ``None``
  falls back to ``os_env.cwd`` then the process cwd.
- ``HARNESS_MIMO_OS_ENV``: JSON-encoded :class:`OSEnvSpec`.
- ``HARNESS_MIMO_SKILLS_FILTER``: JSON ``str | list[str]`` (parity; Mimo ACP
  uses its own native/plugin system in v1).
- ``HARNESS_MIMO_BUNDLE_DIR`` / ``HARNESS_MIMO_AGENT_NAME``: reserved for
  future use.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.inner.mimo_executor import MimoExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

_ENV_MODEL = "HARNESS_MIMO_MODEL"
_ENV_PATH = "HARNESS_MIMO_PATH"
_ENV_CWD = "HARNESS_MIMO_CWD"
_ENV_OS_ENV = "HARNESS_MIMO_OS_ENV"
_ENV_SKILLS_FILTER = "HARNESS_MIMO_SKILLS_FILTER"
_ENV_BUNDLE_DIR = "HARNESS_MIMO_BUNDLE_DIR"
_ENV_AGENT_NAME = "HARNESS_MIMO_AGENT_NAME"


def _resolve_os_env() -> OSEnvSpec:
    """Resolve the inner-executor :class:`OSEnvSpec` from env config."""
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
    """Resolve ``skills_filter`` from env config."""
    raw = os.environ.get(_ENV_SKILLS_FILTER, "").strip()
    if not raw:
        return "all"
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        _logger.warning(
            "%s is not valid JSON (%s); falling back to 'all'",
            _ENV_SKILLS_FILTER,
            exc,
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


def _build_mimo_executor() -> Executor:
    """Construct a :class:`MimoExecutor` from env-var config."""
    bundle_dir_raw = os.environ.get(_ENV_BUNDLE_DIR, "").strip()
    bundle_dir = Path(bundle_dir_raw) if bundle_dir_raw else None
    return MimoExecutor(
        cwd=os.environ.get(_ENV_CWD) or None,
        os_env=_resolve_os_env(),
        model=os.environ.get(_ENV_MODEL) or None,
        mimo_path=os.environ.get(_ENV_PATH) or None,
        bundle_dir=bundle_dir,
        agent_name=os.environ.get(_ENV_AGENT_NAME, "").strip() or None,
        skills_filter=_resolve_skills_filter(),
    )


def create_app() -> FastAPI:
    """Build the mimo harness's FastAPI app."""
    adapter = ExecutorAdapter(executor_factory=_build_mimo_executor)
    return adapter.build()
