"""E2E regression: claude-native message delivery survives a transiently
unresponsive Claude Code TUI.

The failure mode this guards: a user sends a message from the Omnigent web
chat to a ``claude-native`` session; the bridge pastes the draft into Claude
Code's TUI input box and the draft visibly commits, but the TUI is
unresponsive at submit time (a CPU-starved host, a paste-coalescing burst) and
does not process the submit ``Enter`` for tens of seconds. The bridge used to
give up after a fixed 10s window and raise "The message was not delivered";
``ClaudeNativeExecutor.run_turn`` then logged ``claude-native: failed to
deliver message to harness`` and yielded an ``ExecutorError``, which the
runner surfaced to the web UI as a failed turn -- even though the committed
draft would have delivered moments later once the TUI caught up.

This test drives the REAL transport end to end: a real ``tmux`` server on a
private socket advertised through the production ``write_tmux_target``, and a
fake Claude TUI that renders the framed chat composer, visibly commits the
pasted draft, then STALLS (stops reading stdin, queueing input in the pty)
well past the legacy 10s give-up before recovering and processing the queued
submit. Delivery must succeed:

- ``inject_user_message`` -- the public bridge delivery call -- returns
  instead of raising the delivery-timeout ``RuntimeError``.
- ``ClaudeNativeExecutor.run_turn`` yields ``TurnComplete`` -- not the
  ``ExecutorError`` failed turn -- and never logs the delivery failure.

Runs with no LLM, no ``claude`` binary and no server -- only ``tmux``::

    pytest tests/e2e/test_claude_native_delivery_timeout_e2e.py -v
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge as claude_bridge
from omnigent.harnesses.claude_native import delivery_diagnostics, framework_context, hook
from omnigent.harnesses.claude_native.bridge import (
    _PASTE_COMMIT_TIMEOUT_S,
    _SUBMIT_VERIFY_TIMEOUT_S,
    _capture_pane,
    inject_user_message,
    write_tmux_target,
)
from omnigent.inner import claude_native_executor
from omnigent.inner.claude_native_executor import ClaudeNativeExecutor
from omnigent.inner.executor import ExecutorError, TurnComplete
from omnigent.inner.terminal_lifecycle import (
    TERMINAL_INSTANCE_ID_ENV,
    TERMINAL_LAUNCH_ID_ENV,
    TERMINAL_LAUNCH_SESSION_ID_ENV,
)
from omnigent.runtime.harnesses import _scaffold
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter, InnerExecutorError
from omnigent.runtime.harnesses._scaffold import TurnContext
from omnigent.server.schemas import CreateResponseRequest
from tests._helpers.polling import poll_until, poll_until_async

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="requires tmux on PATH")

# The user message delivered from the web chat. Its first line is the needle
# the bridge looks for in the input box to confirm the paste committed.
_MESSAGE = "deliver me despite a slow tui"

# How long the fake TUI stays unresponsive after the draft commits. Past the
# legacy 10s fixed submit window (where delivery used to hard-fail), and clear
# of the current submit ceiling so a healthy recovery lands inside it.
_TUI_STALL_S = 13.0

# Upper bound on how long a single delivery may take: the submit ceiling plus
# slack for tmux/readiness overhead and queued-input replay. Tied to the
# ceiling constant so this bound tracks it if the window is later tuned.
_DELIVERY_BOUND_S = _SUBMIT_VERIFY_TIMEOUT_S + 5.0

# The fake Claude TUI. It renders the framed composer Claude Code shows -- a
# box rule, a prompt row, a closing rule -- so the bridge's readiness gate and
# draft-visibility poll both pass and the pasted draft is seen to land. Once
# the draft contains the expected message (argv[1]) it goes unresponsive for
# argv[2] seconds -- stdin queues in the pty, exactly like a CPU-starved TUI
# at submit time -- then recovers and processes the queued submit Enter, which
# moves the draft into the transcript and clears the input box.
_FAKE_CLAUDE_TUI = """\
import json, os, sys, termios, time, tty, uuid
from pathlib import Path

