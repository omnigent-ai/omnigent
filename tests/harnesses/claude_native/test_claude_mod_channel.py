"""Tests for the Claude Code mod channel: protocol, control endpoints and the shipped mod."""

from __future__ import annotations

import contextlib
import json
import os
import queue
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent._platform import resolve_cli_binary
from omnigent.harnesses.claude_native import bridge as claude_bridge
from omnigent.harnesses.claude_native import mod_channel
from omnigent.harnesses.claude_native.mod_channel import (
    ModCommandOutcome,
    ModCommandQueue,
    may_fall_back_to_tmux,
    normalize_mod_command,
    normalize_mod_event,
    normalize_mod_hello,
    normalize_mod_result,
)
from omnigent.onboarding.harness_install import binary_meets_min_version


@pytest.fixture
def mod_bridge() -> Iterator[Path]:
    """
    Yield a bridge directory served by a real serve-mcp control endpoint.

    The endpoint is the in-process ``_start_http_ingress`` on a Unix socket, so
    requests cross the same transport as the mod does. The bridge lives under
    the production bridge root, which the directory validation requires.

    :yields: Bridge directory whose ``server.json`` advertises the endpoint.
    """
    production_root = (
        Path(tempfile.gettempdir())
        / f"omnigent-{claude_bridge.stable_user_id()}"
        / "claude-native"
    )
    production_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(production_root.parent, 0o700)
    os.chmod(production_root, 0o700)
    with tempfile.TemporaryDirectory(prefix="mod-channel-", dir=production_root) as raw:
        bridge_dir = Path(raw)
        httpd = claude_bridge._start_http_ingress(bridge_dir, _TOKEN, queue.Queue())
        try:
            yield bridge_dir
        finally:
            httpd.shutdown()
            httpd.server_close()


_TOKEN = secrets.token_urlsafe(16)
_HELLO = {
    "v": 1,
    "mod_version": "0.1.0",
    "claude_version": "2.1.308",
    "claude_session_id": "session-1",
    "main_turn_id": None,
}


def _request(
    bridge_dir: Path,
    method: str,
    path: str,
    payload: object | None = None,
    *,
    token: str = _TOKEN,
) -> tuple[int, bytes]:
    server = claude_bridge._read_json_file(bridge_dir / claude_bridge._SERVER_FILE)
    connection = claude_bridge._control_connection(server, timeout=10.0)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = None if payload is None else json.dumps(payload)
    try:
        # A refused request is answered and closed before its body is read.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _post_hello(bridge_dir: Path, **overrides: object) -> None:
    status, body = _request(bridge_dir, "POST", "/mod/hello", {**_HELLO, **overrides})
    assert status == 200, body


def _run_fake_mod(
    bridge_dir: Path,
    *,
    answer: dict[str, object],
    received: list[dict[str, object]] | None = None,
) -> threading.Thread:
    """Start a thread that takes one command from the long poll and posts *answer* for it."""

    def run() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status, body = _request(bridge_dir, "GET", "/mod/next")
            if status == 200:
                command = json.loads(body)
                if received is not None:
                    received.append(command)
                _request(
                    bridge_dir,
                    "POST",
                    "/mod/result",
                    {"v": 1, "id": command["id"], **answer},
                )
                return

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


# --- Protocol --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("operation", "arguments", "expected"),
    [
        ("submit", {"text": "hello"}, {"text": "hello"}),
        ("abort", {}, {}),
        ("model", {"model": "sonnet"}, {"model": "sonnet"}),
        ("model", {"model": "Opus[1M]"}, {"model": "opus[1m]"}),
        ("effort", {"level": "low"}, {"level": "low"}),
        ("compact", {}, {}),
        ("compact", {"instructions": "keep the plan"}, {"instructions": "keep the plan"}),
    ],
)
def test_normalize_mod_command_accepts_allowlisted_operations(
    operation: str, arguments: dict[str, object], expected: dict[str, object]
) -> None:
    assert normalize_mod_command(operation, arguments) == (operation, expected)


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("run_slash", {"command": "exit"}),
        ("submit", {}),
        ("submit", {"text": ""}),
        ("submit", {"text": "bad\x00byte"}),
        ("submit", {"text": "x", "extra": 1}),
        ("abort", {"turn": "x"}),
        ("model", {"model": "sonnet; rm -rf ~"}),
        ("model", {"model": "two words"}),
        # The mod switches through Claude's config row, which takes aliases only.
        ("model", {"model": "system.ai.claude-haiku-5-5"}),
        ("model", {"model": "claude-opus-4-7"}),
        ("effort", {"level": "extreme"}),
        ("compact", {"instructions": "x" * 20_000}),
        ("submit", ["not", "an", "object"]),
    ],
)
def test_normalize_mod_command_refuses_everything_outside_the_allowlist(
    operation: str, arguments: object
) -> None:
    with pytest.raises(ValueError):
        normalize_mod_command(operation, arguments)


