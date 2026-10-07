"""Actual terminal-group signals against disposable hosts and sleeper runners."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.posix_only


def _wait_file(path: Path, proc: subprocess.Popen[bytes], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc.poll() is not None:
            break
        time.sleep(0.02)
    assert path.exists(), (path.parent / "rig.log").read_text()


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[object]:
    """Launch only this fixture's process group; clean up its recorded children."""
    processes: list[tuple[subprocess.Popen[bytes], Path]] = []

    def start(
        mode: str, behavior: str = "ack"
    ) -> tuple[subprocess.Popen[bytes], Path, dict[str, int]]:
        root = tmp_path / f"{mode}-{behavior}"
        root.mkdir()
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("OMNIGENT_", "AP_", "DATABRICKS_", "OPENAI_", "ANTHROPIC_"))
        }
        env["OMNIGENT_DATA_DIR"] = str(root / "data")
        env["HOME"] = str(root / "home")
        env["OMNIGENT_CONFIG_HOME"] = str(root / "config")
        env["XDG_CONFIG_HOME"] = str(root / "config")
        env["XDG_DATA_HOME"] = str(root / "data")
        env["XDG_CACHE_HOME"] = str(root / "cache")
        env["OMNIGENT_TELEMETRY_ENABLED"] = "false"
        env["OMNIGENT_DEBUG_LOGS_ENABLED"] = "false"
        env["OMNIGENT_RUNNER_ZYGOTE"] = "0"
        script = Path(__file__).with_name("shutdown_signal_rig.py")
        with (root / "rig.log").open("wb") as log:
            proc = subprocess.Popen(
                [sys.executable, str(script), str(root), mode, behavior],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        processes.append((proc, root))
        _wait_file(root / "ready.json", proc)
        ready = json.loads((root / "ready.json").read_text())
        assert ready["host_pid"] == proc.pid == ready["host_group"]
        assert ready["host_group"] != os.getpgrp()
        return proc, root, ready

    yield start
    for proc, root in processes:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
        # SIGKILL cannot run the rig's cleanup, so reap only its known sleeper group.
        if not (root / "stopped").exists() and (root / "ready.json").exists():
            ready = json.loads((root / "ready.json").read_text())
            group = ready["runner_group"]
            assert group != os.getpgrp()
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group, signal.SIGKILL)


