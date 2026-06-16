"""
``harness: cmd`` wrap.

Thin module exposing :func:`create_app` for the shared harness runner. It
wraps :class:`omnigent.inner.cmd_executor.CmdExecutor`, which drives
Command Code's ``cmd --print`` mode — a per-turn subprocess (no ACP, no
persistent session; Command Code is a one-shot CLI).

Env vars read at startup:

- ``HARNESS_CMD_MODEL``: Command Code model id, e.g.
  ``"claude-sonnet-4-6"`` or ``"deepseek-v4-pro"``. ``None`` lets
  ``cmd`` pick its configured default.
- ``HARNESS_CMD_PATH``: absolute path to a ``cmd`` CLI binary. ``None``
  searches ``PATH``.
- ``HARNESS_CMD_MAX_TURNS``: cap forwarded to ``cmd --max-turns``.
  ``None`` / unparseable falls back to Command Code's documented default
  (``10``).
- ``HARNESS_CMD_CWD``: working directory the subprocess operates in.
  ``None`` falls back to ``os_env.cwd`` then the process cwd.
- ``HARNESS_CMD_OS_ENV``: JSON-encoded :class:`OSEnvSpec`.
- ``HARNESS_CMD_SKILLS_FILTER``: JSON ``str | list[str]`` (parity; cmd
  has no in-v1 skill wiring).
- ``HARNESS_CMD_BUNDLE_DIR`` / ``HARNESS_CMD_AGENT_NAME``: reserved for
  future use.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI

from omnigent.inner.cmd_executor import _DEFAULT_MAX_TURNS, CmdExecutor
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

_ENV_MODEL = "HARNESS_CMD_MODEL"
_ENV_PATH = "HARNESS_CMD_PATH"
_ENV_MAX_TURNS = "HARNESS_CMD_MAX_TURNS"
_ENV_CWD = "HARNESS_CMD_CWD"
_ENV_OS_ENV = "HARNESS_CMD_OS_ENV"
_ENV_SKILLS_FILTER = "HARNESS_CMD_SKILLS_FILTER"
_ENV_BUNDLE_DIR = "HARNESS_CMD_BUNDLE_DIR"
_ENV_AGENT_NAME = "HARNESS_CMD_AGENT_NAME"


def _resolve_os_env() -> OSEnvSpec:
    """Resolve the inner-executor :class:`OSEnvSpec` from :data:`_ENV_OS_ENV`.

    Decodes the JSON-encoded dict Omnigent serialized via
    :func:`dataclasses.asdict`. When the env var is missing or malformed,
    falls back to ``caller_process + sandbox=none`` — matches the
    cursor / mimo / agy wraps' default for specs without an
    ``os_env:`` block.
    """
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
    """Resolve ``skills_filter`` from :data:`_ENV_SKILLS_FILTER` (defaults ``"all"``)."""
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


def _resolve_max_turns() -> int:
    """Resolve ``--max-turns`` from :data:`_ENV_MAX_TURNS`.

    Falls back to :data:`_DEFAULT_MAX_TURNS` (Command Code's documented
    default of ``10``) when the env var is unset, empty, non-integer, or
    non-positive — a bad value must not silently disable the runaway cap.
    """
    raw = os.environ.get(_ENV_MAX_TURNS, "").strip()
    if not raw:
        return _DEFAULT_MAX_TURNS
    try:
        value = int(raw)
    except ValueError:
        _logger.warning(
            "%s is not an integer (%r); falling back to %d",
            _ENV_MAX_TURNS,
            raw,
            _DEFAULT_MAX_TURNS,
        )
        return _DEFAULT_MAX_TURNS
    if value <= 0:
        _logger.warning(
            "%s must be positive (got %d); falling back to %d",
            _ENV_MAX_TURNS,
            value,
            _DEFAULT_MAX_TURNS,
        )
        return _DEFAULT_MAX_TURNS
    return value


def _build_cmd_executor() -> Executor:
    """Construct a :class:`CmdExecutor` from env-var config.

    Called lazily by the :class:`ExecutorAdapter` on the first turn, so
    an absent ``cmd`` surfaces as a request-time error rather than an
    app-boot crash.

    :raises ImportError: If ``cmd`` isn't on PATH and ``HARNESS_CMD_PATH``
        isn't set.
    """
    bundle_dir_raw = os.environ.get(_ENV_BUNDLE_DIR, "").strip()
    bundle_dir = Path(bundle_dir_raw) if bundle_dir_raw else None
    return CmdExecutor(
        cwd=os.environ.get(_ENV_CWD) or None,
        os_env=_resolve_os_env(),
        model=os.environ.get(_ENV_MODEL) or None,
        cmd_path=os.environ.get(_ENV_PATH) or None,
        max_turns=_resolve_max_turns(),
        bundle_dir=bundle_dir,
        agent_name=os.environ.get(_ENV_AGENT_NAME, "").strip() or None,
        skills_filter=_resolve_skills_filter(),
    )


def create_app() -> FastAPI:
    """Build the cmd harness's FastAPI app (required entry point)."""
    adapter = ExecutorAdapter(executor_factory=_build_cmd_executor)
    return adapter.build()
