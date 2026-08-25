"""Executor that bridges Omnigent messages into a native Codex TUI thread."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import cast

from omnigent.codex_native_app_server import (
    CodexAppServerClient,
    CodexAppServerResponseError,
    client_for_transport,
)
from omnigent.codex_native_bridge import (
    CODEX_NATIVE_BRIDGE_DIR_ENV_VAR,
    CODEX_NATIVE_REQUEST_SESSION_ID_ENV_VAR,
    CodexNativeBridgeState,
    cancel_pending_mcp_startup,
    clear_active_turn_id_if_matches,
    mcp_startup_waiting_detail,
    read_bridge_startup_error,
    read_bridge_state,
    read_mcp_startup,
    update_active_turn_id,
    write_codex_config_model,
)
from omnigent.inner.codex_goal_command import goal_objective_from_content
from omnigent.inner.executor import (
    EnqueuedContent,
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ToolSpec,
    TurnComplete,
)
from omnigent.inner.native_attachments import (
    materialize_attachment,
    parse_data_uri,
    unresolved_attachment_marker,
)
from omnigent.reasoning_effort import (
    CODEX_NATIVE_EFFORTS,
    effort_for_model_switch,
    validate_effort,
)

_logger = logging.getLogger(__name__)

_NO_ACTIVE_TURN_ERROR_CODE = -32600
_NO_ACTIVE_TURN_ERROR_MESSAGE = "no active turn to steer"


def _is_no_active_turn_to_steer(error: CodexAppServerResponseError) -> bool:
    """Return whether Codex explicitly rejected a steer because the turn ended."""
    return (
        error.code == _NO_ACTIVE_TURN_ERROR_CODE
        and error.message is not None
        and error.message.strip().casefold() == _NO_ACTIVE_TURN_ERROR_MESSAGE
    )


async def _start_codex_turn(
    client: CodexAppServerClient,
    *,
    bridge_dir: Path,
    state: CodexNativeBridgeState,
    input_items: list[dict[str, object]],
    settings_overrides: Mapping[str, object],
) -> None:
    """Apply optional settings and start one Codex turn on an idle thread."""
    if settings_overrides:
        await client.request(
            "thread/settings/update",
            {
                "threadId": state.thread_id,
                **settings_overrides,
            },
        )
        switched_model = settings_overrides.get("model")
        if isinstance(switched_model, str) and switched_model:
            if not write_codex_config_model(bridge_dir, switched_model):
                _logger.warning(
                    "Failed to mirror codex model switch into config.toml: model=%s",
                    switched_model,
                )
    response = await client.request(
        "turn/start",
        {
            "threadId": state.thread_id,
            "input": input_items,
            "environments": [
                {
                    "environmentId": "local",
                    "cwd": state.cwd or str(Path.cwd()),
                }
            ],
        },
    )
    result = _json_object(response.get("result"))
    turn = _json_object(result.get("turn")) if result is not None else None
    turn_id = turn.get("id") if turn is not None else None
    if isinstance(turn_id, str) and turn_id:
        update_active_turn_id(bridge_dir, turn_id)
        _logger.info("Codex native started turn: turn_id=%s", turn_id)


async def _steer_codex_turn(
    client: CodexAppServerClient,
    *,
    bridge_dir: Path,
    state: CodexNativeBridgeState,
    input_items: list[dict[str, object]],
) -> None:
    """Steer one bridge-recorded active Codex turn."""
    assert state.active_turn_id is not None
    response = await client.request(
        "turn/steer",
        {
            "threadId": state.thread_id,
            "expectedTurnId": state.active_turn_id,
            "input": input_items,
        },
    )
    result = _json_object(response.get("result"))
    turn_id = result.get("turnId") if result is not None else None
    if isinstance(turn_id, str) and turn_id:
        update_active_turn_id(bridge_dir, turn_id)
        _logger.info("Codex native steered active turn: turn_id=%s", turn_id)


async def _inject_codex_turn(
    client: CodexAppServerClient,
    *,
    bridge_dir: Path,
    state: CodexNativeBridgeState,
    input_items: list[dict[str, object]],
    settings_overrides: Mapping[str, object],
) -> None:
    """Steer an active turn or start one, recovering one proven stale steer."""
    if state.active_turn_id is None:
        await _start_codex_turn(
            client,
            bridge_dir=bridge_dir,
            state=state,
            input_items=input_items,
            settings_overrides=settings_overrides,
        )
        return

    expected_turn_id = state.active_turn_id
    try:
        await _steer_codex_turn(
            client,
            bridge_dir=bridge_dir,
            state=state,
            input_items=input_items,
        )
        return
    except CodexAppServerResponseError as error:
        if not _is_no_active_turn_to_steer(error):
            raise

    # Codex authoritatively says A ended. Clear A only if it is still the
    # bridge's value; a concurrent turn/started(B) must survive this recovery.
    clear_active_turn_id_if_matches(bridge_dir, expected_turn_id)
    recovered_state = read_bridge_state(bridge_dir)
    if recovered_state is None or recovered_state.session_id != state.session_id:
        raise RuntimeError("Codex native bridge changed while recovering a stale turn")
    if recovered_state.active_turn_id is not None:
        _logger.info(
            "Codex native stale steer raced with a newer turn; steering turn_id=%s",
            recovered_state.active_turn_id,
        )
        await _steer_codex_turn(
            client,
            bridge_dir=bridge_dir,
            state=recovered_state,
            input_items=input_items,
        )
        return
    _logger.info("Codex native reconciled completed stale turn: turn_id=%s", expected_turn_id)
    await _start_codex_turn(
        client,
        bridge_dir=bridge_dir,
        state=recovered_state,
        input_items=input_items,
        settings_overrides=settings_overrides,
    )


class CodexNativeExecutor(Executor):
    """
    Harness-side executor for ``omnigent codex`` web UI turns.

    :param bridge_dir: Optional bridge directory override. ``None``
        reads :data:`CODEX_NATIVE_BRIDGE_DIR_ENV_VAR`.
    """

    def __init__(self, bridge_dir: Path | None = None) -> None:
        self._bridge_dir = bridge_dir or _bridge_dir_from_env()
        self._request_session_id = _request_session_id_from_env()
        # Serializes injection into the shared native Codex thread.
        # ``run_turn`` (initiating message) and ``enqueue_session_message``
        # (mid-turn steering) run concurrently against this one cached
        # instance. Each reads the active turn id, decides
        # ``turn/start`` vs ``turn/steer``, makes the RPC, then writes
        # the new turn id back. Without this lock two concurrent
        # injections race that read-decide-write — both can see "no
        # active turn" and double-start, or clobber ``active_turn_id``.
        # See designs/NATIVE_INJECTION_SERIALIZATION.md. Relies on the
        # adapter caching one executor per conversation.
        self._inject_lock = asyncio.Lock()

    def supports_streaming(self) -> bool:
        """:returns: ``False`` because output is emitted by the native forwarder."""
        return False

    def supports_live_message_queue(self) -> bool:
        """:returns: ``True`` because active turns accept ``turn/steer``."""
        return True

    async def enqueue_session_message(self, session_key: str, content: EnqueuedContent) -> bool:
        """
        Steer an active native Codex turn.

        :param session_key: Adapter session key. The native bridge is
            per conversation, so this value is only used for API parity.
        :param content: User-supplied content, usually a string.
        :returns: ``True`` when Codex accepted the steering message.
        """
        del session_key
        input_items = _content_to_input_items(content, self._bridge_dir)
        if not input_items:
            return False
        # Serialized against run_turn so the read-decide-RPC-write below
        # is atomic with respect to the initiating-message injection.
        async with self._inject_lock:
            state = read_bridge_state(self._bridge_dir)
            if state is None or not _session_is_active(state.session_id, self._request_session_id):
                _logger.info("Codex native injection skipped: bridge state missing or inactive")
                return False
            if state.active_turn_id is None:
                _logger.info("Codex native injection skipped: no active turn")
                return False
            client = client_for_transport(
                state.socket_path,
                client_name="omnigent-codex-native",
            )
            await client.connect()
            try:
                await _inject_codex_turn(
                    client,
                    bridge_dir=self._bridge_dir,
                    state=state,
                    input_items=input_items,
                    settings_overrides={},
                )
            except Exception:  # noqa: BLE001 - steering is best-effort from the runner facade.
                _logger.warning("Codex native turn/steer failed", exc_info=True)
                return False
            finally:
                await client.close()
            return True

    async def interrupt_session(self, session_key: str) -> bool:
        """
        Interrupt the active native Codex turn and any in-flight MCP startup.

        Stop means "stop everything": the active turn (which codex may be
        holding back until MCP startup settles) is interrupted with its
        recorded turn id, and a still-pending MCP startup round is
        cancelled the way the Codex TUI does — ``turn/interrupt`` with an
        empty turn id (its ``startup_interrupt``). Either alone also works:
        no recorded turn cancels just the startup; no pending startup
        interrupts just the turn.

        :param session_key: Adapter session key. Unused because the
            bridge is per conversation.
        :returns: ``True`` when an interrupt or a startup cancel was sent.
        """
        del session_key
        state = read_bridge_state(self._bridge_dir)
        if state is None:
            return False
        # Flip the local map first: the cancelled record is what the web
        # band and turn-error text read, even if Codex never acknowledges.
        # Unlike the runner's Stop handler, the flipped map is not
        # published here — the inner process has no server client; web
        # Stop routes through the runner handler, which does publish.
        pending = cancel_pending_mcp_startup(self._bridge_dir)
        if state.active_turn_id is None and not pending:
            return False
        client = client_for_transport(
            state.socket_path,
            client_name="omnigent-codex-native",
        )
        await client.connect()
        try:
            if pending:
                # Startup interrupt first and best-effort: the local
                # cancel above already updated what Omnigent shows, and a
                # failure here must not block the active-turn interrupt.
                try:
                    await client.request(
                        "turn/interrupt",
                        {"threadId": state.thread_id, "turnId": ""},
                    )
                except Exception:  # noqa: BLE001 - the local cancel above already took effect.
                    _logger.warning("Codex native MCP startup interrupt failed", exc_info=True)
                _logger.info("Codex native MCP startup cancelled: %s", ", ".join(pending))
            if state.active_turn_id is not None:
                await client.request(
                    "turn/interrupt",
                    {
                        "threadId": state.thread_id,
                        "turnId": state.active_turn_id,
                    },
                )
        finally:
            await client.close()
        return True

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """
        Send the latest user message to the native Codex app-server.

        :param messages: Conversation history in executor message
            shape. The latest user message is delivered to Codex.
        :param tools: Tool schemas from Omnigent. Ignored here;
            native Codex owns its own tool surface.
        :param system_prompt: System prompt from the agent spec. Native
            startup instructions are configured before the app-server launches.
        :param config: Per-turn executor config. Its ``model`` and
            ``extra["reasoning_effort"]`` (carrying the Omnigent web
            ``/model`` pick) are applied via a ``thread/settings/update``
            request ahead of ``turn/start``; everything else is ignored
            by this bridge.
        :returns: Async iterator yielding one terminal event.
        """
        del tools, system_prompt
        settings_overrides = _model_effort_overrides(config)
        latest_user_content = _latest_user_content(messages)
        goal_objective = goal_objective_from_content(latest_user_content)
        input_items: list[dict[str, object]] = (
            [{"type": "text", "text": goal_objective}]
            if goal_objective is not None
            else _content_to_input_items(latest_user_content, self._bridge_dir)
        )
        if not input_items:
            yield ExecutorError(message="Codex native turn had no user input to send")
            return
        # Wait for the bridge to boot OUTSIDE the injection lock: this is a
        # one-time poll for the state file to appear (first turn, app-server
        # starting), with no shared-state mutation, so holding the lock
        # across its up-to-60s wait would needlessly block concurrent
        # steering (enqueue_session_message). Once the state exists, the
        # decision/RPC/write below runs under the lock — re-reading state so
        # it's atomic with respect to a steer that landed during the wait.
        state = read_bridge_state(self._bridge_dir)
        if state is None:
            for _ in range(60):
                # Startup already failed; the runner recorded the cause — stop waiting.
                if read_bridge_startup_error(self._bridge_dir) is not None:
                    break
                await asyncio.sleep(1.0)
                state = read_bridge_state(self._bridge_dir)
                if state is not None:
                    break

        # No client-side wait for Codex MCP startup: the app-server accepts
        # ``turn/start`` mid-startup and defers execution until the round
        # settles (verified against codex 0.142.5), so sending immediately
        # is safe. The web UI's MCP-startup band explains the wait.

        # Serialized against enqueue_session_message: the
        # turn/start-vs-turn/steer decision, the RPC, and the
        # active_turn_id write must be atomic with respect to mid-turn
        # steering. The terminal event is yielded after the lock releases.
        error_msg: str | None = None
        async with self._inject_lock:
            state = read_bridge_state(self._bridge_dir)
            if state is None:
                startup_error = read_bridge_startup_error(self._bridge_dir)
                error_msg = (
                    f"Codex native thread never started: {startup_error}"
                    if startup_error
                    else "Codex native bridge state is missing"
                )
            elif not _session_is_active(state.session_id, self._request_session_id):
                error_msg = "Codex native session is no longer active"
            else:
                client = client_for_transport(
                    state.socket_path,
                    client_name="omnigent-codex-native",
                )
                await client.connect()
                try:
                    if goal_objective is not None:
                        await client.request(
                            "thread/goal/set",
                            {
                                "threadId": state.thread_id,
                                "objective": goal_objective,
                            },
                        )
                    await _inject_codex_turn(
                        client,
                        bridge_dir=self._bridge_dir,
                        state=state,
                        input_items=input_items,
                        settings_overrides=settings_overrides,
                    )
                except Exception as exc:  # noqa: BLE001 - converted into a harness error event.
                    error_msg = f"Codex native executor error: {exc}"
                    # Name the servers a still-unsettled MCP startup is
                    # blocked on — the most common cause of an injection
                    # failure this early in the session's life.
                    waiting = mcp_startup_waiting_detail(read_mcp_startup(self._bridge_dir))
                    if waiting:
                        error_msg = f"{error_msg} ({waiting})"
                finally:
                    await client.close()
        if error_msg is not None:
            yield ExecutorError(message=error_msg)
        else:
            yield TurnComplete(response=None)


def _model_effort_overrides(config: ExecutorConfig | None) -> dict[str, object]:
    """
    Build Codex ``thread/settings/update`` model / reasoning-effort overrides.

    A model or reasoning-effort change selected in the Omnigent web UI is
    applied to the running native thread via a ``thread/settings/update``
    request (whose ``ThreadSettingsUpdateParams`` carries ``model`` and
    ``effort``); the change persists to this and later turns. ``turn/start``
    itself takes no model/effort — its params are input/context only — which
    is why the picker was previously a no-op. The runner threads the web
    ``/model`` pick into ``config.model`` and the effort into
    ``config.extra["reasoning_effort"]`` (see
    :class:`~omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter`).
    When neither is pinned the override dict is empty and the native
    thread keeps its launch-pinned model — so this is a no-op for
    sessions that never touch the web picker.

    :param config: Per-turn executor config, or ``None``.
    :returns: Override dict for ``thread/settings/update`` params, e.g.
        ``{"model": "gpt-5.3-codex", "effort": "high"}``. Empty when
        nothing is pinned.
    """
    if config is None:
        return {}
    overrides: dict[str, object] = {}
    model = config.model
    if isinstance(model, str) and model:
        overrides["model"] = model
    raw_effort = config.extra.get("reasoning_effort")
    try:
        effort = validate_effort(raw_effort, "codex", CODEX_NATIVE_EFFORTS)
    except ValueError:
        # A bad effort must not sink the turn — drop it and keep Codex's
        # current effort rather than failing the whole dispatch.
        _logger.warning("Ignoring unsupported codex reasoning effort: %r", raw_effort)
        effort = None
    model_str = model if isinstance(model, str) and model else None
    # A model switch inherits config.toml's effort (the user's xhigh default),
    # which the switched-to model may reject (GLM has no xhigh). Guard the live
    # turn: clamp an explicit effort, and when none was requested but the model
    # caps below the codex default, send that ceiling so the turn does not 400.
    effort = effort_for_model_switch(effort, model_str)
    if effort:
        overrides["effort"] = effort
    return overrides


def _bridge_dir_from_env() -> Path:
    """
    Resolve the native Codex bridge directory from harness spawn env.

    :returns: Bridge directory path.
    :raises RuntimeError: If the env var is missing.
    """
    raw = os.environ.get(CODEX_NATIVE_BRIDGE_DIR_ENV_VAR, "").strip()
    if not raw:
        raise RuntimeError(f"{CODEX_NATIVE_BRIDGE_DIR_ENV_VAR} is required")
    return Path(raw)


def _request_session_id_from_env() -> str | None:
    """
    Resolve the Omnigent session id that requested this harness process.

    :returns: Omnigent session id, e.g. ``"conv_abc123"``, or ``None``.
    """
    raw = os.environ.get(CODEX_NATIVE_REQUEST_SESSION_ID_ENV_VAR, "").strip()
    return raw or None


def _session_is_active(session_id: str, request_session_id: str | None) -> bool:
    """
    Return whether this harness may inject into the native thread.

    :param session_id: Session id from bridge state.
    :param request_session_id: Session id from harness spawn env.
    :returns: ``True`` when injection is allowed.
    """
    return request_session_id is None or request_session_id == session_id


def _latest_user_content(messages: list[Message]) -> object:
    """
    Return the latest user message content.

    :param messages: Executor message list.
    :returns: The latest user content, or ``None`` when absent.
    """
    for message in reversed(messages):
        if message.get("role") == "user":
            return message.get("content")
    return None


def _content_to_input_items(content: object, bridge_dir: Path) -> list[dict[str, object]]:
    """
    Normalize executor content into Codex app-server input items.

    Text becomes ``{"type": "text", "text": ...}``. Images are
    materialized to disk and referenced as
    ``{"type": "localImage", "path": ...}`` — sending the base64 data
    URI inline as text would blow past the app-server's 1 MiB input
    limit. Files inline their decoded text when they are textual;
    binary files are materialized and referenced by path in a text
    item so the model can open them with its tools.

    :param content: Message content, e.g. a string or a list of content
        blocks like ``{"type": "input_text", "text": "..."}`` and
        ``{"type": "input_image", "image_url": "data:image/png;base64,..."}``.
    :param bridge_dir: Bridge directory for materializing attachments.
    :returns: Codex input item dicts.
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        items: list[dict[str, object]] = []
        for raw_block in content:
            block = _json_object(raw_block)
            if block is None:
                continue
            block_type = block.get("type")
            if block_type in {"input_text", "text"}:
                text = block.get("text")
                if isinstance(text, str) and text:
                    items.append({"type": "text", "text": text})
            elif block_type == "input_image":
                path = materialize_attachment(block, bridge_dir)
                if path is not None:
                    items.append({"type": "localImage", "path": str(path)})
                else:
                    items.append({"type": "text", "text": unresolved_attachment_marker(block)})
            elif block_type == "input_file":
                file_item = _file_block_to_input_item(block, bridge_dir)
                if file_item is not None:
                    items.append(file_item)
        return items
    if content is None:
        return []
    return [{"type": "text", "text": json.dumps(content, ensure_ascii=True)}]


