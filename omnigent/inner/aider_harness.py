"""
``harness: aider`` wrap.

Thin module exposing :func:`create_app` — the entrypoint the
shared :mod:`omnigent.runtime.harnesses._runner` invokes after
the parent process resolves ``"aider"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Internally, instantiates :class:`omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter`
around a :class:`omnigent.inner.aider_executor.AiderExecutor`
configured from env vars the parent process sets before spawning.
Mirrors the qwen wrap (``qwen_harness.py``).

Env vars read at startup:

- ``HARNESS_AIDER_MODEL``: model identifier passed to ``aider --model``,
  e.g. ``"claude-3-5-sonnet"``. ``None`` falls back to aider's default.
- ``HARNESS_AIDER_CWD``: working directory the executor launches aider in.
  ``None`` falls back to ``OMNIGENT_RUNNER_WORKSPACE`` if set, then to the
  subprocess's inherited cwd.
- ``HARNESS_AIDER_PATH``: absolute path to an ``aider`` CLI binary.
  ``None`` searches ``PATH``.
- ``HARNESS_AIDER_OS_ENV``: JSON-encoded :class:`OSEnvSpec`
  (from :func:`dataclasses.asdict`). When unset, the wrap falls back to a
  default ``OSEnvSpec(type="caller_process", sandbox=type="none")``.
- ``HARNESS_AIDER_GATEWAY_BASE_URL`` / ``HARNESS_AIDER_GATEWAY_AUTH_COMMAND``:
  OpenAI-compatible provider/gateway routing from the spec's ``auth:`` /
  ``providers:`` config. When both are set, the executor exports
  ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` / ``OPENAI_MODEL`` into the aider
  subprocess (aider routes via LiteLLM) instead of relying on ambient auth.
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import FastAPI

from omnigent.inner.aider_executor import AiderExecutor
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

# Env-var keys the wrap reads at executor construction time. Centralizing as
# constants so misconfigurations surface as a single grep target.
_ENV_MODEL = "HARNESS_AIDER_MODEL"
_ENV_CWD = "HARNESS_AIDER_CWD"
_ENV_AIDER_PATH = "HARNESS_AIDER_PATH"
_ENV_OS_ENV = "HARNESS_AIDER_OS_ENV"
# Generic-provider / gateway routing: an OpenAI-compatible base URL plus a
# shell command that prints a bearer token. The executor translates them into
# the OPENAI_* env vars aider's LiteLLM backend reads.
_ENV_GATEWAY_BASE_URL = "HARNESS_AIDER_GATEWAY_BASE_URL"
_ENV_GATEWAY_AUTH_COMMAND = "HARNESS_AIDER_GATEWAY_AUTH_COMMAND"


def _resolve_os_env() -> OSEnvSpec:
    """
    Resolve the inner-executor :class:`OSEnvSpec` from env config.

    Reads :data:`_ENV_OS_ENV` and decodes the JSON-encoded dict Omnigent
    serialized via :func:`dataclasses.asdict` on its :class:`OSEnvSpec`. When
    the env var is missing or malformed, falls back to
    ``caller_process + sandbox=none``. Mirrors
    :func:`omnigent.inner.qwen_harness._resolve_os_env`.

    :returns: An :class:`OSEnvSpec` to hand to :class:`AiderExecutor`.
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


def _build_aider_executor() -> Executor:
    """
    Construct an :class:`AiderExecutor` from env-var config.

    Called lazily by the :class:`ExecutorAdapter` on the first turn.

    :returns: A configured :class:`AiderExecutor` instance.
    """
    cwd_raw = os.environ.get(_ENV_CWD) or os.environ.get("OMNIGENT_RUNNER_WORKSPACE")
    cwd = cwd_raw or None
    model_raw = os.environ.get(_ENV_MODEL, "").strip()
    model = model_raw or None
    aider_path_raw = os.environ.get(_ENV_AIDER_PATH, "").strip()
    aider_path = aider_path_raw or None
    gateway_base_url = os.environ.get(_ENV_GATEWAY_BASE_URL, "").strip() or None
    gateway_auth_command = os.environ.get(_ENV_GATEWAY_AUTH_COMMAND, "").strip() or None

    return AiderExecutor(
        cwd=cwd,
        os_env=_resolve_os_env(),
        model=model,
        aider_path=aider_path,
        gateway_base_url=gateway_base_url,
        gateway_auth_command=gateway_auth_command,
    )


def create_app() -> FastAPI:
    """
    Build the aider harness's FastAPI app.

    Required entry point per the harness contract — the runner imports this
    module (resolved from :data:`omnigent.runtime.harnesses._HARNESS_MODULES`)
    and invokes ``create_app()`` to get the app it serves. The wrapped
    :class:`AiderExecutor` is constructed lazily on the first turn, so an absent
    ``aider`` CLI surfaces as a request-time error, not an app-boot crash.

    :returns: The FastAPI app from :class:`ExecutorAdapter`'s :meth:`build`.
    """
    adapter = ExecutorAdapter(executor_factory=_build_aider_executor, harness_label="Aider")
    return adapter.build()