def _frames(root: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in (root / "frames.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("mode", ["direct", "zygote"])
def test_foreground_group_ctrl_c_records_intent_before_runner_teardown(rig, mode: str) -> None:
    proc, root, ready = rig(mode)
    assert ready["runner_group"] != ready["host_group"]
    os.killpg(proc.pid, signal.SIGINT)
    assert proc.wait(timeout=10) == 0, (root / "rig.log").read_text()
    shutdown = next(row for row in _frames(root) if row["frame"]["kind"] == "host.shutdown")
    assert shutdown["runner_alive"] is True
    assert shutdown["frame"]["runner_ids"] == ["disposable-runner"]
    intent = shutdown["frame"]["intent"]
    assert intent["reason"] == "host_interrupted_sigint"
    assert intent["signal_name"] == "SIGINT"
    assert intent["initiator"] == "unknown"
    assert intent["initiator_user_id"] is None
    assert (root / "stopped").exists()


@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGHUP"])
def test_raw_signal_keeps_unknown_actor_and_reason(rig, signal_name: str) -> None:
    sig = getattr(signal, signal_name)
    proc, root, _ = rig("direct")
    os.killpg(proc.pid, sig)
    assert proc.wait(timeout=10) == -sig, (root / "rig.log").read_text()
    intent = _frames(root)[0]["frame"]["intent"]
    assert intent["reason"] == intent["initiator"] == "unknown"
    assert intent["initiator_user_id"] is None
    assert intent["signal_name"] == sig.name
    exit_row = json.loads((root / "exit.json").read_text())
    assert exit_row["reason"] == "signal"
    assert exit_row["signal"] == sig.name


def test_live_host_preserves_inherited_ignored_sighup(rig) -> None:
    proc, root, _ = rig("direct", "ignored_hup")
    os.killpg(proc.pid, signal.SIGHUP)
    time.sleep(0.3)
    assert proc.poll() is None
    assert not (root / "frames.jsonl").exists()
    os.killpg(proc.pid, signal.SIGINT)
    assert proc.wait(timeout=10) == 0, (root / "rig.log").read_text()
    assert _frames(root)[0]["frame"]["intent"]["reason"] == "host_interrupted_sigint"


@pytest.mark.parametrize("behavior", ["unresponsive", "blocked_loop"])
def test_stop_signal_keeps_watchdog_with_unresponsive_peer_or_loop(rig, behavior: str) -> None:
    proc, root, _ = rig("direct", behavior)
    started = time.monotonic()
    os.killpg(proc.pid, signal.SIGTERM)
    assert proc.wait(timeout=8) == -signal.SIGTERM, (root / "rig.log").read_text()
    assert time.monotonic() - started < 8
    exit_row = json.loads((root / "exit.json").read_text())
    assert exit_row["reason"] == "signal" and exit_row["signal"] == "SIGTERM"
    if behavior == "unresponsive":
        assert _frames(root)[0]["runner_alive"] is True
        assert _frames(root)[0]["frame"]["intent"]["reason"] == "unknown"


def test_unresponsive_server_has_bounded_shutdown_and_second_interrupt_escape(rig) -> None:
    proc, root, _ = rig("direct", "unresponsive")
    started = time.monotonic()
    os.killpg(proc.pid, signal.SIGINT)
    assert proc.wait(timeout=8) == 0
    assert time.monotonic() - started < 8
    assert _frames(root)[0]["runner_alive"] is True

    proc, root, _ = rig("direct", "repeat")
    os.killpg(proc.pid, signal.SIGINT)
    _wait_file(root / "frames.jsonl", proc)
    started = time.monotonic()
    os.killpg(proc.pid, signal.SIGINT)
    assert proc.wait(timeout=8) == 130
    assert (root / "forced-interrupt").exists()
    assert (root / "handlers-restored").exists()
    assert time.monotonic() - started < 8  # Notification budget in this rig is 20 seconds.


def test_second_interrupt_escapes_synchronous_runner_cleanup(rig) -> None:
    proc, root, _ = rig("direct", "blocked_cleanup")
    os.killpg(proc.pid, signal.SIGINT)
    _wait_file(root / "cleanup-waiting", proc)
    shutdown = _frames(root)[0]
    assert shutdown["runner_alive"] is True
    assert shutdown["frame"]["intent"]["reason"] == "host_interrupted_sigint"
    assert shutdown["frame"]["intent"]["initiator"] == "unknown"
    started = time.monotonic()
    os.killpg(proc.pid, signal.SIGINT)
    assert proc.wait(timeout=2) == 130, (root / "rig.log").read_text()
    assert time.monotonic() - started < 2  # The production wait timeout is five seconds.
    assert (root / "forced-interrupt").exists()
    assert (root / "handlers-restored").exists()
    assert not (root / "stopped").exists()
    assert len(_frames(root)) == 1


@pytest.mark.parametrize("mode", ["direct", "zygote"])
def test_signal_never_swallows_runner_that_already_crashed(rig, mode: str) -> None:
    proc, root, _ = rig(mode, "prior_crash")
    os.killpg(proc.pid, signal.SIGINT)
    assert proc.wait(timeout=10) == 0
    frames = [row["frame"] for row in _frames(root)]
    assert [frame["kind"] for frame in frames] == ["host.runner_exited", "host.shutdown"]
    assert frames[0]["runner_id"] == "disposable-runner"
    assert frames[1]["runner_ids"] == []


def test_sigkill_has_no_requested_shutdown_evidence(rig) -> None:
    proc, root, _ = rig("direct")
    os.killpg(proc.pid, signal.SIGKILL)
    assert proc.wait(timeout=10) == -signal.SIGKILL
    assert not (root / "frames.jsonl").exists()
