"""Host-owned interactive setup operations for the Providers settings UI.

The browser chooses from a closed action set. When interactive setup is needed,
the host resolves the executable and argv locally and owns the tmux process.
Operation snapshots expose only typed lifecycle metadata. Authentication terminal
bytes are forwarded live and never stored in a snapshot or application log.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import WebSocketDisconnect

from omnigent._platform import resolve_cli_binary
from omnigent.inner.terminal import TerminalInstance
from omnigent.onboarding.databricks_config import (
    DATABRICKS_EXTRA_INSTALL_HINT,
    databricks_sdk_installed,
)
from omnigent.onboarding.provider_config import (
    ANTHROPIC_FAMILY,
    GEMINI_FAMILY,
    OPENAI_FAMILY,
)
from omnigent.terminals.control_bridge import bridge_tmux_control_to_websocket

_COMMAND_TIMEOUT_SECONDS = 10 * 60
_DETACHED_EXPIRATION_SECONDS = 10 * 60
_ATTACH_DRAIN_SECONDS = 5.0
_MAX_INPUT_BYTES = 64 * 1024
_MAX_PENDING_INPUT_EVENTS = 16
_MAX_TERMINAL_COLUMNS = 500
_MAX_TERMINAL_ROWS = 300
_COMPLETED_OPERATION_RETENTION = 128
_DATABRICKS_AGENT_CHOICES = (
    ("claude",),
    ("codex",),
    ("pi",),
    ("claude", "codex", "pi"),
)


class SetupOperationAction(StrEnum):
    CLAUDE_LOGIN = "claude-login"
    CODEX_LOGIN = "codex-login"
    CURSOR_LOGIN = "cursor-login"
    CURSOR_LOGOUT = "cursor-logout"
    ANTIGRAVITY_LOGIN = "antigravity-login"
    OPENCODE_LOGIN = "opencode-login"
    QWEN_CONFIGURE = "qwen-configure"
    GOOSE_CONFIGURE = "goose-configure"
    HERMES_CONFIGURE = "hermes-configure"
    KIRO_LOGIN = "kiro-login"
    KIMI_LOGIN = "kimi-login"
    DATABRICKS_CONFIGURE = "databricks-configure"


class SetupOperationState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


_TERMINAL_STATES = frozenset(
    {
        SetupOperationState.SUCCEEDED,
        SetupOperationState.FAILED,
        SetupOperationState.CANCELLED,
        SetupOperationState.EXPIRED,
    }
)


class SetupOperationError(Exception):
    """Safe, typed failure exposed to the setup transport."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SetupOperationRequest:
    action: SetupOperationAction
    parameters: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> SetupOperationRequest:
        unknown = set(value) - {"action", "parameters"}
        if unknown:
            raise SetupOperationError("invalid_request", "Unexpected setup request fields.")
        try:
            action = SetupOperationAction(value.get("action"))
        except (TypeError, ValueError) as exc:
            raise SetupOperationError("invalid_request", "Unsupported setup action.") from exc
        parameters = value.get("parameters", {})
        if not isinstance(parameters, Mapping):
            raise SetupOperationError("invalid_request", "Setup parameters must be an object.")
        return cls(action=action, parameters=parameters)


@dataclass(frozen=True)
class SetupOperationSnapshot:
    operation_id: str
    state: SetupOperationState
    action: SetupOperationAction
    exit_code: int | None = None
    error: str | None = None
    already_connected: bool = False
    can_verify: bool = False

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "operation_id": self.operation_id,
            "state": self.state.value,
            "action": self.action.value,
            "exit_code": self.exit_code,
            "error": self.error,
        }
        if self.already_connected:
            result["already_connected"] = True
        if self.can_verify:
            result["can_verify"] = True
        return result


TerminalEventSender = Callable[[dict[str, object]], Awaitable[None]]


