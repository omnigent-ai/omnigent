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
- ``HARNESS_ACP_OMNIGENT_MCP``: ``"0"`` to disable Omnigent's MCP relay;
  ``session/new`` still receives an empty ``mcpServers`` array.
- ``HARNESS_ACP_OS_ENV``: JSON-encoded :class:`OSEnvSpec`. When unset, falls
  back to ``caller_process`` + ``sandbox=none``.
- ``HARNESS_ACP_ENV_PASSTHROUGH``: comma-separated environment variable *names*
  the agent may read at spawn (the spawn env is otherwise deny-by-default, so an
  agent authenticating from a variable needs it named here). Names only — each
  value is read from this process's own environment.
- ``HARNESS_ACP_PROMPT_TIMEOUT_S``: optional idle (time-without-progress) deadline in
  seconds for a prompt turn (default 300); must be positive and finite or the child aborts.
- ``HARNESS_ACP_PERMISSION_MODE``: Omnigent permission stance, ``auto`` (default) or
  ``bypassPermissions`` — the latter skips the approval card for a tool call no
  policy had an opinion on, so a headless agent runs without parking on prompts.
- ``HARNESS_ACP_INJECT_SYSTEM_PROMPT``: ``"0"`` to skip folding the Omnigent system
  prompt into the first ACP turn. Recommended for Pi-fork agents (e.g. ``omp``) that
  fully own their own system prompt — prepending Omnigent's text can cause the agent's
  internal Claude model to emit XML tool-call fragments when no MCP relay is backing the
  described tools (see ``omnigent_mcp``). Defaults to ``"1"`` (inject).
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import FastAPI

from omnigent.cli_invocation import cli_invocation
from omnigent.inner.acp_executor import AcpAgentConfig, AcpExecutor
from omnigent.inner.acp_extension import NO_ACP_EXTENSION, AcpExtension
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

_ENV_COMMAND = "HARNESS_ACP_COMMAND"
_ENV_NAME = "HARNESS_ACP_NAME"
_ENV_MODEL = "HARNESS_ACP_MODEL"
_ENV_SESSION_ID_MODE = "HARNESS_ACP_SESSION_ID_MODE"
_ENV_SEND_MODEL = "HARNESS_ACP_SEND_MODEL"
_ENV_OMNIGENT_MCP = "HARNESS_ACP_OMNIGENT_MCP"
_ENV_INJECT_SYSTEM_PROMPT = "HARNESS_ACP_INJECT_SYSTEM_PROMPT"
_ENV_CWD = "HARNESS_ACP_CWD"
_ENV_OS_ENV = "HARNESS_ACP_OS_ENV"
_ENV_ENV_PASSTHROUGH = "HARNESS_ACP_ENV_PASSTHROUGH"
_ENV_PERMISSION_MODE = "HARNESS_ACP_PERMISSION_MODE"
_DEFAULT_PERMISSION_MODE = "auto"


def _env_enabled(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_passthrough_names() -> tuple[str, ...]:
    """Variable names the configured agent may read, from the spawn env.

    Comma-separated names (never values — the parent forwards only names, and
    the value is read from this process's own environment at spawn).
    """
    raw = os.environ.get(_ENV_ENV_PASSTHROUGH, "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


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


def _build_acp_executor(extension: AcpExtension = NO_ACP_EXTENSION) -> Executor:
    """Construct an :class:`AcpExecutor` from env-var config (lazily, on first turn).

    :param extension: Vendor behavior to inject, from the calling wrap. Defaults
        to protocol-only for the generic ``acp`` harness.
    """
    command = os.environ.get(_ENV_COMMAND, "").strip()
    if not command:
        raise RuntimeError(
            f"{_ENV_COMMAND} is not set — no ACP agent command configured. "
            f"Add one via `{cli_invocation()} setup` → configure harnesses → Custom ACP agent."
        )
    name = os.environ.get(_ENV_NAME, "").strip() or "ACP agent"
    model = os.environ.get(_ENV_MODEL, "").strip() or None
    session_id_mode = os.environ.get(_ENV_SESSION_ID_MODE, "").strip() or "server"
    send_model = _env_enabled(_ENV_SEND_MODEL, default=False)
    omnigent_mcp = _env_enabled(_ENV_OMNIGENT_MCP, default=True)
    inject_system_prompt = _env_enabled(_ENV_INJECT_SYSTEM_PROMPT, default=True)
    cwd = os.environ.get(_ENV_CWD) or os.environ.get("OMNIGENT_RUNNER_WORKSPACE") or None
    permission_mode = os.environ.get(_ENV_PERMISSION_MODE, "").strip() or _DEFAULT_PERMISSION_MODE

    config = AcpAgentConfig(
        command=command,
        name=name,
        model=model,
        session_id_mode=session_id_mode,
        send_model_in_session_new=send_model,
        omnigent_mcp=omnigent_mcp,
        env_passthrough=_env_passthrough_names(),
        permission_mode=permission_mode,
        inject_system_prompt=inject_system_prompt,
    )
    return AcpExecutor(config=config, cwd=cwd, os_env=_resolve_os_env(), extension=extension)


def create_app(extension: AcpExtension = NO_ACP_EXTENSION) -> FastAPI:
    """Build the generic ACP harness's FastAPI app (required entry point).

    The wrapped :class:`AcpExecutor` is constructed lazily on the first turn, so
    a missing command / absent agent binary surfaces as a request-time error
    rather than an app-boot crash.

    :param extension: Vendor behavior for the agent this process drives. A
        vendor's own wrap calls this with its extension (see
        :mod:`omnigent.inner.devin.harness`); the runner calls it with no
        argument for ``harness: acp`` and for a builtin ACP CLI row that declares
        no vendor behavior.
    :returns: The app the runner serves.
    """
    label = os.environ.get(_ENV_NAME, "").strip() or "ACP agent"
    adapter = ExecutorAdapter(
        executor_factory=lambda: _build_acp_executor(extension), harness_label=label
    )
    return adapter.build()
