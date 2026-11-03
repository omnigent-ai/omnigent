"""Bounded OSC 7501 parsing and a browser-independent PTY relay."""

from __future__ import annotations

import base64
import binascii
import contextlib
import errno
import json
import os
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_STATES = {"idle", "working", "done", "blocked", "error", "clear"}
_KINDS = {"permission", "question", "auth"}
_VALUE = re.compile(r"[A-Za-z0-9_.,+/=-]*")
_SEGMENT = re.compile(r"[A-Za-z0-9_.+-]{1,32}")
_REPLY = b"\x1b]7501;?:states=idle,working,done,blocked,error:kinds=permission,question,auth\x1b\\"


@dataclass(frozen=True)
class ProgramStatus:
    """One validated record; ids are local to a single terminal."""

    state: str
    app: str | None = None
    id: str | None = None
    kind: str | None = None
    message: str | None = None


def parse_report(body: bytes) -> ProgramStatus | None:
    """Parse an OSC body, validating all fields before applying any of it."""
    text = body.decode("ascii", errors="replace")
    pairs: dict[str, str] = {}
    decoded: dict[str, str] = {}
    for pair in text.split(":"):
        key, sep, value = pair.partition("=")
        key, value = key.strip(), value.strip()
        if len(key) > 16:
            return None
        if (
            sep
            and key == "id"
            and (
                len(value) > 128
                or len(value.split("/")) > 8
                or any(not _SEGMENT.fullmatch(part) for part in value.split("/"))
            )
        ):
            return None
        if sep and key == "app" and len(value) > 32:
            return None
        if not (sep and re.fullmatch("[a-z]+", key) and _VALUE.fullmatch(value)):
            continue
        if key in {"msg", "title"}:
            encoded_limit, decoded_limit = (2732, 2048) if key == "msg" else (256, 192)
            if len(value) > encoded_limit:
                return None
            try:
                raw = base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
                decoded[key] = raw.decode("utf-8")
            except (ValueError, binascii.Error):
                return None
            if len(raw) > decoded_limit or any(
                ord(c) < 32 or 127 <= ord(c) <= 159 for c in decoded[key]
            ):
                return None
        pairs[key] = value
    state = pairs.get("state")
    if state is None or state not in _STATES:
        return None
    record_id = pairs.get("id")
    app = pairs.get("app")
    if app is not None and not _SEGMENT.fullmatch(app):
        app = None
    kind = pairs.get("kind") if state == "blocked" else None
    return ProgramStatus(
        state=state,
        app=app,
        id=record_id,
        kind=kind if kind in _KINDS else None,
        message=decoded.get("msg"),
    )


class ProgramStatusParser:
    """Parse bounded control strings and consume capability queries before tmux.

    Other bytes pass through unchanged. Oversized strings stop being buffered;
    parsing resumes after their terminator, without interpreting their contents.
    """

    def __init__(
        self,
        *,
        on_report: Callable[[ProgramStatus], None],
        on_query: Callable[[], None],
        on_reset: Callable[[bool], None],
    ) -> None:
        self._report = on_report
        self._query = on_query
        self._reset = on_reset
        self._state = "ground"
        self._osc = False
        self._held = bytearray()
        self._overflow = False

    def feed(self, data: bytes) -> bytes:
        """Return pass-through output, withholding only OSC 7501 queries."""
        output = bytearray()
        for byte in data:
            if self._state == "ground":
                if byte == 27:
                    self._state = "escape"
                    self._held.append(byte)
                else:
                    output.append(byte)
            elif self._state == "escape":
                self._state = "ground"
                if byte in (ord("]"), ord("P"), ord("_"), ord("^"), ord("X")):
                    self._osc = byte == ord("]")
                    self._overflow = False
                    self._state = "string"
                    self._held.append(byte)
                    if not self._osc:
                        output.extend(self._held)
                        self._held.clear()
                else:
                    if byte == ord("c"):
                        self._reset(True)
                    output.extend(self._held)
                    self._held.clear()
                    if byte == 27:
                        self._state = "escape"
                        self._held.append(byte)
                    else:
                        output.append(byte)
            else:
                if self._osc and not self._overflow:
                    self._held.append(byte)
                    if len(self._held) > 4096:
                        output.extend(self._held)
                        self._held.clear()
                        self._overflow = True
                else:
                    output.append(byte)
                cancelled = byte in (24, 26)
                terminated = (self._state == "string_escape" and byte == 92) or (
                    self._osc and byte == 7
                )
                if terminated or cancelled:
                    consumed = False
                    if terminated and self._osc and not self._overflow:
                        end = -1 if byte == 7 else -2
                        consumed = self._finish(bytes(self._held[2:end]))
                    if not consumed:
                        output.extend(self._held)
                    self._held.clear()
                    self._state = "ground"
                else:
                    self._state = "string_escape" if byte == 27 else "string"
        return bytes(output)

    def finish(self) -> bytes:
        """Flush an incomplete escape sequence on process exit."""
        pending = bytes(self._held)
        self._held.clear()
        self._state = "ground"
        return pending

    def _finish(self, body: bytes) -> bool:
        if body == b"7501;?":
            self._query()
            return True
        if body.startswith(b"7501;"):
            report = parse_report(body[5:])
            if report is not None:
                self._report(report)
        elif body == b"133;A" or body.startswith(b"133;A;"):
            self._reset(False)
        return False


