"""``harness: acp`` wrap (the generic Agent Client Protocol harness).

Thin module exposing :func:`create_app` — the entry point the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent process
resolves ``"acp"`` (or ``"acp:<slug>"``) to this module.

Wraps an :class:`omnigent.inner.acp_executor.AcpExecutor`, which drives *any*
ACP agent command over the Agent Client Protocol — the vendor-agnostic
counterpart to the ``goose`` / ``qwen`` wraps. Which agent runs is decided by
the spawn-env the runner passes (see
:func:`omnigent.runtime.workflow._build_acp_spawn_env`), which resolves the
picked ``acp:<slug>`` to a user-configured command in the ``acp:`` config block.

Auth is each agent's own (the user logs into their agent via its own CLI);
Omnigent stores no credential. Tool approvals surface as web elicitation cards
via ``session/request_permission`` (bridges the :class:`ExecutorAdapter` installs).

Env vars read at startup:

- ``HARNESS_ACP_COMMAND`` (required): the command to launch, e.g.
  ``"gemini --experimental-acp"``. Missing → a request-time error.
- ``HARNESS_ACP_NAME``: display label for logs / elicitation cards.
- ``HARNESS_ACP_MODEL``: optional model id (only sent when the agent is
  configured to accept one in ``session/new``).
- ``HARNESS_ACP_SESSION_ID_MODE``: ``server`` (default) or ``client``.
- ``HARNESS_ACP_SEND_MODEL``: ``"1"`` to send the model in ``session/new``.
- ``HARNESS_ACP_OS_ENV``: JSON-encoded :class:`OSEnvSpec`. When unset, falls
  back to ``caller_process`` + ``sandbox=none``.
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import FastAPI

from omnigent.inner.acp_executor import AcpAgentConfig, AcpExecutor
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

_ENV_COMMAND = "HARNESS_ACP_COMMAND"
_ENV_NAME = "HARNESS_ACP_NAME"
_ENV_MODEL = "HARNESS_ACP_MODEL"
_ENV_SESSION_ID_MODE = "HARNESS_ACP_SESSION_ID_MODE"
_ENV_SEND_MODEL = "HARNESS_ACP_SEND_MODEL"
_ENV_CWD = "HARNESS_ACP_CWD"
_ENV_OS_ENV = "HARNESS_ACP_OS_ENV"


def _resolve_os_env() -> OSEnvSpec:
    """Resolve the inner-executor :class:`OSEnvSpec` from env config.

    Decodes the JSON-encoded :data:`_ENV_OS_ENV`; falls back to
    ``caller_process`` + ``sandbox=none`` when the var is missing or malformed.
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


def _build_acp_executor() -> Executor:
    """Construct an :class:`AcpExecutor` from env-var config (lazily, on first turn)."""
    command = os.environ.get(_ENV_COMMAND, "").strip()
    if not command:
        raise RuntimeError(
            f"{_ENV_COMMAND} is not set — no ACP agent command configured. "
            "Add one via `omnigent setup` → configure harnesses → Custom ACP agent."
        )
    name = os.environ.get(_ENV_NAME, "").strip() or "ACP agent"
    model = os.environ.get(_ENV_MODEL, "").strip() or None
    session_id_mode = os.environ.get(_ENV_SESSION_ID_MODE, "").strip() or "server"
    send_model = os.environ.get(_ENV_SEND_MODEL, "").strip() in ("1", "true", "yes")
    cwd = os.environ.get(_ENV_CWD) or os.environ.get("OMNIGENT_RUNNER_WORKSPACE") or None

    config = AcpAgentConfig(
        command=command,
        name=name,
        model=model,
        session_id_mode=session_id_mode,
        send_model_in_session_new=send_model,
    )
    return AcpExecutor(config=config, cwd=cwd, os_env=_resolve_os_env())


def create_app() -> FastAPI:
    """Build the generic ACP harness's FastAPI app (required entry point).

    The wrapped :class:`AcpExecutor` is constructed lazily on the first turn, so
    a missing command / absent agent binary surfaces as a request-time error
    rather than an app-boot crash.
    """
    label = os.environ.get(_ENV_NAME, "").strip() or "ACP agent"
    adapter = ExecutorAdapter(executor_factory=_build_acp_executor, harness_label=label)
    return adapter.build()
