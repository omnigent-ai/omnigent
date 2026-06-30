"""
``harness: qwen`` wrap.

Thin module exposing :func:`create_app` — the entrypoint the
shared :mod:`omnigent.runtime.harnesses._runner` invokes after
the parent process resolves ``"qwen"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Internally, instantiates :class:`omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter`
around a :class:`omnigent.inner.qwen_executor.QwenExecutor`
configured from env vars the parent process sets before spawning.
Mirrors the claude-sdk wrap (``claude_sdk_harness.py``) and codex
wrap (``codex_harness.py``); see the claude-sdk module's docstring
for the v1 config-flow rationale (env vars vs per-request).

Env vars read at startup:

- ``HARNESS_QWEN_MODEL``: model identifier, e.g.
  ``"qwen/qwen-plus"``. ``None`` falls back to Qwen's default.
- ``HARNESS_QWEN_CWD``: working directory the executor launches
  the Qwen CLI in. ``None`` falls back to ``OMNIGENT_RUNNER_WORKSPACE`` if set,
  then to the subprocess's inherited cwd.
- ``HARNESS_QWEN_PATH``: absolute path to a ``qwen`` CLI binary.
  ``None`` searches ``PATH``.
- ``HARNESS_QWEN_OS_ENV``: JSON-encoded :class:`OSEnvSpec`
  (from :func:`dataclasses.asdict`). When unset, the wrap
  falls back to a default
  ``OSEnvSpec(type="caller_process", sandbox=type="none")`` so
  Omnigent mode parity with the legacy non-AP path holds for
  specs that don't declare an ``os_env:`` block.
- ``HARNESS_QWEN_GATEWAY_BASE_URL`` / ``HARNESS_QWEN_GATEWAY_AUTH_COMMAND``:
  OpenAI-compatible provider/gateway routing from the spec's ``auth:`` /
  ``providers:`` config. When both are set, the executor exports
  ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` / ``OPENAI_MODEL`` into the ``qwen``
  subprocess instead of relying on the CLI's ambient auth.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

from fastapi import FastAPI

if TYPE_CHECKING:
    pass

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.inner.qwen_executor import QwenExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

# Env-var keys the wrap reads at executor construction time. See
# the module docstring for semantics. Centralizing as constants
# so misconfigurations surface as a single grep target.
_ENV_MODEL = "HARNESS_QWEN_MODEL"
_ENV_CWD = "HARNESS_QWEN_CWD"
_ENV_QWEN_PATH = "HARNESS_QWEN_PATH"
_ENV_OS_ENV = "HARNESS_QWEN_OS_ENV"
# Generic-provider / gateway routing: an OpenAI-compatible base URL plus a
# shell command that prints a bearer token. Emitted by the spawn-env builder
# (workflow.configure_agent_harness_with_provider) from the spec's
# auth:/providers: config; the executor translates them into the OPENAI_* env
# vars the qwen CLI reads. See docs/QWEN_FOLLOWUPS.md.
_ENV_GATEWAY_BASE_URL = "HARNESS_QWEN_GATEWAY_BASE_URL"
_ENV_GATEWAY_AUTH_COMMAND = "HARNESS_QWEN_GATEWAY_AUTH_COMMAND"


def _resolve_os_env() -> OSEnvSpec:
    """
    Resolve the inner-executor :class:`OSEnvSpec` from env config.

    Reads :data:`_ENV_OS_ENV` and decodes the JSON-encoded dict
    Omnigent serialized via :func:`dataclasses.asdict` on its
    :class:`OSEnvSpec`. When the env var is missing or
    malformed, falls back to ``caller_process + sandbox=none``
    so AP-bridged tools stay enabled — matches the legacy
    non-AP path's default for specs without an
    ``os_env:`` block.

    :returns: An :class:`OSEnvSpec` to hand to
        :class:`QwenExecutor`.
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
    # Default: enable natives, no sandbox. Matches the simplest
    # working config; operators who want real sandbox enforcement
    # configure ``os_env.sandbox`` explicitly in the spec.
    return OSEnvSpec(
        type="caller_process",
        cwd=None,
        sandbox=OSEnvSandboxSpec(type="none"),
        fork=False,
    )


def _build_qwen_executor() -> Executor:
    """
    Construct a :class:`QwenExecutor` from env-var config.

    Called lazily by the :class:`ExecutorAdapter` on the first
    turn. Heavyweight init (CLI discovery) happens at this point.

    :returns: A configured :class:`QwenExecutor` instance.
    :raises ImportError: If the ``qwen`` CLI isn't on PATH and
        ``HARNESS_QWEN_PATH`` isn't set — the inner executor's
        constructor surfaces this as a clear ImportError.
    """
    cwd_raw = os.environ.get(_ENV_CWD) or os.environ.get("OMNIGENT_RUNNER_WORKSPACE")
    cwd = cwd_raw or None
    model_raw = os.environ.get(_ENV_MODEL, "").strip()
    model = model_raw or None
    qwen_path_raw = os.environ.get(_ENV_QWEN_PATH, "").strip()
    qwen_path = qwen_path_raw or None
    gateway_base_url = os.environ.get(_ENV_GATEWAY_BASE_URL, "").strip() or None
    gateway_auth_command = os.environ.get(_ENV_GATEWAY_AUTH_COMMAND, "").strip() or None

    return QwenExecutor(
        cwd=cwd,
        os_env=_resolve_os_env(),
        model=model,
        qwen_path=qwen_path,
        gateway_base_url=gateway_base_url,
        gateway_auth_command=gateway_auth_command,
    )


def create_app() -> FastAPI:
    """
    Build the qwen harness's FastAPI app.

    Required entry point per the harness contract — the runner
    imports this module (resolved from
    :data:`omnigent.runtime.harnesses._HARNESS_MODULES`) and
    invokes ``create_app()`` to get the app it serves.

    :returns: The FastAPI app from :class:`ExecutorAdapter`'s
        :meth:`build` method, with all routes from the harness
        API subset wired up. The wrapped :class:`QwenExecutor`
        is constructed lazily on the first turn (so an absent
        ``qwen`` CLI surfaces as a request-time error, not a
        FastAPI app-boot crash).
    """
    adapter = ExecutorAdapter(executor_factory=_build_qwen_executor, harness_label="Qwen")
    return adapter.build()