def _file_block_to_input_item(
    block: Mapping[str, object],
    bridge_dir: Path,
) -> dict[str, object] | None:
    """
    Convert an ``input_file`` block into a Codex input item.

    The Codex app-server has no native file input item, so a textual
    file (``text/*``) is inlined as a ``text`` item. A binary file is
    materialized to disk and referenced by path in a ``text`` item so
    the model can open it with its tools. This keeps multi-megabyte
    base64 payloads out of the turn's text input.

    :param block: An ``input_file`` content block, expected to carry a
        ``file_data`` data URI, e.g.
        ``"data:text/plain;base64,aGVsbG8="``.
    :param bridge_dir: Bridge directory for materializing the file.
    :returns: A Codex ``text`` input item; a visible could-not-load
        marker item when the file failed to materialize; or ``None``
        for an empty text file.
    """
    file_data = block.get("file_data")
    if isinstance(file_data, str) and file_data.startswith("data:"):
        try:
            parsed = parse_data_uri(file_data)
            if parsed.mime_type.startswith("text/"):
                text = base64.b64decode(parsed.base64_payload).decode("utf-8", errors="replace")
                return {"type": "text", "text": text} if text else None
        except (ValueError, binascii.Error):
            _logger.warning("Failed to decode input_file data URI", exc_info=True)
    path = materialize_attachment(block, bridge_dir)
    if path is not None:
        # Marker format is load-bearing: codex echoes this text item back
        # in the mirrored user message, and title seeding strips lines
        # matching _ATTACHMENT_MARKER_RE in
        # omnigent/entities/conversation.py. Keep in sync.
        return {"type": "text", "text": f"[Attached file: {path}]"}
    return {"type": "text", "text": unresolved_attachment_marker(block)}


def _json_object(value: object) -> dict[str, object] | None:
    """Return a string-keyed JSON object, or ``None`` for other shapes."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return None
    return cast("dict[str, object]", value)
