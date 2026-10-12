"""Per-harness handlers for settings, compaction, and cost-popup events on native sessions.

The runner's ``/events`` route dispatches ``model_change``, ``effort_change``,
``compact`` and similar control events here for native (TUI-backed) harnesses.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
import urllib.parse
import weakref
from collections.abc import Callable, Coroutine, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from omnigent.harnesses.claude_native.main import ClaudeNativeUcodeConfig
    from omnigent.harnesses.codex_native.bridge import CodexNativeBridgeState

import httpx
from fastapi.responses import JSONResponse, Response

from omnigent.errors import OmnigentError
from omnigent.harness_aliases import native_terminal_name
from omnigent.runner.app_support import (
    _client_safe_error_detail,
    _SpecEntry,
)
from omnigent.runner.native import (
    _AUTO_OPENCODE_SERVERS,
    _SESSION_METADATA_PARAMS,
    _auto_create_opencode_terminal,
    _claude_native_bridge_id_for_session,
    _codex_native_model_from_spec,
    _CodexNativeModelOptionsNotReady,
    _resolve_opencode_compact_model,
)
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.spec.types import AgentSpec
from omnigent.util.json_types import JsonObject as _JsonObject

if TYPE_CHECKING:
    pass

_logger = logging.getLogger("omnigent.runner.app")


# After a stale-pane recreate the TUI reboots with ``--resume``. Poll until
# the input box is usable before typing into it. Only a recreate waits: a live
# pane is left to ``inject_slash_command``'s own composer reclaim. Same pacing
# as the other pane polls above (one tmux capture each). Module-level so tests
# can tighten the budget.
_CLAUDE_PANE_READY_TIMEOUT_S = 30.0


_CLAUDE_PANE_READY_POLL_S = 0.25


# Claude-native model switch confirmation: how long to watch the pane's
# statusLine snapshot for the switched model after typing ``/model``, and how
# often to re-read it. Module-level so tests can patch the pacing.
_CLAUDE_MODEL_CONFIRM_TIMEOUT_S = 10.0


_CLAUDE_MODEL_CONFIRM_POLL_S = 0.25


# How long the detached watcher keeps answering a /model confirm dialog that
# pops after the active turn settles (a mid-turn switch queues in the
# composer), and how often it looks. Long turns are common; the watch is
# cheap (one tmux capture per poll) and never types blind.
_CLAUDE_MODEL_LATE_DIALOG_BUDGET_S = 1200.0


_CLAUDE_MODEL_LATE_DIALOG_POLL_S = 2.0


# Settle delay between keystrokes when driving Codex TUI popups. The
# slash-command menu, the /permissions popup, and the Full Access confirm
# sub-dialog are each drawn asynchronously; without a pause the next key races
# ahead (e.g. Enter arrives before the menu commits, so the command never
# submits).
_CODEX_POPUP_RENDER_S = 0.7


# Budget for confirming an approval switch actually landed. Codex echoes
# "Permissions updated to <label>" once the popup applies; we poll the pane for
# it so a keystroke that didn't apply (e.g. a slow-rendering popup) fails loud
# instead of the label claiming a mode the TUI never entered.
_CODEX_PERMISSION_CONFIRM_BUDGET_S = 4.0


# Budget for the /permissions popup to render its option rows before we read
# them to find the target preset's menu digit. The popup can be slow to draw
# mid-session (a busy TUI, MCP still starting), so give it room; if it still
# can't be read we fall back to the preset's conventional menu position rather
# than failing the switch. Which rows the popup lists is codex-config-dependent
# (Read Only appears only with a read-only permission profile), so the digit is
# read from the live popup when possible rather than hardcoded.
_CODEX_PERMISSION_MENU_BUDGET_S = 5.0
# Older servers read any reply as a confirmed result, so outlast their
# 20-second forward instead of reporting an unconfirmed timeout.
_LEGACY_SERVER_UPDATE_TIMEOUT_S = 30.0


class _CodexNativeBridgeStateForSessionFn(Protocol):
    async def __call__(
        self, conv_id: str, *, action: str, missing_state_log_level: int = logging.WARNING
    ) -> CodexNativeBridgeState | None: ...


class _EnsureCommentRelayStartedFn(Protocol):
    async def __call__(
        self,
        session_id: str,
        *,
        bridge_id: str | None = None,
        explicit_bridge_dir: Path | None = None,
        await_notify: bool = False,
        session_labels: Mapping[str, str] | None = None,
    ) -> None: ...


class _LoadHistoryAsInputFn(Protocol):
    async def __call__(
        self, session_id: str, drop_item_id: str | None = None
    ) -> list[_JsonObject]: ...


class _PublishTurnStatusFn(Protocol):
    def __call__(
        self,
        conv_id: str,
        status: str,
        error: Mapping[str, object] | None = None,
        *,
        source_error: Mapping[str, object] | None = None,
        response_id: str | None = None,
    ) -> None: ...


class _HandleClaudeNativeCostPopupFn(Protocol):
    async def __call__(
        self, conv_id: str, elicitation_id: str, message: str, policy_name: str | None = None
    ) -> Response: ...


class _HandleCodexNativeCostPopupFn(Protocol):
    async def __call__(
        self, conv_id: str, elicitation_id: str, message: str, policy_name: str | None = None
    ) -> Response: ...


class _HandleCodexNativePlanModeChangeFn(Protocol):
    async def __call__(self, conv_id: str, *, enabled: bool) -> Response: ...


class _HandleCodexNativeSettingsUpdateFn(Protocol):
    async def __call__(
        self, conv_id: str, settings: _JsonObject, *, legacy_server: bool = False
    ) -> Response: ...


class _HandleOpencodeNativeBlockedNoticeFn(Protocol):
    async def __call__(
        self, conv_id: str, message: str, policy_name: str | None = None
    ) -> Response: ...


class _HandleOpencodeNativeCostPopupFn(Protocol):
    async def __call__(
        self, conv_id: str, elicitation_id: str, message: str, policy_name: str | None = None
    ) -> Response: ...


@dataclasses.dataclass(frozen=True)
class NativeControls:
    """Native-harness control handlers the runner app's event dispatch calls."""

    codex_native_model_options: Callable[[str], Coroutine[Any, Any, list[_JsonObject]]]
    handle_claude_native_btw_dismiss: Callable[[str], Coroutine[Any, Any, Response]]
    handle_claude_native_compact: Callable[[str], Coroutine[Any, Any, Response]]
    handle_claude_native_cost_popup: _HandleClaudeNativeCostPopupFn
    handle_claude_native_effort_change: Callable[[str, str | None], Coroutine[Any, Any, Response]]
    handle_claude_native_model_change: Callable[[str, str | None], Coroutine[Any, Any, Response]]
    handle_claude_native_permission_mode_change: Callable[
        [str, str | None], Coroutine[Any, Any, Response]
    ]
    handle_claude_sdk_compact: Callable[[str], Coroutine[Any, Any, Response]]
    handle_codex_native_approval_mode_change: Callable[[str, str], Coroutine[Any, Any, Response]]
    handle_codex_native_compact: Callable[[str], Coroutine[Any, Any, Response]]
    handle_codex_native_cost_popup: _HandleCodexNativeCostPopupFn
    handle_codex_native_plan_mode_change: _HandleCodexNativePlanModeChangeFn
    handle_codex_native_settings_update: _HandleCodexNativeSettingsUpdateFn
    handle_cursor_native_compact: Callable[[str], Coroutine[Any, Any, Response]]
    handle_cursor_native_model_change: Callable[[str, str | None], Coroutine[Any, Any, Response]]
    handle_devin_native_compact: Callable[[str], Coroutine[Any, Any, Response]]
    handle_devin_native_effort_change: Callable[[str, str | None], Coroutine[Any, Any, Response]]
    handle_devin_native_model_change: Callable[[str, str | None], Coroutine[Any, Any, Response]]
    handle_devin_native_permission_mode_change: Callable[
        [str, str | None], Coroutine[Any, Any, Response]
    ]
    handle_hermes_native_compact: Callable[[str], Coroutine[Any, Any, Response]]
    handle_kiro_native_model_change: Callable[[str, str | None], Coroutine[Any, Any, Response]]
    handle_opencode_native_blocked_notice: _HandleOpencodeNativeBlockedNoticeFn
    handle_opencode_native_clear: Callable[[str], Coroutine[Any, Any, Response]]
    handle_opencode_native_compact: Callable[[str], Coroutine[Any, Any, Response]]
    handle_opencode_native_cost_popup: _HandleOpencodeNativeCostPopupFn
    handle_opencode_native_model_change: Callable[[str, str | None], Coroutine[Any, Any, Response]]
    handle_pi_native_compact: Callable[[str], Coroutine[Any, Any, Response]]
    handle_pi_native_effort_change: Callable[[str, str | None], Coroutine[Any, Any, Response]]
    handle_pi_native_model_change: Callable[[str, str | None], Coroutine[Any, Any, Response]]
    handle_qwen_native_compact: Callable[[str], Coroutine[Any, Any, Response]]
    is_sdk_compact_body: Callable[[dict[str, Any]], bool]
    opencode_native_model_options: Callable[[str], Coroutine[Any, Any, list[_JsonObject]]]
    teardown_session_terminals: Callable[[str], Coroutine[Any, Any, None]]