class ProgramStatusReader:
    """Read the runner-owned root snapshot written by a terminal's PTY relay."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> dict[str, Any]:
        try:
            record = json.loads(self.path.read_text())
            if not isinstance(record, dict) or not isinstance(record.get("relay_pid"), int):
                return {}
            os.kill(record["relay_pid"], 0)
            return record
        except (OSError, ValueError):
            return {}

    @property
    def child_pid(self) -> int | None:
        """The inner pane process, used to resolve legacy Claude status files."""
        pid = self._read().get("pid")
        return pid if isinstance(pid, int) else None

    @property
    def status(self) -> ProgramStatus | None:
        """Current root state, or None before discovery / after relay exit."""
        report = self._read().get("status")
        if not isinstance(report, dict):
            return None
        try:
            return ProgramStatus(**report)
        except TypeError:
            return None


def blocked_reason(report: ProgramStatus) -> str | None:
    """Map a blocked report to safe single-line display text."""
    if report.state != "blocked":
        return None
    text = report.message or {
        "permission": "permission prompt",
        "question": "input needed",
        "auth": "authentication",
    }.get(report.kind or "", "input needed")
    return "".join(c for c in text if unicodedata.category(c) != "Cf") or "input needed"


def _write_all(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data) :]


def relay(path: Path, app: str, command: list[str]) -> int:
    """Run a command in an inner PTY, answering queries before tmux can answer DA.

    The relay stays outside the command's existing sandbox wrapper. It owns no
    credentials or policy decisions; it preserves input, output and window size.
    """
    import fcntl
    import pty
    import select
    import signal
    import termios
    import tty

    original = termios.tcgetattr(0)
    size = fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8)
    pid, master = pty.fork()
    if pid == 0:
        exit_code = 126
        try:
            fcntl.ioctl(0, termios.TIOCSWINSZ, size)
            os.execvp(command[0], command)
        except OSError as exc:
            exit_code = 127 if exc.errno == errno.ENOENT else 126
            with contextlib.suppress(OSError):
                _write_all(2, f"relay: cannot exec {command[0]}: {exc}\n".encode())
        finally:
            os._exit(exit_code)
    status: ProgramStatus | None = None
    stopping_at: float | None = None
    exit_status: int | None = None
    stdin_open = True

    def save() -> None:
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps(
                    {
                        "pid": pid,
                        "relay_pid": os.getpid(),
                        "status": asdict(status) if status else None,
                    }
                )
            )
            temporary.replace(path)
        except OSError:
            # Observation failure must not terminate the interactive command.
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)

    def record(report: ProgramStatus) -> None:
        nonlocal status
        if report.id is not None:
            return
        if report.state == "clear":
            if report.app is not None and report.app != app:
                return
            status = None
        elif report.app == app:
            if status == report:
                return
            status = report
        else:
            return
        save()

    def reset(full: bool) -> None:
        nonlocal status
        if full or (status and status.state in {"working", "blocked", "idle"}):
            status = None
            save()

    def reply() -> None:
        _write_all(master, _REPLY)

    def resize(_signum: int, _frame: object) -> None:
        with contextlib.suppress(OSError):
            fcntl.ioctl(master, termios.TIOCSWINSZ, fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))

    def stop(signum: int, _frame: object) -> None:
        nonlocal stopping_at
        if stopping_at is None:
            stopping_at = time.monotonic()
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signum)

    parser = ProgramStatusParser(on_report=record, on_query=reply, on_reset=reset)
    try:
        tty.setraw(0)
        signal.signal(signal.SIGWINCH, resize)
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, stop)
        save()
        while True:
            if stopping_at is not None and time.monotonic() - stopping_at > 2:
                break
            readable, _, _ = select.select([0, master] if stdin_open else [master], [], [], 0.2)
            if master in readable:
                try:
                    data = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not data:
                    break
                _write_all(1, parser.feed(data))
            if 0 in readable:
                try:
                    data = os.read(0, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    data = b""
                if not data:
                    stdin_open = False
                    stop(signal.SIGHUP, None)
                else:
                    _write_all(master, data)
            if exit_status is None:
                child, result = os.waitpid(pid, os.WNOHANG)
                if child:
                    exit_status = result
                    # Drain buffered output, bounding descendants holding the slave open.
                    stopping_at = time.monotonic()
        _write_all(1, parser.finish())
    finally:
        # PTY EOF can precede waitpid readiness; allow the child's real exit to arrive.
        deadline = (stopping_at if stopping_at is not None else time.monotonic()) + 2
        while exit_status is None and time.monotonic() < deadline:
            child, result = os.waitpid(pid, os.WNOHANG)
            if child:
                exit_status = result
            else:
                time.sleep(0.01)
        if exit_status is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
            _, exit_status = os.waitpid(pid, 0)
        os.close(master)
        with contextlib.suppress(OSError, termios.error):
            termios.tcsetattr(0, termios.TCSANOW, original)
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
    return os.waitstatus_to_exitcode(exit_status)


def main() -> None:
    """Private terminal relay entry point."""
    import argparse
    import signal

    parser = argparse.ArgumentParser()
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required")
    code = relay(args.state_file, args.app, command)
    if code < 0:
        if -code != signal.SIGKILL:
            signal.signal(-code, signal.SIG_DFL)
        os.kill(os.getpid(), -code)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