class _ManagedTerminal(Protocol):
    socket_path: Path
    running: bool

    @property
    def tmux_target(self) -> str: ...

    async def launch(self, *, cwd: Path | None = None) -> None: ...

    async def close(self) -> None: ...

    def last_exit_status(self) -> int | None: ...

    def note_client_interaction(self) -> None: ...

    def start_idle_watcher(
        self,
        on_idle: Callable[[], None | Awaitable[None]],
        *,
        on_exit: Callable[[], None | Awaitable[None]] | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class _CommandPlan:
    executable: str
    args: tuple[str, ...]
    label: str


@dataclass
class _Operation:
    operation_id: str
    action: SetupOperationAction
    parameters: dict[str, object]
    terminal: _ManagedTerminal | None
    state: SetupOperationState = SetupOperationState.PENDING
    exit_code: int | None = None
    error: str | None = None
    finishing: bool = False
    monitor_task: asyncio.Task[None] | None = None
    expiration_task: asyncio.Task[None] | None = None
    attachments: dict[str, _TerminalAttachment] = field(default_factory=dict)
    close_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    terminal_closed: bool = False
    already_connected: bool = False

    def snapshot(self) -> SetupOperationSnapshot:
        return SetupOperationSnapshot(
            operation_id=self.operation_id,
            state=self.state,
            action=self.action,
            exit_code=self.exit_code,
            error=self.error,
            already_connected=self.already_connected,
            can_verify=(
                self.state == SetupOperationState.RUNNING
                and self.action == SetupOperationAction.ANTIGRAVITY_LOGIN
            ),
        )


class _TerminalAttachment:
    """WebSocket-shaped adapter over host-tunnel terminal callbacks."""

    def __init__(self, send: TerminalEventSender) -> None:
        self._send = send
        self._incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue(
            maxsize=_MAX_PENDING_INPUT_EVENTS
        )
        self._closed = False
        self._accepting = True
        self.task: asyncio.Task[None] | None = None

    async def send_bytes(self, data: bytes) -> None:
        try:
            await self._send(
                {
                    "type": "output",
                    "encoding": "base64",
                    "data": base64.b64encode(data).decode("ascii"),
                }
            )
        except Exception as exc:
            raise WebSocketDisconnect() from exc

    async def send_text(self, data: str) -> None:
        try:
            await self._send({"type": "control", "data": data})
        except Exception as exc:
            raise WebSocketDisconnect() from exc

    async def receive(self) -> dict[str, object]:
        return await self._incoming.get()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self._closed:
            return
        self._closed = True
        self._accepting = False
        with contextlib.suppress(Exception):
            await self._send({"type": "close", "code": code, "reason": reason})

    async def feed(self, payload: Mapping[str, object]) -> None:
        if not self._accepting:
            raise SetupOperationError("conflict", "Setup attachment is closed.")
        event_type = payload.get("type")
        if event_type == "input":
            raw_data = payload.get("data")
            if payload.get("encoding") != "base64" or not isinstance(raw_data, str):
                raise SetupOperationError("invalid_request", "Terminal input must be base64.")
            try:
                data = base64.b64decode(raw_data, validate=True)
            except (ValueError, TypeError) as exc:
                raise SetupOperationError(
                    "invalid_request", "Terminal input is not valid base64."
                ) from exc
            if len(data) > _MAX_INPUT_BYTES:
                raise SetupOperationError("invalid_request", "Terminal input is too large.")
            self._queue_input({"type": "websocket.receive", "bytes": data})
            return
        if event_type == "resize":
            cols = payload.get("cols")
            rows = payload.get("rows")
            if (
                not isinstance(cols, int)
                or isinstance(cols, bool)
                or not isinstance(rows, int)
                or isinstance(rows, bool)
            ):
                raise SetupOperationError("invalid_request", "Invalid terminal dimensions.")
            if not (1 <= cols <= _MAX_TERMINAL_COLUMNS and 1 <= rows <= _MAX_TERMINAL_ROWS):
                raise SetupOperationError("invalid_request", "Invalid terminal dimensions.")
            text = json.dumps(
                {"type": "resize", "cols": cols, "rows": rows}, separators=(",", ":")
            )
            self._queue_input({"type": "websocket.receive", "text": text})
            return
        raise SetupOperationError("invalid_request", "Unsupported terminal event.")

    async def disconnect(self) -> None:
        self._accepting = False
        while not self._incoming.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._incoming.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self._incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})

    def _queue_input(self, message: dict[str, object]) -> None:
        try:
            self._incoming.put_nowait(message)
        except asyncio.QueueFull as exc:
            self._accepting = False
            while not self._incoming.empty():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._incoming.get_nowait()
            self._incoming.put_nowait({"type": "websocket.disconnect", "code": 1009})
            raise SetupOperationError(
                "conflict", "Setup terminal input buffer exceeded its limit."
            ) from exc


TerminalFactory = Callable[[_CommandPlan, str], _ManagedTerminal]
Verifier = Callable[[SetupOperationAction, Mapping[str, object]], bool | None]
PostSuccess = Callable[[SetupOperationAction, Mapping[str, object]], None]