PROMPT_GLYPH = "\\u276f"
RULE = "\\u2500" * 30
STALL_TRIGGER = sys.argv[1]
STALL_S = float(sys.argv[2])
MODE = sys.argv[3] if len(sys.argv) > 3 else "visible"
EVENTS = Path(sys.argv[4]) if len(sys.argv) > 4 else None
RELEASE = Path(sys.argv[5]) if len(sys.argv) > 5 else None
HOOK_BRIDGE = None
HOOK_METADATA = {}
if os.environ.get("OMNIGENT_TEST_CLAUDE_HOOKS") == "1":
    from omnigent.harnesses.claude_native import framework_context, hook
    from omnigent.harnesses.claude_native.bridge import bridge_dir_for_bridge_id
    from omnigent.inner.terminal_lifecycle import TERMINAL_LAUNCH_SESSION_ID_ENV

    HOOK_BRIDGE = bridge_dir_for_bridge_id(os.environ[TERMINAL_LAUNCH_SESSION_ID_ENV])
    transcript_path = HOOK_BRIDGE / "fake-native-transcript.jsonl"
    transcript_path.touch()
    HOOK_METADATA = {
        "session_id": "fake-" + uuid.uuid4().hex,
        "transcript_path": str(transcript_path),
    }


def emit_claude_hook(event, **fields):
    if HOOK_BRIDGE is not None:
        payload = {"hook_event_name": event, **HOOK_METADATA, **fields}
        hook.record_hook_payload(
            HOOK_BRIDGE,
            payload,
            prompt_framework_context=True,
        )
        if event == "UserPromptSubmit":
            framework_context.consume_prompt_framework_context(HOOK_BRIDGE, payload)


def record(event, **fields):
    if EVENTS is not None:
        with EVENTS.open("a") as handle:
            handle.write(json.dumps({"event": event, **fields}) + "\\n")


def render(draft, transcript):
    sys.stdout.write("\\x1b[2J\\x1b[H")
    for line in transcript:
        sys.stdout.write(line + "\\r\\n")
    sys.stdout.write(RULE + "\\r\\n")
    sys.stdout.write(PROMPT_GLYPH + " " + draft + "\\r\\n")
    sys.stdout.write(RULE + "\\r\\n")
    sys.stdout.flush()


def main():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    chars = []
    transcript = []
    stalled = False
    marker = b""
    pasted = None
    emit_claude_hook("SessionStart")
    if MODE == "hidden":
        sys.stdout.write("\\x1b[?2004h")
    render("", transcript)
    try:
        while True:
            data = os.read(fd, 1)
            if not data:
                break
            byte = data[0]
            if MODE == "hidden":
                if marker or byte == 27:
                    marker += data
                    if marker == b"\\x1b[200~":
                        pasted = bytearray()
                        marker = b""
                    elif marker == b"\\x1b[201~":
                        content = pasted.decode("utf-8").replace("\\r", "\\n").removesuffix("\\n")
                        record("paste", content=content)
                        chars = list(content)
                        marker = b""
                        pasted = None
                        deadline = time.monotonic() + STALL_S
                        while not RELEASE.exists():
                            if time.monotonic() >= deadline:
                                raise RuntimeError("test did not release the hidden draft")
                            time.sleep(0.01)
                        render("".join(chars), transcript)
                        record("draft_visible")
                    elif not any(
                        value.startswith(marker) for value in (b"\\x1b[200~", b"\\x1b[201~")
                    ):
                        marker = b""
                    continue
                if pasted is not None:
                    pasted.extend(data)
                    continue
            if byte == 3:  # Ctrl-C tears the pane down cleanly on teardown
                break
            if byte == 13:  # Enter submits a non-empty draft
                record("enter")
                if chars:
                    emit_claude_hook("UserPromptSubmit", prompt="".join(chars))
                    record("submitted", content="".join(chars))
                    transcript.append("sent: " + "".join(chars))
                    chars = []
                    render("", transcript)
                continue
            if byte < 0x20:
                # Other control bytes (Escape, Ctrl-A/Ctrl-K, the ESC of
                # bracketed-paste markers) are swallowed.
                continue
            chars.append(chr(byte))
            render("".join(chars), transcript)
            if MODE == "visible" and not stalled and STALL_TRIGGER in "".join(chars):
                # The draft has visibly committed; go unresponsive past the
                # legacy fixed submit window while input queues in the pty.
                stalled = True
                time.sleep(STALL_S)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


