"""Call the model control directly; no server, ASGI client or native CLI is started."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import pytest_asyncio

from omnigent.harnesses.claude_native import bridge
from omnigent.runner import native_controls
from omnigent.runner.resource_registry import SessionResourceRegistry
from tests._helpers.providers import isolate_provider_resolution
from tests.harnesses.claude_native._model_switch_support import (
    _session_model_bridge,
    _SessionModelComposer,
)


@pytest_asyncio.fixture
async def model_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[native_controls.NativeControls, _SessionModelComposer, Path]]:
    isolate_provider_resolution(monkeypatch, tmp_path)
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path)
    composer = _SessionModelComposer()
    bridge_dir = _session_model_bridge(tmp_path, composer)
    composer.install(monkeypatch)
    monkeypatch.setattr(bridge, "bridge_dir_for_bridge_id", lambda _: bridge_dir)
    monkeypatch.setattr(
        native_controls, "_claude_native_bridge_id_for_session", AsyncMock(return_value="bridge")
    )
    registry = Mock(spec=SessionResourceRegistry)
    registry.terminal_registry = None
    unused = Mock(side_effect=AssertionError("Unrelated runner control was invoked"))
    unused_async = AsyncMock(side_effect=AssertionError("Unrelated runner control was invoked"))
    background_tasks: set[asyncio.Task[Any]] = set()
    controls = native_controls.build_native_controls(
        _active_turns={},
        _background_tasks=background_tasks,
        _begin_turn_slot=unused,
        _claude_model_options_rows={},
        _codex_native_bridge_state_for_session=unused_async,
        _codex_native_bridge_state_and_dir_for_session=unused_async,
        _codex_terminal_ensure_locks={},
        _ensure_comment_relay_started=unused_async,
        _ensure_native_terminal_for_turn=unused_async,
        _fetch_session_model_override=unused_async,
        _ingest_cond={},
        _ingest_next_seq={},
        _ingest_now_serving={},
        _load_history_as_input=unused_async,
        _native_cost_popup_config_file=unused_async,
        _native_pane_status={},
        _opencode_terminal_ensure_locks={},
        _publish_event=unused,
        _publish_turn_status=unused_async,
        _release_session_harness=unused_async,
        _resolve_session_agent_spec=unused_async,
        _resolve_session_claude_launch_config=AsyncMock(return_value=None),
        _run_turn_bg=unused_async,
        _sdk_compact_inprogress=set(),
        _session_cursor_model_names={},
        _session_harness_name=unused,
        _session_histories={},
        _session_message_buffers={},
        _session_reasoning_effort={},
        _session_spec_cache={},
        resource_registry=registry,
        server_client=Mock(spec=httpx.AsyncClient),
    )
    try:
        yield controls, composer, bridge_dir
    finally:
        pending = list(background_tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize("version", ["2.1.322", "2.1.323", None])
async def test_model_control_accepts_the_original_two_event_fields(
    model_control: tuple[native_controls.NativeControls, _SessionModelComposer, Path],
    version: str | None,
) -> None:
    controls, composer, bridge_dir = model_control
    composer.version = version or "unknown"
    path = bridge_dir / bridge.CONTEXT_RAW_FILE
    raw = json.loads(path.read_text())
    if version is None:
        raw.pop("version")
    else:
        raw["version"] = version
    path.write_text(json.dumps(raw))
    event = {"type": "model_change", "model": "private-glm[1m]"}

    response = await controls.handle_claude_native_model_change("web-session", event["model"])

    assert response.status_code == 204
    assert composer.model == event["model"]
    assert composer.status_queries == int(version == "2.1.323")
    assert composer.saved_default == (
        composer.options[0] if version == "2.1.323" else event["model"]
    )


@pytest.mark.parametrize("version", ["2.1.322", "2.1.323"])
@pytest.mark.parametrize("selected", ["private/model-b[large]", "private-claude-opus[1m]"])
async def test_bound_model_control_keeps_the_literal_through_the_real_actuator(
    model_control: tuple[native_controls.NativeControls, _SessionModelComposer, Path],
    version: str,
    selected: str,
) -> None:
    from omnigent.inference_config import inference_config_scope

    controls, composer, bridge_dir = model_control
    composer.version = version
    composer.options[-1] = selected
    _session_model_bridge(bridge_dir.parent, composer)
    if version == "2.1.322":
        metadata = bridge_dir / "bridge.json"
        raw = json.loads(metadata.read_text())
        raw.pop("model_picker_values")
        raw.pop("model_picker_options")
        metadata.write_text(json.dumps(raw))
        if selected == "private-claude-opus[1m]":
            composer.options.append("opus")
    config = {
        "providers": {"gateway": {"kind": "gateway"}},
        "inference": {
            "harnesses": {
                "claude-native": {
                    "provider": "gateway",
                    "default_model": selected,
                    "model_allowlist": [selected],
                }
            }
        },
    }
    with inference_config_scope(config):
        response = await controls.handle_claude_native_model_change("web-session", selected)

    assert response.status_code == 204
    assert composer.model == selected
    if version == "2.1.322":
        assert composer.saved_default == selected
        assert [command for command in composer.submitted if command] == [f"/model {selected}"]
    else:
        assert composer.saved_default == composer.options[0]
        assert composer.submitted == ["/model", "/status"]


@pytest.mark.parametrize("result_kind", ["absent", "different-session-only", "different-legacy"])
async def test_model_control_rejects_a_missing_or_mismatched_result(
    model_control: tuple[native_controls.NativeControls, _SessionModelComposer, Path],
    monkeypatch: pytest.MonkeyPatch,
    result_kind: str,
) -> None:
    controls, composer, _ = model_control
    result = (
        None
        if result_kind == "absent"
        else bridge.ClaudeLegacyModelSwitchResult("another-model", "another-model")
        if result_kind == "different-legacy"
        else bridge.ClaudeModelSwitchResult("another-model", "x", "x", "x", "native-session")
    )
    monkeypatch.setattr(bridge, "inject_model_command", Mock(return_value=result))

    response = await controls.handle_claude_native_model_change("web-session", "private-glm[1m]")

    assert response.status_code == 503
    assert json.loads(response.body)["error"] == "claude_native_model_failed"
    assert composer.sends == []


async def test_legacy_submission_failure_reports_uncertainty_about_the_saved_default(
    model_control: tuple[native_controls.NativeControls, _SessionModelComposer, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controls, composer, bridge_dir = model_control
    path = bridge_dir / bridge.CONTEXT_RAW_FILE
    raw = json.loads(path.read_text())
    raw["version"] = "2.1.322"
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(bridge, "_verify_submit_accepted", Mock(return_value=False))

    response = await controls.handle_claude_native_model_change("web-session", "private-glm[1m]")

    assert response.status_code == 503
    error = json.loads(response.body)
    assert error["error"] == "claude_native_model_unconfirmed"
    assert "saved default" in error["detail"]
    assert composer.model == composer.saved_default == "private-glm[1m]"


async def test_busy_legacy_model_control_handles_a_confirmation_after_the_immediate_watch(
    model_control: tuple[native_controls.NativeControls, _SessionModelComposer, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controls, composer, bridge_dir = model_control
    composer.version = "2.1.322"
    composer.busy = True
    composer.legacy_dialog_delay_s = bridge._CONFIRM_DIALOG_TIMEOUT_S + 5
    _session_model_bridge(bridge_dir.parent, composer)
    monkeypatch.setattr(native_controls, "_CLAUDE_MODEL_LATE_DIALOG_POLL_S", 0.001)
    loop = asyncio.get_running_loop()
    confirmed = asyncio.Event()
    original_send = bridge._run_tmux

    def send(socket_path: str, *args: str) -> None:
        original_send(socket_path, *args)
        if composer.confirmations:
            loop.call_soon_threadsafe(confirmed.set)

    monkeypatch.setattr(bridge, "_run_tmux", send)
    response = await controls.handle_claude_native_model_change("web-session", "private-glm[1m]")

    assert response.status_code == 204
    assert composer.model == composer.options[0]
    assert composer.confirmations == 0
    assert bridge.time.monotonic() < composer._legacy_dialog_at
    bridge.time.sleep(composer.legacy_dialog_delay_s)
    await asyncio.wait_for(confirmed.wait(), timeout=2)

    assert composer.model == composer.saved_default == "private-glm[1m]"
    assert composer.confirmations == 1
    assert [command for command in composer.submitted if command] == ["/model private-glm[1m]"]


async def test_legacy_model_dialog_watch_has_a_finite_budget(
    model_control: tuple[native_controls.NativeControls, _SessionModelComposer, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controls, composer, bridge_dir = model_control
    composer.version = "2.1.322"
    _session_model_bridge(bridge_dir.parent, composer)
    monkeypatch.setattr(native_controls, "_CLAUDE_MODEL_LATE_DIALOG_BUDGET_S", 0)
    confirm = Mock(side_effect=AssertionError("An expired watcher drove the pane"))
    monkeypatch.setattr(bridge, "confirm_legacy_model_dialog", confirm)

    response = await controls.handle_claude_native_model_change("web-session", "private-glm[1m]")
    await asyncio.sleep(0)

    assert response.status_code == 204
    confirm.assert_not_called()


async def test_an_older_model_control_cannot_restart_its_watch_after_a_newer_request(
    model_control: tuple[native_controls.NativeControls, _SessionModelComposer, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controls, composer, bridge_dir = model_control
    composer.version = "2.1.322"
    _session_model_bridge(bridge_dir.parent, composer)
    receipt = bridge.inject_model_command(bridge_dir, model="private-glm[1m]")
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    requests = 0
    watches = 0

    async def drive(deadline: Any, function: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal requests, watches
        if function is bridge.inject_model_command:
            requests += 1
            if requests == 1:
                first_started.set()
                await release_first.wait()
            return receipt
        assert function is bridge.confirm_legacy_model_dialog
        watches += 1
        return True

    monkeypatch.setattr(native_controls, "_drive_claude_pane", drive)
    first = asyncio.create_task(
        controls.handle_claude_native_model_change("web-session", "private-glm[1m]")
    )
    try:
        await asyncio.wait_for(first_started.wait(), timeout=2)
        second = await controls.handle_claude_native_model_change("web-session", "private-glm[1m]")
        await asyncio.sleep(0)
        release_first.set()
        original = await asyncio.wait_for(first, timeout=2)
        await asyncio.sleep(0)
        assert original.status_code == second.status_code == 204
        assert watches == 1
    finally:
        release_first.set()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
