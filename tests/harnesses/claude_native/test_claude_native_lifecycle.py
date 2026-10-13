"""Lifecycle evidence survives real hook recording, rotations, and bounded history."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import FileLock

from omnigent._platform import stable_user_id
from omnigent.harnesses.claude_native import bridge, hook, lifecycle
from omnigent.inner.terminal_lifecycle import (
    TERMINAL_INSTANCE_ID_ENV,
    TERMINAL_LAUNCH_ID_ENV,
    TerminalLifecycleTrace,
)

_INSTANCE_ID = "a" * 32


@pytest.fixture
def launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, TerminalLifecycleTrace]:
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path)
    monkeypatch.setenv("OMNIGENT_HARNESS_STDERR_ENABLED", "0")
    directory = bridge.prepare_bridge_dir("session-a", workspace=tmp_path)
    trace = TerminalLifecycleTrace(session_id="session-a")
    for key, value in trace.launch_environment(_INSTANCE_ID).items():
        monkeypatch.setenv(key, value)
    return directory, trace


def _hook(
    monkeypatch: pytest.MonkeyPatch,
    directory: Path,
    name: str,
    at: float,
    *,
    session_id: str = "claude-a",
    **payload: object,
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(hook, "time", SimpleNamespace(**{**vars(time), "time": lambda: at}))
        patch.setattr(
            sys,
            "stdin",
            io.StringIO(
                json.dumps({"hook_event_name": name, "session_id": session_id, **payload})
            ),
        )
        assert hook.main(["--bridge-dir", str(directory)]) == 0


def _read(launch: tuple[Path, TerminalLifecycleTrace]) -> dict[str, object]:
    directory, trace = launch
    return lifecycle.read_lifecycle_snapshot(directory, _INSTANCE_ID, trace.launch_id)


@pytest.mark.parametrize(
    ("reason", "expected", "reason_status"),
    [
        ("prompt_input_exit", "prompt_input_exit", "reported"),
        ("clear", "clear", "reported"),
        ("logout", "logout", "reported"),
        ("session_close", "session_close", "reported"),
        ("signal", "signal", "reported"),
        ("unknown", "unknown", "reported"),
        (None, "unknown", "missing"),
        ("unexpected private reason", "unknown", "unrecognized"),
        ({"secret": "private-token"}, "unknown", "unrecognized"),
    ],
)
def test_session_end_is_source_timed_private_and_independent_of_debug_capture(
    launch: tuple[Path, TerminalLifecycleTrace],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reason: object,
    expected: str,
    reason_status: str,
) -> None:
    directory, trace = launch
    _hook(monkeypatch, directory, "SessionStart", 100.0, source="startup")
    _hook(monkeypatch, directory, "UserPromptSubmit", 101.0, prompt="private user prompt")
    _hook(monkeypatch, directory, "Stop", 102.0, last_assistant_message="private answer")
    before = (directory / "state.json").read_bytes()
    _hook(
        monkeypatch,
        directory,
        "SessionEnd",
        103.5,
        reason=reason,
        signal="SIGTERM",
        prompt="private user prompt",
        token="private-token",
        transcript_path="/private/workspace/transcript.jsonl",
    )

    snapshot = _read(launch)
    assert snapshot["session_end_reason"] == expected
    assert snapshot["session_end_evidence"] == "claude_hook"
    assert snapshot["session_end_identity"] == "matched"
    assert snapshot["hook_turn_in_progress"] is False
    end = snapshot["session_end"]
    assert end["recorded_at"] == 103.5
    assert end["timestamp_source"] == "hook_received"
    assert end["reason_status"] == reason_status
    assert end["signal"] == "SIGTERM"
    assert (directory / "state.json").read_bytes() == before
    assert "SessionEnd" not in (directory / "hooks.jsonl").read_text()
    path = directory / f"lifecycle-{trace.launch_id}.json"
    assert path.stat().st_mode & 0o777 == 0o600
    for private in (
        "private user prompt",
        "private answer",
        "private-token",
        "/private/workspace",
        "unexpected private reason",
    ):
        assert private not in path.read_text()
        assert private not in json.dumps(snapshot)
    assert capsys.readouterr().out == ""


def test_long_session_keeps_identity_anchor_outside_recent_history(
    launch: tuple[Path, TerminalLifecycleTrace], monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, _ = launch
    _hook(monkeypatch, directory, "SessionStart", 100.0, source="startup")
    for turn in range(lifecycle._MAX_EVENTS + 2):
        _hook(monkeypatch, directory, "UserPromptSubmit", 101.0 + turn * 2)
        _hook(monkeypatch, directory, "Stop", 102.0 + turn * 2)
    _hook(monkeypatch, directory, "SessionEnd", 200.0, reason="prompt_input_exit")

    snapshot = _read(launch)
    assert snapshot["session_end_reason"] == "prompt_input_exit"
    assert snapshot["session_end_identity"] == "matched"
    assert snapshot["claude_session_id"] == "claude-a"
    assert snapshot["session_start"]["recorded_at"] == 100.0
    assert snapshot["events_omitted"] > 0
    assert len(snapshot["recent_hooks"]) == lifecycle._MAX_EVENTS
    assert all(event["event_name"] != "SessionStart" for event in snapshot["recent_hooks"])


@pytest.mark.parametrize("turn_end", [None, "Stop", "StopFailure"])
def test_same_identity_compaction_preserves_active_or_ended_turn(
    launch: tuple[Path, TerminalLifecycleTrace],
    monkeypatch: pytest.MonkeyPatch,
    turn_end: str | None,
) -> None:
    directory, _ = launch
    _hook(monkeypatch, directory, "SessionStart", 100.0, source="startup")
    _hook(monkeypatch, directory, "UserPromptSubmit", 101.0)
    if turn_end is not None:
        _hook(monkeypatch, directory, turn_end, 102.0)
    _hook(monkeypatch, directory, "PreCompact", 103.0)
    _hook(monkeypatch, directory, "SessionStart", 104.0, source="compact")
    _hook(monkeypatch, directory, "SessionStart", 105.0, source="compact")
    _hook(monkeypatch, directory, "SessionEnd", 106.0, reason="signal")

    snapshot = _read(launch)
    assert snapshot["hook_turn_in_progress"] is (turn_end is None)
    assert snapshot["session_start"]["recorded_at"] == 105.0
    assert snapshot["session_start"]["identity_started_at"] == 100.0
    assert snapshot["session_end_reason"] == "signal"
    assert snapshot["session_end_identity"] == "matched"


@pytest.mark.parametrize(
    ("source", "session_id"),
    [("compact", "claude-b"), ("clear", "claude-a"), ("resume", "claude-a")],
)
def test_new_identity_or_non_compacting_start_resets_turn_context(
    launch: tuple[Path, TerminalLifecycleTrace],
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    session_id: str,
) -> None:
    directory, _ = launch
    _hook(monkeypatch, directory, "SessionStart", 100.0, source="startup")
    _hook(monkeypatch, directory, "UserPromptSubmit", 101.0)
    _hook(monkeypatch, directory, "SessionStart", 102.0, source=source, session_id=session_id)
    _hook(monkeypatch, directory, "SessionEnd", 103.0, session_id=session_id, reason="logout")

    snapshot = _read(launch)
    assert snapshot["claude_session_id"] == session_id
    assert snapshot["hook_turn_in_progress"] is None
    assert snapshot["session_start"]["identity_started_at"] == 102.0
    assert snapshot["session_end_reason"] == "logout"
    assert snapshot["session_end_identity"] == "matched"


@pytest.mark.parametrize(
    ("stored_omitted", "previous_omitted"), [(7, 7), (-4, 0), (True, 0), ("bad", 0), (None, 0)]
)
def test_malformed_tail_preserves_valid_end_and_counts_omissions_once(
    launch: tuple[Path, TerminalLifecycleTrace],
    monkeypatch: pytest.MonkeyPatch,
    stored_omitted: object,
    previous_omitted: int,
) -> None:
    directory, trace = launch
    _hook(monkeypatch, directory, "SessionStart", 100.0, source="startup")
    _hook(monkeypatch, directory, "UserPromptSubmit", 101.0)
    _hook(monkeypatch, directory, "Stop", 102.0)
    _hook(monkeypatch, directory, "SessionEnd", 103.0, reason="logout")
    end = _read(launch)["session_end"]
    path = directory / f"lifecycle-{trace.launch_id}.json"
    state = json.loads(path.read_text())
    older_valid = [
        {"event_name": "PreCompact", "recorded_at": 50.0 + i, "claude_session_id": "claude-a"}
        for i in range(15)
    ]
    malformed = [
        None,
        {},
        {"event_name": "SessionEnd", "recorded_at": 10**400},
        {"event_name": "SessionEnd", "recorded_at": False},
        {"event_name": "unsupported", "recorded_at": 104.0},
    ] * 4
    state["events"] = older_valid + state["events"] + malformed
    state["events_omitted"] = stored_omitted
    path.write_text(json.dumps(state))

    snapshot = _read(launch)
    assert snapshot["session_end_reason"] == "logout"
    assert snapshot["session_end"]["event_id"] == end["event_id"]
    assert len(snapshot["recent_hooks"]) == lifecycle._MAX_EVENTS
    # Twenty malformed and three excess valid records are omitted.
    assert snapshot["events_omitted"] == previous_omitted + 23
    assert _read(launch) == snapshot

    _hook(monkeypatch, directory, "SessionEnd", 104.0, reason="logout")
    snapshot = _read(launch)
    assert snapshot["session_end"]["event_id"] == end["event_id"]
    assert snapshot["session_end"]["observation_count"] == 2
    assert snapshot["events_omitted"] == previous_omitted + 23
    assert len(json.loads(path.read_text())["events"]) == lifecycle._MAX_EVENTS

    _hook(monkeypatch, directory, "PreCompact", 105.0)
    snapshot = _read(launch)
    assert snapshot["session_end_reason"] == "logout"
    assert snapshot["events_omitted"] == previous_omitted + 24


def test_contended_lifecycle_lock_is_bounded_and_session_end_can_recover(
    launch: tuple[Path, TerminalLifecycleTrace],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory, trace = launch
    _hook(monkeypatch, directory, "SessionStart", 100.0, source="startup")
    path = directory / f"lifecycle-{trace.launch_id}.json"
    with FileLock(str(path) + ".lock", mode=0o600, timeout=0):
        started = time.monotonic()
        _hook(monkeypatch, directory, "SessionEnd", 101.0, reason="logout")
        assert time.monotonic() - started < 3
    assert _read(launch)["session_end_reason"] == "unknown"
    assert "could not record hook evidence" in capsys.readouterr().err

    _hook(monkeypatch, directory, "SessionEnd", 102.0, reason="logout")
    assert _read(launch)["session_end_reason"] == "logout"


def test_lifecycle_lock_is_private_while_writing_evidence(
    launch: tuple[Path, TerminalLifecycleTrace], monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, trace = launch
    lifecycle_path = directory / f"lifecycle-{trace.launch_id}.json"
    lock_modes: list[int] = []
    write_json_file = bridge._write_json_file

    def write(path: Path, value: object) -> None:
        if path == lifecycle_path:
            lock_modes.append(path.with_suffix(".json.lock").stat().st_mode & 0o777)
        write_json_file(path, value)

    monkeypatch.setattr(bridge, "_write_json_file", write)
    _hook(monkeypatch, directory, "SessionEnd", 100.0, reason="logout")
    assert lock_modes == [0o600]
    assert _read(launch)["session_end_reason"] == "logout"


@pytest.mark.parametrize("source", ["clear", "resume"])
def test_rotations_and_delayed_old_hooks_do_not_supply_the_current_exit_reason(
    launch: tuple[Path, TerminalLifecycleTrace], monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    directory, _ = launch
    _hook(monkeypatch, directory, "SessionStart", 100.0, source="startup")
    _hook(monkeypatch, directory, "SessionEnd", 101.0, reason="clear")
    _hook(monkeypatch, directory, "SessionStart", 102.0, session_id="claude-b", source=source)
    _hook(monkeypatch, directory, "SessionEnd", 105.0, reason="prompt_input_exit")
    # An older invocation may finish after the new SessionStart has persisted.
    _hook(monkeypatch, directory, "SessionStart", 99.0, source="startup")
    assert _read(launch)["session_end_reason"] == "unknown"
    assert _read(launch)["claude_session_id"] == "claude-b"

    _hook(monkeypatch, directory, "SessionEnd", 106.0, session_id="claude-b", reason="logout")
    snapshot = _read(launch)
    assert snapshot["session_end_reason"] == "logout"
    assert snapshot["session_end"]["recorded_at"] == 106.0


def test_repeated_end_observations_keep_the_original_time_and_stable_event_id(
    launch: tuple[Path, TerminalLifecycleTrace], monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, _ = launch
    _hook(monkeypatch, directory, "SessionStart", 100.0, source="startup")
    _hook(monkeypatch, directory, "SessionEnd", 101.0, reason="signal")
    first = _read(launch)["session_end"]
    for repeat in range(lifecycle._MAX_EVENTS + 2):
        _hook(monkeypatch, directory, "SessionEnd", 102.0 + repeat, reason="signal")

    snapshot = _read(launch)
    assert snapshot["session_end"]["event_id"] == first["event_id"]
    assert snapshot["session_end"]["recorded_at"] == 101.0
    assert snapshot["session_end"]["observation_count"] == lifecycle._MAX_EVENTS + 3
    assert _read(launch) == snapshot


def test_source_capture_precedes_synchronous_session_rotation(
    launch: tuple[Path, TerminalLifecycleTrace], monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, _ = launch

    def rotate(_directory: Path) -> str:
        snapshot = _read(launch)
        assert snapshot["session_start"]["recorded_at"] == 100.0
        bridge.write_active_session_id(directory, "session-b")
        return "session-b"

    monkeypatch.setattr(hook, "_rotate_session_on_clear", rotate)
    _hook(monkeypatch, directory, "SessionStart", 100.0, source="clear")
    assert _read(launch)["session_start"]["bridge_session_id"] == "session-a"
    assert bridge.read_active_session_id(directory) == "session-b"


def test_launch_identity_separates_replacement_and_late_old_hooks(
    launch: tuple[Path, TerminalLifecycleTrace], monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, old = launch
    _hook(monkeypatch, directory, "SessionStart", 100.0, source="startup")
    replacement = TerminalLifecycleTrace(session_id="session-a")
    new_env = replacement.launch_environment("b" * 32)
    with monkeypatch.context() as patch:
        for key, value in new_env.items():
            patch.setenv(key, value)
        _hook(patch, directory, "SessionStart", 102.0, session_id="claude-b", source="startup")
    _hook(monkeypatch, directory, "SessionEnd", 103.0, reason="prompt_input_exit")

    current = lifecycle.read_lifecycle_snapshot(directory, "b" * 32, replacement.launch_id)
    assert current["session_end_reason"] == "unknown"
    assert _read(launch)["session_end_reason"] == "prompt_input_exit"
    assert (
        lifecycle.read_lifecycle_snapshot(directory, "b" * 32, old.launch_id)["read_status"]
        == "identity_mismatch"
    )


@pytest.mark.parametrize("missing", [TERMINAL_INSTANCE_ID_ENV, TERMINAL_LAUNCH_ID_ENV])
def test_unbound_hooks_cannot_be_attributed_to_the_current_launch(
    launch: tuple[Path, TerminalLifecycleTrace], monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    directory, _ = launch
    monkeypatch.delenv(missing)
    _hook(monkeypatch, directory, "SessionEnd", 100.0, reason="prompt_input_exit")
    assert _read(launch)["session_end_evidence"] == "not_observed"


def test_subagent_hooks_do_not_override_root_and_new_prompt_invalidates_prior_end(
    launch: tuple[Path, TerminalLifecycleTrace], monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, _ = launch
    _hook(monkeypatch, directory, "SessionStart", 100.0, source="startup")
    _hook(
        monkeypatch, directory, "SessionEnd", 101.0, reason="prompt_input_exit", agent_id="child-a"
    )
    assert _read(launch)["session_end_evidence"] == "not_observed"
    _hook(monkeypatch, directory, "SessionEnd", 102.0, reason="clear")
    _hook(monkeypatch, directory, "UserPromptSubmit", 103.0)
    assert _read(launch)["session_end_reason"] == "unknown"
    assert _read(launch)["hook_turn_in_progress"] is True


def test_end_without_start_is_preserved_as_unverified_identity(
    launch: tuple[Path, TerminalLifecycleTrace], monkeypatch: pytest.MonkeyPatch
) -> None:
    _hook(monkeypatch, launch[0], "SessionEnd", 100.0, reason="session_close")
    snapshot = _read(launch)
    assert snapshot["session_end_reason"] == "session_close"
    assert snapshot["session_end_identity"] == "unverified"


def test_unreadable_evidence_and_recording_failure_do_not_fail_session_end(
    launch: tuple[Path, TerminalLifecycleTrace],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory, trace = launch
    path = directory / f"lifecycle-{trace.launch_id}.json"
    path.write_text("{broken")
    assert _read(launch)["read_status"] == "unreadable"

    def fail(*_args: object) -> None:
        raise OSError("sensitive exception text")

    path.unlink()
    monkeypatch.setattr(bridge, "_write_json_file", fail)
    _hook(monkeypatch, directory, "SessionEnd", 100.0, reason="prompt_input_exit")
    output = capsys.readouterr()
    assert output.out == ""
    assert "sensitive exception text" not in output.err
    assert _read(launch)["session_end_reason"] == "unknown"


def test_prepare_bridge_dir_preserves_launch_evidence_until_bridge_cleanup(
    launch: tuple[Path, TerminalLifecycleTrace], monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, _ = launch
    _hook(monkeypatch, directory, "SessionStart", 100.0, source="startup")
    _hook(monkeypatch, directory, "SessionEnd", 101.0, reason="logout")
    original = _read(launch)
    transient = directory / "server.json"
    transient.write_text("{}")

    assert bridge.prepare_bridge_dir("session-a", workspace=directory.parent) == directory
    assert not transient.exists()
    assert not (directory / "state.json").exists()
    assert _read(launch) == original
    assert bridge.prune_orphaned_bridge_dirs() == 0

    monkeypatch.setattr("omnigent.inner.terminal._process_alive", lambda _pid: False)
    assert bridge.prune_orphaned_bridge_dirs() == 1
    assert not directory.exists()
    assert _read(launch)["read_status"] == "not_found"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo", "oversize"])
def test_lifecycle_reader_rejects_linked_nonregular_and_oversize_input(
    launch: tuple[Path, TerminalLifecycleTrace], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    directory, trace = launch
    _hook(monkeypatch, directory, "SessionStart", 100.0, source="startup")
    _hook(monkeypatch, directory, "SessionEnd", 101.0, reason="logout")
    path = directory / f"lifecycle-{trace.launch_id}.json"
    if kind == "oversize":
        path.write_bytes(path.read_bytes().ljust(lifecycle._MAX_BYTES, b" "))
        assert _read(launch)["session_end_reason"] == "logout"
        with path.open("ab") as stream:
            stream.write(b" ")
    else:
        target = path.with_name("linked-evidence.json")
        path.rename(target)
        if kind == "symlink":
            path.symlink_to(target)
        elif kind == "hardlink":
            path.hardlink_to(target)
        elif kind == "directory":
            path.mkdir()
        else:
            os.mkfifo(path, 0o600)

    snapshot = _read(launch)
    assert snapshot["read_status"] == "unreadable"
    assert snapshot["session_end_reason"] == "unknown"
    assert snapshot["session_end_evidence"] == "not_observed"


def test_generated_session_end_command_records_evidence_in_an_isolated_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(
        bridge, "_BRIDGE_ROOT", tmp_path / f"omnigent-{stable_user_id()}" / "claude-native"
    )
    directory = bridge.prepare_bridge_dir("synthetic-session", workspace=tmp_path)
    trace = TerminalLifecycleTrace(session_id="synthetic-session")
    for key, value in trace.launch_environment(_INSTANCE_ID).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("OMNIGENT_HARNESS_STDERR_ENABLED", "0")
    settings = bridge.build_hook_settings(directory)
    for name, fields in (
        ("SessionStart", {"source": "startup"}),
        ("SessionEnd", {"reason": "prompt_input_exit", "prompt": "private synthetic prompt"}),
    ):
        command = settings["hooks"][name][0]["hooks"][0]["command"]
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            input=json.dumps(
                {"hook_event_name": name, "session_id": "claude-synthetic", **fields}
            ),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""

    evidence = lifecycle.read_lifecycle_snapshot(directory, _INSTANCE_ID, trace.launch_id)
    assert evidence["session_end_reason"] == "prompt_input_exit"
    assert evidence["session_end_identity"] == "matched"
    assert evidence["launch_session_id"] == "synthetic-session"
    assert "private synthetic prompt" not in json.dumps(evidence)


def test_unknown_schema_is_not_used_or_overwritten(
    launch: tuple[Path, TerminalLifecycleTrace], monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, trace = launch
    _hook(monkeypatch, directory, "SessionStart", 100.0, source="startup")
    path = directory / f"lifecycle-{trace.launch_id}.json"
    state = json.loads(path.read_text())
    state["schema_version"] = 99
    path.write_text(json.dumps(state))
    assert _read(launch)["read_status"] == "unsupported_schema"
    _hook(monkeypatch, directory, "SessionEnd", 101.0, reason="prompt_input_exit")
    assert json.loads(path.read_text())["schema_version"] == 99


@pytest.mark.parametrize(
    ("content", "read_status"), [("[]", "unreadable"), ("{}", "unsupported_schema")]
)
def test_invalid_record_is_preserved_instead_of_treated_as_a_missing_file(
    launch: tuple[Path, TerminalLifecycleTrace],
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    read_status: str,
) -> None:
    directory, trace = launch
    path = directory / f"lifecycle-{trace.launch_id}.json"
    path.write_text(content)
    assert _read(launch)["read_status"] == read_status
    _hook(monkeypatch, directory, "SessionEnd", 101.0, reason="logout")
    assert path.read_text() == content
    assert _read(launch)["session_end_reason"] == "unknown"
    assert _read(launch)["session_end_reason"] == "unknown"