def build_native_controls(
    *,
    _active_turns: dict[str, asyncio.Task[None] | None],
    _background_tasks: set[asyncio.Task[Any]],
    _begin_turn_slot: Callable[[str], None],
    _claude_model_options_rows: dict[str, tuple[float, list[dict[str, object]]]],
    _codex_native_bridge_state_for_session: _CodexNativeBridgeStateForSessionFn,
    _ensure_comment_relay_started: _EnsureCommentRelayStartedFn,
    _ensure_native_terminal_for_turn: Callable[[str, str | None], Coroutine[Any, Any, None]],
    _fetch_session_model_override: Callable[[str], Coroutine[Any, Any, str | None]],
    _ingest_cond: dict[str, asyncio.Condition],
    _ingest_next_seq: dict[str, int],
    _ingest_now_serving: dict[str, int],
    _load_history_as_input: _LoadHistoryAsInputFn,
    _model_dialog_watchers: set[asyncio.Task[None]],
    _native_cost_popup_config_file: Callable[[str, str], Coroutine[Any, Any, Path]],
    _native_pane_status: dict[str, str],
    _publish_event: Callable[[str, Mapping[str, object]], None],
    _publish_turn_status: _PublishTurnStatusFn,
    _resolve_session_agent_spec: Callable[[str], Coroutine[Any, Any, AgentSpec | None]],
    _resolve_session_claude_launch_config: Callable[
        [str], Coroutine[Any, Any, ClaudeNativeUcodeConfig | None]
    ],
    _run_turn_bg: Callable[[_JsonObject, str], Coroutine[Any, Any, None]],
    _sdk_compact_inprogress: set[str],
    _session_cursor_model_names: dict[str, dict[str, str]],
    _session_harness_name: Callable[[str], str | None],
    _session_histories: dict[str, list[_JsonObject]],
    _session_message_buffers: dict[str, list[dict[str, Any]]],
    _session_reasoning_effort: dict[str, str],
    _session_spec_cache: dict[str, _SpecEntry | None],
    resource_registry: SessionResourceRegistry,
    server_client: httpx.AsyncClient,
) -> NativeControls:
    """Build the native-harness control handlers over the runner app's session state.

    The keyword arguments are the runner app's shared session state and helpers.
    """
    # Weak values drop idle sessions, so a holder must keep its lock in a local.
    _codex_settings_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
        weakref.WeakValueDictionary()
    )

    async def _handle_codex_native_settings_update(
        conv_id: str,
        settings: _JsonObject,
        *,
        legacy_server: bool = False,
    ) -> Response:
        if not settings:
            return Response(status_code=204)
        lock = _codex_settings_locks.get(conv_id)
        if lock is None:
            lock = _codex_settings_locks[conv_id] = asyncio.Lock()
        # The lock also covers the public mirror so the server sees efforts in apply order.
        async with lock:
            response, resolved = await _apply_codex_native_settings_update(
                conv_id, settings, legacy_server=legacy_server
            )
            if "effort" in settings and (
                response.status_code == 504 or (legacy_server and response.status_code == 503)
            ):
                # The server keeps this selection, so the next turn applies it; a
                # resolved Default must stay explicit, as Codex reads null as unchanged.
                effort = resolved.get("effort", settings["effort"])
                if isinstance(effort, str) and effort:
                    _session_reasoning_effort[conv_id] = effort
                else:
                    _session_reasoning_effort.pop(conv_id, None)
            return response

    def _unmirrored_codex_settings(bridge_dir: Path) -> dict[str, str]:
        """Return applied settings the config still lacks, retrying their writes."""
        from omnigent.harnesses.codex_native.bridge import (
            mirror_applied_codex_settings,
            read_unmirrored_codex_settings,
        )

        # Any later rewrite, such as a terminal switch, makes the config current.
        pending = read_unmirrored_codex_settings(bridge_dir)
        if pending:
            for key in mirror_applied_codex_settings(bridge_dir, pending):
                _logger.warning("Could not mirror pending Codex %s in %s", key, bridge_dir)
        return pending

    async def _apply_codex_native_settings_update(
        conv_id: str,
        settings: _JsonObject,
        *,
        legacy_server: bool,
    ) -> tuple[Response, _JsonObject]:
        """Apply *settings* and return the response with the settings as resolved."""
        from omnigent.harnesses.codex_native.app_server import (
            client_for_transport,
            resolve_codex_effort_for_model,
        )
        from omnigent.harnesses.codex_native.bridge import (
            bridge_dir_for_codex_home,
            mirror_applied_codex_settings,
            read_codex_config_effort,
            read_codex_config_model,
        )
        from omnigent.runner.turn_routing import SETTINGS_UPDATE_TIMEOUT_S
        from omnigent.util.reasoning_effort import effort_for_model_switch

        state = await _codex_native_bridge_state_for_session(conv_id, action="settings update")
        if state is None:
            # No loaded Codex bridge means nothing applied the settings; a
            # silent 204 here would let the caller claim a switch the
            # app-server never saw.
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_settings_update_failed",
                    "detail": "Codex-native settings update requires a loaded Codex bridge.",
                },
            ), settings

        bridge_dir = bridge_dir_for_codex_home(Path(state.codex_home))
        settings = dict(settings)
        model = None
        unmirrored: dict[str, str] = {}
        if "model" in settings or "effort" in settings:
            # A failed config write must not make the stale value the next update's base.
            # The record takes a cross-process file lock, so keep it off the event loop.
            unmirrored = await asyncio.to_thread(_unmirrored_codex_settings, bridge_dir)
            model = (
                settings.get("model")
                or unmirrored.get("model")
                or read_codex_config_model(bridge_dir)
            )
        if "effort" in settings and settings["effort"] is None and not isinstance(model, str):
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_input",
                    "detail": "Codex effort reset requires a current model",
                },
            ), settings
        if isinstance(settings.get("effort"), str) and not isinstance(model, str):
            _logger.warning(
                "Codex-native effort change without a known model skips validation for session=%s",
                conv_id,
                extra={"session_id": conv_id},
            )
        codex_client = client_for_transport(
            state.socket_path,
            client_name="omnigent-codex-native-runner",
        )
        try:
            # Bounded so a hung app-server cannot hold the settings lock indefinitely.
            await asyncio.wait_for(codex_client.connect(), timeout=SETTINGS_UPDATE_TIMEOUT_S)
            if "model" in settings or "effort" in settings:
                # Only an absent key inherits config; null selects the model's default.
                effort = (
                    settings["effort"]
                    if "effort" in settings
                    else unmirrored.get("effort") or read_codex_config_effort(bridge_dir)
                )
                if (
                    "model" in settings
                    and "effort" not in settings
                    and isinstance(model, str)
                    and effort is None
                ):
                    effort = effort_for_model_switch(None, model)
                    if effort is not None:
                        settings["effort"] = effort
                if isinstance(model, str) and (
                    isinstance(effort, str) or ("effort" in settings and effort is None)
                ):
                    resolved = await resolve_codex_effort_for_model(
                        codex_client, effort, model, transport=state.socket_path
                    )
                    settings["effort"] = resolved
            try:
                await asyncio.wait_for(
                    codex_client.request(
                        "thread/settings/update",
                        {
                            "threadId": state.thread_id,
                            **settings,
                        },
                    ),
                    timeout=(
                        _LEGACY_SERVER_UPDATE_TIMEOUT_S
                        if legacy_server
                        else SETTINGS_UPDATE_TIMEOUT_S
                    ),
                )
            except TimeoutError:
                # Codex may still apply the update, so this is not a refusal.
                _logger.warning(
                    "Codex-native thread/settings/update timed out for session=%s",
                    conv_id,
                    extra={"session_id": conv_id},
                )
                return JSONResponse(
                    status_code=504,
                    content={
                        "error": "codex_native_settings_update_timeout",
                        "detail": "Codex did not confirm the settings update in time.",
                    },
                ), settings
        except Exception as exc:  # noqa: BLE001 - surface app-server settings failures.
            _logger.warning(
                "Codex-native thread/settings/update failed for session=%s thread=%s settings=%s",
                conv_id,
                state.thread_id,
                sorted(settings),
                exc_info=True,
                extra={"session_id": conv_id},
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_settings_update_failed",
                    "detail": _client_safe_error_detail(
                        exc, context="codex-native settings update"
                    ),
                },
            ), settings
        finally:
            with contextlib.suppress(Exception):
                await codex_client.close()
        applied: dict[str, str] = {}
        model = settings.get("model")
        if isinstance(model, str):
            applied["model"] = model
        effort = settings.get("effort")
        if isinstance(effort, str) and effort:
            _session_reasoning_effort[conv_id] = effort
            applied["effort"] = effort
        if applied:
            failed = await asyncio.to_thread(mirror_applied_codex_settings, bridge_dir, applied)
            for key in failed:
                _logger.warning("Could not mirror Codex %s for session=%s", key, conv_id)
        if isinstance(effort, str) and effort:
            # Codex emits no settings notification when normalization leaves
            # its effort unchanged, so confirm the applied value explicitly.
            if server_client is not None:
                try:
                    mirrored = await server_client.post(
                        f"/v1/sessions/{urllib.parse.quote(conv_id, safe='')}/events",
                        json={
                            "type": "external_reasoning_effort_change",
                            "data": {"reasoning_effort": effort},
                        },
                        timeout=5.0,
                    )
                    if mirrored.status_code >= 400:
                        _logger.warning(
                            "Could not mirror applied Codex effort for session=%s: HTTP %s",
                            conv_id,
                            mirrored.status_code,
                        )
                except (httpx.HTTPError, ConnectionError):
                    _logger.warning(
                        "Could not mirror applied Codex effort for session=%s",
                        conv_id,
                        exc_info=True,
                    )
        return Response(status_code=204), settings

    async def _codex_native_model_and_effort_for_settings_update(
        conv_id: str,
    ) -> tuple[str | None, str | None]:
        model: str | None = None
        effort: str | None = None
        if server_client is not None:
            try:
                resp = await server_client.get(
                    f"/v1/sessions/{urllib.parse.quote(conv_id, safe='')}",
                    params=_SESSION_METADATA_PARAMS,
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    snapshot = resp.json()
                    if isinstance(snapshot, dict):
                        # ``llm_model`` is the harness's own report — the model
                        # the pane is actually on. ``model_override`` is only a
                        # request and may predate a relaunch or an unconfirmed
                        # switch, so it is the fallback, not the lead: a
                        # plan-mode toggle must re-assert the pane's real
                        # model, never resurrect a stale ask.
                        raw_model = snapshot.get("llm_model") or snapshot.get("model_override")
                        if isinstance(raw_model, str) and raw_model.strip():
                            model = raw_model.strip()
                        raw_effort = snapshot.get("reasoning_effort")
                        if isinstance(raw_effort, str) and raw_effort.strip():
                            effort = raw_effort.strip()
            except (httpx.HTTPError, RuntimeError, ValueError):
                _logger.warning(
                    "Codex-native plan-mode update could not fetch session snapshot for %s",
                    conv_id,
                    exc_info=True,
                    extra={"session_id": conv_id},
                )

        if model is None:
            model = _codex_native_model_from_spec(_session_spec_cache.get(conv_id))
        return model, effort

    async def _handle_codex_native_plan_mode_change(
        conv_id: str,
        *,
        enabled: bool,
    ) -> Response:
        state = await _codex_native_bridge_state_for_session(conv_id, action="plan-mode update")
        if state is None:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_settings_update_failed",
                    "detail": "Codex-native plan-mode update requires a loaded Codex bridge.",
                },
            )
        model, effort = await _codex_native_model_and_effort_for_settings_update(conv_id)
        if model is None:
            _logger.warning(
                "Codex-native plan-mode update skipped for %s: current model is unknown",
                conv_id,
                extra={"session_id": conv_id},
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_settings_update_failed",
                    "detail": "Codex-native plan-mode update requires a current model.",
                },
            )
        from omnigent.harnesses.codex_native.bridge import (
            DeveloperInstructionsReadState,
            read_codex_config_developer_instructions_state_from_home,
        )

        _di_read = read_codex_config_developer_instructions_state_from_home(Path(state.codex_home))
        if _di_read.state is DeveloperInstructionsReadState.UNREADABLE:
            _logger.warning(
                "Codex-native plan-mode update skipped for %s: developer_instructions "
                "config unreadable — refusing to guess and risk wiping live state.",
                conv_id,
                extra={"session_id": conv_id},
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_settings_update_failed",
                    "detail": (
                        "Codex-native plan-mode update requires reading the current "
                        "developer_instructions config; it could not be read."
                    ),
                },
            )
        developer_instructions = _di_read.value
        return await _handle_codex_native_settings_update(
            conv_id,
            {
                "collaborationMode": {
                    "mode": "plan" if enabled else "default",
                    "settings": {
                        "model": model,
                        "reasoning_effort": effort,
                        "developer_instructions": developer_instructions,
                    },
                },
            },
        )

    async def _handle_codex_native_approval_mode_change(
        conv_id: str,
        mode: str,
    ) -> Response:
        # Codex switches approval stance through its own /permissions popup, not
        # thread/settings/update (that RPC drives model/effort but no-ops for
        # approval). So drive the popup by keystroke into the codex tmux pane —
        # the same channel /compact uses — reading the popup to find the preset's
        # menu digit (row positions vary by codex build/platform, so we discover
        # it rather than hardcode it) and confirming Codex echoed the switch.
        from omnigent.codex_approval_modes import codex_permission_preset

        preset = codex_permission_preset(mode)
        if preset is None:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_input",
                    "detail": f"Unknown codex approval mode {mode!r}",
                },
            )
        registry = resource_registry.terminal_registry
        instance = registry.get(conv_id, "codex", "main") if registry is not None else None
        if instance is None or not instance.running:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_approval_mode_failed",
                    "detail": "Codex terminal is not running; reconnect first.",
                },
            )
        try:
            offered = await asyncio.to_thread(
                _inject_codex_permission_mode,
                str(instance.socket_path),
                instance.tmux_target,
                label=preset.label,
                needs_confirm=preset.needs_confirm,
            )
            # Confirm the switch landed before reporting success — the injection
            # is otherwise fire-and-forget, so a slow-rendering popup would leave
            # the label claiming a mode the TUI never entered.
            confirmed = offered and await asyncio.to_thread(
                _codex_permission_mode_confirmed,
                str(instance.socket_path),
                instance.tmux_target,
                preset.label,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_approval_mode_failed",
                    "detail": _client_safe_error_detail(
                        exc, context="codex-native approval mode change"
                    ),
                },
            )
        if not offered:
            # The popup was read but had no row for this preset. Codex lists Read
            # Only only when the session runs with a read-only permission profile,
            # so point the user at the reachable path instead of a phantom switch.
            if preset.value == "read-only":
                detail = (
                    "Codex's /permissions popup doesn't offer Read Only on this "
                    "platform — Codex lists it only on Windows or when a permission "
                    "profile is active. To run read-only, start a new session in "
                    "read-only mode."
                )
            else:
                detail = (
                    f"Codex's /permissions popup on this session doesn't offer {preset.label!r}."
                )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_approval_mode_unsupported",
                    "detail": detail,
                },
            )
        if not confirmed:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_approval_mode_failed",
                    "detail": (
                        f"Codex didn't confirm the switch to {preset.label!r} within "
                        f"{_CODEX_PERMISSION_CONFIRM_BUDGET_S:g}s; please try again."
                    ),
                },
            )
        return JSONResponse(status_code=200, content={"approval_mode": preset.value})

    async def _codex_native_model_options(conv_id: str) -> list[_JsonObject]:
        from omnigent.harnesses.codex_native.app_server import (
            client_for_transport,
            list_codex_model_options,
            mark_launch_default,
        )
        from omnigent.harnesses.codex_native.bridge import read_codex_home_config_model

        state = await _codex_native_bridge_state_for_session(
            conv_id,
            action="model options",
            missing_state_log_level=logging.DEBUG,
        )
        if state is None:
            raise _CodexNativeModelOptionsNotReady("Codex-native model options are not ready yet.")

        codex_client = client_for_transport(
            state.socket_path,
            client_name="omnigent-codex-native-runner",
        )
        try:
            await codex_client.connect()
            rows = await list_codex_model_options(codex_client)
        finally:
            with contextlib.suppress(Exception):
                await codex_client.close()
        active_model = await asyncio.to_thread(
            read_codex_home_config_model,
            Path(state.codex_home),
        )
        marked = mark_launch_default(rows, active_model)
        # Write the live account rows back to the shared catalog store so the
        # pre-launch picker converges to account truth after the first
        # session — keeping the SHAPE's stored default (a session's own pin
        # must not become the host-wide default).
        asyncio.get_running_loop().create_task(
            _write_back_codex_catalog(conv_id, [dict(row) for row in rows])
        )
        return marked

    async def _write_back_codex_catalog(session_id: str, rows: list[_JsonObject]) -> None:
        try:
            from omnigent.harnesses.codex_native.app_server import (
                codex_catalog_fingerprint,
                mark_launch_default,
                resolve_native_codex_launch,
            )
            from omnigent.models import model_catalog_store

            spec = await _resolve_session_agent_spec(session_id)
            if spec is None:
                return
            launch = await asyncio.to_thread(resolve_native_codex_launch, model=None, spec=spec)
            fingerprint = codex_catalog_fingerprint(launch)
            stored = model_catalog_store.read_catalog("codex-native", fingerprint)
            stored_default = next(
                (row.get("id") for row in stored or [] if row.get("isDefault") is True),
                None,
            )
            shaped = mark_launch_default(
                rows, stored_default if isinstance(stored_default, str) else None
            )
            await asyncio.to_thread(
                model_catalog_store.write_catalog, "codex-native", fingerprint, shaped
            )
        except Exception:  # noqa: BLE001 — write-back is best-effort
            _logger.debug(
                "codex model-catalog write-back skipped",
                exc_info=True,
                extra={"session_id": session_id},
            )

    async def _handle_pi_native_effort_change(
        conv_id: str,
        effort: str | None,
    ) -> Response:
        from omnigent.harnesses.pi_native.bridge import (
            bridge_dir_for_session_id,
            enqueue_thinking_level_change,
        )
        from omnigent.util.reasoning_effort import to_pi_thinking_level

        if effort is None or not effort.strip():
            return Response(status_code=204)
        thinking = to_pi_thinking_level(effort)
        try:
            await asyncio.to_thread(
                enqueue_thinking_level_change,
                bridge_dir_for_session_id(conv_id),
                thinking,
            )
        except OSError as exc:
            _logger.warning(
                "Pi-native effort change failed for session=%s",
                conv_id,
                exc_info=True,
                extra={"session_id": conv_id},
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "pi_native_effort_failed",
                    "detail": _client_safe_error_detail(exc, context="pi-native effort change"),
                },
            )
        return Response(status_code=204)

    async def _handle_pi_native_model_change(
        conv_id: str,
        model: str | None,
    ) -> Response:
        from omnigent.harnesses.pi_native.bridge import (
            bridge_dir_for_session_id,
            enqueue_model_change,
        )

        if model is None or not model.strip():
            return Response(status_code=204)
        try:
            await asyncio.to_thread(
                enqueue_model_change,
                bridge_dir_for_session_id(conv_id),
                model.strip(),
            )
        except OSError as exc:
            _logger.warning(
                "Pi-native model change failed for session=%s",
                conv_id,
                exc_info=True,
                extra={"session_id": conv_id},
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "pi_native_model_failed",
                    "detail": _client_safe_error_detail(exc, context="pi-native model change"),
                },
            )
        return Response(status_code=204)

    async def _teardown_session_terminals(conv_id: str) -> None:
        from omnigent.entities.session_resources import terminal_resource_id
        from omnigent.runner.tool_dispatch import _publish_terminal_deleted_event

        terminal_registry = resource_registry.terminal_registry
        if terminal_registry is None:
            return
        terminals = [
            (entry.terminal_name, entry.session_key)
            for entry in terminal_registry.list_for_conversation(conv_id)
        ]
        for terminal_name, session_key in terminals:
            terminal_id = terminal_resource_id(terminal_name, session_key)
            try:
                await resource_registry.close_terminal(conv_id, terminal_id)
            except (RuntimeError, OSError):
                _logger.warning(
                    "Failed to close terminal %s for session %s during stop",
                    terminal_id,
                    conv_id,
                    exc_info=True,
                    extra={"session_id": conv_id},
                )
            _publish_terminal_deleted_event(
                conversation_id=conv_id,
                terminal_name=terminal_name,
                session_key=session_key,
                publish_event=_publish_event,
            )

    async def _handle_claude_native_permission_mode_change(
        conv_id: str,
        mode: str | None,
    ) -> Response:
        """
        Switch a live claude-native session's permission mode.

        Claude Code can only set the mode at launch (``--permission-mode``)
        or from its own shift+tab cycle, so the bridge drives that cycle
        and verifies the pane landed on *mode*. A 200 carries the mode now
        rendered, which the Omnigent server persists as the session's
        current mode.
        """
        from omnigent.harnesses.claude_native.bridge import (
            bridge_dir_for_bridge_id,
            set_permission_mode,
        )

        if mode is None or not mode.strip():
            return Response(status_code=204)
        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        await _prepare_claude_native_pane_for_injection(conv_id, bridge_dir)
        try:
            settled = await asyncio.to_thread(
                set_permission_mode,
                bridge_dir,
                mode=mode.strip(),
                timeout_s=1.0,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_permission_mode_failed",
                    "detail": _client_safe_error_detail(
                        exc, context="claude-native permission mode change"
                    ),
                },
            )
        return JSONResponse(status_code=200, content={"permission_mode": settled})

    async def _handle_claude_native_btw_dismiss(conv_id: str) -> Response:
        """
        Close a live claude-native session's ``/btw`` overlay from the web UI.

        The reader dismissed the transient side-chat overlay in the web view;
        mirror that to the terminal by sending Escape to the pane — but only
        when the ``/btw`` overlay is actually on screen (the bridge guards on
        this), because a blind Escape on the bare composer would cancel an
        in-flight turn. Best-effort: the overlay also auto-dismisses on the
        next injected message, so a miss is harmless.
        """
        from omnigent.harnesses.claude_native.bridge import (
            bridge_dir_for_bridge_id,
            dismiss_btw_overlay,
        )

        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        try:
            await asyncio.to_thread(dismiss_btw_overlay, bridge_dir)
        except (RuntimeError, ValueError):
            # Nothing to recover: the pane overlay closes on the next inject.
            return Response(status_code=204)
        return Response(status_code=200)

    async def _prepare_claude_native_pane_for_injection(
        conv_id: str,
        bridge_dir: Path,
    ) -> None:
        """Heal a dead-but-registered Claude pane before typing into it.

        Registry membership is not liveness: a pane whose tmux server died
        without ``close()`` stays registered advertising a socket that is
        gone, so anything typed into it fails: tmux cannot connect, or the pane
        never renders. ``_ensure_native_terminal_for_turn`` already probes and
        recreates such an entry, so it is reused rather than growing a second
        recovery path.

        Only a recreate waits for :func:`claude_pane_ready`, because only a
        recreate reboots the TUI (via ``--resume``) with no input box yet.
        That is what the ``is_alive()`` probe distinguishes. On a LIVE pane the
        one thing that reports "not ready" is a surface occupying the composer
        (shell mode, the ctrl+r search, a hand-opened picker), which this poll
        has no way to clear: ``inject_slash_command`` reclaims the composer
        itself in ``_restore_occupied_input``. Waiting here would stall the
        case inject already handles for the whole budget, then inject anyway.

        No terminal registry means there is nothing to heal, and a recreate that
        produced no pane is not waited on either (inject keeps its own short
        advertisement timeout in both cases).
        """
        from omnigent.harnesses.claude_native.bridge import claude_pane_ready

        terminal_registry = resource_registry.terminal_registry if resource_registry else None
        if terminal_registry is None:
            return
        terminal_name = native_terminal_name("claude-native")
        if terminal_name is None:
            return
        # This probe duplicates the ensure path's own detection on purpose: its
        # only job is to decide whether the readiness poll below runs at all.
        instance = terminal_registry.get(conv_id, terminal_name, "main")
        if instance is not None and await instance.is_alive():
            return
        await _ensure_native_terminal_for_turn(conv_id, "claude-native")
        if terminal_registry.get(conv_id, terminal_name, "main") is None:
            # The ensure swallows its own failures, so an unregistered pane here
            # means nothing was created and nothing is booting. Waiting cannot
            # help; let the injection fail fast as it did before.
            return
        deadline = time.monotonic() + _CLAUDE_PANE_READY_TIMEOUT_S
        while True:
            if await asyncio.to_thread(claude_pane_ready, bridge_dir):
                return
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(_CLAUDE_PANE_READY_POLL_S)
        # Let the injection report its own failure, as it did before this heal
        # existed. Logged, else a pane that recreates but never boots is
        # indistinguishable from a plain slow request.
        _logger.warning(
            "claude-native pane not ready %ss after re-create for session=%s; injecting anyway",
            _CLAUDE_PANE_READY_TIMEOUT_S,
            conv_id,
            extra={"session_id": conv_id},
        )

    async def _handle_claude_native_effort_change(
        conv_id: str,
        effort: str | None,
    ) -> Response:
        from omnigent.harnesses.claude_native.bridge import (
            EFFORT_DIALOG_HINT,
            bridge_dir_for_bridge_id,
            inject_slash_command,
        )
        from omnigent.util.reasoning_effort import CLAUDE_EFFORTS

        if effort is None or effort not in CLAUDE_EFFORTS:
            return Response(status_code=204)
        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        await _prepare_claude_native_pane_for_injection(conv_id, bridge_dir)
        command = f"/effort {effort}"
        try:
            # An effort switch invalidates the prompt cache on a session with
            # history, so Claude Code asks to confirm; the chat UI cannot render
            # that TUI dialog, so answer it by its own title.
            await asyncio.to_thread(
                inject_slash_command,
                bridge_dir,
                command=command,
                timeout_s=1.0,
                auto_confirm=True,
                confirm_hint=EFFORT_DIALOG_HINT,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_effort_failed",
                    "detail": _client_safe_error_detail(
                        exc, context="claude-native effort change"
                    ),
                },
            )
        return Response(status_code=204)

    async def _watch_late_model_dialog(
        conv_id: str,
        bridge_dir: Path,
        expected: set[str],
    ) -> None:
        """Answer a ``/model`` confirm dialog that pops after the active turn.

        A mid-turn switch queues in Claude's composer; the confirm dialog
        renders only when the turn settles — potentially minutes after the
        injection's own watch and the request's confirm window. This watcher
        presses Enter ONLY when the model dialog is verifiably on screen
        (never blind), stops as soon as the statusLine reports one of the
        expected spellings, and gives up quietly after its budget — the
        persisted request and the forwarder's verbatim report remain the
        authoritative record either way.
        """
        from omnigent.harnesses.claude_native.bridge import (
            SWITCH_MODEL_DIALOG_HINT,
            confirm_dialog_if_open,
            read_claude_status_model,
        )

        deadline = time.monotonic() + _CLAUDE_MODEL_LATE_DIALOG_BUDGET_S
        while time.monotonic() < deadline:
            try:
                current = await asyncio.to_thread(read_claude_status_model, bridge_dir)
                if current and current in expected:
                    return
                await asyncio.to_thread(
                    confirm_dialog_if_open, bridge_dir, hint=SWITCH_MODEL_DIALOG_HINT
                )
            except Exception:  # noqa: BLE001 — best-effort; the report reconciles
                _logger.debug(
                    "late model-dialog watch errored for session=%s",
                    conv_id,
                    exc_info=True,
                    extra={"session_id": conv_id},
                )
                return
            await asyncio.sleep(_CLAUDE_MODEL_LATE_DIALOG_POLL_S)
        _logger.info(
            "late model-dialog watch for session=%s ended without a confirmed switch",
            conv_id,
            extra={"session_id": conv_id},
        )

    async def _handle_claude_native_model_change(
        conv_id: str,
        model: str | None,
    ) -> Response:
        from omnigent.harnesses.claude_native.bridge import (
            SWITCH_MODEL_DIALOG_HINT,
            bridge_dir_for_bridge_id,
            confirm_dialog_if_open,
            inject_slash_command,
            read_claude_status_model,
            read_model_env,
            read_model_picker_values,
        )
        from omnigent.harnesses.claude_native.main import (
            resolve_claude_native_model_selection,
            stored_claude_catalog_rows,
            stored_claude_picker_values,
        )
        from omnigent.inference_config import (
            binding_for_harness,
            load_runtime_inference_config,
            resolve_bound_model,
        )
        from omnigent.models.claude_model_vocabulary import (
            claude_model_command_arg,
            picker_command_values,
        )

        if model is None or not model.strip():
            return Response(status_code=204)
        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        await _prepare_claude_native_pane_for_injection(conv_id, bridge_dir)
        selected_model = model.strip()
        claude_config = await _resolve_session_claude_launch_config(conv_id)
        resolved_model = (
            resolve_claude_native_model_selection(selected_model, claude_config) or selected_model
        )
        # Translate through the pane's picker values, aliases, and custom slot.
        # An unknown spelling must fail before any command reaches the terminal.
        env = read_model_env(bridge_dir) or None
        cached_options = _claude_model_options_rows.get(conv_id)
        if cached_options is not None:
            picker_values = picker_command_values(cached_options[1])
        else:
            stored_rows = stored_claude_catalog_rows(claude_config)
            if stored_rows is not None and not stored_rows:
                # Discovery stored an authoritative empty catalog (every
                # picker entry disabled): launch-recorded bridge values are
                # stale vocabulary, so nothing is switchable.
                picker_values: list[str] = []
            else:
                picker_values = read_model_picker_values(bridge_dir)
                if not picker_values:
                    picker_values = stored_claude_picker_values(claude_config, stored_rows)
        model_arg = claude_model_command_arg(resolved_model, env, picker_values=picker_values)
        inference_config = load_runtime_inference_config()
        if binding_for_harness(inference_config, "claude-native") is not None:
            resolved_model = resolve_bound_model(inference_config, "claude-native", selected_model)
            model_arg = resolved_model
        if model_arg is None:
            _logger.warning(
                "claude-native model change: %r has no spelling session=%s accepts "
                "(pins=%s, picker=%s)",
                resolved_model,
                conv_id,
                sorted(env or ()),
                picker_values,
                extra={"session_id": conv_id},
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_model_unsupported",
                    "detail": (
                        f"This Claude terminal cannot switch to {resolved_model}: "
                        "its /model picker has no spelling for that model."
                    ),
                },
            )
        command = f"/model {model_arg}"
        baseline = await asyncio.to_thread(read_claude_status_model, bridge_dir)
        try:
            # Accepted trade-off: ``/model <id>`` also saves the pick as the
            # person's global default in ``~/.claude/settings.json``. Driving
            # the interactive picker instead avoided that write but needed
            # ~35s of fragile tmux automation, so the write stands.
            await asyncio.to_thread(
                inject_slash_command,
                bridge_dir,
                command=command,
                timeout_s=1.0,
                auto_confirm=True,
                confirm_hint=SWITCH_MODEL_DIALOG_HINT,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_model_failed",
                    "detail": _client_safe_error_detail(exc, context="claude-native model change"),
                },
            )
        # Verify against the statusLine snapshot the forwarder already polls:
        # Claude rewrites it on every render, including right after ``/model``.
        # Expected spellings come from this session's own catalog rows (every
        # row's ``model`` is the harness's own resolution), plus the typed arg
        # and its selection mapping. Success replies only after the pane
        # actually switched; the swallowed-dialog case answers non-2xx so the
        # server surfaces it instead of the row silently claiming the pick.
        expected = {value for value in (resolved_model, model_arg) if value}
        cached_rows = _claude_model_options_rows.get(conv_id)
        for row in cached_rows[1] if cached_rows is not None else []:
            if row.get("id") in (selected_model, resolved_model) or row.get("model") in (
                selected_model,
                resolved_model,
            ):
                row_model = row.get("model")
                if isinstance(row_model, str) and row_model:
                    expected.add(row_model)
        deadline = time.monotonic() + _CLAUDE_MODEL_CONFIRM_TIMEOUT_S
        while True:
            current = await asyncio.to_thread(read_claude_status_model, bridge_dir)
            if current and (current in expected or (baseline and current != baseline)):
                # The pane switched. When it landed somewhere other than the
                # expected spelling, the forwarder's verbatim report is the
                # truth the UI will settle on — the command still took effect.
                return Response(status_code=204)
            if baseline is None and current is None:
                # No statusLine snapshot on either side of the injection: a
                # live wrapper-managed pane writes one on every render, so
                # this is a shape without the wrapper — the switch is
                # unverifiable, not failed. Report success and leave the
                # forwarder to reconcile the row.
                _logger.warning(
                    "claude-native model change for session=%s could not be verified: "
                    "no statusLine snapshot",
                    conv_id,
                    extra={"session_id": conv_id},
                )
                return Response(status_code=204)
            # The confirm dialog can render well after the injection's own
            # short watch (a warm repaint, or a queued command surfacing) —
            # answer it whenever it shows inside the window.
            await asyncio.to_thread(
                confirm_dialog_if_open, bridge_dir, hint=SWITCH_MODEL_DIALOG_HINT
            )
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(_CLAUDE_MODEL_CONFIRM_POLL_S)
        if _native_pane_status.get(conv_id) in ("running", "waiting"):
            # Mid-turn switch: Claude queues the typed command and applies it
            # when the turn settles — its confirm dialog can pop minutes from
            # now. Not a failure: answer success, keep a detached watcher on
            # the late dialog, and let the forwarder's report settle the
            # picker when the switch actually lands.
            watcher = asyncio.create_task(
                _watch_late_model_dialog(conv_id, bridge_dir, expected),
                name=f"claude-model-dialog-{conv_id}",
            )
            _model_dialog_watchers.add(watcher)
            watcher.add_done_callback(_model_dialog_watchers.discard)
            _logger.info(
                "claude-native model change for session=%s is queued behind an active "
                "turn; watching for the late confirm dialog",
                conv_id,
                extra={"session_id": conv_id},
            )
            return Response(status_code=204)
        return JSONResponse(
            status_code=503,
            content={
                "error": "claude_native_model_unconfirmed",
                "detail": (
                    f"the terminal did not confirm the switch to {model_arg} within "
                    f"{_CLAUDE_MODEL_CONFIRM_TIMEOUT_S:.0f}s — a dialog may be open in the pane"
                ),
            },
        )

    async def _handle_cursor_native_model_change(
        conv_id: str,
        model: str | None,
    ) -> Response:
        from omnigent.harnesses.cursor_native.bridge import (
            bridge_dir_for_session_id,
            inject_model_command,
        )

        if model is None or not model.strip():
            return Response(status_code=204)
        bridge_dir = bridge_dir_for_session_id(conv_id)
        selected_model = model.strip()
        expected_display_name = _session_cursor_model_names.get(conv_id, {}).get(selected_model)
        try:
            await asyncio.to_thread(
                inject_model_command,
                bridge_dir,
                model=selected_model,
                expected_display_name=expected_display_name,
                timeout_s=1.0,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "cursor_native_model_failed",
                    "detail": _client_safe_error_detail(exc, context="cursor-native model change"),
                },
            )
        return Response(status_code=204)

    async def _handle_kiro_native_model_change(
        conv_id: str,
        model: str | None,
    ) -> Response:
        from omnigent.harnesses.kiro_native.bridge import (
            bridge_dir_for_session_id,
            inject_model_command,
        )

        if model is None or not model.strip():
            return Response(status_code=204)
        bridge_dir = bridge_dir_for_session_id(conv_id)
        try:
            await asyncio.to_thread(
                inject_model_command,
                bridge_dir,
                model=model.strip(),
                timeout_s=1.0,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "kiro_native_model_failed",
                    "detail": _client_safe_error_detail(exc, context="kiro-native model change"),
                },
            )
        return Response(status_code=204)

    async def _handle_devin_native_model_change(
        conv_id: str,
        model: str | None,
    ) -> Response:
        from omnigent.harnesses.devin_native.bridge import (
            bridge_dir_for_session_id,
            inject_model_command,
        )
        from omnigent.harnesses.devin_native.main import resolve_devin_launch_model

        if model is None or not model.strip():
            return Response(status_code=204)
        # Devin has no separate effort flag — effort is a suffix on the model id.
        # Compose the picked family with the session's remembered effort so a
        # New-Chat (model, effort) pick lands on the same variant the launch path
        # composes (compose is idempotent for an already-composed id).
        composed = await asyncio.to_thread(
            resolve_devin_launch_model,
            model.strip(),
            _session_reasoning_effort.get(conv_id),
        )
        if not composed:
            return Response(status_code=204)
        bridge_dir = bridge_dir_for_session_id(conv_id)
        try:
            await asyncio.to_thread(
                inject_model_command,
                bridge_dir,
                model=composed,
                timeout_s=1.0,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "devin_native_model_failed",
                    "detail": _client_safe_error_detail(exc, context="devin-native model change"),
                },
            )
        return Response(status_code=204)

    async def _handle_devin_native_permission_mode_change(
        conv_id: str,
        mode: str | None,
    ) -> Response:
        from omnigent.harnesses.devin_native.bridge import (
            bridge_dir_for_session_id,
            inject_permission_mode,
        )

        if mode is None or not mode.strip():
            return Response(status_code=204)
        bridge_dir = bridge_dir_for_session_id(conv_id)
        try:
            settled = await asyncio.to_thread(
                inject_permission_mode,
                bridge_dir,
                mode=mode.strip(),
                timeout_s=1.0,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "devin_native_permission_mode_failed",
                    "detail": _client_safe_error_detail(
                        exc, context="devin-native permission mode change"
                    ),
                },
            )
        return JSONResponse(status_code=200, content={"permission_mode": settled})

    async def _handle_devin_native_effort_change(
        conv_id: str,
        effort: str | None,
    ) -> Response:
        from omnigent.harnesses.devin_native.bridge import (
            bridge_dir_for_session_id,
            inject_model_command,
        )
        from omnigent.harnesses.devin_native.main import resolve_devin_launch_model

        # Devin has no `/effort`: effort is a suffix on the model id, so an effort
        # switch is a `/model <family+effort>` re-inject. Re-compose the session's
        # pinned model with the new effort. With no pinned model there is nothing
        # to re-inject now — the executor still composes it on the next turn.
        model = await _fetch_session_model_override(conv_id)
        if not model or not model.strip():
            return Response(status_code=204)
        composed = await asyncio.to_thread(resolve_devin_launch_model, model.strip(), effort)
        if not composed:
            return Response(status_code=204)
        bridge_dir = bridge_dir_for_session_id(conv_id)
        try:
            await asyncio.to_thread(
                inject_model_command,
                bridge_dir,
                model=composed,
                timeout_s=1.0,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "devin_native_effort_failed",
                    "detail": _client_safe_error_detail(exc, context="devin-native effort change"),
                },
            )
        return Response(status_code=204)

    async def _handle_devin_native_compact(conv_id: str) -> Response:
        from omnigent.harnesses.devin_native.bridge import (
            bridge_dir_for_session_id,
            inject_slash_command,
        )

        bridge_dir = bridge_dir_for_session_id(conv_id)
        try:
            await asyncio.to_thread(
                inject_slash_command,
                bridge_dir,
                command="/compact",
                timeout_s=1.0,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "devin_native_compact_failed",
                    "detail": _client_safe_error_detail(exc, context="devin-native compact"),
                },
            )
        return Response(status_code=200)

    async def _handle_claude_native_compact(conv_id: str) -> Response:
        from omnigent.harnesses.claude_native.bridge import (
            bridge_dir_for_bridge_id,
            inject_slash_command,
        )

        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        await _prepare_claude_native_pane_for_injection(conv_id, bridge_dir)
        try:
            await asyncio.to_thread(
                inject_slash_command,
                bridge_dir,
                command="/compact",
                timeout_s=1.0,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_compact_failed",
                    "detail": _client_safe_error_detail(exc, context="claude-native compact"),
                },
            )
        return Response(status_code=200)

    async def _handle_codex_native_compact(conv_id: str) -> Response:
        registry = resource_registry.terminal_registry
        instance = registry.get(conv_id, "codex", "main") if registry is not None else None
        if instance is None or not instance.running:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_compact_failed",
                    "detail": "Codex terminal is not running; reconnect first.",
                },
            )

        socket_path = str(instance.socket_path)
        target = instance.tmux_target

        try:
            await asyncio.to_thread(_inject_codex_compact, socket_path, target)
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_compact_failed",
                    "detail": _client_safe_error_detail(exc, context="codex-native compact"),
                },
            )
        return Response(status_code=200)

    async def _handle_opencode_native_compact(conv_id: str) -> Response:
        from omnigent.harnesses.opencode_native.bridge import (
            bridge_dir_for_bridge_id,
            read_bridge_state,
        )
        from omnigent.harnesses.opencode_native.client import OpenCodeClientError

        server = _AUTO_OPENCODE_SERVERS.get(conv_id)
        state = read_bridge_state(bridge_dir_for_bridge_id(conv_id))
        if server is None or state is None or not state.opencode_session_id:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "opencode_native_compact_failed",
                    "detail": "OpenCode session is not active; reconnect first.",
                },
            )
        client = server.client()
        try:
            session = await client.get_session(state.opencode_session_id)
            messages = await client.list_messages(state.opencode_session_id)
            provider_id, model_id = _resolve_opencode_compact_model(
                session, messages, state.model_override
            )
            if not provider_id or not model_id:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "opencode_native_compact_failed",
                        "detail": "Could not resolve a compaction model; try switching the model.",
                    },
                )
            await client.summarize(
                state.opencode_session_id, provider_id=provider_id, model_id=model_id
            )
        except (httpx.HTTPError, OpenCodeClientError, RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "opencode_native_compact_failed",
                    "detail": _client_safe_error_detail(exc, context="opencode-native compact"),
                },
            )
        finally:
            await client.aclose()
        return Response(status_code=200)

    async def _opencode_native_model_options(conv_id: str) -> list[_JsonObject]:
        from omnigent.harnesses.opencode_native.app_server import (
            filtered_server_env,
            list_opencode_cli_model_options,
        )
        from omnigent.harnesses.opencode_native.bridge import (
            bridge_dir_for_bridge_id,
            read_bridge_state,
        )
        from omnigent.harnesses.opencode_native.client import OpenCodeClient

        bridge_dir = bridge_dir_for_bridge_id(conv_id)
        state = read_bridge_state(bridge_dir)
        if state is None or not state.server_base_url:
            raise _CodexNativeModelOptionsNotReady("OpenCode-native app-server is not ready yet.")

        cli_env = filtered_server_env(
            bridge_dir=bridge_dir,
            auth_secret=state.auth_secret or "",
        )
        try:
            return await asyncio.to_thread(list_opencode_cli_model_options, env=cli_env)
        except Exception as exc:  # noqa: BLE001 - fall back to the server catalog.
            _logger.debug(
                "OpenCode CLI model list failed for %s: %r",
                conv_id,
                exc,
                extra={"session_id": conv_id},
            )

        client = OpenCodeClient(
            base_url=state.server_base_url,
            headers=state.auth_headers(),
        )
        try:
            return await client.list_models()
        finally:
            await client.aclose()

    async def _handle_opencode_native_model_change(conv_id: str, model: str | None) -> Response:
        from omnigent.harnesses.opencode_native.bridge import (
            bridge_dir_for_bridge_id,
            update_model_override,
        )
        from omnigent.inference_config import (
            binding_for_harness,
            load_runtime_inference_config,
            resolve_bound_model,
        )

        inference_config = load_runtime_inference_config()
        if binding_for_harness(inference_config, "opencode-native") is not None:
            selected = resolve_bound_model(inference_config, "opencode-native", model)
            model = f"omnigent/{selected}" if selected is not None else None
        updated = await asyncio.to_thread(
            update_model_override, bridge_dir_for_bridge_id(conv_id), model
        )
        return Response(status_code=200 if updated else 204)

    async def _handle_opencode_native_clear(conv_id: str) -> Response:
        if _session_harness_name(conv_id) != "opencode-native":
            return Response(status_code=204)
        if server_client is not None:
            with contextlib.suppress(httpx.HTTPError):
                await server_client.patch(
                    f"/v1/sessions/{urllib.parse.quote(conv_id, safe='')}",
                    json={"external_session_id": None},
                    params={"include_usage": "false"},
                    timeout=10.0,
                )
        try:
            spec = await _resolve_session_agent_spec(conv_id)
        except OmnigentError:
            spec = None
        try:
            await _auto_create_opencode_terminal(
                conv_id,
                resource_registry,
                _publish_event,
                agent_spec=spec,
                server_client=server_client,
                ensure_comment_relay=_ensure_comment_relay_started,
            )
        except Exception as exc:  # noqa: BLE001 - report relaunch failure to caller.
            return JSONResponse(
                status_code=503,
                content={
                    "error": "opencode_native_clear_failed",
                    "detail": _client_safe_error_detail(exc, context="opencode-native clear"),
                },
            )
        return Response(status_code=200)

    async def _handle_cursor_native_compact(conv_id: str) -> Response:
        from omnigent.harnesses.cursor_native.bridge import (
            bridge_dir_for_session_id,
            inject_user_message,
        )

        bridge_dir = bridge_dir_for_session_id(conv_id)
        _publish_event(conv_id, {"type": "response.compaction.in_progress", "task_id": conv_id})
        try:
            await asyncio.to_thread(
                inject_user_message,
                bridge_dir,
                content="/summarize",
                timeout_s=1.0,
            )
        except (RuntimeError, ValueError, OSError) as exc:
            _publish_event(conv_id, {"type": "response.compaction.failed", "task_id": conv_id})
            return JSONResponse(
                status_code=503,
                content={
                    "error": "cursor_native_compact_failed",
                    "detail": _client_safe_error_detail(exc, context="cursor-native compact"),
                },
            )
        return Response(status_code=200)

    async def _handle_pi_native_compact(conv_id: str) -> Response:
        from omnigent.harnesses.pi_native.bridge import bridge_dir_for_session_id, enqueue_compact

        try:
            await asyncio.to_thread(
                enqueue_compact,
                bridge_dir_for_session_id(conv_id),
            )
        except OSError as exc:
            _logger.warning(
                "Pi-native compact failed for session=%s",
                conv_id,
                exc_info=True,
                extra={"session_id": conv_id},
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "pi_native_compact_failed",
                    "detail": _client_safe_error_detail(exc, context="pi-native compact"),
                },
            )
        return Response(status_code=200)

    def _inject_codex_compact(socket_path: str, target: str) -> None:
        # Typing "/compact" opens Codex's slash-command popup, which draws
        # asynchronously: an Enter sent back-to-back is swallowed by the
        # still-opening popup and the command never submits, so settle first.
        from omnigent.harnesses.claude_native.bridge import _run_tmux

        _run_tmux(socket_path, "send-keys", "-t", target, "C-u")
        _run_tmux(socket_path, "send-keys", "-l", "-t", target, "/compact")
        time.sleep(_CODEX_POPUP_RENDER_S)
        _run_tmux(socket_path, "send-keys", "-t", target, "Enter")

    def _inject_codex_permission_mode(
        socket_path: str,
        target: str,
        *,
        label: str,
        needs_confirm: bool,
    ) -> bool:
        # Drive Codex's /permissions popup: open it, read its rows to find the
        # digit that selects *label*, then press it. Which rows the popup lists
        # depends on the session's codex config (Read Only shows only with a
        # read-only permission profile) and their order can vary, so the digit is
        # read from the live popup. If the popup can't be read in time (a busy
        # TUI, a slow draw), fall back to the preset's conventional menu position
        # rather than failing the switch. Full Access opens a "Yes, continue
        # anyway" sub-dialog whose first option (digit 1) confirms. A settle pause
        # between keystrokes is required — each screen draws asynchronously, and
        # typing the command then pressing Enter back-to-back races the slash-menu
        # so the command never submits.
        #
        # Returns True once a digit is pressed for *label* (from the live popup,
        # or its conventional position on fallback); False when the popup was read
        # but lists no row for *label*.
        from omnigent.codex_approval_modes import (
            CODEX_NATIVE_PERMISSION_PRESETS,
            codex_permissions_menu,
        )
        from omnigent.harnesses.claude_native.bridge import _capture_pane, _run_tmux

        # Reset to a clean composer so the command submits: close any stray
        # menu/popup, then clear the line. C-u also wipes any text the TUI user
        # was mid-typing — a rare, accepted cost for reliable injection.
        _run_tmux(socket_path, "send-keys", "-t", target, "Escape")
        _run_tmux(socket_path, "send-keys", "-t", target, "C-u")
        _run_tmux(socket_path, "send-keys", "-l", "-t", target, "/permissions")
        time.sleep(_CODEX_POPUP_RENDER_S)
        _run_tmux(socket_path, "send-keys", "-t", target, "Enter")

        # Wait for the popup to draw. Anchor on seeing at least two known preset
        # rows (every popup variant lists Ask for approval and Full Access)
        # rather than any numbered line, so a numbered list still visible in the
        # transcript above can't be mistaken for the rendered popup.
        known_labels = {preset.label for preset in CODEX_NATIVE_PERMISSION_PRESETS}

        def _read_menu() -> dict[str, str]:
            return codex_permissions_menu(_capture_pane(socket_path, target))

        deadline = time.monotonic() + _CODEX_PERMISSION_MENU_BUDGET_S
        options: dict[str, str] = {}
        while True:
            time.sleep(_CODEX_POPUP_RENDER_S)
            options = _read_menu()
            if len(options.keys() & known_labels) >= 2:
                break
            if time.monotonic() >= deadline:
                options = {}
                break

        if options:
            # The live popup was read: trust it. A missing row means this
            # session's codex genuinely doesn't offer the preset (e.g. Read Only
            # without a read-only permission profile). Re-read once to tolerate a
            # partially-drawn popup before concluding the row is absent.
            digit = options.get(label)
            if digit is None:
                time.sleep(_CODEX_POPUP_RENDER_S)
                digit = _read_menu().get(label)
            if digit is None:
                # Close the popup so the TUI isn't stranded in it.
                _run_tmux(socket_path, "send-keys", "-t", target, "Escape")
                return False
        else:
            # The popup couldn't be read within the budget (a busy or slow TUI).
            # Fall back to the preset's conventional menu position and let the
            # confirmation echo gate correctness, rather than failing the switch.
            digit = next(
                (
                    str(position)
                    for position, preset in enumerate(CODEX_NATIVE_PERMISSION_PRESETS, start=1)
                    if preset.label == label
                ),
                None,
            )
            if digit is None:
                return False
        _run_tmux(socket_path, "send-keys", "-l", "-t", target, digit)
        if needs_confirm:
            time.sleep(_CODEX_POPUP_RENDER_S)
            _run_tmux(socket_path, "send-keys", "-l", "-t", target, "1")
        return True

    def _codex_permission_mode_confirmed(socket_path: str, target: str, label: str) -> bool:
        from omnigent.codex_approval_modes import codex_permission_switch_confirmed
        from omnigent.harnesses.claude_native.bridge import _capture_pane

        deadline = time.monotonic() + _CODEX_PERMISSION_CONFIRM_BUDGET_S
        while True:
            if codex_permission_switch_confirmed(_capture_pane(socket_path, target), label):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_CODEX_POPUP_RENDER_S)

    async def _handle_hermes_native_compact(conv_id: str) -> Response:
        from omnigent.harnesses.hermes_native.bridge import (
            bridge_dir_for_session_id,
            inject_compress_command,
        )

        bridge_dir = bridge_dir_for_session_id(conv_id)
        try:
            await asyncio.to_thread(inject_compress_command, bridge_dir, timeout_s=1.0)
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "hermes_native_compact_failed",
                    "detail": _client_safe_error_detail(exc, context="hermes-native compact"),
                },
            )
        return Response(status_code=200)

    async def _handle_qwen_native_compact(conv_id: str) -> Response:
        from omnigent.harnesses.qwen_native.bridge import (
            bridge_dir_for_session_id,
            submit_user_message,
        )

        bridge_dir = bridge_dir_for_session_id(conv_id)
        _publish_event(conv_id, {"type": "response.compaction.in_progress", "task_id": conv_id})
        try:
            await asyncio.to_thread(submit_user_message, bridge_dir, content="/compress")
        except (RuntimeError, OSError) as exc:
            _publish_event(conv_id, {"type": "response.compaction.failed", "task_id": conv_id})
            return JSONResponse(
                status_code=503,
                content={
                    "error": "qwen_native_compact_failed",
                    "detail": _client_safe_error_detail(exc, context="qwen-native compact"),
                },
            )
        return Response(status_code=200)

    def _is_sdk_compact_body(body: dict[str, Any]) -> bool:
        """Return whether a buffered body is the synthesized claude-sdk ``/compact``.

        The compact control is dispatched as a resumed turn whose sole content is
        the literal ``/compact`` slash command. The continuation drain uses this
        to dispatch a buffered ``/compact`` as its OWN turn (never coalesced
        behind a later message), so the SDK still sees it as the turn prompt and
        runs native compaction.
        """
        content = body.get("content")
        if not isinstance(content, list) or len(content) != 1:
            return False
        part = content[0]
        return (
            isinstance(part, dict)
            and part.get("type") == "input_text"
            and part.get("text") == "/compact"
        )

    async def _handle_claude_sdk_compact(conv_id: str) -> Response:
        """Compact a claude-sdk session by sending it the ``/compact`` command.

        The Claude SDK owns its own context window in the harness subprocess,
        so Omnigent-side transcript compaction is ineffective for it. The
        effective path is to send the literal ``/compact`` slash command to
        the live client, which runs native compaction — the same PreCompact
        path auto-compaction uses, whose ``response.compaction.completed`` the
        executor already emits. We do that by dispatching a resumed
        ``/compact`` turn: buffered behind an in-flight turn (the harness has a
        single client), started immediately otherwise. Returns 200 so the
        Omnigent server treats the control as handled and skips its own
        (transcript-only) compaction.
        """
        compact_body: _JsonObject = {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "/compact"}],
            "conversation_id": conv_id,
        }
        # Serialize the active-turn check + slot bind through the same ingest
        # gate the message path uses. Without it, the idle branch's check and
        # its `_active_turns` bind straddle an `await` (history load), so a
        # message arriving in that window also sees the session idle and binds
        # the slot — the two turns then clobber each other's `_active_turns`
        # entry and race the single live SDK client. Under the gate one reaches
        # its bind before the other's check, so the loser buffers instead.
        _seq = _ingest_next_seq.get(conv_id, 0)
        _ingest_next_seq[conv_id] = _seq + 1
        _cond = _ingest_cond.get(conv_id)
        if _cond is None:
            _cond = asyncio.Condition()
            _ingest_cond[conv_id] = _cond
        async with _cond:
            while _ingest_now_serving.get(conv_id, 0) != _seq:
                await _cond.wait()
        try:
            # A turn is already running: buffer so /compact runs as the next turn
            # rather than racing the live one; the buffer drains via
            # _check_and_start_next_turn once the active turn ends. The buffered
            # compact's own in_progress comes from the executor when it later runs
            # — publishing an up-front spinner here would show "Compacting…" while
            # the prior turn is still working, so only the idle path does that.
            if conv_id in _active_turns:
                _session_message_buffers.setdefault(conv_id, []).append(compact_body)
                return Response(status_code=200)

            # Publish the compaction spinner up front so the UI shows "Compacting
            # conversation…" immediately, like the native handlers — the executor's
            # own in_progress fires only once the SDK PreCompact hook hits (mid-turn),
            # by which point the generic running status has shown "Working…". The
            # relay swallows the executor's later duplicate; `completed` (or the
            # turn-end `failed` fallback in _on_proxy_stream_end) clears the spinner.
            _publish_event(
                conv_id, {"type": "response.compaction.in_progress", "task_id": conv_id}
            )
            _sdk_compact_inprogress.add(conv_id)
            try:
                new_item: _JsonObject = {
                    "type": "message",
                    "role": "user",
                    "content": compact_body["content"],
                }
                if conv_id in _session_histories:
                    _session_histories[conv_id].append(new_item)
                else:
                    loaded = await _load_history_as_input(conv_id)
                    loaded.append(new_item)
                    _session_histories[conv_id] = loaded

                _begin_turn_slot(conv_id)
                _publish_turn_status(conv_id, "running")
                _turn_task = asyncio.create_task(
                    _run_turn_bg(compact_body, conv_id),
                    name=f"compact-{conv_id}",
                )
                _active_turns[conv_id] = _turn_task
                _turn_task.add_done_callback(_background_tasks.discard)
                _background_tasks.add(_turn_task)
            except Exception:
                # Never strand the spinner if the turn fails to start.
                _sdk_compact_inprogress.discard(conv_id)
                _publish_event(conv_id, {"type": "response.compaction.failed", "task_id": conv_id})
                raise
            return Response(status_code=200)
        finally:
            async with _cond:
                _ingest_now_serving[conv_id] = _seq + 1
                _cond.notify_all()

    async def _handle_claude_native_cost_popup(
        conv_id: str,
        elicitation_id: str,
        message: str,
        policy_name: str | None = None,
    ) -> Response:
        from omnigent.harnesses.claude_native.bridge import (
            bridge_dir_for_bridge_id,
            display_cost_approval_popup,
        )

        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        config_file = await _native_cost_popup_config_file(conv_id, "claude-native")
        try:
            await asyncio.to_thread(
                display_cost_approval_popup,
                bridge_dir,
                session_id=conv_id,
                elicitation_id=elicitation_id,
                message=message,
                policy_name=policy_name,
                timeout_s=1.0,
                config_file=config_file,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_cost_popup_failed",
                    "detail": _client_safe_error_detail(exc, context="claude-native cost popup"),
                },
            )
        return Response(status_code=204)

    async def _handle_codex_native_cost_popup(
        conv_id: str,
        elicitation_id: str,
        message: str,
        policy_name: str | None = None,
    ) -> Response:
        from omnigent.native.native_cost_popup import launch_cost_popup

        registry = resource_registry.terminal_registry
        instance = registry.get(conv_id, "codex", "main") if registry is not None else None
        if instance is None or not instance.running:
            return Response(status_code=204)
        config_file = await _native_cost_popup_config_file(conv_id, "codex-native")
        try:
            await asyncio.to_thread(
                launch_cost_popup,
                str(instance.socket_path),
                instance.tmux_target,
                config_file,
                session_id=conv_id,
                elicitation_id=elicitation_id,
                message=message,
                policy_name=policy_name,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_cost_popup_failed",
                    "detail": _client_safe_error_detail(exc, context="codex-native cost popup"),
                },
            )
        return Response(status_code=204)

    async def _handle_opencode_native_cost_popup(
        conv_id: str,
        elicitation_id: str,
        message: str,
        policy_name: str | None = None,
    ) -> Response:
        from omnigent.native.native_cost_popup import launch_cost_popup

        registry = resource_registry.terminal_registry
        instance = registry.get(conv_id, "opencode", "main") if registry is not None else None
        if instance is None or not instance.running:
            return Response(status_code=204)
        config_file = await _native_cost_popup_config_file(conv_id, "opencode-native")
        try:
            await asyncio.to_thread(
                launch_cost_popup,
                str(instance.socket_path),
                instance.tmux_target,
                config_file,
                session_id=conv_id,
                elicitation_id=elicitation_id,
                message=message,
                policy_name=policy_name,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "opencode_native_cost_popup_failed",
                    "detail": _client_safe_error_detail(exc, context="opencode-native cost popup"),
                },
            )
        return Response(status_code=204)

    async def _handle_opencode_native_blocked_notice(
        conv_id: str,
        message: str,
        policy_name: str | None = None,
    ) -> Response:
        from omnigent.native.native_cost_popup import launch_blocked_notice

        registry = resource_registry.terminal_registry
        instance = registry.get(conv_id, "opencode", "main") if registry is not None else None
        if instance is None or not instance.running:
            return Response(status_code=204)
        try:
            await asyncio.to_thread(
                launch_blocked_notice,
                str(instance.socket_path),
                instance.tmux_target,
                message=message,
                policy_name=policy_name,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "opencode_native_blocked_notice_failed",
                    "detail": _client_safe_error_detail(
                        exc, context="opencode-native blocked notice"
                    ),
                },
            )
        return Response(status_code=204)

    return NativeControls(
        codex_native_model_options=_codex_native_model_options,
        handle_claude_native_btw_dismiss=_handle_claude_native_btw_dismiss,
        handle_claude_native_compact=_handle_claude_native_compact,
        handle_claude_native_cost_popup=_handle_claude_native_cost_popup,
        handle_claude_native_effort_change=_handle_claude_native_effort_change,
        handle_claude_native_model_change=_handle_claude_native_model_change,
        handle_claude_native_permission_mode_change=_handle_claude_native_permission_mode_change,
        handle_claude_sdk_compact=_handle_claude_sdk_compact,
        handle_codex_native_approval_mode_change=_handle_codex_native_approval_mode_change,
        handle_codex_native_compact=_handle_codex_native_compact,
        handle_codex_native_cost_popup=_handle_codex_native_cost_popup,
        handle_codex_native_plan_mode_change=_handle_codex_native_plan_mode_change,
        handle_codex_native_settings_update=_handle_codex_native_settings_update,
        handle_cursor_native_compact=_handle_cursor_native_compact,
        handle_cursor_native_model_change=_handle_cursor_native_model_change,
        handle_devin_native_compact=_handle_devin_native_compact,
        handle_devin_native_effort_change=_handle_devin_native_effort_change,
        handle_devin_native_model_change=_handle_devin_native_model_change,
        handle_devin_native_permission_mode_change=_handle_devin_native_permission_mode_change,
        handle_hermes_native_compact=_handle_hermes_native_compact,
        handle_kiro_native_model_change=_handle_kiro_native_model_change,
        handle_opencode_native_blocked_notice=_handle_opencode_native_blocked_notice,
        handle_opencode_native_clear=_handle_opencode_native_clear,
        handle_opencode_native_compact=_handle_opencode_native_compact,
        handle_opencode_native_cost_popup=_handle_opencode_native_cost_popup,
        handle_opencode_native_model_change=_handle_opencode_native_model_change,
        handle_pi_native_compact=_handle_pi_native_compact,
        handle_pi_native_effort_change=_handle_pi_native_effort_change,
        handle_pi_native_model_change=_handle_pi_native_model_change,
        handle_qwen_native_compact=_handle_qwen_native_compact,
        is_sdk_compact_body=_is_sdk_compact_body,
        opencode_native_model_options=_opencode_native_model_options,
        teardown_session_terminals=_teardown_session_terminals,
    )
