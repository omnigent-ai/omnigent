"""Native-harness interrupt / stop control for the runner app.

The runner receives ``interrupt`` and ``stop_session`` events on
``/v1/sessions/{id}/events`` and must forward them into the resident vendor
TUI/bridge for the session's native harness. The per-harness logic used to be
sixteen closures inside :mod:`omnigent.runner.app`; this module collapses them
onto one :class:`NativeInterruptRunner` so the dispatch is registry-driven.

Mirrors :class:`omnigent.runner.codex.goal.CodexGoalRunner`: app-scope state
(the AP client, resource registry, event publisher, sub-agent wake plumbing) is
injected at construction so the class stays out of the already-large app module
while preserving the exact behavior of the original closures.

The seven uniform interrupt harnesses and six uniform stop harnesses differ
only by bridge module, control-function name, and error label; they collapse to
two parametrized methods driven by :data:`_UNIFORM_INTERRUPT` /
:data:`_UNIFORM_STOP`. claude interrupt (bridge-id resolution) and codex
interrupt (MCP-startup + app-server ``turn/interrupt``) keep dedicated methods
(so nine interrupt handlers total); claude stop is likewise special-cased and
codex/pi alias stop to their interrupt handler (so seven stop handlers total).

Coverage note: antigravity-native and opencode-native have no handler here and
:meth:`interrupt` / :meth:`stop` return ``None`` for them, so the caller falls
through to the in-process turn cancel — unchanged from before this seam. Wiring
their native interrupt (agy ``interrupt_turn`` / opencode ``client.abort``) is a
deferred follow-up.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import httpx
from fastapi.responses import JSONResponse, Response

from omnigent.native_coding_agents import native_coding_agent_for_harness
from omnigent.runner.native.orchestration import (
    _cancel_auto_forwarder_task,
    _claude_native_bridge_id_for_session,
    _session_labels_for_runner_spawn,
)
from omnigent.runner.resource_registry import SessionResourceRegistry

if TYPE_CHECKING:
    from omnigent.codex_native_bridge import CodexNativeBridgeState
    from omnigent.harness_plugins import NativeCodingAgent


class SubagentDeliveryAck(Protocol):
    """Result of attempting to deliver a terminal sub-agent payload."""

    @property
    def delivered(self) -> bool:
        raise NotImplementedError

    @property
    def entry(self) -> object | None:
        raise NotImplementedError

    @property
    def reason(self) -> str:
        raise NotImplementedError


class MarkSubagentTerminalAndWake(Protocol):
    """Mark a sub-agent work entry terminal and wake its parent."""

    def __call__(
        self, child_session_id: str, *, status: str, output: str | None
    ) -> SubagentDeliveryAck:
        raise NotImplementedError


class ClientSafeErrorDetail(Protocol):
    """Log an exception and return safe client-facing detail."""

    def __call__(self, exc: BaseException, *, context: str) -> str:
        raise NotImplementedError


class CodexBridgeStateForSession(Protocol):
    """Resolve a live Codex app-server bridge state for a session."""

    async def __call__(
        self,
        conv_id: str,
        *,
        action: str,
        missing_state_log_level: int = logging.WARNING,
    ) -> CodexNativeBridgeState | None:
        raise NotImplementedError


@dataclass(frozen=True)
class _UniformInterrupt:
    """Descriptor for a uniform bridge-inject interrupt handler.

    :param module: The harness bridge module, e.g. ``"omnigent.pi_native_bridge"``.
    :param inject_fn: The bridge control function name — ``"inject_interrupt"``
        for the TUI harnesses, ``"enqueue_interrupt"`` for pi.
    :param error_code: The structured error code returned on failure, e.g.
        ``"pi_native_interrupt_failed"``.
    :param context: The ``_client_safe_error_detail`` context label.
    :param error_types: Exception types the inject call may raise that map to a
        503 (pi raises ``OSError``; the TUI harnesses raise ``RuntimeError``).
    :param with_timeout: Whether the inject call takes a ``timeout_s=`` kwarg
        (the TUI harnesses do; pi's ``enqueue_interrupt`` does not).
    :param log_on_error: Whether to log a warning before the 503 — only pi's
        original handler did; the TUI handlers returned without logging.
    """

    module: str
    inject_fn: str
    error_code: str
    context: str
    error_types: tuple[type[BaseException], ...]
    with_timeout: bool
    log_on_error: bool = False


@dataclass(frozen=True)
class _UniformStop:
    """Descriptor for a uniform bridge-kill stop handler.

    :param module: The harness bridge module.
    :param error_code: The structured error code returned on failure.
    :param context: The ``_client_safe_error_detail`` context label.
    :param display_name: Human name for the delivery-not-confirmed warning.
    """

    module: str
    error_code: str
    context: str
    display_name: str


# The seven uniform interrupt harnesses (claude/codex are special-cased). pi uses
# enqueue_interrupt + OSError and no timeout; the rest inject_interrupt +
# RuntimeError + timeout_s.
_UNIFORM_INTERRUPT: dict[str, _UniformInterrupt] = {
    "pi": _UniformInterrupt(
        "omnigent.pi_native_bridge",
        "enqueue_interrupt",
        "pi_native_interrupt_failed",
        "pi-native interrupt",
        (OSError,),
        False,
        log_on_error=True,
    ),
    "cursor": _UniformInterrupt(
        "omnigent.cursor_native_bridge",
        "inject_interrupt",
        "cursor_native_interrupt_failed",
        "cursor-native interrupt",
        (RuntimeError,),
        True,
    ),
    "goose": _UniformInterrupt(
        "omnigent.goose_native_bridge",
        "inject_interrupt",
        "goose_native_interrupt_failed",
        "goose-native interrupt",
        (RuntimeError,),
        True,
    ),
    "kiro": _UniformInterrupt(
        "omnigent.kiro_native_bridge",
        "inject_interrupt",
        "kiro_native_interrupt_failed",
        "kiro-native interrupt",
        (RuntimeError,),
        True,
    ),
    "kimi": _UniformInterrupt(
        "omnigent.kimi_native_bridge",
        "inject_interrupt",
        "kimi_native_interrupt_failed",
        "kimi-native interrupt",
        (RuntimeError,),
        True,
    ),
    "hermes": _UniformInterrupt(
        "omnigent.hermes_native_bridge",
        "inject_interrupt",
        "hermes_native_interrupt_failed",
        "hermes-native interrupt",
        (RuntimeError,),
        True,
    ),
    "qwen": _UniformInterrupt(
        "omnigent.qwen_native_bridge",
        "inject_interrupt",
        "qwen_native_interrupt_failed",
        "qwen-native interrupt",
        (RuntimeError,),
        True,
    ),
}

# The six uniform stop harnesses (claude has a special stop; codex/pi have no
# distinct stop — they route to interrupt, handled in ``stop``).
_UNIFORM_STOP: dict[str, _UniformStop] = {
    "cursor": _UniformStop(
        "omnigent.cursor_native_bridge",
        "cursor_native_stop_failed",
        "cursor-native stop",
        "Cursor",
    ),
    "goose": _UniformStop(
        "omnigent.goose_native_bridge",
        "goose_native_stop_failed",
        "goose-native stop",
        "Goose",
    ),
    "kiro": _UniformStop(
        "omnigent.kiro_native_bridge",
        "kiro_native_stop_failed",
        "kiro-native stop",
        "Kiro",
    ),
    "kimi": _UniformStop(
        "omnigent.kimi_native_bridge",
        "kimi_native_stop_failed",
        "kimi-native stop",
        "Kimi",
    ),
    "hermes": _UniformStop(
        "omnigent.hermes_native_bridge",
        "hermes_native_stop_failed",
        "hermes-native stop",
        "Hermes",
    ),
    "qwen": _UniformStop(
        "omnigent.qwen_native_bridge",
        "qwen_native_stop_failed",
        "qwen-native stop",
        "Qwen",
    ),
}


def native_agent_for_cancel(wrapper_label: str | None) -> NativeCodingAgent | None:
    """Resolve a native agent from a session wrapper or sub-agent wrapper label.

    :param wrapper_label: ``omnigent.wrapper`` value, e.g. ``"goose-native-ui"``
        or ``"claude-code-native-ui-subagent"``.
    :returns: The matching :class:`~omnigent.harness_plugins.NativeCodingAgent`,
        or ``None`` when the label is missing or not native.
    """
    from omnigent.native_coding_agents import (
        NATIVE_CODING_AGENTS,
        native_coding_agent_for_wrapper_label,
    )

    agent = native_coding_agent_for_wrapper_label(wrapper_label)
    if agent is not None:
        return agent
    if not wrapper_label:
        return None
    for candidate in NATIVE_CODING_AGENTS:
        if candidate.subagent_wrapper_label == wrapper_label:
            return candidate
    return None


def native_cancel_capability(wrapper_label: str | None) -> str:
    """Classify a child's wrapper for parent-side ``sys_cancel_task`` routing.

    Mirrors :meth:`NativeInterruptRunner.stop` instead of comparing one Claude
    wrapper label:

    * ``"stop"`` — Claude's dedicated stop, or a key in :data:`_UNIFORM_STOP`
    * ``"best_effort"`` — remaining native agents (Codex/Pi alias stop to
      interrupt; Antigravity/OpenCode have no stop handler)
    * ``"inprocess"`` — no native agent for this label

    :param wrapper_label: The work entry's ``omnigent.wrapper`` value.
    :returns: One of ``"stop"``, ``"best_effort"``, or ``"inprocess"``.
    """
    agent = native_agent_for_cancel(wrapper_label)
    if agent is None:
        return "inprocess"
    if agent.key == "claude" or agent.key in _UNIFORM_STOP:
        return "stop"
    return "best_effort"


class NativeInterruptRunner:
    """Forward interrupt / stop events into a session's native harness bridge."""

    def __init__(
        self,
        *,
        server_client: httpx.AsyncClient,
        resource_registry: SessionResourceRegistry,
        publish_event: Callable[[str, dict[str, object]], None],
        mark_subagent_terminal_and_wake: MarkSubagentTerminalAndWake,
        session_sub_agent_names: Mapping[str, str],
        codex_bridge_state_for_session: CodexBridgeStateForSession,
        client_safe_error_detail: ClientSafeErrorDetail,
        logger: logging.Logger,
    ) -> None:
        self._server_client = server_client
        self._resource_registry = resource_registry
        self._publish_event = publish_event
        self._mark_subagent_terminal_and_wake = mark_subagent_terminal_and_wake
        self._session_sub_agent_names = session_sub_agent_names
        self._codex_bridge_state_for_session = codex_bridge_state_for_session
        self._client_safe_error_detail = client_safe_error_detail
        self._logger = logger

    async def interrupt(self, harness_name: str | None, conv_id: str) -> Response | None:
        """Dispatch an interrupt to the harness's bridge.

        :returns: A response when this harness has an interrupt handler, else
            ``None`` so the caller falls through to the in-process turn cancel
            (antigravity/opencode).
        """
        agent = native_coding_agent_for_harness(harness_name)
        if agent is None:
            return None
        key = agent.key
        if key == "claude":
            return await self._claude_interrupt(conv_id)
        if key == "codex":
            return await self._codex_interrupt(conv_id)
        spec = _UNIFORM_INTERRUPT.get(key)
        if spec is None:
            return None
        return await self._uniform_interrupt(spec, conv_id)

    async def stop(self, harness_name: str | None, conv_id: str) -> Response | None:
        """Dispatch a stop_session to the harness's bridge.

        codex/pi have no distinct stop — they route to their interrupt handler,
        exactly as the original dispatch chain did.

        :returns: A response when this harness has a stop handler, else ``None``
            so the caller falls through to the in-process turn cancel.
        """
        agent = native_coding_agent_for_harness(harness_name)
        if agent is None:
            return None
        key = agent.key
        if key == "claude":
            return await self._claude_stop(conv_id)
        if key in ("codex", "pi"):
            return await self.interrupt(harness_name, conv_id)
        spec = _UNIFORM_STOP.get(key)
        if spec is None:
            return None
        return await self._uniform_stop(spec, conv_id)

    def _wake_parent_after_native_interrupt(self, conv_id: str) -> None:
        delivery_ack = self._mark_subagent_terminal_and_wake(
            conv_id,
            status="cancelled",
            output="[System: sub-agent interrupted]",
        )
        if not delivery_ack.delivered and (
            delivery_ack.entry is not None or conv_id in self._session_sub_agent_names
        ):
            self._logger.warning(
                "Native interrupt: sub-agent delivery not confirmed; session=%s reason=%s",
                conv_id,
                delivery_ack.reason,
            )

    async def _teardown_session_terminals(self, conv_id: str) -> None:
        from omnigent.entities.session_resources import terminal_resource_id
        from omnigent.runner.tool_dispatch import _publish_terminal_deleted_event

        terminal_registry = self._resource_registry.terminal_registry
        if terminal_registry is None:
            return
        terminals = [
            (entry.terminal_name, entry.session_key)
            for entry in terminal_registry.list_for_conversation(conv_id)
        ]
        for terminal_name, session_key in terminals:
            terminal_id = terminal_resource_id(terminal_name, session_key)
            try:
                await self._resource_registry.close_terminal(conv_id, terminal_id)
            except (RuntimeError, OSError):
                self._logger.warning(
                    "Failed to close terminal %s for session %s during stop",
                    terminal_id,
                    conv_id,
                    exc_info=True,
                )
            _publish_terminal_deleted_event(
                conversation_id=conv_id,
                terminal_name=terminal_name,
                session_key=session_key,
                publish_event=self._publish_event,
            )

    async def _uniform_interrupt(self, spec: _UniformInterrupt, conv_id: str) -> Response:
        module = importlib.import_module(spec.module)
        bridge_dir = module.bridge_dir_for_session_id(conv_id)
        inject = getattr(module, spec.inject_fn)
        try:
            if spec.with_timeout:
                await asyncio.to_thread(inject, bridge_dir, timeout_s=1.0)
            else:
                await asyncio.to_thread(inject, bridge_dir)
        except spec.error_types as exc:
            if spec.log_on_error:
                self._logger.warning(
                    "%s failed for session=%s", spec.context, conv_id, exc_info=True
                )
            return JSONResponse(
                status_code=503,
                content={
                    "error": spec.error_code,
                    "detail": self._client_safe_error_detail(exc, context=spec.context),
                },
            )
        self._wake_parent_after_native_interrupt(conv_id)
        return Response(status_code=204)

    async def _uniform_stop(self, spec: _UniformStop, conv_id: str) -> Response:
        module = importlib.import_module(spec.module)
        try:
            await asyncio.to_thread(
                module.kill_session, module.bridge_dir_for_session_id(conv_id), timeout_s=1.0
            )
        except RuntimeError as exc:
            # 503 means the kill attempt failed — including a transient tmux
            # error against a still-running pane. This is not proof the pane
            # is already gone. Claude's genuine gone case is
            # ``TmuxSessionNotAdvertised`` and returns 204 from ``_claude_stop``.
            return JSONResponse(
                status_code=503,
                content={
                    "error": spec.error_code,
                    "detail": self._client_safe_error_detail(exc, context=spec.context),
                },
            )
        await self._teardown_session_terminals(conv_id)
        await _cancel_auto_forwarder_task(conv_id)
        self._publish_event(conv_id, {"type": "session.status", "status": "idle"})
        delivery_ack = self._mark_subagent_terminal_and_wake(
            conv_id,
            status="cancelled",
            output="[System: sub-agent stopped]",
        )
        if not delivery_ack.delivered and (
            delivery_ack.entry is not None or conv_id in self._session_sub_agent_names
        ):
            self._logger.warning(
                "%s-native stop succeeded but sub-agent delivery was "
                "not confirmed; session=%s reason=%s",
                spec.display_name,
                conv_id,
                delivery_ack.reason,
            )
        return Response(status_code=204)

    async def _claude_interrupt(self, conv_id: str) -> Response:
        from omnigent.claude_native_bridge import bridge_dir_for_bridge_id, inject_interrupt

        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=self._server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        try:
            await asyncio.to_thread(inject_interrupt, bridge_dir, timeout_s=1.0)
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_interrupt_failed",
                    "detail": self._client_safe_error_detail(
                        exc, context="claude-native interrupt"
                    ),
                },
            )
        self._wake_parent_after_native_interrupt(conv_id)
        return Response(status_code=204)

    async def _claude_stop(self, conv_id: str) -> Response:
        from omnigent.claude_native_bridge import (
            TmuxSessionNotAdvertised,
            bridge_dir_for_bridge_id,
            kill_session,
        )

        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=self._server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        try:
            await asyncio.to_thread(kill_session, bridge_dir, timeout_s=1.0)
        except TmuxSessionNotAdvertised:
            self._logger.debug("claude-native stop: no live tmux for %s", conv_id)
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_stop_failed",
                    "detail": self._client_safe_error_detail(exc, context="claude-native stop"),
                },
            )
        await self._teardown_session_terminals(conv_id)
        self._publish_event(conv_id, {"type": "session.status", "status": "idle"})
        delivery_ack = self._mark_subagent_terminal_and_wake(
            conv_id,
            status="cancelled",
            output="[System: sub-agent stopped]",
        )
        if not delivery_ack.delivered and (
            delivery_ack.entry is not None or conv_id in self._session_sub_agent_names
        ):
            self._logger.warning(
                "Claude-native stop succeeded but sub-agent delivery was "
                "not confirmed; session=%s reason=%s",
                conv_id,
                delivery_ack.reason,
            )
        return Response(status_code=204)

    async def _codex_interrupt(self, conv_id: str) -> Response:
        from omnigent.codex_native_app_server import client_for_transport
        from omnigent.codex_native_bridge import (
            CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
            bridge_dir_for_bridge_id,
            cancel_pending_mcp_startup,
            read_mcp_startup,
        )

        state = await self._codex_bridge_state_for_session(conv_id, action="interrupt")
        if state is None:
            return Response(status_code=204)
        labels = await _session_labels_for_runner_spawn(
            server_client=self._server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(
            labels.get(CODEX_NATIVE_BRIDGE_ID_LABEL_KEY) or conv_id
        )
        pending_mcp = cancel_pending_mcp_startup(bridge_dir)
        if state.active_turn_id is None and not pending_mcp:
            self._logger.info(
                "Codex-native interrupt skipped for %s: no active turn or MCP startup.",
                conv_id,
            )
            return Response(status_code=204)
        if pending_mcp:
            self._logger.info(
                "Codex-native interrupt for %s cancels MCP startup: %s",
                conv_id,
                ", ".join(pending_mcp),
            )
            try:
                await self._server_client.post(
                    f"/v1/sessions/{conv_id}/events",
                    json={
                        "type": "external_mcp_startup",
                        "data": {"servers": read_mcp_startup(bridge_dir)},
                    },
                    timeout=10.0,
                )
            except Exception:  # noqa: BLE001 - the bridge flip already took effect locally.
                self._logger.warning(
                    "Failed to publish cancelled MCP startup for %s", conv_id, exc_info=True
                )

        codex_client = client_for_transport(
            state.socket_path,
            client_name="omnigent-codex-native-runner",
        )
        try:
            await codex_client.connect()
            if pending_mcp:
                try:
                    await codex_client.request(
                        "turn/interrupt",
                        {"threadId": state.thread_id, "turnId": ""},
                    )
                except Exception:  # noqa: BLE001 - the local cancel already took effect.
                    self._logger.warning(
                        "Codex-native MCP startup interrupt failed for session=%s thread=%s",
                        conv_id,
                        state.thread_id,
                        exc_info=True,
                    )
            if state.active_turn_id is not None:
                await codex_client.request(
                    "turn/interrupt",
                    {
                        "threadId": state.thread_id,
                        "turnId": state.active_turn_id,
                    },
                )
        except Exception as exc:  # noqa: BLE001 - surface active-turn interrupt failures.
            self._logger.warning(
                "Codex-native turn/interrupt failed for session=%s thread=%s turn=%s",
                conv_id,
                state.thread_id,
                state.active_turn_id,
                exc_info=True,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_interrupt_failed",
                    "detail": self._client_safe_error_detail(
                        exc, context="codex-native interrupt"
                    ),
                },
            )
        finally:
            with contextlib.suppress(Exception):
                await codex_client.close()
        self._wake_parent_after_native_interrupt(conv_id)
        return Response(status_code=204)
