"""Version compatibility for automated Claude model changes, with an inert TUI."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnigent.harnesses.claude_native import bridge
from tests.harnesses.claude_native._model_switch_support import (
    _session_model_bridge,
    _SessionModelComposer,
)


@pytest.fixture(autouse=True)
def _trust_test_bridge_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path)


@pytest.mark.parametrize(
    ("version", "session_only"),
    [
        ("2.1.302", False),
        ("2.1.311", False),
        ("2.1.322", False),
        ("2.1.323", True),
        ("2.1.324", True),
        ("2.2.0", True),
        ("3.0.0", True),
        ("2.1.323-preview", False),
        (None, False),
    ],
)
@pytest.mark.parametrize("busy", [False, True], ids=["idle", "busy"])
def test_model_changes_preserve_the_version_appropriate_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str | None,
    session_only: bool,
    busy: bool,
) -> None:
    composer = _SessionModelComposer(busy=busy)
    composer.version = version or "unknown"
    bridge_dir = _session_model_bridge(tmp_path, composer)
    if version is None:
        path = bridge_dir / bridge.CONTEXT_RAW_FILE
        raw = json.loads(path.read_text())
        raw.pop("version")
        path.write_text(json.dumps(raw))
    initial_default = composer.saved_default
    composer.install(monkeypatch)

    result = bridge.inject_model_command(bridge_dir, model="private-glm[1m]")

    assert result.requested_model == composer.model == "private-glm[1m]"
    if session_only:
        assert isinstance(result, bridge.ClaudeModelSwitchResult)
        assert result.status_model == composer.model
        assert result.session_id == composer.session
        assert composer.saved_default == initial_default
        assert composer.status_queries == 1
        assert composer.submitted == ["/model", "/status"]
        assert [args[-1] for args in composer.sends].count("s") == 1
    else:
        assert type(result).__name__ == "ClaudeLegacyModelSwitchResult"
        assert not isinstance(result, bridge.ClaudeModelSwitchResult)
        assert composer.saved_default == composer.model
        assert composer.status_queries == 0
        assert [command for command in composer.submitted if command] == ["/model private-glm[1m]"]
        assert "s" not in [args[-1] for args in composer.sends]


def test_legacy_bridge_metadata_needs_no_reregistration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    composer = _SessionModelComposer()
    target = composer.options[-1] = "claude-opus-4-8[1m]"
    bridge_dir = _session_model_bridge(tmp_path, composer)
    (bridge_dir / "bridge.json").write_text("{}")
    (bridge_dir / "state.json").unlink()
    (bridge_dir / bridge.CONTEXT_RAW_FILE).unlink()
    composer.install(monkeypatch)

    result = bridge.inject_model_command(bridge_dir, model=target)

    assert type(result).__name__ == "ClaudeLegacyModelSwitchResult"
    assert result.picker_value == composer.model == composer.saved_default == target
    assert [command for command in composer.submitted if command] == [f"/model {target}"]
    assert composer.status_queries == 0


@pytest.mark.parametrize("version", ["2.1.322", "2.1.323"])
def test_direct_model_commands_keep_the_version_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    composer = _SessionModelComposer()
    composer.version = version
    bridge_dir = _session_model_bridge(tmp_path, composer)
    composer.install(monkeypatch)

    if version == "2.1.323":
        with pytest.raises(bridge.ClaudeModelSwitchRefused, match="session-only picker"):
            bridge.inject_slash_command(bridge_dir, command="/model private-glm[1m]")
        assert composer.sends == []
    else:
        bridge.inject_slash_command(bridge_dir, command="/model private-glm[1m]")
        assert composer.model == composer.saved_default == "private-glm[1m]"
        assert composer.submitted == ["/model private-glm[1m]"]


@pytest.mark.parametrize("failure", ["catalog", "picker", "status"])
def test_supported_selection_failure_never_retries_as_a_legacy_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    composer = _SessionModelComposer()
    bridge_dir = _session_model_bridge(tmp_path, composer)
    composer.install(monkeypatch)
    legacy = Mock(side_effect=AssertionError("A supported selection must not downgrade"))
    monkeypatch.setattr(bridge, "_inject_slash_command_locked", legacy)

    def capture(socket_path: str, target: str) -> str:
        pane = composer._capture_pane(socket_path, target)
        if failure == "picker" and composer.overlay == "picker":
            return pane.replace("s to use this session only", "session-only unavailable")
        if failure == "status" and composer.overlay == "status":
            return pane.replace("Model: private-glm[1m]", "Model: another-model")
        return pane

    monkeypatch.setattr(bridge, "_capture_pane", capture)
    initial_default = composer.saved_default
    with pytest.raises(bridge.ClaudeModelSwitchRefused):
        bridge.inject_model_command(
            bridge_dir,
            model="absent-private-model" if failure == "catalog" else "private-glm[1m]",
        )

    legacy.assert_not_called()
    assert composer.saved_default == initial_default
    assert not any(command.startswith("/model ") for command in composer.submitted)


@pytest.mark.parametrize("failure", ["unsupported-model", "cancelled", "after-submission"])
def test_legacy_failure_never_authorizes_a_dependent_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    composer = _SessionModelComposer()
    composer.version = "2.1.322"
    bridge_dir = _session_model_bridge(tmp_path, composer)
    composer.install(monkeypatch)
    cancelled = threading.Event()
    if failure == "cancelled":
        cancelled.set()
    if failure == "after-submission":
        monkeypatch.setattr(bridge, "_verify_submit_accepted", Mock(return_value=False))

    with (
        bridge.cancellable_injection(cancelled),
        pytest.raises(bridge.ClaudeModelSwitchRefused) as caught,
    ):
        bridge.inject_model_command(
            bridge_dir,
            model="absent-private-model" if failure == "unsupported-model" else "private-glm[1m]",
        )

    assert caught.value.model_may_have_changed is (failure == "after-submission")
    if failure == "after-submission":
        assert "legacy" in str(caught.value)
        assert "saved default" in str(caught.value)
        assert composer.model == composer.saved_default == "private-glm[1m]"
    else:
        assert composer.sends == []
