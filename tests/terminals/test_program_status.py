"""Wire parsing for program status; real launch coverage lives with the runner."""

from __future__ import annotations

import base64
import contextlib
import os
import select
import signal
import subprocess
import sys
import time
import tty
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.terminals.program_status import (
    _REPLY,
    _UNSENT_INPUT_LIMIT,
    ProgramStatus,
    ProgramStatusParser,
    parse_report,
)


@pytest.mark.parametrize("ending", [b"\x07", b"\x1b\\"])
def test_fragmented_reports_queries_and_resets(ending: bytes) -> None:
    events = []
    parser = ProgramStatusParser(
        on_report=events.append,
        on_query=lambda: events.append("query"),
        on_reset=lambda full: events.append(("reset", full)),
    )
    stream = (
        b"screen\x1b]7501;?"
        + ending
        + b"\x1b]7501;state=blocked:app=claude-code:kind=question:msg=QW5zd2VyPw"
        + ending
        + b"\x1b]7501;state=working:app=claude-code"
        + ending
        + b"\x1b]133;A;redraw=0"
        + ending
        + b"\x1bc"
    )
    output = b"".join(parser.feed(bytes([byte])) for byte in stream)
    assert output == stream.replace(b"\x1b]7501;?" + ending, b"")
    assert events == [
        "query",
        ProgramStatus("blocked", app="claude-code", kind="question", message="Answer?"),
        ProgramStatus("working", app="claude-code"),
        ("reset", False),
        ("reset", True),
    ]


@pytest.mark.parametrize(
    "body",
    [
        b"app=claude-code",
        b"state=future",
        b"state=working:id=",
        b"state=working:id=a//b",
        b"state=working:id=bad!",
        b"state=working:id=" + b"a/" * 8 + b"a",
        b"state=working:app=" + b"a" * 33,
        b"state=working:" + b"k" * 17 + b"=value",
        b"state=working:msg=a",
        b"state=working:msg=a:msg=QQ",
        b"state=working:app=" + b"a" * 33 + b":app=claude-code",
        b"state=working:title=/w==",
        b"state=working:msg=" + base64.b64encode(b"line\nbreak"),
        b"state=working:msg=" + base64.b64encode("\u0085".encode()),
        b"state=working:title=" + base64.b64encode(b"a" * 193),
        b"state=working:msg=" + base64.b64encode(b"a" * 2049),
    ],
)
def test_invalid_reports_are_discarded_whole(body: bytes) -> None:
    assert parse_report(body) is None


def test_unknown_keys_and_malformed_pairs_do_not_discard_valid_state() -> None:
    assert parse_report(
        b" broken : state=idle : future=x : state=working : msg=invalid! : kind=auth"
    ) == ProgramStatus("working")


def test_other_control_strings_and_oversized_reports_cannot_change_status() -> None:
    events = []
    parser = ProgramStatusParser(
        on_report=events.append, on_query=lambda: None, on_reset=lambda _: None
    )
    parser.feed(b"\x1bPignored\x1b]7501;state=error\x07\x1b\\")
    parser.feed(b"\x1b]0;title\x1b]7501;state=error\x07")
    parser.feed(b"\x1b]7501;state=error:unknown=" + b"x" * 4096 + b"\x1b\\")
    parser.feed(b"\x1b]7501;state=error\x18")
    parser.feed(b"\x1b]7501;state=idle\x07")
    assert events == [ProgramStatus("idle")]


@pytest.mark.parametrize("chunk_size", [1, 7, 65536])
def test_output_is_preserved_except_capability_query(chunk_size: int) -> None:
    stream = (
        b"text\x1b[?1049h\x1b[31mred\x1b[0m\n"
        b"\x1b]7501;?\x07\x1b[c"
        b"\x1bPnested\x1b]7501;?\x07\x1b\\"
        b"\x1b]7501;state=working:msg=" + b"a" * 5000 + b"\x1b\\"
        b"\x1b]7501;cancelled\x18\x1b]0;title\x07\x1b\x1bXignored\x1b\\"
        b"\x1b]unfinished"
    )
    queries = []
    parser = ProgramStatusParser(
        on_report=lambda _: None, on_query=lambda: queries.append(True), on_reset=lambda _: None
    )
    output = (
        b"".join(
            parser.feed(stream[i : i + chunk_size]) for i in range(0, len(stream), chunk_size)
        )
        + parser.finish()
    )
    assert output == stream.replace(b"\x1b]7501;?\x07\x1b[c", b"\x1b[c")
    assert queries == [True]