def test_normalize_mod_hello_requires_the_main_turn_field() -> None:
    assert normalize_mod_hello({**_HELLO, "main_turn_id": "turn-7"})["main_turn_id"] == "turn-7"
    assert normalize_mod_hello(_HELLO)["main_turn_id"] is None
    without_field = {key: value for key, value in _HELLO.items() if key != "main_turn_id"}
    with pytest.raises(ValueError, match="main_turn_id"):
        normalize_mod_hello(without_field)
    with pytest.raises(ValueError):
        normalize_mod_hello({**_HELLO, "v": 2})


def test_normalize_mod_event_checks_kind_and_reason() -> None:
    start = {
        "v": 1,
        "kind": "turn.start",
        "claude_session_id": "s",
        "turn_id": "t",
        "agent_id": None,
    }
    assert normalize_mod_event(start) == {
        "kind": "turn.start",
        "claude_session_id": "s",
        "turn_id": "t",
        "agent_id": None,
    }
    complete = {**start, "kind": "turn.complete", "agent_id": "sub-1", "reason": "aborted"}
    assert normalize_mod_event(complete)["reason"] == "aborted"
    with pytest.raises(ValueError, match="kind"):
        normalize_mod_event({**start, "kind": "tool.call"})
    with pytest.raises(ValueError, match="reason"):
        normalize_mod_event({**complete, "reason": "timeout"})


def test_normalize_mod_result_checks_the_version_command_id_and_shape() -> None:
    command_id = "0" * 32
    result = {"v": 1, "id": command_id, "ok": True, "result": {"ended": True}}
    assert normalize_mod_result(result) == (command_id, True, {"ended": True}, None)
    with pytest.raises(ValueError):
        normalize_mod_result({**result, "id": "not-hex"})
    with pytest.raises(ValueError):
        normalize_mod_result({"v": 1, "id": command_id, "ok": False})
    with pytest.raises(ValueError, match="version"):
        normalize_mod_result({**result, "v": 2})


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("ok", False),
        ("rejected", True),
        ("not_started", True),
        ("unanswered", False),
    ],
)
def test_only_undelivered_outcomes_may_fall_back_to_tmux(status: str, expected: bool) -> None:
    outcome = ModCommandOutcome(status=status)  # type: ignore[arg-type]
    assert may_fall_back_to_tmux(outcome) is expected


def test_state_and_event_records_follow_the_contract_keys() -> None:
    hello = normalize_mod_hello({**_HELLO, "main_turn_id": "turn-1"})
    state = mod_channel.mod_state_record(hello, server_pid=4321, now=100.5)
    assert set(state) == {
        "v",
        "mod_version",
        "claude_version",
        "claude_session_id",
        "main_turn_id",
        "server_pid",
        "updated_at",
    }
    event = normalize_mod_event(
        {
            "v": 1,
            "kind": "turn.complete",
            "claude_session_id": "s",
            "turn_id": "t",
            "agent_id": None,
            "reason": "answer",
        }
    )
    record = mod_channel.mod_event_record(event, now=200.25)
    assert record == {
        "v": 1,
        "kind": "turn.complete",
        "at": 200.25,
        "claude_session_id": "s",
        "turn_id": "t",
        "agent_id": None,
        "reason": "answer",
    }


