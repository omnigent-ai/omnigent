"""Call the model control directly; no server, ASGI client or native CLI is started."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omnigent.harnesses.claude_native import bridge
from omnigent.runner import native_controls
from omnigent.runner.resource_registry import SessionResourceRegistry
from tests._helpers.providers import isolate_provider_resolution
from tests.harnesses.claude_native._model_switch_support import (
    _session_model_bridge,
    _SessionModelComposer,
)


@pytest.fixture
def model_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[native_controls.NativeControls, _SessionModelComposer, Path]:
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
    controls = native_controls.build_native_controls(
        _active_turns={},
        _background_tasks=set(),
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
    return controls, composer, bridge_dir


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