@pytest.mark.parametrize("failure", ["missing", "not-executable", "stdin-eof", "stdin-eio"])
def test_relay_process_failure_cleanup(tmp_path: Path, failure: str) -> None:
    """Exercise exec errors and input loss with a real child and PTY."""
    state_file = tmp_path / "status.json"
    ready = tmp_path / "ready"
    command = [str(tmp_path / "command")]
    wrapper = "from omnigent.terminals.program_status import main; main()"
    if failure == "not-executable":
        Path(command[0]).write_text("#!/bin/sh\nexit 0\n")
    if failure.startswith("stdin-"):
        command = [
            sys.executable,
            "-c",
            "import os,signal,time; from pathlib import Path; "
            "signal.signal(signal.SIGHUP, lambda *_: "
            "(time.sleep(0.05), print('graceful-exit-tail', flush=True), exit(23))); "
            # Publish readiness only after the PID file is fully written and closed.
            f"ready = Path({str(ready)!r}); pending = ready.with_suffix('.tmp'); "
            "pending.write_text(str(os.getpid())); pending.replace(ready); "
            "time.sleep(30)",
        ]
        # Inject only the outer input failure; fork, inner PTY and child are real.
        wrapper = (
            "import errno,os\n"
            "from omnigent.terminals.program_status import main\n"
            "read = os.read\n"
            "def disconnected_read(fd, size):\n"
            " if fd == 0:\n"
            + (
                "  raise OSError(errno.EIO, 'terminal disconnected')\n"
                if failure == "stdin-eio"
                else "  return b''\n"
            )
            + " return read(fd, size)\n"
            "os.read = disconnected_read\n"
            "main()\n"
        )
    master, slave = os.openpty()
    try:
        with subprocess.Popen(
            [
                sys.executable,
                "-c",
                wrapper,
                "--state-file",
                str(state_file),
                "--app",
                "claude-code",
                "--",
                *command,
            ],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ) as process:
            if failure.startswith("stdin-"):
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    assert process.poll() is None
                    time.sleep(0.01)
                assert ready.exists(), "child never became ready"
                os.write(master, b"x")
            output, _ = process.communicate(timeout=10)
            assert b"Traceback" not in output, output
            if failure.startswith("stdin-"):
                assert process.returncode == 23, output
                assert b"graceful-exit-tail" in output
                with pytest.raises(ProcessLookupError):
                    os.kill(int(ready.read_text()), 0)
            else:
                assert process.returncode == (127 if failure == "missing" else 126), output
                assert b"cannot exec" in output
            assert not state_file.exists()
    finally:
        os.close(master)
        os.close(slave)


@contextlib.contextmanager
def _start_relay(
    state_file: Path, command: list[str], stdin: int
) -> Iterator[subprocess.Popen[bytes]]:
    with subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from omnigent.terminals.program_status import main; main()",
            "--state-file",
            str(state_file),
            "--app",
            "claude-code",
            "--",
            *command,
        ],
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ) as process:
        try:
            yield process
        finally:
            # A failed assertion must not leave Popen's exit waiting on a stuck relay.
            if process.poll() is None:
                process.kill()


def _wait_for_file(path: Path, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        assert process.poll() is None
        time.sleep(0.01)
    assert path.exists(), "child never became ready"


def _write_until_stalled(master: int, data: bytes) -> int:
    """Write until the relay stops draining its input for 0.5 s; return the bytes written."""
    written = 0
    stalled_since = time.monotonic()
    fill_deadline = stalled_since + 10
    while written < len(data) and time.monotonic() - stalled_since < 0.5:
        assert time.monotonic() < fill_deadline, "relay kept accepting input"
        try:
            written += os.write(master, data[written : written + 4096])
            stalled_since = time.monotonic()
        except BlockingIOError:
            time.sleep(0.05)
    return written


@pytest.mark.parametrize("child_on_stop", ["ignores", "exits"])
def test_stop_signal_releases_relay_blocked_on_unread_input(
    tmp_path: Path, child_on_stop: str
) -> None:
    """A raw-mode child that stops reading input cannot keep the relay and its PTY alive."""
    state_file = tmp_path / "status.json"
    ready = tmp_path / "ready"
    ignore = ""
    if child_on_stop == "ignores":
        ignore = "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    command = [
        sys.executable,
        "-c",
        "import os,signal,time,tty; from pathlib import Path; "
        f"{ignore}tty.setraw(0); "
        f"ready = Path({str(ready)!r}); pending = ready.with_suffix('.tmp'); "
        "pending.write_text(str(os.getpid())); pending.replace(ready); "
        "time.sleep(30)",
    ]
    master, slave = os.openpty()
    tty.setraw(slave)
    os.set_blocking(master, False)
    try:
        with _start_relay(state_file, command, slave) as process:
            _wait_for_file(ready, process)
            child = int(ready.read_text())
            flood = b"x" * (1 << 20)
            assert _write_until_stalled(master, flood) < len(flood), "input never backed up"
            process.send_signal(signal.SIGTERM)
            try:
                output, _ = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                with contextlib.suppress(ProcessLookupError):
                    os.kill(child, signal.SIGKILL)
                pytest.fail("relay stayed blocked after SIGTERM")
            assert b"Traceback" not in output, output
            # The relay mirrors the reaped child's status: SIGKILL after the grace period.
            expected = signal.SIGKILL if child_on_stop == "ignores" else signal.SIGTERM
            assert process.returncode == -expected, output
            with pytest.raises(ProcessLookupError):
                os.kill(child, 0)
            assert not state_file.exists()
    finally:
        os.close(master)
        os.close(slave)