# --- Command queue ---------------------------------------------------------------


def test_queue_hands_over_the_command_and_returns_the_mod_result() -> None:
    queue_ = ModCommandQueue()
    pending = queue_.enqueue("abort", {})
    assert queue_.take(hold_s=0.5) is pending
    assert queue_.finish(pending.command_id, ok=True, result={"ended": True}, error=None)
    assert queue_.await_outcome(pending, timeout_s=0.5) == ModCommandOutcome(
        status="ok", result={"ended": True}
    )


def test_queue_reports_a_refused_command_as_rejected() -> None:
    queue_ = ModCommandQueue()
    pending = queue_.enqueue("submit", {"text": "hi"})
    queue_.take(hold_s=0.5)
    queue_.finish(pending.command_id, ok=False, result={}, error="busy: a turn is running")
    outcome = queue_.await_outcome(pending, timeout_s=0.5)
    assert outcome.status == "rejected"
    assert outcome.error == "busy: a turn is running"


def test_queue_abandons_a_command_the_mod_never_took() -> None:
    queue_ = ModCommandQueue()
    pending = queue_.enqueue("model", {"model": "haiku"})
    outcome = queue_.await_outcome(pending, timeout_s=0.01)
    assert outcome.status == "not_started"
    assert queue_.take(hold_s=0.01) is None


def test_queue_reports_a_taken_command_without_an_answer_as_unanswered() -> None:
    queue_ = ModCommandQueue()
    pending = queue_.enqueue("abort", {})
    queue_.take(hold_s=0.5)
    outcome = queue_.await_outcome(pending, timeout_s=0.01)
    assert outcome.status == "unanswered"
    assert queue_.finish(pending.command_id, ok=True, result={}, error=None) is False


def test_queue_requeues_a_command_whose_poll_response_was_lost() -> None:
    queue_ = ModCommandQueue()
    pending = queue_.enqueue("compact", {})
    taken = queue_.take(hold_s=0.5)
    assert taken is pending
    queue_.hand_back(pending)
    assert queue_.take(hold_s=0.5) is pending


# --- Installed copy, version gate and the shipped mod ----------------------------


