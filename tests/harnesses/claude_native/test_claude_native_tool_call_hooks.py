"""Tests for the hooks Claude Code runs synchronously on every native tool call."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from omnigent.harnesses.claude_native.bridge import build_hook_settings, prepare_bridge_dir

# Claude Code blocks its TUI until each command hook exits, so a hook that waits
# on an interpreter this slow is user-visible latency on every tool call.
_SLOW_INTERPRETER_S = 2.0
_HOOK_BUDGET_S = 1.0
# A relay that records the observation but answers only after the hook's one-second
# curl budget; the hook must stop waiting at the budget, not after this.
_SLOW_RELAY_S = _HOOK_BUDGET_S + 1.0
_PAYLOAD: dict[str, Any] = {
    "session_id": "claude-session",
    "tool_name": "Bash",
    "tool_input": {"command": "echo step"},
    "tool_response": {"stdout": "step\n", "stderr": ""},
}


class _Request(NamedTuple):
    path: str
    authorization: str | None
    payload: Any


class _Relay(NamedTuple):
    url: str
    received: list[_Request]


@pytest.fixture(autouse=True)
def _trust_tmp_bridge_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path)


@pytest.fixture
def relay(request: pytest.FixtureRequest) -> Iterator[_Relay]:
    """Stand-in tool relay that allows every policy call and records each POST.

    Indirect params model the delivered-but-unhelpful cases: ``response_delay_s``
    holds the answer back past the hook's budget, and ``required_token`` rejects
    any other bearer with 401 (a stale advertisement after a relay restart).
    """
    options: dict[str, Any] = getattr(request, "param", {})
    response_delay_s: float = options.get("response_delay_s", 0.0)
    required_token: str | None = options.get("required_token")
    received: list[_Request] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            authorization = self.headers.get("Authorization")
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            received.append(_Request(self.path, authorization, json.loads(raw)))
            if response_delay_s:
                time.sleep(response_delay_s)
            rejected = required_token is not None and authorization != f"Bearer {required_token}"
            body = json.dumps({"result": "POLICY_ACTION_ALLOW"}).encode()
            try:
                self.send_response(401 if rejected else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                # curl closed the connection at --max-time; the POST already landed.
                pass

        def log_message(self, *_: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Relay(f"http://127.0.0.1:{server.server_address[1]}", received)
    finally:
        server.shutdown()
        server.server_close()


def _bridge_dir_with_relay(tmp_path: Path, relay_url: str) -> Path:
    bridge_dir = prepare_bridge_dir("conv_abc", bridge_id="bridge_test", workspace=tmp_path)
    (bridge_dir / "tool_relay.env").write_text(
        f"OMNIGENT_RELAY_URL='{relay_url}'\nOMNIGENT_RELAY_TOKEN='token'\n"
    )
    (bridge_dir / "tool_relay.json").write_text(json.dumps({"url": relay_url, "token": "token"}))
    return bridge_dir


def _fake_python(tmp_path: Path, body: str) -> Path:
    fake = tmp_path / "fake-python"
    fake.write_text(f"#!/bin/sh\n{body}\n")
    fake.chmod(0o755)
    return fake


def _every_tool_call_commands(settings: dict[str, Any], event: str) -> list[str]:
    return [
        hook["command"]
        for entry in settings["hooks"].get(event, [])
        if not entry.get("matcher")
        for hook in entry["hooks"]
    ]


def _run_hook(
    command: str, event: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", "-c", command],
        input=json.dumps({**_PAYLOAD, "hook_event_name": event}),
        capture_output=True,
        text=True,
        timeout=_SLOW_INTERPRETER_S + 10,
        check=False,
        env=env,
    )


@pytest.mark.skipif(shutil.which("curl") is None, reason="the hooks' fast path needs curl")
def test_every_tool_call_hooks_return_without_waiting_on_an_interpreter(
    tmp_path: Path, relay: _Relay
) -> None:
    bridge_dir = _bridge_dir_with_relay(tmp_path, relay.url)
    slow_python = _fake_python(tmp_path, f"sleep {_SLOW_INTERPRETER_S}")
    settings = build_hook_settings(
        bridge_dir, python_executable=str(slow_python), ap_server_url="http://127.0.0.1:8787"
    )

    waited_on_interpreter: dict[str, float] = {}
    for event in ("PreToolUse", "PostToolUse"):
        commands = _every_tool_call_commands(settings, event)
        assert commands, f"{event} registers no every-tool hook"
        for command in commands:
            started = time.monotonic()
            proc = _run_hook(command, event)
            elapsed = time.monotonic() - started
            assert proc.returncode == 0, (event, command, proc.stderr)
            if "observe-tool" in command:
                # Claude parses PostToolUse stdout as hook output; the observer has none.
                assert proc.stdout == ""
            if elapsed >= _HOOK_BUDGET_S:
                waited_on_interpreter[f"{event}: {command}"] = round(elapsed, 2)

    assert not waited_on_interpreter, (
        "hooks on Claude's blocking tool-call path waited on the interpreter: "
        f"{waited_on_interpreter}"
    )
    # Returning quickly must not mean skipping the observation.
    observed = [request for request in relay.received if request.path == "/hook/observe-tool"]
    assert observed == [
        _Request(
            "/hook/observe-tool", "Bearer token", {**_PAYLOAD, "hook_event_name": "PostToolUse"}
        )
    ]


@pytest.mark.parametrize("missing", ["curl", "relay_env", "relay"])
def test_observer_hook_falls_back_to_the_python_observer(
    tmp_path: Path, relay: _Relay, missing: str
) -> None:
    bridge_dir = _bridge_dir_with_relay(tmp_path, relay.url)
    argv_log = tmp_path / "fake-python.argv"
    stdin_log = tmp_path / "fake-python.stdin"
    fake_python = _fake_python(
        tmp_path,
        f"printf '%s\\n' \"$@\" > {shlex.quote(str(argv_log))}; "
        f"cat > {shlex.quote(str(stdin_log))}",
    )
    settings = build_hook_settings(bridge_dir, python_executable=str(fake_python))
    [command] = [
        c for c in _every_tool_call_commands(settings, "PostToolUse") if "observe-tool" in c
    ]

    env: dict[str, str] | None = None
    if missing == "curl":
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for tool in ("cat", "env", "printf"):
            path = shutil.which(tool)
            assert path, tool
            os.symlink(path, bin_dir / tool)
        env = {**os.environ, "PATH": str(bin_dir)}
    elif missing == "relay_env":
        (bridge_dir / "tool_relay.env").unlink()
    else:
        # Nothing listens on port 1, so curl fails to connect.
        (bridge_dir / "tool_relay.env").write_text(
            "OMNIGENT_RELAY_URL='http://127.0.0.1:1'\nOMNIGENT_RELAY_TOKEN='token'\n"
        )

    proc = _run_hook(command, "PostToolUse", env=env)

    assert proc.returncode == 0, proc.stderr
    assert argv_log.read_text().splitlines() == [
        "-I",
        "-m",
        "omnigent.harnesses.claude_native.hook",
        "observe-tool",
        "--bridge-dir",
        str(bridge_dir),
    ]
    assert json.loads(stdin_log.read_text()) == {**_PAYLOAD, "hook_event_name": "PostToolUse"}
    assert relay.received == []


@pytest.mark.skipif(shutil.which("curl") is None, reason="the hooks' fast path needs curl")
@pytest.mark.parametrize("relay", [{"response_delay_s": _SLOW_RELAY_S}], indirect=True)
def test_delivered_observation_is_not_replayed_after_a_slow_relay_response(
    tmp_path: Path, relay: _Relay
) -> None:
    """Once curl hands off the payload, a relay that is slow to answer (it finishes
    recording in the background) must not fall back to the interpreter-spawning
    Python observer, which would respawn the interpreter and double-record."""
    bridge_dir = _bridge_dir_with_relay(tmp_path, relay.url)
    ran_marker = tmp_path / "python-observer.ran"
    fake_python = _fake_python(tmp_path, f"cat >/dev/null; : > {shlex.quote(str(ran_marker))}")
    settings = build_hook_settings(bridge_dir, python_executable=str(fake_python))
    [command] = [
        c for c in _every_tool_call_commands(settings, "PostToolUse") if "observe-tool" in c
    ]

    started = time.monotonic()
    proc = _run_hook(command, "PostToolUse")
    elapsed = time.monotonic() - started

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    # The hook returns at curl's budget rather than waiting out the relay's response.
    assert elapsed < _SLOW_RELAY_S
    # The relay recorded the observation once and the Python observer never replayed it.
    assert [request.path for request in relay.received] == ["/hook/observe-tool"]
    assert not ran_marker.exists()


@pytest.mark.skipif(shutil.which("curl") is None, reason="the hooks' fast path needs curl")
@pytest.mark.parametrize("relay", [{"required_token": "fresh"}], indirect=True)
def test_stale_env_token_replays_through_the_python_observer(
    tmp_path: Path, relay: _Relay
) -> None:
    """A relay restart can leave a stale token in the env file while the JSON
    advertisement already carries the new one. curl (reading the env) is rejected
    with 401, so the hook must replay into the Python observer, which reads the
    fresh JSON token and records the observation instead of dropping it."""
    bridge_dir = _bridge_dir_with_relay(tmp_path, relay.url)
    (bridge_dir / "tool_relay.env").write_text(
        f"OMNIGENT_RELAY_URL='{relay.url}'\nOMNIGENT_RELAY_TOKEN='stale'\n"
    )
    (bridge_dir / "tool_relay.json").write_text(json.dumps({"url": relay.url, "token": "fresh"}))
    settings = build_hook_settings(bridge_dir, python_executable=sys.executable)
    [command] = [
        c for c in _every_tool_call_commands(settings, "PostToolUse") if "observe-tool" in c
    ]

    proc = _run_hook(command, "PostToolUse")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    # curl is rejected with the stale env token, then the Python observer records
    # with the fresh JSON token, so the observation reaches the relay exactly once.
    assert [request.authorization for request in relay.received] == [
        "Bearer stale",
        "Bearer fresh",
    ]
    assert all(request.path == "/hook/observe-tool" for request in relay.received)
    assert all(
        request.payload == {**_PAYLOAD, "hook_event_name": "PostToolUse"}
        for request in relay.received
    )
