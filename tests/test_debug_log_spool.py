"""Tests for the debug-log spool (local durability + at-most-once replay)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from omnigent import debug_log_spool as sp


def _rows(n: int, prefix: str = "row") -> list[dict[str, object]]:
    return [
        {"message": f"{prefix} {i}", "client_time": int(time.time() * 1_000_000), "attributes": {}}
        for i in range(n)
    ]


def _spool(tmp_path: Path, dest: str = "https://zerobus.example/insert") -> sp.DebugLogSpool:
    return sp.DebugLogSpool(tmp_path / "spool", dest)


def _always_continue() -> bool:
    return True


def _files(spool: sp.DebugLogSpool, suffix: str = ".jsonl") -> list[Path]:
    return sorted(spool.directory.glob(f"*{suffix}"))


def test_write_then_replay_delivers_tagged_rows_and_deletes(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    assert spool.write(_rows(250)) == 250
    assert len(_files(spool)) == 3  # 100 + 100 + 50
    assert all(oct(p.stat().st_mode & 0o777) == "0o600" for p in _files(spool))

    delivered: list[dict[str, object]] = []

    def deliver(batch: list[dict[str, object]]) -> sp.DeliveryResult:
        delivered.extend(batch)
        return "delivered"

    assert spool.replay(deliver, should_continue=_always_continue) == "done"
    assert [r["message"] for r in delivered] == [f"row {i}" for i in range(250)]
    attrs = delivered[0]["attributes"]
    assert isinstance(attrs, dict)
    assert attrs["spooled"] == "true"
    assert float(attrs["spool_delay_s"]) >= 0
    assert _files(spool) == []


def test_failed_replay_keeps_file_for_retry(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    spool.write(_rows(150))

    assert spool.replay(lambda _b: "failed", should_continue=_always_continue) == "failed"
    assert len(_files(spool)) == 2
    assert _files(spool, ".sending") == []


@pytest.mark.parametrize("result", ["unknown", "rejected"])
def test_ambiguous_or_rejected_replay_is_dropped_not_retried(
    tmp_path: Path, result: sp.DeliveryResult
) -> None:
    spool = _spool(tmp_path)
    spool.write(_rows(10))
    calls: list[int] = []

    def deliver(batch: list[dict[str, object]]) -> sp.DeliveryResult:
        calls.append(len(batch))
        return result

    assert spool.replay(deliver, should_continue=_always_continue) == "done"
    assert spool.replay(deliver, should_continue=_always_continue) == "done"
    assert calls == [10]
    assert list(spool.directory.glob("*.json*")) == []


def test_leftover_sending_file_is_dropped_unsent(tmp_path: Path) -> None:
    """A file a dead uploader left mid-POST may already be in ZeroBus."""
    spool = _spool(tmp_path)
    spool.write(_rows(5, prefix="maybe-sent"))
    spool.write(_rows(5, prefix="fresh"))
    first = _files(spool)[0]
    os.replace(first, first.with_suffix(".sending"))
    delivered: list[dict[str, object]] = []

    def deliver(batch: list[dict[str, object]]) -> sp.DeliveryResult:
        delivered.extend(batch)
        return "delivered"

    assert spool.replay(deliver, should_continue=_always_continue) == "done"
    assert {str(r["message"]).split()[0] for r in delivered} == {"fresh"}
    assert list(spool.directory.glob("*.sending")) == []


def test_other_destination_files_are_left_alone(tmp_path: Path) -> None:
    prod = _spool(tmp_path, "https://prod/insert")
    dev = _spool(tmp_path, "https://dev/insert")
    prod.write(_rows(3))
    calls: list[int] = []

    def deliver(batch: list[dict[str, object]]) -> sp.DeliveryResult:
        calls.append(len(batch))
        return "delivered"

    assert dev.replay(deliver, should_continue=_always_continue) == "done"
    assert calls == []
    assert len(_files(prod)) == 1


def test_concurrent_replay_is_busy(tmp_path: Path) -> None:
    first = _spool(tmp_path)
    second = _spool(tmp_path)
    first.write(_rows(3))
    nested: list[sp.ReplayResult] = []

    def deliver(batch: list[dict[str, object]]) -> sp.DeliveryResult:
        nested.append(second.replay(lambda _b: "delivered", should_continue=_always_continue))
        return "delivered"

    assert first.replay(deliver, should_continue=_always_continue) == "done"
    assert nested == ["busy"]


def test_should_continue_pauses_between_files(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    spool.write(_rows(300))
    sent: list[int] = []
    gate = iter([True, False])

    def deliver(batch: list[dict[str, object]]) -> sp.DeliveryResult:
        sent.append(len(batch))
        return "delivered"

    assert spool.replay(deliver, should_continue=lambda: next(gate)) == "paused"
    assert sent == [100]
    assert len(_files(spool)) == 2


def test_size_cap_drops_oldest_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sp, "MAX_FILES", 3)
    spool = _spool(tmp_path)
    for i in range(5):
        spool.write(_rows(1, prefix=f"batch{i}"))

    remaining = [json.loads(p.read_text().splitlines()[1])["message"] for p in _files(spool)]
    assert remaining == ["batch2 0", "batch3 0", "batch4 0"]


def test_expired_files_are_dropped(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    spool.write(_rows(2))
    (path,) = _files(spool)
    old_ms = int((time.time() - sp.MAX_AGE_S - 60) * 1000)
    os.replace(path, path.with_name(f"{old_ms:013d}-{path.name.split('-', 1)[1]}"))

    calls: list[int] = []
    spool.replay(lambda b: calls.append(len(b)) or "delivered", should_continue=_always_continue)
    assert calls == []
    assert _files(spool) == []


def test_write_respects_deadline(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    assert spool.write(_rows(500), deadline=time.monotonic() - 1) == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fork / signals")
def test_killed_uploader_releases_lock_and_its_batch_is_not_resent(tmp_path: Path) -> None:
    """SIGKILL mid-POST: the lock frees with the process; the batch is dropped."""
    spool = _spool(tmp_path)
    spool.write(_rows(4, prefix="in-flight"))
    script = textwrap.dedent(
        f"""
        import time
        from pathlib import Path
        from omnigent.debug_log_spool import DebugLogSpool
        spool = DebugLogSpool(Path({str(spool.directory)!r}), "https://zerobus.example/insert")
        def deliver(batch):
            print("sending", flush=True)
            time.sleep(60)
        spool.replay(deliver, should_continue=lambda: True)
        """
    )
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "sending"
        assert spool.replay(lambda _b: "delivered", should_continue=_always_continue) == "busy"
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()

    calls: list[int] = []
    spool.replay(lambda b: calls.append(len(b)) or "delivered", should_continue=_always_continue)
    assert calls == []
    assert list(spool.directory.glob("*.json*")) == []
    assert list(spool.directory.glob("*.sending")) == []


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_forked_child_does_not_pin_a_dead_parents_upload_lock(tmp_path: Path) -> None:
    """A runner forked while the lock is held must not keep it after the parent dies."""
    spool = _spool(tmp_path)
    spool.directory.mkdir(parents=True)
    fd = spool._try_lock()
    assert fd is not None
    pid = os.fork()
    if pid == 0:  # child: outlive the parent's hold
        time.sleep(3)
        os._exit(0)
    try:
        # Simulate the parent dying while holding the lock: its fd closes
        # without an explicit unlock.
        sp._held_lock_fds.discard(fd)
        os.close(fd)
        other = _spool(tmp_path)
        # The child closes its copy in an after-fork hook; give it a moment to run.
        deadline = time.monotonic() + 1.5
        reacquired = other._try_lock()
        while reacquired is None and time.monotonic() < deadline:
            time.sleep(0.05)
            reacquired = other._try_lock()
        assert reacquired is not None, "the forked child kept the upload lock held"
        other._unlock(reacquired)
    finally:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