def test_install_copies_only_the_shipped_plugin_files_and_writes_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "package"
    shutil.copytree(mod_channel.MOD_PACKAGE_DIR, package)
    (package / "tsconfig.json").write_text("{}")
    (package / "types").mkdir()
    (package / "types" / "index.d.ts").write_text("")
    (package / "hooks" / "tsconfig.json").write_text("{}")
    monkeypatch.setattr(mod_channel, "MOD_PACKAGE_DIR", package)
    # A copy an older install left behind.
    earlier = tmp_path / "bridge" / "claude-mod" / "hooks"
    earlier.mkdir(parents=True)
    (earlier / "retired.ts").write_text("")

    installed = mod_channel.install_claude_mod(tmp_path / "bridge")

    assert installed == tmp_path / "bridge" / "claude-mod"
    assert not (installed / "hooks" / "retired.ts").exists()
    manifest = json.loads((installed / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "omnigent-bridge"
    assert set(manifest) == {"name", "version", "description"}
    assert (installed / "hooks" / "hooks.json").is_file()
    assert (installed / "hooks" / "register.ts").is_file()
    assert not (installed / "tsconfig.json").exists()
    assert not (installed / "types").exists()
    assert not (installed / "hooks" / "tsconfig.json").exists()


def test_launch_without_the_packaged_mod_files_skips_the_mod(
    mod_bridge: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    package = tmp_path / "package"
    (package / "hooks").mkdir(parents=True)
    (package / "hooks" / "hooks.json").write_text("{}")
    monkeypatch.setattr(mod_channel, "MOD_PACKAGE_DIR", package)
    bridge_dir = mod_bridge / "partial-install"
    bridge_dir.mkdir()

    with pytest.raises(FileNotFoundError, match=r"register\.ts"):
        mod_channel.install_claude_mod(bridge_dir)
    with caplog.at_level("WARNING", logger=claude_bridge.__name__):
        args = claude_bridge.augment_claude_args(
            (), bridge_dir=bridge_dir, python_executable="/venv/bin/python", claude_mod=True
        )

    assert "--plugin-dir" not in args
    assert "launching without the mod" in caplog.text


def test_shipped_mod_has_no_dot_folder_for_git_to_ignore() -> None:
    """The repository ignores dot-folders (``.gitignore`` ``.*/``), so none may ship."""
    dot_folders = [path for path in mod_channel.MOD_PACKAGE_DIR.rglob(".*") if path.is_dir()]

    assert dot_folders == []


def test_mod_launch_needs_the_feature_flag_and_the_version_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod_channel, "claude_cli_supports_mod", lambda binary: binary == "new")

    assert mod_channel.claude_mod_enabled_for_launch(feature_enabled=True, claude_binary="new")
    assert not mod_channel.claude_mod_enabled_for_launch(
        feature_enabled=False, claude_binary="new"
    )
    assert not mod_channel.claude_mod_enabled_for_launch(feature_enabled=True, claude_binary="old")
    assert not mod_channel.claude_mod_enabled_for_launch(feature_enabled=True, claude_binary=None)


def test_version_floor_compares_the_reported_claude_version(tmp_path: Path) -> None:
    for reported in ("2.1.300", "2.1.301"):
        binary = tmp_path / f"claude-{reported}"
        binary.write_text(f"#!/bin/sh\necho '{reported} (Claude Code)'\n")
        binary.chmod(0o755)
        expected = reported == "2.1.301"
        assert binary_meets_min_version(str(binary), "2.1.301") is expected


def _claude_at_or_above_mod_floor() -> str | None:
    binary = resolve_cli_binary("claude")
    if binary is None:
        return None
    if not binary_meets_min_version(binary, mod_channel._CLAUDE_MOD_MIN_VERSION):
        return None
    return binary


def test_shipped_mod_passes_claude_plugin_validate(tmp_path: Path) -> None:
    binary = _claude_at_or_above_mod_floor()
    if binary is None:
        pytest.skip(f"needs Claude Code >= {mod_channel._CLAUDE_MOD_MIN_VERSION} installed")
    installed = mod_channel.install_claude_mod(tmp_path / "bridge")

    result = subprocess.run(
        [binary, "plugin", "validate", str(installed)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Validation passed" in result.stdout


# --- Control endpoints on the real serve-mcp transport ---------------------------


def test_hello_writes_mod_json_and_marks_the_channel_live(mod_bridge: Path) -> None:
    _post_hello(mod_bridge, main_turn_id="turn-9")

    state = claude_bridge._read_json_file(mod_bridge / mod_channel.MOD_STATE_FILE)
    assert state["v"] == 1
    assert state["main_turn_id"] == "turn-9"
    assert state["server_pid"] == os.getpid()
    assert claude_bridge.mod_channel_live(mod_bridge)
    assert not claude_bridge.mod_channel_live(
        mod_bridge, now=float(state["updated_at"]) + mod_channel.MOD_LIVE_MAX_AGE_S + 1
    )


def test_mod_endpoints_refuse_a_wrong_token(mod_bridge: Path) -> None:
    status, _ = _request(mod_bridge, "POST", "/mod/hello", _HELLO, token="wrong")
    assert status == 401
    # Larger than a socket buffer, so the refusal must come before the body is read.
    padded = {**_HELLO, "padding": "x" * (mod_channel.MOD_MAX_BODY_BYTES - 1024)}
    assert _request(mod_bridge, "POST", "/mod/hello", padded, token="wrong")[0] == 401
    assert not (mod_bridge / mod_channel.MOD_STATE_FILE).exists()
    assert _request(mod_bridge, "GET", "/mod/next", token="wrong")[0] == 401


def test_mod_post_with_a_malformed_length_is_a_bad_request(mod_bridge: Path) -> None:
    server = claude_bridge._read_json_file(mod_bridge / claude_bridge._SERVER_FILE)
    connection = claude_bridge._control_connection(server, timeout=10.0)
    try:
        connection.putrequest("POST", "/mod/hello")
        connection.putheader("Authorization", f"Bearer {_TOKEN}")
        connection.putheader("Content-Length", "twelve")
        connection.endheaders()
        status = connection.getresponse().status
    finally:
        connection.close()

    assert status == 400


def test_events_are_appended_one_line_each_and_invalid_ones_refused(mod_bridge: Path) -> None:
    start = {
        "v": 1,
        "kind": "turn.start",
        "claude_session_id": "s",
        "turn_id": "t-1",
        "agent_id": None,
    }
    complete = {**start, "kind": "turn.complete", "reason": "aborted"}
    assert _request(mod_bridge, "POST", "/mod/event", start)[0] == 200
    assert _request(mod_bridge, "POST", "/mod/event", complete)[0] == 200
    assert _request(mod_bridge, "POST", "/mod/event", {**start, "kind": "nope"})[0] == 400

    lines = (mod_bridge / mod_channel.MOD_EVENTS_FILE).read_text().splitlines()
    records = [json.loads(line) for line in lines]
    assert [record["kind"] for record in records] == ["turn.start", "turn.complete"]
    assert records[1]["reason"] == "aborted"
    assert records[0]["agent_id"] is None
    assert all(record["v"] == 1 and isinstance(record["at"], float) for record in records)


def test_idle_long_poll_answers_no_content(
    mod_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod_channel, "MOD_POLL_HOLD_S", 0.2)

    status, body = _request(mod_bridge, "GET", "/mod/next")

    assert status == 204
    assert body == b""


def test_harness_abort_round_trips_through_the_mod(mod_bridge: Path) -> None:
    _post_hello(mod_bridge)
    fake_mod = _run_fake_mod(mod_bridge, answer={"ok": True, "result": {"ended": True}})

    outcome = claude_bridge.send_mod_command(mod_bridge, "abort", {}, timeout_s=5.0)

    fake_mod.join(timeout=5)
    assert outcome == ModCommandOutcome(status="ok", result={"ended": True})


def test_harness_command_the_mod_refuses_reaches_the_caller_as_rejected(mod_bridge: Path) -> None:
    _post_hello(mod_bridge)
    fake_mod = _run_fake_mod(mod_bridge, answer={"ok": False, "error": "busy: a turn is running"})

    outcome = claude_bridge.send_mod_command(
        mod_bridge, "submit", {"text": "hello"}, timeout_s=5.0
    )

    fake_mod.join(timeout=5)
    assert outcome.status == "rejected"
    assert outcome.error == "busy: a turn is running"
    assert may_fall_back_to_tmux(outcome)


def test_command_nobody_takes_is_not_started(mod_bridge: Path) -> None:
    _post_hello(mod_bridge)

    outcome = claude_bridge.send_mod_command(mod_bridge, "effort", {"level": "low"}, timeout_s=0.2)

    assert outcome.status == "not_started"
    assert may_fall_back_to_tmux(outcome)


def test_command_outside_the_allowlist_is_refused_before_queueing(
    mod_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod_channel, "MOD_POLL_HOLD_S", 0.2)
    _post_hello(mod_bridge)

    outcome = claude_bridge.send_mod_command(
        mod_bridge, "run_slash", {"command": "exit"}, timeout_s=0.2
    )

    assert outcome.status == "not_started"
    assert _request(mod_bridge, "GET", "/mod/next")[0] == 204


def test_command_without_a_live_mod_never_connects(mod_bridge: Path) -> None:
    outcome = claude_bridge.send_mod_command(mod_bridge, "abort", {}, timeout_s=0.2)

    assert outcome.status == "not_started"
    assert "not live" in (outcome.error or "")


def test_pane_is_captured_only_once_the_mod_is_live(
    mod_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a live mod the pane is never read, so the tmux path costs what it did."""
    probed: list[Path] = []

    def pane_with_a_dialog(bridge_dir: Path) -> bool:
        probed.append(bridge_dir)
        return False

    monkeypatch.setattr(claude_bridge, "claude_pane_ready", pane_with_a_dialog)

    before = claude_bridge.send_mod_command_if_pane_ready(mod_bridge, "compact", {}, timeout_s=0.2)
    _post_hello(mod_bridge)
    after = claude_bridge.send_mod_command_if_pane_ready(mod_bridge, "compact", {}, timeout_s=0.2)

    assert before.status == "not_started"
    assert "not live" in (before.error or "")
    assert after.status == "not_started"
    assert "dialog" in (after.error or "")
    assert probed == [mod_bridge]


@pytest.mark.parametrize(
    ("status_code", "body", "expected"),
    [
        (400, b'{"error":"unknown mod operation"}', "not_started"),
        (401, b"<html>Unauthorized</html>", "not_started"),
        (404, b"<html>Not Found</html>", "not_started"),
        (413, b"<html>Request Entity Too Large</html>", "not_started"),
        (200, b'{"status":"ok","result":{},"error":null}', "ok"),
        (200, b"<html>torn</html>", "unanswered"),
        (500, b"<html>Internal Server Error</html>", "unanswered"),
    ],
)
def test_only_refusals_before_queueing_count_as_not_started(
    status_code: int, body: bytes, expected: str
) -> None:
    assert claude_bridge._outcome_from_wire(status_code, body).status == expected


def test_submit_of_non_ascii_text_near_the_cap_reaches_the_mod(mod_bridge: Path) -> None:
    """Text inside the submit cap must fit the body cap, so it travels as raw UTF-8."""
    text = "\U0001f600" * (mod_channel.MOD_SUBMIT_MAX_BYTES // 4)
    _post_hello(mod_bridge)
    received: list[dict[str, object]] = []
    fake_mod = _run_fake_mod(mod_bridge, answer={"ok": True, "result": {}}, received=received)

    outcome = claude_bridge.send_mod_command(mod_bridge, "submit", {"text": text}, timeout_s=5.0)

    fake_mod.join(timeout=5)
    assert outcome.status == "ok"
    assert received[0]["args"] == {"text": text}


def test_result_for_an_unknown_command_is_not_found(mod_bridge: Path) -> None:
    status, _ = _request(mod_bridge, "POST", "/mod/result", {"v": 1, "id": "0" * 32, "ok": True})
    assert status == 404


def test_augment_loads_the_mod_only_when_the_launch_asks_for_it(mod_bridge: Path) -> None:
    without_dir = mod_bridge / "without"
    with_dir = mod_bridge / "with"
    without_dir.mkdir()
    with_dir.mkdir()

    without = claude_bridge.augment_claude_args(
        ("--resume", "abc"), bridge_dir=without_dir, python_executable="/venv/bin/python"
    )
    with_mod = claude_bridge.augment_claude_args(
        ("--resume", "abc"),
        bridge_dir=with_dir,
        python_executable="/venv/bin/python",
        claude_mod=True,
    )

    assert "--plugin-dir" not in without
    assert not (without_dir / mod_channel.MOD_PLUGIN_DIR_NAME).exists()
    assert with_mod[with_mod.index("--plugin-dir") + 1] == str(
        with_dir / mod_channel.MOD_PLUGIN_DIR_NAME
    )
    assert (
        with_dir / mod_channel.MOD_PLUGIN_DIR_NAME / ".claude-plugin" / "plugin.json"
    ).is_file()


@pytest.mark.parametrize("feature_on", [False, True])
def test_cli_launch_loads_the_mod_only_with_the_feature_flag(
    mod_bridge: Path, monkeypatch: pytest.MonkeyPatch, feature_on: bool
) -> None:
    from omnigent.harnesses.claude_native import main as claude_main

    monkeypatch.setenv("OMNIGENT_FEATURES", "claude_mod_channel" if feature_on else "")
    monkeypatch.setattr(claude_main, "resolve_cli_binary", lambda command: f"/bin/{command}")
    monkeypatch.setattr(mod_channel, "claude_cli_supports_mod", lambda binary: True)
    bridge_dir = mod_bridge / ("on" if feature_on else "off")
    bridge_dir.mkdir()

    body = claude_main._claude_terminal_request((), command="claude", bridge_dir=bridge_dir)

    args = body["spec"]["args"]
    assert ("--plugin-dir" in args) is feature_on