class SetupOperationManager:
    """Own the interactive setup process set for one connected host."""

    def __init__(
        self,
        *,
        terminal_factory: TerminalFactory | None = None,
        executable_resolver: Callable[[str], str | None] = shutil.which,
        verifier: Verifier | None = None,
        post_success: PostSuccess | None = None,
        command_timeout_seconds: float = _COMMAND_TIMEOUT_SECONDS,
        detached_expiration_seconds: float = _DETACHED_EXPIRATION_SECONDS,
    ) -> None:
        self._operations: dict[str, _Operation] = {}
        self._lock = asyncio.Lock()
        self._terminal_factory = terminal_factory or _create_terminal
        self._resolve_executable = executable_resolver
        self._verifier = verifier or _verify_action
        self._post_success = post_success or _persist_verified_action
        self._command_timeout_seconds = command_timeout_seconds
        self._detached_expiration_seconds = detached_expiration_seconds
        self._closed = False

    def has_active_operation(self) -> bool:
        return any(
            operation.state not in _TERMINAL_STATES or operation.finishing
            for operation in self._operations.values()
        )

    def supported_actions(self) -> tuple[SetupOperationAction, ...]:
        """Return actions whose fixed executables are available on this host."""
        if self._resolve_executable("tmux") is None:
            return ()
        supported = [
            action
            for action, (binary, _args, _label) in _FIXED_COMMANDS.items()
            if self._action_executable(action, binary) is not None
        ]
        if (
            self._resolve_executable("databricks") is not None
            and (
                self._resolve_executable("uvx") is not None
                or self._resolve_executable("ucode") is not None
            )
            and databricks_sdk_installed()
        ):
            supported.append(SetupOperationAction.DATABRICKS_CONFIGURE)
        return tuple(supported)

    def _action_executable(self, action: SetupOperationAction, binary: str) -> str | None:
        if action == SetupOperationAction.ANTIGRAVITY_LOGIN:
            return resolve_cli_binary(binary, which=self._resolve_executable)
        return self._resolve_executable(binary)

    async def start(
        self, request: SetupOperationRequest | Mapping[str, object]
    ) -> SetupOperationSnapshot:
        if not isinstance(request, SetupOperationRequest):
            request = SetupOperationRequest.from_dict(request)
        parameters = _validated_parameters(request)
        operation_id = uuid4().hex

        async with self._lock:
            if self._closed:
                raise SetupOperationError("unavailable", "Setup operations are shutting down.")
            self._prune_completed()
            if self.has_active_operation():
                raise SetupOperationError(
                    "conflict", "Another setup operation is already running."
                )
            if request.action == SetupOperationAction.ANTIGRAVITY_LOGIN:
                try:
                    connected = await self._check_verifier(request.action, parameters)
                except Exception:  # noqa: BLE001 - vendor status may include auth details
                    connected = False
                if connected:
                    operation = _Operation(operation_id, request.action, parameters, None)
                    self._operations[operation_id] = operation
                    try:
                        self._post_success(request.action, parameters)
                    except Exception:  # noqa: BLE001 - persistence errors may contain secrets
                        operation.state = SetupOperationState.FAILED
                        operation.error = (
                            "Setup was verified, but Omnigent could not save the connection."
                        )
                    else:
                        operation.state = SetupOperationState.SUCCEEDED
                        operation.already_connected = True
                    return operation.snapshot()
            plan = self._command_plan(request.action, parameters)
            terminal = self._terminal_factory(plan, operation_id)
            operation = _Operation(operation_id, request.action, parameters, terminal)
            self._operations[operation_id] = operation
            try:
                await terminal.launch()
            except Exception:  # noqa: BLE001 - expose only the fixed safe error below
                operation.state = SetupOperationState.FAILED
                operation.error = (
                    "Could not start the setup terminal. Ensure tmux is installed and retry."
                )
                await self._close_terminal(operation)
                return operation.snapshot()
            operation.state = SetupOperationState.RUNNING
            exited = asyncio.Event()
            terminal.start_idle_watcher(lambda: None, on_exit=exited.set)
            operation.monitor_task = asyncio.create_task(
                self._monitor(operation, exited), name=f"setup-operation-{operation_id}"
            )
            operation.expiration_task = asyncio.create_task(
                self._expire_when_detached(operation), name=f"setup-expiration-{operation_id}"
            )
            return operation.snapshot()

    async def get(self, operation_id: str) -> SetupOperationSnapshot:
        async with self._lock:
            return self._require(operation_id).snapshot()

    async def verify(self, operation_id: str) -> SetupOperationSnapshot:
        """Finish an interactive Antigravity login after its host status is ready."""
        async with self._lock:
            operation = self._require(operation_id)
            if operation.finishing:
                raise SetupOperationError("conflict", "The setup operation is finishing.")
            if operation.state in _TERMINAL_STATES:
                return operation.snapshot()
            if operation.action != SetupOperationAction.ANTIGRAVITY_LOGIN:
                raise SetupOperationError(
                    "conflict", "This setup operation cannot be verified here."
                )
            try:
                verified = await self._check_verifier(operation.action, operation.parameters)
            except Exception as exc:  # Vendor status may include auth details.
                raise SetupOperationError(
                    "verification_failed",
                    "Could not check the sign-in status. Retry verification.",
                ) from exc
            if not verified:
                raise SetupOperationError(
                    "conflict",
                    "Sign-in is not verified yet. Complete it in the browser and retry.",
                )
            operation.finishing = True
            tasks = self._cancel_background_tasks(operation)
            attachments = list(operation.attachments.values())
            operation.attachments.clear()
        closed = False
        close_error: Exception | None = None
        try:
            await _disconnect_attachments(attachments)
            await self._close_terminal(operation)
            closed = True
        except Exception as exc:  # noqa: BLE001 - cleanup errors may contain auth output
            close_error = exc
        finally:
            try:
                await _join_tasks(tasks, timeout_seconds=None)
            finally:
                async with self._lock:
                    if closed and operation.state == SetupOperationState.RUNNING:
                        try:
                            self._post_success(operation.action, operation.parameters)
                        except Exception:  # noqa: BLE001 - persistence may contain secrets
                            operation.state = SetupOperationState.FAILED
                            operation.error = (
                                "Setup was verified, but Omnigent could not save the connection."
                            )
                        else:
                            operation.state = SetupOperationState.SUCCEEDED
                    if operation.state != SetupOperationState.CANCELLED:
                        operation.finishing = False
                    if operation.state == SetupOperationState.RUNNING:
                        self._schedule_expiration_if_detached(operation)
                    snapshot = operation.snapshot()
        if close_error is not None:
            raise SetupOperationError(
                "verification_failed",
                "Sign-in verified, but the setup terminal could not be stopped. "
                "Retry Check connection or Cancel.",
            ) from close_error
        return snapshot

    async def _check_verifier(
        self, action: SetupOperationAction, parameters: Mapping[str, object]
    ) -> bool | None:
        verification = asyncio.create_task(asyncio.to_thread(self._verifier, action, parameters))
        try:
            return await asyncio.shield(verification)
        except asyncio.CancelledError:
            # The status probe can own a short-lived vendor process.
            with contextlib.suppress(Exception):
                await asyncio.shield(verification)
            raise

    async def cancel(self, operation_id: str) -> SetupOperationSnapshot:
        async with self._lock:
            operation = self._require(operation_id)
            if operation.finishing and operation.state == SetupOperationState.RUNNING:
                raise SetupOperationError("conflict", "The setup operation is finishing.")
            if operation.state in _TERMINAL_STATES:
                return operation.snapshot()
            operation.state = SetupOperationState.CANCELLED
            operation.error = "Setup operation was cancelled."
            operation.finishing = True
            tasks = self._cancel_background_tasks(operation)
            attachments = list(operation.attachments.values())
            operation.attachments.clear()
            snapshot = operation.snapshot()
        await _disconnect_attachments(attachments)
        await self._close_terminal(operation)
        await _join_tasks(tasks, timeout_seconds=None)
        async with self._lock:
            operation.finishing = False
        return snapshot

    async def attach(
        self,
        operation_id: str,
        attachment_id: str,
        send: TerminalEventSender,
    ) -> SetupOperationSnapshot:
        if not attachment_id or len(attachment_id) > 160:
            raise SetupOperationError("invalid_request", "Invalid setup attachment id.")
        async with self._lock:
            operation = self._require(operation_id)
            if operation.finishing:
                raise SetupOperationError("conflict", "The setup operation is finishing.")
            if operation.state != SetupOperationState.RUNNING:
                raise SetupOperationError("conflict", "The setup operation is no longer running.")
            if operation.attachments:
                raise SetupOperationError(
                    "conflict", "The setup operation already has an active attachment."
                )
            if operation.expiration_task is not None:
                operation.expiration_task.cancel()
                operation.expiration_task = None
            attachment = _TerminalAttachment(send)
            operation.attachments[attachment_id] = attachment
            attachment.task = asyncio.create_task(
                self._run_attachment(operation, attachment_id, attachment),
                name=f"setup-attachment-{attachment_id}",
            )
            return operation.snapshot()

    async def handle_terminal(
        self,
        operation_id: str,
        attachment_id: str,
        payload: Mapping[str, object],
    ) -> None:
        async with self._lock:
            operation = self._require(operation_id)
            attachment = operation.attachments.get(attachment_id)
            if attachment is None:
                raise SetupOperationError("not_found", "Setup attachment was not found.")
        await attachment.feed(payload)

    async def detach(self, operation_id: str, attachment_id: str) -> None:
        async with self._lock:
            operation = self._require(operation_id)
            attachment = operation.attachments.pop(attachment_id, None)
            if attachment is None:
                return
            self._schedule_expiration_if_detached(operation)
        await attachment.disconnect()
        if attachment.task is not None:
            await _join_tasks([attachment.task])

    async def shutdown(self) -> None:
        async with self._lock:
            self._closed = True
            active = [
                operation
                for operation in self._operations.values()
                if operation.state not in _TERMINAL_STATES or operation.finishing
            ]
            tasks: list[asyncio.Task[None]] = []
            attachments: list[_TerminalAttachment] = []
            for operation in active:
                if operation.state not in _TERMINAL_STATES:
                    operation.state = SetupOperationState.CANCELLED
                    operation.error = "Setup operation stopped with the host."
                operation.finishing = True
                tasks.extend(self._cancel_background_tasks(operation))
                attachments.extend(operation.attachments.values())
                operation.attachments.clear()
        await _disconnect_attachments(attachments)
        await asyncio.gather(
            *(self._close_terminal(operation) for operation in active),
            return_exceptions=True,
        )
        await _join_tasks(tasks, timeout_seconds=None)
        async with self._lock:
            for operation in active:
                operation.finishing = False

    def _command_plan(
        self, action: SetupOperationAction, parameters: Mapping[str, object]
    ) -> _CommandPlan:
        if self._resolve_executable("tmux") is None:
            raise SetupOperationError(
                "unavailable", "tmux is required for guided setup on this host."
            )
        if action == SetupOperationAction.DATABRICKS_CONFIGURE:
            if not databricks_sdk_installed():
                raise SetupOperationError(
                    "unavailable",
                    "Databricks setup requires the databricks extra on this host. "
                    f"Install it with: {DATABRICKS_EXTRA_INSTALL_HINT}",
                )
            if self._resolve_executable("databricks") is None:
                raise SetupOperationError(
                    "unavailable", "Databricks CLI is not installed on this host."
                )
            if (
                self._resolve_executable("uvx") is None
                and self._resolve_executable("ucode") is None
            ):
                raise SetupOperationError(
                    "unavailable", "ucode requires uvx or ucode on this host."
                )
            agents = parameters["agents"]
            assert isinstance(agents, tuple)
            return _CommandPlan(
                sys.executable,
                (
                    "-m",
                    "omnigent.host.setup_operations",
                    "run-databricks",
                    str(parameters["workspace_url"]),
                    ",".join(agents),
                ),
                "Databricks",
            )
        binary, args, label = _FIXED_COMMANDS[action]
        executable = self._action_executable(action, binary)
        if executable is None:
            raise SetupOperationError("unavailable", f"{label} is not installed on this host.")
        return _CommandPlan(executable, args, label)

    async def _monitor(self, operation: _Operation, exited: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(exited.wait(), timeout=self._command_timeout_seconds)
        except asyncio.TimeoutError:
            await self._finish(
                operation,
                SetupOperationState.FAILED,
                exit_code=None,
                error="Setup operation timed out.",
            )
            return
        except asyncio.CancelledError:
            return

        assert operation.terminal is not None
        exit_code = operation.terminal.last_exit_status()
        if exit_code != 0:
            await self._finish(
                operation,
                SetupOperationState.FAILED,
                exit_code=exit_code,
                error="The setup command did not complete successfully.",
            )
            return
        verification_task = asyncio.create_task(
            asyncio.to_thread(
                self._verifier,
                operation.action,
                operation.parameters,
            ),
            name=f"setup-verification-{operation.operation_id}",
        )
        try:
            verified = await asyncio.shield(verification_task)
        except asyncio.CancelledError:
            # The default verifier may own a short-lived CLI status process.
            # Let it finish after cancellation so manager cleanup does not
            # outlive the active-operation guard or leak an owned process.
            with contextlib.suppress(Exception):
                await asyncio.shield(verification_task)
            return
        except Exception:  # noqa: BLE001 - verifier errors may contain auth details
            await self._finish(
                operation,
                SetupOperationState.FAILED,
                exit_code=exit_code,
                error="The setup verification failed.",
            )
            return
        if verified is False:
            await self._finish(
                operation,
                SetupOperationState.FAILED,
                exit_code=exit_code,
                error="The command completed, but setup could not be verified.",
            )
            return
        await self._finish(
            operation,
            SetupOperationState.SUCCEEDED,
            exit_code=exit_code,
            persist_verified=bool(verified),
        )

    async def _finish(
        self,
        operation: _Operation,
        state: SetupOperationState,
        *,
        exit_code: int | None,
        error: str | None = None,
        persist_verified: bool = False,
    ) -> None:
        async with self._lock:
            if operation.state in _TERMINAL_STATES:
                return
            if persist_verified:
                try:
                    self._post_success(operation.action, operation.parameters)
                except Exception:  # noqa: BLE001 - persistence errors may contain secrets
                    state = SetupOperationState.FAILED
                    error = "Setup was verified, but Omnigent could not save the connection."
            operation.state = state
            operation.exit_code = exit_code
            operation.error = error
            operation.finishing = True
            if operation.expiration_task is not None:
                operation.expiration_task.cancel()
                operation.expiration_task = None
            attachments = list(operation.attachments.values())
            operation.attachments.clear()
        await _disconnect_attachments(attachments)
        await self._close_terminal(operation)
        async with self._lock:
            operation.finishing = False

    async def _run_attachment(
        self,
        operation: _Operation,
        attachment_id: str,
        attachment: _TerminalAttachment,
    ) -> None:
        assert operation.terminal is not None
        try:
            await bridge_tmux_control_to_websocket(
                attachment,  # type: ignore[arg-type]
                socket_path=str(operation.terminal.socket_path),
                tmux_target=operation.terminal.tmux_target,
                read_only=False,
                seed_scrollback=False,
                on_client_interaction=operation.terminal.note_client_interaction,
            )
        finally:
            async with self._lock:
                current = operation.attachments.get(attachment_id)
                if current is attachment:
                    del operation.attachments[attachment_id]
                    self._schedule_expiration_if_detached(operation)

    async def _expire_when_detached(self, operation: _Operation) -> None:
        try:
            await asyncio.sleep(self._detached_expiration_seconds)
            async with self._lock:
                if operation.state != SetupOperationState.RUNNING or operation.attachments:
                    return
                operation.state = SetupOperationState.EXPIRED
                operation.error = "Setup operation expired while detached."
                operation.finishing = True
                monitor = operation.monitor_task
                operation.monitor_task = None
            if monitor is not None:
                monitor.cancel()
            await self._close_terminal(operation)
            await _join_tasks([monitor] if monitor is not None else [], timeout_seconds=None)
            async with self._lock:
                operation.finishing = False
        except asyncio.CancelledError:
            return

    def _schedule_expiration_if_detached(self, operation: _Operation) -> None:
        if operation.state != SetupOperationState.RUNNING or operation.attachments:
            return
        if operation.expiration_task is None or operation.expiration_task.done():
            operation.expiration_task = asyncio.create_task(
                self._expire_when_detached(operation),
                name=f"setup-expiration-{operation.operation_id}",
            )

    def _cancel_background_tasks(self, operation: _Operation) -> list[asyncio.Task[None]]:
        tasks = [task for task in (operation.monitor_task, operation.expiration_task) if task]
        operation.monitor_task = None
        operation.expiration_task = None
        for task in tasks:
            task.cancel()
        return tasks

    async def _close_terminal(self, operation: _Operation) -> None:
        async with operation.close_lock:
            if operation.terminal_closed:
                return
            if operation.terminal is not None:
                await operation.terminal.close()
            operation.terminal_closed = True

    def _require(self, operation_id: str) -> _Operation:
        operation = self._operations.get(operation_id)
        if operation is None:
            raise SetupOperationError("not_found", "Setup operation was not found.")
        return operation

    def _prune_completed(self) -> None:
        removable = [
            operation_id
            for operation_id, operation in self._operations.items()
            if operation.state in _TERMINAL_STATES and not operation.finishing
        ]
        excess = len(self._operations) - _COMPLETED_OPERATION_RETENTION + 1
        for operation_id in removable[: max(0, excess)]:
            del self._operations[operation_id]


_FIXED_COMMANDS: dict[SetupOperationAction, tuple[str, tuple[str, ...], str]] = {
    SetupOperationAction.CLAUDE_LOGIN: (
        "claude",
        ("auth", "login", "--claudeai"),
        "Claude CLI",
    ),
    SetupOperationAction.CODEX_LOGIN: ("codex", ("login",), "Codex CLI"),
    SetupOperationAction.CURSOR_LOGIN: ("cursor-agent", ("login",), "Cursor CLI"),
    SetupOperationAction.CURSOR_LOGOUT: ("cursor-agent", ("logout",), "Cursor CLI"),
    SetupOperationAction.ANTIGRAVITY_LOGIN: ("agy", (), "Antigravity CLI"),
    SetupOperationAction.OPENCODE_LOGIN: ("opencode", ("auth", "login"), "OpenCode CLI"),
    SetupOperationAction.QWEN_CONFIGURE: ("qwen", (), "Qwen Code CLI"),
    SetupOperationAction.GOOSE_CONFIGURE: ("goose", ("configure",), "Goose CLI"),
    SetupOperationAction.HERMES_CONFIGURE: ("hermes", ("model",), "Hermes CLI"),
    SetupOperationAction.KIRO_LOGIN: ("kiro-cli", ("login",), "Kiro CLI"),
    SetupOperationAction.KIMI_LOGIN: ("kimi", ("login",), "Kimi CLI"),
}


def _validated_parameters(request: SetupOperationRequest) -> dict[str, object]:
    parameters = dict(request.parameters)
    if request.action != SetupOperationAction.DATABRICKS_CONFIGURE:
        if parameters:
            raise SetupOperationError(
                "invalid_request", "This setup action accepts no parameters."
            )
        return {}
    if set(parameters) != {"workspace_url", "agents"}:
        raise SetupOperationError(
            "invalid_request", "Databricks setup requires workspace_url and agents."
        )
    raw_url = parameters["workspace_url"]
    if not isinstance(raw_url, str) or not raw_url.strip() or len(raw_url) > 2048:
        raise SetupOperationError("invalid_request", "Invalid Databricks workspace URL.")
    from omnigent.onboarding.databricks_config import normalize_workspace_url

    workspace_url = normalize_workspace_url(raw_url)
    parsed = urlparse(workspace_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise SetupOperationError(
            "invalid_request", "Databricks workspace URL must be an HTTPS origin."
        )
    raw_agents = parameters["agents"]
    if (
        not isinstance(raw_agents, Sequence)
        or isinstance(raw_agents, (str, bytes))
        or tuple(raw_agents) not in _DATABRICKS_AGENT_CHOICES
    ):
        raise SetupOperationError("invalid_request", "Unsupported Databricks setup agent.")
    agents = tuple(raw_agents)
    return {"workspace_url": workspace_url, "agents": agents}


class _SetupTerminal(TerminalInstance):
    async def close(self) -> None:
        await super().close()
        # Setup panes may contain authentication output. Keep it only while the
        # operation is live; TerminalInstance normally retains this diagnostic.
        self._last_pane_snapshot = None


def _create_terminal(plan: _CommandPlan, operation_id: str) -> TerminalInstance:
    private_dir = Path(tempfile.mkdtemp(prefix="omnigent-terminal-"))
    private_dir.chmod(0o700)
    (private_dir / "owner.pid").write_text(str(os.getpid()), encoding="utf-8")
    return _SetupTerminal(
        name="setup",
        session_key=operation_id,
        socket_path=private_dir / "tmux.sock",
        private_dir=private_dir,
        command=plan.executable,
        args=list(plan.args),
        env={"SHELL": "/bin/sh"},
        env_unset=["BASH_ENV", "ENV"],
        scrollback=1000,
        keep_alive_after_exit=True,
    )


def _verify_action(
    action: SetupOperationAction,
    parameters: Mapping[str, object],
) -> bool | None:
    if action == SetupOperationAction.DATABRICKS_CONFIGURE:
        from omnigent.onboarding.ucode_setup import ucode_workspace_exists

        return ucode_workspace_exists(str(parameters["workspace_url"]))
    from omnigent.onboarding.harness_install import (
        CURSOR_KEY,
        READINESS_CLI_PROBE_TIMEOUT_S,
        harness_cli_logged_in,
        invalidate_harness_login_cache,
    )

    login_key = {
        SetupOperationAction.CLAUDE_LOGIN: ANTHROPIC_FAMILY,
        SetupOperationAction.CODEX_LOGIN: OPENAI_FAMILY,
        SetupOperationAction.CURSOR_LOGIN: CURSOR_KEY,
        SetupOperationAction.ANTIGRAVITY_LOGIN: GEMINI_FAMILY,
    }.get(action)
    if login_key is not None:
        if action == SetupOperationAction.ANTIGRAVITY_LOGIN:
            return harness_cli_logged_in(login_key, timeout=READINESS_CLI_PROBE_TIMEOUT_S)
        return harness_cli_logged_in(login_key)
    logout_key = {
        SetupOperationAction.CURSOR_LOGOUT: CURSOR_KEY,
    }.get(action)
    if logout_key is not None:
        invalidate_harness_login_cache(logout_key)
        return not harness_cli_logged_in(logout_key)
    if action == SetupOperationAction.OPENCODE_LOGIN:
        from omnigent.onboarding.opencode_auth import opencode_auth_summary

        return opencode_auth_summary().has_provider
    if action == SetupOperationAction.QWEN_CONFIGURE:
        from omnigent.onboarding.qwen_auth import qwen_auth_configured

        return qwen_auth_configured()
    if action == SetupOperationAction.GOOSE_CONFIGURE:
        from omnigent.onboarding.goose_auth import goose_config_summary

        return goose_config_summary().provider is not None
    if action == SetupOperationAction.HERMES_CONFIGURE:
        from omnigent.onboarding.hermes_auth import hermes_config_summary

        return hermes_config_summary().ready
    if action == SetupOperationAction.KIMI_LOGIN:
        from omnigent.onboarding.kimi_auth import kimi_auth_configured

        return kimi_auth_configured()
    # Kiro has no stable local status probe. Databricks checks ucode state here
    # and persists its provider only after that verification succeeds.
    return None


def _persist_verified_action(
    action: SetupOperationAction,
    parameters: Mapping[str, object],
) -> None:
    cli = {
        SetupOperationAction.CLAUDE_LOGIN: "claude",
        SetupOperationAction.CODEX_LOGIN: "codex",
    }.get(action)
    from omnigent.onboarding.setup_operations import (
        record_databricks_provider,
        record_subscription,
    )

    if cli is not None:
        record_subscription(cli)
        return
    if action != SetupOperationAction.DATABRICKS_CONFIGURE:
        return
    from omnigent.onboarding.setup import _existing_profile_hosts, _host_matches

    workspace_url = str(parameters["workspace_url"])
    profile = next(
        (
            name
            for name, host in _existing_profile_hosts().items()
            if _host_matches(host, workspace_url)
        ),
        None,
    )
    if profile is None:
        raise RuntimeError("Databricks profile was not created")
    agents = parameters["agents"]
    assert isinstance(agents, tuple)
    surface = None
    if len(agents) == 1:
        surface = {
            "claude": "anthropic",
            "codex": "openai",
            "opencode": "openai",
            "pi": "pi",
        }.get(agents[0])
    record_databricks_provider(profile, surface=surface)


async def _disconnect_attachments(attachments: Sequence[_TerminalAttachment]) -> None:
    await asyncio.gather(
        *(attachment.disconnect() for attachment in attachments), return_exceptions=True
    )
    tasks = [attachment.task for attachment in attachments if attachment.task is not None]
    await _join_tasks(tasks)


async def _join_tasks(
    tasks: Sequence[asyncio.Task[Any]],
    *,
    timeout_seconds: float | None = _ATTACH_DRAIN_SECONDS,
) -> None:
    if not tasks:
        return
    joined = asyncio.gather(*tasks, return_exceptions=True)
    if timeout_seconds is None:
        await joined
        return
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(joined, timeout_seconds)


def _run_databricks(workspace_url: str, agents_csv: str) -> int:
    """Run fixed Databricks login and ucode configuration, then verify state."""
    agents = tuple(agents_csv.split(","))
    if agents not in _DATABRICKS_AGENT_CHOICES:
        return 2
    try:
        from omnigent.onboarding.setup import login_databricks_workspace
        from omnigent.onboarding.ucode_setup import (
            configure_ucode_for_workspace,
            ucode_workspace_exists,
        )

        profile = login_databricks_workspace(workspace_url)
        configure_ucode_for_workspace(workspace_url, agents=agents)
        if not ucode_workspace_exists(workspace_url):
            return 1
        del profile
    except Exception as exc:  # noqa: BLE001 - auth errors must be replaced with safe text
        # Never echo arbitrary exception data from auth/config paths.
        print(
            "Databricks setup did not complete. Review the output above and retry.",
            file=sys.stderr,
        )
        del exc
        return 1
    return 0


def _main(argv: Sequence[str]) -> int:
    if len(argv) != 4 or argv[1] != "run-databricks":
        return 2
    request = SetupOperationRequest(
        SetupOperationAction.DATABRICKS_CONFIGURE,
        {"workspace_url": argv[2], "agents": argv[3].split(",")},
    )
    parameters = _validated_parameters(request)
    agents = parameters["agents"]
    assert isinstance(agents, tuple)
    return _run_databricks(str(parameters["workspace_url"]), ",".join(agents))


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