def test_backed_up_input_reaches_the_child_intact(tmp_path: Path) -> None:
    """Input and a query reply queued while the child is not reading arrive once, in order."""
    ready = tmp_path / "ready"
    resume = tmp_path / "resume"
    received = tmp_path / "received"
    payload = bytes(range(256)) * 1024
    child_script = (
        "import os, time, tty\n"
        "from pathlib import Path\n"
        "tty.setraw(0)\n"
        f"Path({str(ready)!r}).touch()\n"
        f"while not Path({str(resume)!r}).exists():\n"
        "    time.sleep(0.01)\n"
        "os.write(1, b'\\x1b]7501;?\\x1b\\\\')\n"
        "data = b''\n"
        f"while len(data) < {len(payload) + len(_REPLY)}:\n"
        "    data += os.read(0, 65536)\n"
        f"Path({str(received)!r}).write_bytes(data)\n"
    )
    master, slave = os.openpty()
    tty.setraw(slave)
    os.set_blocking(master, False)
    try:
        with _start_relay(
            tmp_path / "status.json", [sys.executable, "-c", child_script], slave
        ) as process:
            _wait_for_file(ready, process)
            written = _write_until_stalled(master, payload)
            assert written < len(payload), "input never backed up"
            # The child queries while the relay still holds unsent input, then reads it all.
            resume.touch()
            deadline = time.monotonic() + 10
            while written < len(payload):
                assert time.monotonic() < deadline, "relay stopped forwarding input"
                try:
                    written += os.write(master, payload[written:])
                except BlockingIOError:
                    time.sleep(0.01)
            output, _ = process.communicate(timeout=10)
            assert process.returncode == 0, output
        data = received.read_bytes()
        assert data.count(_REPLY) == 1
        assert data.replace(_REPLY, b"") == payload
    finally:
        os.close(master)
        os.close(slave)


def test_query_replies_stop_queueing_at_the_limit_and_resume(tmp_path: Path) -> None:
    """Replies to a child that is not reading stop queueing at the limit, then resume."""
    resume = tmp_path / "resume"
    backlog = tmp_path / "backlog"
    after = tmp_path / "after"
    queries = 2 * _UNSENT_INPUT_LIMIT // len(_REPLY)
    child_script = (
        "import os, select, time, tty\n"
        "from pathlib import Path\n"
        "tty.setraw(0)\n"
        f"flood = b'\\x1b]7501;?\\x1b\\\\' * {queries} + b'flooded'\n"
        "while flood:\n"
        "    flood = flood[os.write(1, flood) :]\n"
        f"while not Path({str(resume)!r}).exists():\n"
        "    time.sleep(0.01)\n"
        "data = b''\n"
        "while select.select([0], [], [], 1)[0]:\n"
        "    data += os.read(0, 65536)\n"
        f"Path({str(backlog)!r}).write_bytes(data)\n"
        "os.write(1, b'\\x1b]7501;?\\x1b\\\\')\n"
        "data = b''\n"
        f"while len(data) < {len(_REPLY)}:\n"
        f"    data += os.read(0, {len(_REPLY)} - len(data))\n"
        f"Path({str(after)!r}).write_bytes(data)\n"
    )
    master, slave = os.openpty()
    tty.setraw(slave)
    try:
        with _start_relay(
            tmp_path / "status.json", [sys.executable, "-c", child_script], slave
        ) as process:
            assert process.stdout is not None
            # The relay forwards this marker only after handling every earlier query.
            output = b""
            deadline = time.monotonic() + 10
            while b"flooded" not in output:
                assert time.monotonic() < deadline, output
                if select.select([process.stdout], [], [], 0.1)[0]:
                    output += os.read(process.stdout.fileno(), 65536)
            resume.touch()
            rest, _ = process.communicate(timeout=15)
            assert process.returncode == 0, output + rest
        drained = backlog.read_bytes()
        replies = len(drained) // len(_REPLY)
        assert drained == _REPLY * replies
        assert _UNSENT_INPUT_LIMIT // len(_REPLY) <= replies < queries
        assert after.read_bytes() == _REPLY
    finally:
        os.close(master)
        os.close(slave)
