"""Physical bridge/editor pairing without starting native processes."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import psutil
import pytest

from omnigent.harnesses.claude_native import bridge, editor_handoff, hook
from omnigent.harnesses.claude_native.editor_owner import NativeEditorProcess


@pytest.fixture
def binding_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "claude-native")
    root = bridge.prepare_bridge_dir("conversation", workspace=tmp_path)
    bridge.record_hook_event(
        root, {"hook_event_name": "SessionStart", "session_id": "native-session"}
    )
    bridge.write_tmux_target(root, socket_path=tmp_path / "tmux.sock", tmux_target="%7")
    binary = tmp_path / "claude"
    binary.touch()
    selected = NativeEditorProcess(71, 123.5, str(binary), "%7")
    pane = MagicMock(spec=psutil.Process)
    pane.pid = 60
    pane.create_time.return_value = 120.0
    pane.is_running.return_value = True
    native = MagicMock(spec=psutil.Process)
    native.pid = 71
    native.create_time.return_value = selected.created_at
    native.exe.return_value = selected.executable
    native.is_running.return_value = True
    native.parents.return_value = [pane]
    processes = MagicMock(side_effect=lambda pid: pane if pid == pane.pid else native)
    monkeypatch.setattr(
        editor_handoff, "psutil", SimpleNamespace(Process=processes, Error=psutil.Error)
    )
    observation = MagicMock(return_value=selected)
    monkeypatch.setattr(editor_handoff, "observe_native_editor_process", observation)
    result = SimpleNamespace(returncode=0, stdout="%7 60\n")
    query = MagicMock(return_value=result)
    monkeypatch.setattr(
        editor_handoff,
        "subprocess",
        SimpleNamespace(run=query, SubprocessError=subprocess.SubprocessError),
    )
    return SimpleNamespace(
        root=root,
        binary=binary,
        pane=pane,
        native=native,
        selected=selected,
        observation=observation,
        result=result,
        query=query,
        payload={"hook_event_name": "UserPromptSubmit", "session_id": "native-session"},
    )


def test_hook_and_editor_share_only_the_current_physical_bridge_owner(
    binding_context: SimpleNamespace,
) -> None:
    context = binding_context
    recorded = editor_handoff.record_editor_bridge_binding(
        context.root, context.payload, context.binary
    )
    assert recorded is not None
    assert recorded.owner.bridge_session_id == "conversation"
    assert recorded.owner.native_session_id == "native-session"
    assert recorded.owner.process == context.selected
    assert recorded.pane_pid == context.pane.pid
    assert recorded.pane_created_at == 120.0
    assert (context.root / "editor-owner.json").stat().st_mode & 0o777 == 0o600
    assert editor_handoff.read_editor_bridge_binding(context.root, context.binary) == recorded
    context.observation.reset_mock()
    assert editor_handoff.read_controller_editor_binding(context.root) == recorded
    context.observation.assert_not_called()


@pytest.mark.parametrize("reader", ["editor", "controller"])
@pytest.mark.parametrize(
    "change",
    [
        "bridge-session",
        "native-session",
        "native-incarnation",
        "pane-incarnation",
        "native-pid",
        "pane-id",
        "other-pane-tree",
        "advertisement",
        "native-gone",
        "tmux-unavailable",
        "malformed-record",
        "invalid-version",
        "rotation-during-observation",
    ],
)
def test_changed_or_unverified_owners_cannot_use_a_recorded_binding(
    monkeypatch: pytest.MonkeyPatch,
    binding_context: SimpleNamespace,
    change: str,
    reader: str,
) -> None:
    context = binding_context
    assert editor_handoff.record_editor_bridge_binding(
        context.root, context.payload, context.binary
    )
    if change == "bridge-session":
        bridge.write_active_session_id(context.root, "other-conversation")
    elif change == "native-session":
        bridge.record_hook_event(
            context.root, {"hook_event_name": "SessionStart", "session_id": "other-native"}
        )
    elif change == "native-incarnation":
        context.native.create_time.return_value += 1
    elif change == "pane-incarnation":
        context.pane.create_time.return_value += 1
    elif change == "native-pid":
        context.observation.return_value = replace(context.selected, pid=72)
        context.native.pid = 72
    elif change == "pane-id":
        context.result.stdout = "%8 60"
    elif change == "other-pane-tree":
        context.native.parents.return_value = []
    elif change == "advertisement":
        bridge.write_tmux_target(
            context.root, socket_path=context.root / "other.sock", tmux_target="%7"
        )
    elif change == "native-gone":
        context.observation.return_value = None
        context.native.is_running.return_value = False
    elif change == "tmux-unavailable":
        context.result.returncode = 1
    elif change == "malformed-record":
        (context.root / "editor-owner.json").write_text("{")
    elif change == "invalid-version":
        record = json.loads((context.root / "editor-owner.json").read_text())
        record["version"] = True
        (context.root / "editor-owner.json").write_text(json.dumps(record))
    else:
        monkeypatch.setattr(
            bridge,
            "read_active_session_id",
            MagicMock(side_effect=["conversation", "other-conversation"]),
        )
    if reader == "editor":
        assert editor_handoff.read_editor_bridge_binding(context.root, context.binary) is None
    else:
        assert editor_handoff.read_controller_editor_binding(context.root) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pid", True),
        ("pid", 2**40),
        ("created_at", True),
        ("created_at", float("nan")),
        ("created_at", float("inf")),
        ("created_at", 10**400),
        ("executable", "relative/claude"),
        ("executable", "/invalid\x00claude"),
        ("pane_id", "7"),
    ],
)
def test_invalid_stored_process_cannot_authorize_controller_observation(
    binding_context: SimpleNamespace, field: str, value: object
) -> None:
    context = binding_context
    assert editor_handoff.record_editor_bridge_binding(
        context.root, context.payload, context.binary
    )
    path = context.root / "editor-owner.json"
    record = json.loads(path.read_text())
    record["binding"]["owner"]["process"][field] = value
    path.write_text(json.dumps(record))
    context.query.reset_mock()
    assert editor_handoff.read_controller_editor_binding(context.root) is None
    context.query.assert_not_called()


def test_unexpected_metadata_and_unavailable_observation_fail_closed(
    monkeypatch: pytest.MonkeyPatch, binding_context: SimpleNamespace
) -> None:
    context = binding_context
    assert editor_handoff.record_editor_bridge_binding(
        context.root, context.payload, context.binary
    )
    path = context.root / "editor-owner.json"
    record = json.loads(path.read_text())
    path.write_text(json.dumps({**record, "extra": "unverified"}))
    assert editor_handoff.read_controller_editor_binding(context.root) is None
    path.write_text(json.dumps(record))
    monkeypatch.setattr(bridge, "read_active_session_id", MagicMock(side_effect=OSError))
    assert editor_handoff.read_controller_editor_binding(context.root) is None
    assert editor_handoff.read_editor_bridge_binding(context.root, context.binary) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"hook_event_name": "Stop", "session_id": "native-session"},
        {"hook_event_name": {}, "session_id": "native-session"},
        {"hook_event_name": "UserPromptSubmit", "session_id": "other-native"},
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "native-session",
            "agent_id": "child",
        },
        {
            "hook_event_name": "SessionStart",
            "session_id": "native-session",
            "transcript_path": "/synthetic/subagents/child.jsonl",
        },
    ],
)
def test_side_channel_hooks_cannot_record_editor_ownership(
    binding_context: SimpleNamespace, payload: dict[str, object]
) -> None:
    context = binding_context
    assert (
        editor_handoff.record_editor_bridge_binding(context.root, payload, context.binary) is None
    )
    assert not (context.root / "editor-owner.json").exists()


@pytest.mark.parametrize("configured", [False, True])
def test_recorder_only_observes_editor_ownership_when_explicitly_configured(
    monkeypatch: pytest.MonkeyPatch, binding_context: SimpleNamespace, configured: bool
) -> None:
    context = binding_context
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(context.payload)))
    recorder = MagicMock(return_value=None)
    monkeypatch.setattr(hook, "record_hook_payload", recorder)
    binding = MagicMock()
    monkeypatch.setattr(editor_handoff, "record_editor_bridge_binding", binding)
    arguments = ["--bridge-dir", str(context.root)]
    if configured:
        arguments += ["--native-editor-executable", str(context.binary)]
    assert hook.main(arguments) == 0
    recorder.assert_called_once_with(context.root, context.payload, conversation_url=None)
    if configured:
        binding.assert_called_once_with(context.root, context.payload, context.binary)
    else:
        binding.assert_not_called()