main()
"""


@pytest.fixture
def stalling_claude_pane(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, str]]:
    """A claude-native bridge dir advertising a real tmux pane whose fake TUI
    commits the pasted draft, stalls past the legacy submit window, then
    accepts the queued submit.

    Yields the bridge dir and the tmux socket path. The tmux server is always
    killed on teardown, even when the test body raises.
    """
    work = Path(tempfile.mkdtemp(prefix="slowtui-"))
    # Keep the socket path short: a long path overflows the AF_UNIX limit.
    socket_path = work / "t.sock"
    tui_path = work / "fake_claude_tui.py"
    tui_path.write_text(_FAKE_CLAUDE_TUI, encoding="utf-8")
    mode = getattr(request, "param", "visible")
    monkeypatch.setattr(claude_bridge, "_TRUSTED_PARENT", work)
    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", work / "bridges")
    session_id = f"slowtui-{uuid.uuid4().hex}"
    launch_id, instance_id = uuid.uuid4().hex, uuid.uuid4().hex
    monkeypatch.setenv(TERMINAL_LAUNCH_ID_ENV, launch_id)
    monkeypatch.setenv(TERMINAL_INSTANCE_ID_ENV, instance_id)
    monkeypatch.setenv(TERMINAL_LAUNCH_SESSION_ID_ENV, session_id)
    monkeypatch.setenv(claude_bridge.REQUEST_SESSION_ID_ENV_VAR, session_id)
    bridge_dir = claude_bridge.prepare_bridge_dir(session_id, workspace=work)
    transcript_path = bridge_dir / "native-transcript.jsonl"
    transcript_path.write_text("")

    try:
        subprocess.run(
            [
                "tmux",
                "-S",
                str(socket_path),
                "new-session",
                "-d",
                "-s",
                "claude",
                "-x",
                "80",
                "-y",
                "24",
                sys.executable,
                str(tui_path),
                _MESSAGE,
                str(_TUI_STALL_S if mode == "visible" else _DELIVERY_BOUND_S),
                mode,
                str(bridge_dir / "tui-events.jsonl"),
                str(bridge_dir / "release-draft"),
            ],
            check=True,
            timeout=30.0,
        )

        write_tmux_target(
            bridge_dir,
            socket_path=socket_path,
            tmux_target="claude",
            launch_id=launch_id,
            instance_id=instance_id,
        )
        hook.record_hook_payload(
            bridge_dir,
            {
                "hook_event_name": "SessionStart",
                "session_id": f"native-{session_id}",
                "transcript_path": str(transcript_path),
            },
            prompt_framework_context=True,
        )
        owner = claude_bridge.read_delivery_owner(bridge_dir)
        assert owner.session_id == session_id
        assert owner.native_session_id == f"native-{session_id}"
        assert owner.launch_id == launch_id
        assert framework_context.prompt_framework_context_capability_ready(bridge_dir)

        poll_until(
            lambda: "❯" in _capture_pane(str(socket_path), "claude"),
            timeout_s=10.0,
            interval_s=0.1,
        )

        yield bridge_dir, str(socket_path)
    finally:
        subprocess.run(
            ["tmux", "-S", str(socket_path), "kill-server"],
            check=False,
            timeout=30.0,
        )
        shutil.rmtree(work)


def test_inject_user_message_delivers_despite_stalled_submit(
    stalling_claude_pane: tuple[Path, str],
) -> None:
    """The bridge delivery call out-waits a transiently unresponsive TUI and
    the committed draft is submitted -- not abandoned as undelivered."""
    bridge_dir, socket_path = stalling_claude_pane

    start = time.monotonic()
    inject_user_message(bridge_dir, content=_MESSAGE)
    elapsed = time.monotonic() - start

    # The delivery must actually have out-waited the stall (a fixture whose
    # TUI never stalled would trivially pass) and still land within the ceiling.
    assert _TUI_STALL_S - 2.0 <= elapsed <= _DELIVERY_BOUND_S, elapsed

    # The message left the input box and landed in the transcript.
    pane = _capture_pane(socket_path, "claude")
    assert "sent: " in pane and _MESSAGE[:20] in pane, pane


async def test_run_turn_completes_despite_stalled_submit(
    stalling_claude_pane: tuple[Path, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``ClaudeNativeExecutor.run_turn`` completes the turn once the stalled
    TUI catches up -- it must not log ``failed to deliver message to harness``
    or yield the ``ExecutorError`` the runner would surface to the web UI as a
    failed turn."""
    bridge_dir, _socket_path = stalling_claude_pane
    executor = ClaudeNativeExecutor(bridge_dir=bridge_dir)

    events = []
    with caplog.at_level("ERROR", logger="omnigent.inner.claude_native_executor"):
        start = time.monotonic()
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": _MESSAGE}],
            tools=[],
            system_prompt="",
            config=None,
        ):
            events.append(event)
        elapsed = time.monotonic() - start

    assert len(events) == 1, events
    assert not isinstance(events[0], ExecutorError), events[0]
    assert isinstance(events[0], TurnComplete), events[0]
    assert _TUI_STALL_S - 2.0 <= elapsed <= _DELIVERY_BOUND_S, elapsed

    assert not any(
        "failed to deliver message to harness" in record.getMessage() for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


@pytest.mark.parametrize("stalling_claude_pane", ["hidden"], indirect=True)
@pytest.mark.parametrize("action", ["reveal", "stop", "watchdog"])
async def test_run_turn_owns_one_unobserved_paste_through_delivery_or_stop(
    stalling_claude_pane: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    """A real PTY hides one paste past the first wait; late visibility or Stop owns its result."""
    bridge_dir, socket_path = stalling_claude_pane
    acknowledgement_wait = threading.Event()
    worker_finished = threading.Event()
    set_stage = delivery_diagnostics.set_stage
    inject = claude_native_executor.inject_user_message

    def observe_stage(stage: str) -> None:
        set_stage(stage)
        if stage == "waiting_for_paste_ack":
            acknowledgement_wait.set()

    def observe_worker(*args: object, **kwargs: object) -> None:
        try:
            inject(*args, **kwargs)
        finally:
            worker_finished.set()

    monkeypatch.setattr(delivery_diagnostics, "set_stage", observe_stage)
    monkeypatch.setattr(claude_native_executor, "inject_user_message", observe_worker)
    executor = ClaudeNativeExecutor(bridge_dir=bridge_dir)
    adapter = ExecutorAdapter(executor_factory=lambda: executor)
    ctx = TurnContext("response-hidden-paste", asyncio.Queue(), asyncio.Event())
    if action == "watchdog":
        monkeypatch.setattr(_scaffold, "_TURN_IDLE_TIMEOUT_S", _PASTE_COMMIT_TIMEOUT_S + 5.0)
        monkeypatch.setattr(_scaffold, "_TURN_ABSOLUTE_TIMEOUT_S", 60.0)
        monkeypatch.setattr(_scaffold, "_WEDGED_TURN_RECOVERY_RETRIES", 1)
    turn = asyncio.create_task(
        adapter._guarded_run_turn(
            CreateResponseRequest(model="claude-native", input=_MESSAGE), ctx
        )
    )
    event_path = bridge_dir / "tui-events.jsonl"
    try:
        await poll_until_async(
            lambda: acknowledgement_wait.is_set() or turn.done(), timeout_s=_DELIVERY_BOUND_S
        )
        assert acknowledgement_wait.is_set(), (
            f"delivery ended before the hidden wait: {turn.result()}"
        )
        assert [json.loads(line) for line in event_path.read_text().splitlines()] == [
            {"event": "paste", "content": _MESSAGE}
        ]
        if action in {"stop", "watchdog"}:
            if action == "stop":
                ctx.cancelled.set()
                turn.cancel()
                await asyncio.wait_for(turn, timeout=5.0)
                terminal = await adapter._build_terminal_event(ctx, "claude-native", turn, 1)
                assert terminal.type == "response.cancelled"
            else:
                with pytest.raises(InnerExecutorError) as caught:
                    await asyncio.wait_for(turn, timeout=_DELIVERY_BOUND_S)
                assert caught.value.code == "native_delivery_unconfirmed"
                assert caught.value.undelivered is False
                assert not ctx.cancelled.is_set()
                assert adapter._executor is executor
                assert adapter._abandoned_executor_cleanup is None
                assert (
                    subprocess.run(
                        ["tmux", "-S", socket_path, "has-session", "-t", "claude"],
                        check=False,
                        timeout=5.0,
                    ).returncode
                    == 0
                )
            assert worker_finished.is_set(), "cancellation must drain the real injection worker"
            assert [json.loads(line) for line in event_path.read_text().splitlines()] == [
                {"event": "paste", "content": _MESSAGE}
            ]
        else:
            (bridge_dir / "release-draft").touch()
            await asyncio.wait_for(turn, timeout=_DELIVERY_BOUND_S)
            terminal = await adapter._build_terminal_event(ctx, "claude-native", turn, 1)
            assert terminal.type == "response.completed"
            assert worker_finished.is_set()
            assert [json.loads(line) for line in event_path.read_text().splitlines()] == [
                {"event": "paste", "content": _MESSAGE},
                {"event": "draft_visible"},
                {"event": "enter"},
                {"event": "submitted", "content": _MESSAGE},
            ]
    finally:
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        await adapter.on_shutdown()
