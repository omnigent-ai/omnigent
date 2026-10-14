"""``harness: zcode`` wrap.

Exposes :func:`create_app`. The runner resolves ``"zcode"`` to this module
and reads the env vars the parent set before spawn.

Env vars:

- ``HARNESS_ZCODE_MODEL``: unsupported model override. If set, the executor
  rejects the turn rather than silently using ZCode's default.
- ``HARNESS_ZCODE_CWD``: working directory. Falls back to
  ``OMNIGENT_RUNNER_WORKSPACE``.
- ``HARNESS_ZCODE_MODE``: must be ``yolo``. Unset also means ``yolo``.
- ``OMNIGENT_ZCODE_PATH``: absolute path to the ``zcode`` binary.
- ``HARNESS_ZCODE_OS_ENV``: JSON :class:`OSEnvSpec`.
- ``HARNESS_ZCODE_DISALLOWED_TOOLS``: JSON list of tool names for
  ``--disallowed-tools``. This selects tools; it is not a hard yolo boundary.
"""

from __future__ import annotations

import json
import logging
import os
from typing import cast

from fastapi import FastAPI

from omnigent.harness_startup_config import resolve_harness_path
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.inner.os_env_serialization import decode_sandbox_spec
from omnigent.inner.zcode_executor import ZCodeExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

_ENV_MODEL = "HARNESS_ZCODE_MODEL"
_ENV_CWD = "HARNESS_ZCODE_CWD"
_ENV_MODE = "HARNESS_ZCODE_MODE"
_ENV_OS_ENV = "HARNESS_ZCODE_OS_ENV"
_ENV_DISALLOWED = "HARNESS_ZCODE_DISALLOWED_TOOLS"


def _resolve_os_env() -> OSEnvSpec:
    """Decode ``HARNESS_ZCODE_OS_ENV``. Missing or malformed input means no OS sandbox."""
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
                decode_sandbox_spec(sandbox_payload) if isinstance(sandbox_payload, dict) else None
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


def _disallowed_tools() -> list[str]:
    raw = os.environ.get(_ENV_DISALLOWED, "").strip()
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        _logger.warning("%s is not valid JSON (%s); ignoring", _ENV_DISALLOWED, exc)
        return []
    if isinstance(decoded, list) and all(isinstance(item, str) for item in decoded):
        return cast(list[str], decoded)
    _logger.warning("%s decoded to %r; ignoring", _ENV_DISALLOWED, decoded)
    return []


def _build_zcode_executor() -> Executor:
    """Construct a :class:`ZCodeExecutor` from the spawn env."""
    mode = os.environ.get(_ENV_MODE, "").strip() or None
    model = os.environ.get(_ENV_MODEL, "").strip() or None
    return ZCodeExecutor(
        zcode_path=resolve_harness_path("zcode"),
        cwd=os.environ.get(_ENV_CWD) or os.environ.get("OMNIGENT_RUNNER_WORKSPACE"),
        model=model,
        mode=mode,
        os_env=_resolve_os_env(),
        disallowed_tools=_disallowed_tools(),
    )


def create_app() -> FastAPI:
    """Build the ZCode harness FastAPI app. The CLI is resolved on the first turn."""
    adapter = ExecutorAdapter(executor_factory=_build_zcode_executor)
    return adapter.build()
