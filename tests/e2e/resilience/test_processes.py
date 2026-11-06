"""Process-state guarantees of the resilience lab's freeze control."""

from __future__ import annotations

import contextlib
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator

import psutil
import pytest

from tests._helpers.polling import poll_until
from tests.e2e.resilience.lab.events import EventLog
from tests.e2e.resilience.lab.processes import frozen

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="SIGSTOP requires POSIX")


@pytest.fixture
def owned_processes() -> Iterator[list[psutil.Process]]:
    children = [
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]) for _ in range(2)
    ]
    processes = [psutil.Process(child.pid) for child in children]
    try:
        yield processes
    finally:
        for child, process in zip(children, processes, strict=True):
            with contextlib.suppress(psutil.NoSuchProcess):
                process.send_signal(signal.SIGCONT)
            child.kill()
            child.wait(timeout=5)


@pytest.mark.parametrize("delayed_signal_name", ["SIGSTOP", "SIGCONT"])
def test_freeze_confirms_signal_delivery_before_emitting_events(
    owned_processes: list[psutil.Process],
    monkeypatch: pytest.MonkeyPatch,
    delayed_signal_name: str,
) -> None:
    owned_process = owned_processes[0]
    delayed_signal = getattr(signal, delayed_signal_name)
    events = EventLog()
    observed: dict[str, str] = {}
    events.subscribe(lambda event: observed.update({event.kind: owned_process.status()}))
    send_signal = owned_process.send_signal
    deliveries: list[threading.Timer] = []

    def delayed_delivery(sig: signal.Signals) -> None:
        if sig == delayed_signal:
            delivery = threading.Timer(0.05, send_signal, args=(sig,))
            deliveries.append(delivery)
            delivery.start()
        else:
            send_signal(sig)

    monkeypatch.setattr(owned_process, "send_signal", delayed_delivery)
    try:
        with frozen([owned_process], events):
            assert owned_process.status() == psutil.STATUS_STOPPED
        assert owned_process.status() != psutil.STATUS_STOPPED
    finally:
        for delivery in deliveries:
            delivery.join(timeout=5)
        monkeypatch.undo()
    assert observed["freeze"] == psutil.STATUS_STOPPED
    assert observed["thaw"] != psutil.STATUS_STOPPED


@pytest.mark.parametrize("failure_stage", ["setup", "body"])
def test_freeze_resumes_process_after_failure(
    owned_processes: list[psutil.Process], monkeypatch: pytest.MonkeyPatch, failure_stage: str
) -> None:
    events = EventLog()

    def fail_stop(_sig: signal.Signals) -> None:
        poll_until(lambda: owned_processes[0].status() == psutil.STATUS_STOPPED)
        raise RuntimeError("freeze setup failed")

    if failure_stage == "setup":
        monkeypatch.setattr(owned_processes[1], "send_signal", fail_stop)
    with pytest.raises(RuntimeError, match=f"freeze {failure_stage} failed"):
        with frozen(owned_processes, events):
            assert failure_stage == "body"
            raise RuntimeError("freeze body failed")
    assert all(process.status() != psutil.STATUS_STOPPED for process in owned_processes)


@pytest.mark.parametrize("reap", [False, True])
def test_freeze_does_not_wait_for_exited_processes(
    owned_processes: list[psutil.Process], reap: bool
) -> None:
    process = owned_processes[0]
    process.kill()
    poll_until(lambda: process.status() == psutil.STATUS_ZOMBIE)
    if reap:
        process.wait(timeout=5)
    with frozen(owned_processes, EventLog()):
        assert owned_processes[1].status() == psutil.STATUS_STOPPED
    assert owned_processes[1].status() != psutil.STATUS_STOPPED
