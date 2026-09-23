"""Unit tests for ``omnigent.harnesses.goose_native.bridge``.

The bridge advertises the runner's tmux pane on disk and injects web-UI input into
it. ``tmux`` is replaced by a scripted fake and time by a manual clock, so the
readiness polling and paste/submit ordering run instantly without a tmux binary.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from omnigent.harnesses.goose_native import bridge

_SOCK = "/tmp/goose.sock"
_TARGET = "omnigent:0"


class _Clock:
    """Manual clock: ``sleep`` advances time instead of blocking."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class _Tmux:
    """Scripted ``subprocess.run`` for tmux invocations."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        # subcommand -> handler(argv) -> CompletedProcess | raises
        self.handlers: dict[str, Callable[[list[str]], subprocess.CompletedProcess[str]]] = {}
        self.default_rc = 0

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv[:3] == ["tmux", "-S", _SOCK]
        self.calls.append(argv)
        handler = self.handlers.get(argv[3])
        if handler is not None:
            return handler(argv)
        return subprocess.CompletedProcess(argv, self.default_rc, stdout="", stderr="")

    def subcommands(self) -> list[str]:
        return [argv[3] for argv in self.calls]

    def send_keys(self) -> list[str]:
        return [argv[-1] for argv in self.calls if argv[3] == "send-keys"]


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(bridge, "time", fake)
    return fake


@pytest.fixture()
def tmux(monkeypatch: pytest.MonkeyPatch) -> _Tmux:
    fake = _Tmux()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def _advertise(bridge_dir: Path) -> None:
    bridge.write_tmux_target(bridge_dir, socket_path=Path(_SOCK), tmux_target=_TARGET)


def _capture_returns(*panes: str) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    """Serve successive pane captures; the last one repeats."""
    remaining = list(panes)

    def handler(argv: list[str]) -> subprocess.CompletedProcess[str]:
        pane = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return subprocess.CompletedProcess(argv, 0, stdout=pane, stderr="")

    return handler


# ── paths and spawn env ──────────────────────────────────────


def test_bridge_dir_is_a_stable_hash_under_the_root() -> None:
    first = bridge.bridge_dir_for_session_id("conv_1")

    assert first == bridge.bridge_dir_for_session_id("conv_1")
    assert first != bridge.bridge_dir_for_session_id("conv_2")
    assert first.parent == bridge.bridge_root()
    assert len(first.name) == 32 and "conv_1" not in str(first)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_ensure_dir_is_owner_only_and_tolerates_chmod_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "a" / "b"

    bridge._ensure_dir(target)
    assert target.is_dir() and (target.stat().st_mode & 0o777) == 0o700

    def failing_chmod(path: object, mode: int) -> None:
        raise OSError("read-only fs")

    monkeypatch.setattr(os, "chmod", failing_chmod)
    bridge._ensure_dir(tmp_path / "c")  # must not raise
    assert (tmp_path / "c").is_dir()


def test_spawn_env_pins_bridge_dir_theme_and_optional_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path)

    base = bridge.build_goose_native_spawn_env("conv_1")
    pinned = bridge.build_goose_native_spawn_env("conv_1", provider="anthropic", model="m-1")

    expected_dir = bridge.bridge_dir_for_session_id("conv_1")
    assert expected_dir.is_dir()
    assert base == {bridge.BRIDGE_DIR_ENV_VAR: str(expected_dir), "GOOSE_CLI_THEME": "ansi"}
    assert pinned == {**base, "GOOSE_PROVIDER": "anthropic", "GOOSE_MODEL": "m-1"}


# ── tmux target advertisement ────────────────────────────────


def test_write_and_read_tmux_target_round_trip(tmp_path: Path, clock: _Clock) -> None:
    bridge_dir = tmp_path / "nested" / "bridge"

    bridge.write_tmux_target(bridge_dir, socket_path=Path(_SOCK), tmux_target=_TARGET, pid=4242)

    payload = json.loads((bridge_dir / "tmux.json").read_text(encoding="utf-8"))
    assert payload["pid"] == 4242 and payload["updated_at"] == clock.now
    assert bridge.read_tmux_info(bridge_dir) == {"socket_path": _SOCK, "tmux_target": _TARGET}
    assert not (bridge_dir / "tmux.json.tmp").exists()


def test_write_tmux_target_omits_pid_when_unknown(tmp_path: Path) -> None:
    _advertise(tmp_path)

    assert "pid" not in json.loads((tmp_path / "tmux.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "contents",
    [
        None,  # file absent
        "not json",
        json.dumps({"tmux_target": _TARGET}),
        json.dumps({"socket_path": _SOCK}),
        json.dumps({"socket_path": "", "tmux_target": _TARGET}),
        json.dumps({"socket_path": _SOCK, "tmux_target": 7}),
    ],
)
def test_read_tmux_info_rejects_missing_or_invalid_advertisements(
    tmp_path: Path, contents: str | None
) -> None:
    if contents is not None:
        (tmp_path / "tmux.json").write_text(contents, encoding="utf-8")

    assert bridge.read_tmux_info(tmp_path) is None


def test_wait_for_tmux_info_polls_until_advertised(tmp_path: Path, clock: _Clock) -> None:
    original_sleep = clock.sleep

    def advertise_on_sleep(seconds: float) -> None:
        original_sleep(seconds)
        if len(clock.slept) == 3:
            _advertise(tmp_path)

    clock.sleep = advertise_on_sleep  # type: ignore[method-assign]

    info = bridge._wait_for_tmux_info(tmp_path, timeout_s=30.0)

    assert info == {"socket_path": _SOCK, "tmux_target": _TARGET}
    assert len(clock.slept) == 3


def test_wait_for_tmux_info_times_out(tmp_path: Path, clock: _Clock) -> None:
    with pytest.raises(RuntimeError, match="tmux target was not advertised within 2s"):
        bridge._wait_for_tmux_info(tmp_path, timeout_s=2.0)

    assert clock.now >= 1002.0


# ── tmux command wrappers ────────────────────────────────────


def test_run_tmux_passes_socket_and_args(tmux: _Tmux) -> None:
    bridge._run_tmux(_SOCK, "send-keys", "-t", _TARGET, "Enter")

    assert tmux.calls == [["tmux", "-S", _SOCK, "send-keys", "-t", _TARGET, "Enter"]]


@pytest.mark.parametrize(
    ("stdout", "stderr", "detail"),
    [
        ("", "no server running\n", "no server running"),
        ("only stdout\n", "", "only stdout"),
        ("", "", "<no output>"),
    ],
)
def test_run_tmux_reports_failures(tmux: _Tmux, stdout: str, stderr: str, detail: str) -> None:
    tmux.handlers["send-keys"] = lambda argv: subprocess.CompletedProcess(
        argv, 1, stdout=stdout, stderr=stderr
    )

    with pytest.raises(RuntimeError, match=rf"tmux command failed \(rc=1\): {detail}"):
        bridge._run_tmux(_SOCK, "send-keys")


def test_run_tmux_reports_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    def timing_out(argv: list[str], **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(argv, 10)

    monkeypatch.setattr(subprocess, "run", timing_out)

    with pytest.raises(RuntimeError, match="tmux command timed out"):
        bridge._run_tmux(_SOCK, "send-keys")


def test_capture_pane_returns_text_or_empty_on_failure(
    tmux: _Tmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmux.handlers["capture-pane"] = _capture_returns("hello pane")
    assert bridge._capture_pane(_SOCK, _TARGET) == "hello pane"
    assert tmux.calls[-1][3:] == ["capture-pane", "-p", "-t", _TARGET]

    tmux.handlers["capture-pane"] = lambda argv: subprocess.CompletedProcess(
        argv, 1, stdout="ignored", stderr=""
    )
    assert bridge._capture_pane(_SOCK, _TARGET) == ""

    for exc in (subprocess.TimeoutExpired("tmux", 10), OSError("no tmux")):

        def failing(argv: list[str], exc: Exception = exc, **kwargs: object) -> None:
            raise exc

        monkeypatch.setattr(subprocess, "run", failing)
        assert bridge._capture_pane(_SOCK, _TARGET) == ""


def test_session_alive_reflects_has_session(tmux: _Tmux, monkeypatch: pytest.MonkeyPatch) -> None:
    assert bridge._session_alive(_SOCK, _TARGET) is True

    tmux.default_rc = 1
    assert bridge._session_alive(_SOCK, _TARGET) is False

    for exc in (subprocess.TimeoutExpired("tmux", 10), OSError("no tmux")):

        def failing(argv: list[str], exc: Exception = exc, **kwargs: object) -> None:
            raise exc

        monkeypatch.setattr(subprocess, "run", failing)
        assert bridge._session_alive(_SOCK, _TARGET) is False


# ── paste encoding and needle ────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain", b"plain"),
        ("a\nb", b"a\rb"),
        ("a\r\nb\rc", b"a\rb\rc"),
        ("col1\tcol2", b"col1\tcol2"),
        # ESC and other control bytes are dropped: a stray ESC would end the
        # bracketed paste early.
        ("x\x1b[31my\x00z\x07", b"x[31myz"),
        ("héllo ✓", "héllo ✓".encode()),
    ],
)
def test_paste_payload_bytes(text: str, expected: bytes) -> None:
    assert bridge._paste_payload_bytes(text) == expected


@pytest.mark.parametrize(
    ("content", "needle"),
    [
        ("first line\nsecond line", "second line"),
        ("  padded tail  \n", "padded tail"),
        # Short trailing lines are skipped in favour of the last usable one.
        ("real content here\nok\n\n", "real content here"),
        ("x" * 40, "x" * 24),
        ("abc", ""),
        ("", ""),
    ],
)
def test_submit_needle_anchors_on_the_last_usable_line(content: str, needle: str) -> None:
    assert bridge._submit_needle(content) == needle


# ── pane settling ────────────────────────────────────────────


def test_settle_pane_returns_once_the_pane_is_stable(tmux: _Tmux, clock: _Clock) -> None:
    tmux.handlers["capture-pane"] = _capture_returns("spinner 1", "spinner 2", "idle")

    bridge._settle_pane(_SOCK, _TARGET, timeout_s=30.0)

    # Two changing captures, then three consecutive identical polls.
    assert len(clock.slept) == 5
    assert all(seconds == bridge._POLL_INTERVAL_S for seconds in clock.slept)


def test_settle_pane_never_counts_empty_captures_as_stable(tmux: _Tmux, clock: _Clock) -> None:
    tmux.handlers["capture-pane"] = _capture_returns("")

    bridge._settle_pane(_SOCK, _TARGET, timeout_s=2.0)

    # The pane never settled, so it polled for the whole timeout and gave up quietly.
    assert clock.now - 1000.0 >= 2.0


def test_settle_pane_gives_up_quietly_while_the_pane_keeps_changing(
    tmux: _Tmux, clock: _Clock
) -> None:
    counter = iter(range(1000))
    tmux.handlers["capture-pane"] = lambda argv: subprocess.CompletedProcess(
        argv, 0, stdout=f"frame {next(counter)}", stderr=""
    )

    bridge._settle_pane(_SOCK, _TARGET, timeout_s=1.0)  # must not raise

    assert clock.now - 1000.0 >= 1.0


# ── message injection ────────────────────────────────────────


def test_inject_rejects_empty_content(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="requires non-empty content"):
        bridge.inject_user_message(tmp_path, content="")


def test_inject_fails_fast_when_the_tui_exited(tmp_path: Path, tmux: _Tmux, clock: _Clock) -> None:
    _advertise(tmp_path)
    tmux.handlers["has-session"] = lambda argv: subprocess.CompletedProcess(argv, 1, "", "")

    with pytest.raises(RuntimeError, match="goose terminal is no longer running"):
        bridge.inject_user_message(tmp_path, content="hello")

    assert tmux.subcommands() == ["has-session"]  # never settled or pasted


def test_inject_pastes_content_then_submits_with_one_enter(
    tmp_path: Path, tmux: _Tmux, clock: _Clock
) -> None:
    _advertise(tmp_path)
    pasted: list[bytes] = []
    tmux.handlers["capture-pane"] = _capture_returns(
        "idle pane", "idle pane", "idle pane", "idle pane"
    )

    def load_buffer(argv: list[str]) -> subprocess.CompletedProcess[str]:
        pasted.append(Path(argv[-1]).read_bytes())
        return subprocess.CompletedProcess(argv, 0, "", "")

    tmux.handlers["load-buffer"] = load_buffer
    # After the paste the message text shows up in the pane.
    seen_paste = {"done": False}

    def paste_buffer(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen_paste["done"] = True
        tmux.handlers["capture-pane"] = _capture_returns("> multi line\nsecond line")
        return subprocess.CompletedProcess(argv, 0, "", "")

    tmux.handlers["paste-buffer"] = paste_buffer

    bridge.inject_user_message(tmp_path, content="multi line\nsecond line")

    assert seen_paste["done"]
    # Draft cleared, content pasted with a trailing newline (CR-encoded), single Enter last.
    assert tmux.send_keys() == ["C-a", "C-k", "Enter"]
    assert pasted == [b"multi line\rsecond line\r"]
    order = tmux.subcommands()
    assert order.index("load-buffer") < order.index("paste-buffer") < len(order) - 1
    assert order[-1] == "send-keys"
    paste_call = next(argv for argv in tmux.calls if argv[3] == "paste-buffer")
    assert paste_call[4:] == ["-p", "-d", "-b", bridge._PASTE_BUFFER, "-t", _TARGET]
    # The temp paste file is cleaned up even though tmux consumed it.
    assert list(tmp_path.glob("paste_*")) == []


def test_inject_waits_for_the_paste_to_render_before_enter(
    tmp_path: Path, tmux: _Tmux, clock: _Clock
) -> None:
    _advertise(tmp_path)
    captures = iter(["idle"] * 4 + ["not yet", "still not", "finished message here"])
    tmux.handlers["capture-pane"] = lambda argv: subprocess.CompletedProcess(
        argv, 0, stdout=next(captures), stderr=""
    )

    bridge.inject_user_message(tmp_path, content="finished message here")

    # The Enter waited out two empty polls (plus the settle pause).
    assert tmux.send_keys()[-1] == "Enter"
    assert clock.slept.count(bridge._POLL_INTERVAL_S) >= 5


def test_inject_submits_blind_when_content_has_no_usable_needle(
    tmp_path: Path, tmux: _Tmux, clock: _Clock
) -> None:
    _advertise(tmp_path)
    tmux.handlers["capture-pane"] = _capture_returns("idle")

    bridge.inject_user_message(tmp_path, content="ok")

    assert tmux.send_keys()[-1] == "Enter"
    # After settling, the only further wait is the fixed paste-settle pause.
    assert clock.slept[-1] == bridge._PASTE_SETTLE_S


def test_inject_gives_up_waiting_for_render_after_the_commit_timeout(
    tmp_path: Path, tmux: _Tmux, clock: _Clock
) -> None:
    _advertise(tmp_path)
    tmux.handlers["capture-pane"] = _capture_returns("idle")  # needle never appears

    bridge.inject_user_message(tmp_path, content="this never renders")

    assert tmux.send_keys()[-1] == "Enter"
    assert clock.now - 1000.0 >= bridge._PASTE_COMMIT_TIMEOUT_S


def test_inject_removes_the_paste_file_when_tmux_fails(
    tmp_path: Path, tmux: _Tmux, clock: _Clock
) -> None:
    _advertise(tmp_path)
    tmux.handlers["capture-pane"] = _capture_returns("idle")
    tmux.handlers["load-buffer"] = lambda argv: subprocess.CompletedProcess(argv, 1, "", "boom")

    with pytest.raises(RuntimeError, match="tmux command failed"):
        bridge.inject_user_message(tmp_path, content="hello world")

    assert list(tmp_path.glob("paste_*")) == []
    assert "Enter" not in tmux.send_keys()


# ── interrupt / kill / pane access ───────────────────────────


def test_inject_interrupt_sends_escape(tmp_path: Path, tmux: _Tmux, clock: _Clock) -> None:
    _advertise(tmp_path)

    bridge.inject_interrupt(tmp_path)

    assert tmux.calls == [["tmux", "-S", _SOCK, "send-keys", "-t", _TARGET, "Escape"]]


def test_kill_session_kills_the_tmux_session(tmp_path: Path, tmux: _Tmux, clock: _Clock) -> None:
    _advertise(tmp_path)

    bridge.kill_session(tmp_path)

    assert tmux.calls == [["tmux", "-S", _SOCK, "kill-session", "-t", _TARGET]]


def test_interrupt_and_kill_fail_when_no_target_is_advertised(
    tmp_path: Path, tmux: _Tmux, clock: _Clock
) -> None:
    with pytest.raises(RuntimeError, match="not advertised"):
        bridge.inject_interrupt(tmp_path, timeout_s=1.0)
    with pytest.raises(RuntimeError, match="not advertised"):
        bridge.kill_session(tmp_path, timeout_s=1.0)
    assert tmux.calls == []


def test_capture_goose_pane_distinguishes_missing_dead_and_empty(
    tmp_path: Path, tmux: _Tmux
) -> None:
    assert bridge.capture_goose_pane(tmp_path) is None  # nothing advertised

    _advertise(tmp_path)
    tmux.handlers["has-session"] = lambda argv: subprocess.CompletedProcess(argv, 1, "", "")
    assert bridge.capture_goose_pane(tmp_path) is None  # dead pane

    tmux.handlers["has-session"] = lambda argv: subprocess.CompletedProcess(argv, 0, "", "")
    tmux.handlers["capture-pane"] = _capture_returns("Allow tool?")
    assert bridge.capture_goose_pane(tmp_path) == "Allow tool?"

    tmux.handlers["capture-pane"] = _capture_returns("")
    assert bridge.capture_goose_pane(tmp_path) == ""  # live but empty, not None


def test_send_goose_pane_keys_forwards_each_key(tmp_path: Path, tmux: _Tmux) -> None:
    _advertise(tmp_path)

    bridge.send_goose_pane_keys(tmp_path, "Down", "Enter")

    assert tmux.calls == [["tmux", "-S", _SOCK, "send-keys", "-t", _TARGET, "Down", "Enter"]]


def test_send_goose_pane_keys_requires_an_advertised_target(tmp_path: Path, tmux: _Tmux) -> None:
    with pytest.raises(RuntimeError, match="goose-native tmux target not advertised"):
        bridge.send_goose_pane_keys(tmp_path, "Enter")

    assert tmux.calls == []
