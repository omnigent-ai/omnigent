"""``harness: goose`` wrap (the headless Goose ACP harness).

Thin module exposing :func:`create_app` — the entry point the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent process
resolves ``"goose"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Wraps a :class:`omnigent.inner.goose_executor.GooseExecutor`, which drives
``goose acp`` over the Agent Client Protocol — the chat-first, headless
counterpart to the terminal-first ``goose-native`` TUI harness
(:mod:`omnigent.inner.goose_native_harness`). Goose's mid-turn tool approvals
surface as web elicitation cards (via Omnigent's TOOL_CALL policy + ``ctx.elicit``
bridges the :class:`ExecutorAdapter` installs), mirroring the qwen wrap.

Auth is Goose's own configuration (``goose configure`` → keyring /
``~/.config/goose/config.yaml``); Omnigent stores no Goose credential. A spec
``executor.model`` is forwarded as a ``GOOSE_MODEL`` override; the provider stays
whatever ``goose configure`` selected unless ``HARNESS_GOOSE_PROVIDER`` overrides
it.

Env vars read at startup:

- ``HARNESS_GOOSE_MODEL``: optional ``GOOSE_MODEL`` override. ``None`` uses
  Goose's configured default.
- ``HARNESS_GOOSE_PROVIDER``: optional ``GOOSE_PROVIDER`` override.
- ``HARNESS_GOOSE_CWD``: working directory for the goose subprocess. ``None``
  falls back to ``OMNIGENT_RUNNER_WORKSPACE`` then the inherited cwd.
- ``HARNESS_GOOSE_PATH``: absolute path to a ``goose`` CLI binary. ``None``
  searches ``PATH``.
- ``HARNESS_GOOSE_BUILTINS``: comma-separated Goose builtin extensions to load
  (``--with-builtin``). ``None`` defaults to ``developer`` (shell + editor).
- ``HARNESS_GOOSE_OS_ENV``: JSON-encoded :class:`OSEnvSpec`. When unset, falls
  back to ``caller_process`` + ``sandbox=none``.
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import FastAPI

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.inner.goose_executor import GooseExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

_ENV_MODEL = "HARNESS_GOOSE_MODEL"
_ENV_PROVIDER = "HARNESS_GOOSE_PROVIDER"
_ENV_CWD = "HARNESS_GOOSE_CWD"
_ENV_GOOSE_PATH = "HARNESS_GOOSE_PATH"
_ENV_BUILTINS = "HARNESS_GOOSE_BUILTINS"
_ENV_OS_ENV = "HARNESS_GOOSE_OS_ENV"


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


def _build_goose_executor() -> Executor:
    """Construct a :class:`GooseExecutor` from env-var config (lazily, on first turn)."""
    cwd_raw = os.environ.get(_ENV_CWD) or os.environ.get("OMNIGENT_RUNNER_WORKSPACE")
    cwd = cwd_raw or None
    model = os.environ.get(_ENV_MODEL, "").strip() or None
    provider = os.environ.get(_ENV_PROVIDER, "").strip() or None
    goose_path = os.environ.get(_ENV_GOOSE_PATH, "").strip() or None
    builtins_raw = os.environ.get(_ENV_BUILTINS, "").strip()
    builtins = (
        tuple(part.strip() for part in builtins_raw.split(",") if part.strip())
        if builtins_raw
        else None
    )

    return GooseExecutor(
        cwd=cwd,
        os_env=_resolve_os_env(),
        model=model,
        provider=provider,
        goose_path=goose_path,
        builtins=builtins,
    )


def create_app() -> FastAPI:
    """Build the goose harness's FastAPI app (required entry point).

    The wrapped :class:`GooseExecutor` is constructed lazily on the first turn,
    so an absent ``goose`` CLI surfaces as a request-time error rather than an
    app-boot crash.
    """
    adapter = ExecutorAdapter(executor_factory=_build_goose_executor, harness_label="Goose")
    return adapter.build()
