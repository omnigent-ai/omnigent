"""Version compatibility for automated Claude model changes, with an inert TUI."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
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


def test_legacy_model_switch_keeps_composer_height_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    composer = _SessionModelComposer()
    composer.version = "2.1.322"
    bridge_dir = _session_model_bridge(tmp_path, composer)
    composer.install(monkeypatch)
    original_clear = bridge._clear_composer
    clear_attempts = 0
    recoveries = 0

    def clear(*args: object, **kwargs: object) -> None:
        nonlocal clear_attempts
        clear_attempts += 1
        if clear_attempts == 1:
            raise bridge.ClaudeComposerNotCleared("The composer is clipped")
        original_clear(*args, **kwargs)

    @contextmanager
    def recover(*_args: object) -> Iterator[bool]:
        nonlocal recoveries
        recoveries += 1
        assert composer.sends == []
        yield True

    monkeypatch.setattr(bridge, "_clear_composer", clear)
    monkeypatch.setattr(bridge, "_usable_composer_height", recover)

    result = bridge.inject_model_command(bridge_dir, model="private-glm[1m]")

    assert type(result).__name__ == "ClaudeLegacyModelSwitchResult"
    assert recoveries == 1 and clear_attempts == 2
    assert composer.model == composer.saved_default == "private-glm[1m]"
    assert [command for command in composer.submitted if command] == ["/model private-glm[1m]"]


@pytest.mark.parametrize("publication", ["lock-wait", "readiness-wait"])
def test_legacy_model_switch_rechecks_the_version_after_lock_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, publication: str
) -> None:
    composer = _SessionModelComposer()
    composer.version = "2.1.322"
    bridge_dir = _session_model_bridge(tmp_path, composer)
    composer.install(monkeypatch)
    original_read = bridge._read_json_file
    reads = 0

    def read(path: Path) -> dict[str, object] | None:
        nonlocal reads
        result = original_read(path)
        if path.name == bridge.CONTEXT_RAW_FILE:
            reads += 1
            if publication == "lock-wait" and reads > 1:
                assert result is not None
                return {**result, "version": "2.1.323"}
        return result

    monkeypatch.setattr(bridge, "_read_json_file", read)
    if publication == "readiness-wait":
        original_ready = bridge._reclaim_and_wait_for_prompt

        def ready(*args: object, **kwargs: object) -> None:
            original_ready(*args, **kwargs)
            path = bridge_dir / bridge.CONTEXT_RAW_FILE
            raw = json.loads(path.read_text())
            raw["version"] = "2.1.323"
            path.write_text(json.dumps(raw))

        monkeypatch.setattr(bridge, "_reclaim_and_wait_for_prompt", ready)

    with pytest.raises(bridge.ClaudeModelSwitchRefused, match="version changed"):
        bridge.inject_model_command(bridge_dir, model="private-glm[1m]")

    assert composer.sends == []
    assert composer.saved_default == composer.options[0]


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


@pytest.mark.parametrize("changed", ["advertisement", "target", "owner", "native-session"])
def test_late_legacy_confirmation_stops_when_the_receiving_pane_or_session_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    composer = _SessionModelComposer(cache_cost_label="Choice 1")
    composer.version = "2.1.322"
    bridge_dir = _session_model_bridge(tmp_path, composer)
    composer.install(monkeypatch)
    receipt = bridge.inject_model_command(bridge_dir, model="private-glm[1m]")
    assert isinstance(receipt, bridge.ClaudeLegacyModelSwitchResult)
    composer.overlay = "cache-cost"
    before = list(composer.sends)
    if changed in {"advertisement", "target"}:
        assert receipt.pane is not None
        bridge.write_tmux_target(
            bridge_dir,
            socket_path=Path(receipt.pane.socket_path),
            tmux_target="another:0.0" if changed == "target" else receipt.pane.tmux_target,
        )
    elif changed == "owner":
        bridge.write_active_session_id(bridge_dir, "another-web-session")
    else:
        (bridge_dir / "state.json").write_text('{"claude_session_id":"another-native-session"}')

    assert bridge.confirm_legacy_model_dialog(bridge_dir, receipt=receipt) is None
    assert composer.sends == before


def test_late_legacy_confirmation_rechecks_the_session_after_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    composer = _SessionModelComposer(cache_cost_label="Choice 1")
    composer.version = "2.1.322"
    bridge_dir = _session_model_bridge(tmp_path, composer)
    composer.install(monkeypatch)
    receipt = bridge.inject_model_command(bridge_dir, model="private-glm[1m]")
    assert isinstance(receipt, bridge.ClaudeLegacyModelSwitchResult)
    composer.overlay = "cache-cost"
    before = list(composer.sends)

    def capture(socket_path: str, target: str) -> str:
        pane = composer._capture_pane(socket_path, target)
        bridge.write_active_session_id(bridge_dir, "another-web-session")
        return pane

    monkeypatch.setattr(bridge, "_capture_pane", capture)
    with pytest.raises(bridge.ClaudeModelSwitchRefused, match="session changed"):
        bridge.confirm_legacy_model_dialog(bridge_dir, receipt=receipt)
    assert composer.sends == before
