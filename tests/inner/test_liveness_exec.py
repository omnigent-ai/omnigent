"""Failure-path tests for the worker liveness wrapper."""

from __future__ import annotations

import os
import signal
from unittest.mock import Mock

import pytest

from omnigent.inner import _liveness_exec


def test_refresh_census_does_not_adopt_reused_worker_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = Mock(pid=43210)
    replacement = Mock(pid=43210)
    replacement.create_time.return_value = 200.0
    replacement.children.return_value = [Mock(pid=43211)]
    census = {43210: 100.0}
    monkeypatch.setattr(_liveness_exec.psutil, "Process", lambda pid: replacement)

    _liveness_exec._refresh_census(child, census)

    assert census == {43210: 100.0}
    replacement.children.assert_not_called()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is POSIX-only")
def test_detached_reaper_fork_failure_falls_back_to_inline_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = Mock()
    signals: list[tuple[int, int]] = []
    pgid = os.getpid()

    monkeypatch.setattr(
        "omnigent.inner._liveness_exec.os.fork",
        Mock(side_effect=OSError("ENOMEM")),
    )
    monkeypatch.setattr("omnigent.inner._liveness_exec._group_members", Mock(return_value=[]))
    monkeypatch.setattr(
        "omnigent.inner._liveness_exec._signal_members",
        lambda group, sig: signals.append((group, sig)),
    )

    _liveness_exec._terminate_group(pgid, child, detached_reaper=True)

    assert signals == [
        (pgid, signal.SIGTERM),
        (pgid, getattr(signal, "SIGKILL", signal.SIGTERM)),
    ]
    child.wait.assert_called_once_with(timeout=1)
