"""``harness: droid`` wrap (the headless Factory AI Droid ACP harness).

Thin module exposing :func:`create_app` — the entry point the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent process
resolves ``"droid"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Wraps a :class:`omnigent.inner.droid_executor.DroidExecutor`, which drives
``droid exec --output-format acp`` over the Agent Client Protocol — the
chat-first, headless Factory AI Droid harness, mirroring the goose/qwen ACP
wraps. Droid's mid-turn tool approvals surface as web elicitation cards (via
Omnigent's TOOL_CALL policy + ``ctx.elicit`` bridges the
:class:`ExecutorAdapter` installs).

Auth is Droid's own configuration (a ``FACTORY_API_KEY`` env var, or the
interactive ``droid`` device-pairing login); Omnigent stores no Droid
credential. A spec ``executor.model`` is forwarded as ``droid exec -m <model>``.

Env vars read at startup:

- ``HARNESS_DROID_MODEL``: optional model id (``droid exec -m``). ``None`` uses
  Droid's default (``claude-opus-4-8``).
- ``HARNESS_DROID_CWD``: working directory for the droid subprocess. ``None``
  falls back to ``OMNIGENT_RUNNER_WORKSPACE`` then the inherited cwd.
- ``HARNESS_DROID_PATH``: absolute path to a ``droid`` CLI binary. ``None``
  searches ``PATH``.
- ``HARNESS_DROID_AUTO``: Droid autonomy tier (``low`` / ``medium`` / ``high``).
  ``None`` defaults to ``high`` (enables Droid's write/execute tools, which are
  blocked in its read-only default; individual calls stay gated via ACP
  permission → policy/elicitation).
- ``HARNESS_DROID_REASONING``: optional reasoning-effort level (``droid exec
  -r``). ``None`` uses Droid's per-model default.
- ``HARNESS_DROID_OS_ENV``: JSON-encoded :class:`OSEnvSpec`. When unset, falls
  back to ``caller_process`` + ``sandbox=none``.
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import FastAPI

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.droid_executor import DroidExecutor
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

_ENV_MODEL = "HARNESS_DROID_MODEL"
_ENV_CWD = "HARNESS_DROID_CWD"
_ENV_DROID_PATH = "HARNESS_DROID_PATH"
_ENV_AUTO = "HARNESS_DROID_AUTO"
_ENV_REASONING = "HARNESS_DROID_REASONING"
_ENV_OS_ENV = "HARNESS_DROID_OS_ENV"


def _resolve_os_env() -> OSEnvSpec:
    """Resolve the inner-executor :class:`OSEnvSpec` from env config.

    Decodes the JSON-encoded :data:`_ENV_OS_ENV` (serialized via
    :func:`dataclasses.asdict`); falls back to ``caller_process`` +
    ``sandbox=none`` when the var is missing or malformed.
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


def _build_droid_executor() -> Executor:
    """Construct a :class:`DroidExecutor` from env-var config (lazily, on first turn)."""
    cwd_raw = os.environ.get(_ENV_CWD) or os.environ.get("OMNIGENT_RUNNER_WORKSPACE")
    cwd = cwd_raw or None
    model = os.environ.get(_ENV_MODEL, "").strip() or None
    droid_path = os.environ.get(_ENV_DROID_PATH, "").strip() or None
    auto = os.environ.get(_ENV_AUTO, "").strip() or None
    reasoning = os.environ.get(_ENV_REASONING, "").strip() or None

    return DroidExecutor(
        cwd=cwd,
        os_env=_resolve_os_env(),
        model=model,
        droid_path=droid_path,
        auto=auto,
        reasoning=reasoning,
    )


def create_app() -> FastAPI:
    """Build the droid harness's FastAPI app (required entry point).

    The wrapped :class:`DroidExecutor` is constructed lazily on the first turn,
    so an absent ``droid`` CLI surfaces as a request-time error rather than an
    app-boot crash.
    """
    adapter = ExecutorAdapter(executor_factory=_build_droid_executor, harness_label="Droid")
    return adapter.build()
