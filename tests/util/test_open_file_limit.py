"""Raising and reporting the open-file limit of Omnigent host processes."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace

import psutil
import pytest

from omnigent.util import open_file_limit
from omnigent.util.open_file_limit import OpenFileLimit, ProcessOpenFiles

_INFINITY = 2**63 - 1


class _FakeResource:
    """``resource`` stand-in whose kernel accepts soft limits up to *max_soft*."""

    RLIMIT_NOFILE = 8
    RLIM_INFINITY = _INFINITY

    def __init__(self, soft: int, hard: int, *, max_soft: int = _INFINITY) -> None:
        self.limits = (soft, hard)
        self.max_soft = max_soft
        self.requested: list[int] = []

    def getrlimit(self, which: int) -> tuple[int, int]:
        assert which == self.RLIMIT_NOFILE
        return self.limits

    def setrlimit(self, which: int, limits: tuple[int, int]) -> None:
        assert which == self.RLIMIT_NOFILE
        soft, hard = limits
        self.requested.append(soft)
        if hard != self.limits[1]:
            raise ValueError("cannot change the hard limit")
        if soft > self.max_soft:
            raise ValueError("current limit exceeds maximum limit")
        self.limits = (soft, hard)


@pytest.mark.parametrize(
    ("limits", "max_soft", "expected", "requested"),
    [
        pytest.param((256, _INFINITY), _INFINITY, 65536, [65536], id="macos-launchd-default"),
        pytest.param((256, _INFINITY), 10240, 10240, [65536, 10240], id="macos-open-max"),
        pytest.param((256, _INFINITY), 4096, 4096, [65536, 10240, 4096], id="tuned-maxfiles"),
        pytest.param(
            (256, _INFINITY), 1024, 1024, [65536, 10240, 4096, 1024], id="only-last-step-fits"
        ),
        pytest.param((256, 4000), _INFINITY, 4000, [4000], id="finite-hard-limit"),
        pytest.param((1024, 1048576), _INFINITY, 65536, [65536], id="linux-default"),
    ],
)
def test_raise_soft_open_file_limit_falls_back_through_fixed_steps(
    monkeypatch: pytest.MonkeyPatch,
    limits: tuple[int, int],
    max_soft: int,
    expected: int,
    requested: list[int],
) -> None:
    """The soft limit rises toward 65536 without touching the hard limit."""
    fake = _FakeResource(*limits, max_soft=max_soft)
    monkeypatch.setitem(sys.modules, "resource", fake)

    assert open_file_limit.raise_soft_open_file_limit() == OpenFileLimit(expected, limits[1])
    assert fake.requested == requested


@pytest.mark.parametrize(
    "limits",
    [
        pytest.param((65536, _INFINITY), id="already-at-target"),
        pytest.param((100000, _INFINITY), id="above-target"),
        pytest.param((_INFINITY, _INFINITY), id="unlimited"),
        pytest.param((4096, 4096), id="at-hard-limit"),
    ],
)
def test_raise_soft_open_file_limit_leaves_sufficient_limits_alone(
    monkeypatch: pytest.MonkeyPatch, limits: tuple[int, int]
) -> None:
    """An inherited limit that is already high enough is never lowered or rewritten."""
    fake = _FakeResource(*limits)
    monkeypatch.setitem(sys.modules, "resource", fake)

    assert open_file_limit.raise_soft_open_file_limit() == OpenFileLimit(*limits)
    assert fake.requested == []


@pytest.mark.parametrize(
    ("soft", "level"),
    [pytest.param(256, logging.WARNING, id="low"), pytest.param(10240, logging.INFO, id="ample")],
)
def test_raise_soft_open_file_limit_reports_a_rejected_raise(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    soft: int,
    level: int,
) -> None:
    """A kernel that rejects every step keeps the old limit and says why."""
    monkeypatch.setitem(sys.modules, "resource", _FakeResource(soft, _INFINITY, max_soft=soft))

    with caplog.at_level(logging.INFO, logger=open_file_limit.__name__):
        assert open_file_limit.raise_soft_open_file_limit() == OpenFileLimit(soft, _INFINITY)

    [record] = caplog.records
    assert record.levelno == level
    assert record.getMessage().startswith(
        f"could not raise soft open-file limit from {soft} (hard unlimited): 65536: "
    )


def test_raise_soft_open_file_limit_is_a_no_op_without_rlimits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows has no ``resource`` module and no open-file soft limit."""
    monkeypatch.setitem(sys.modules, "resource", None)

    assert open_file_limit.raise_soft_open_file_limit() is None


@dataclass
class _FakeProcess:
    pid: int
    fds: int
    command: list[str]
    soft_limit: int | psutil.Error | None = None
    descendants: list[_FakeProcess] = field(default_factory=list)
    error: psutil.Error | None = None

    def __post_init__(self) -> None:
        soft_limit = self.soft_limit
        if soft_limit is not None:
            self.rlimit: Callable[[int], tuple[int, int]] = lambda _which: _read_rlimit(soft_limit)

    def children(self, *, recursive: bool) -> list[_FakeProcess]:
        assert recursive
        if self.error is not None:
            raise self.error
        return self.descendants

    def cmdline(self) -> list[str]:
        if self.error is not None:
            raise self.error
        return self.command

    def num_fds(self) -> int:
        return self.fds


def _read_rlimit(soft_limit: int | psutil.Error) -> tuple[int, int]:
    if isinstance(soft_limit, psutil.Error):
        raise soft_limit
    return soft_limit, _INFINITY


def _install_process_tree(monkeypatch: pytest.MonkeyPatch, root: _FakeProcess) -> None:
    def _process(pid: int) -> _FakeProcess:
        if pid != root.pid:
            raise psutil.NoSuchProcess(pid)
        return root

    _process.num_fds = _FakeProcess.num_fds  # type: ignore[attr-defined]
    fake_psutil = SimpleNamespace(
        Process=_process, Error=psutil.Error, AccessDenied=psutil.AccessDenied, RLIMIT_NOFILE=7
    )
    monkeypatch.setattr(open_file_limit, "psutil", fake_psutil)


def _daemon_tree(*, soft_limit: int | psutil.Error | None) -> _FakeProcess:
    zygote = [sys.executable, "-P", "-m", "omnigent.runner._zygote"]
    return _FakeProcess(
        pid=4242,
        fds=40,
        command=[sys.executable, "-P", "-m", "omnigent.host._daemon_entry"],
        soft_limit=soft_limit,
        descendants=[
            _FakeProcess(pid=4243, fds=20, command=zygote, soft_limit=soft_limit),
            _FakeProcess(pid=4250, fds=230, command=zygote, soft_limit=soft_limit),
            _FakeProcess(pid=4260, fds=900, command=["tmux", "-S", "sock"], soft_limit=soft_limit),
            _FakeProcess(pid=4270, fds=1, command=zygote, error=psutil.NoSuchProcess(4270)),
            _FakeProcess(pid=4280, fds=1, command=zygote, error=psutil.AccessDenied(4280)),
        ],
    )


@pytest.mark.parametrize(
    ("rlimit_soft", "inherited_limit", "limit", "near_limit"),
    [
        pytest.param(256, 65536, 256, [True, False, False], id="readable-rlimit"),
        pytest.param(None, 10240, 10240, [False, False, False], id="macos-recorded-limit"),
        pytest.param(
            psutil.AccessDenied(0), 10240, 10240, [False, False, False], id="denied-rlimit"
        ),
        pytest.param(-1, 65536, None, [False, False, False], id="unlimited-rlimit"),
    ],
)
def test_host_process_open_files_lists_omnigent_processes_closest_to_limit_first(
    monkeypatch: pytest.MonkeyPatch,
    rlimit_soft: int | psutil.Error | None,
    inherited_limit: int,
    limit: int | None,
    near_limit: list[bool],
) -> None:
    """The busiest runner leads; agent CLIs, tmux and unreadable processes are skipped.

    The daemon's recorded limit stands in where psutil cannot read another
    process's rlimit (macOS, or access denied); on Linux unlimited reads as ``-1``.
    """
    _install_process_tree(monkeypatch, _daemon_tree(soft_limit=rlimit_soft))

    usage = open_file_limit.host_process_open_files(4242, inherited_limit=inherited_limit)

    assert usage == [
        ProcessOpenFiles(pid=4250, count=230, limit=limit),
        ProcessOpenFiles(pid=4242, count=40, limit=limit),
        ProcessOpenFiles(pid=4243, count=20, limit=limit),
    ]
    assert [entry.near_limit for entry in usage] == near_limit


@pytest.mark.parametrize(
    ("count", "limit", "near_limit"),
    [
        pytest.param(204, 256, False, id="below"),
        pytest.param(205, 256, True, id="above"),
        pytest.param(8192, 10240, True, id="exactly-80-percent"),
        pytest.param(8191, 10240, False, id="just-below-80-percent"),
        pytest.param(10_000, None, False, id="unknown-limit"),
    ],
)
def test_process_open_files_is_near_limit_from_80_percent(
    count: int, limit: int | None, near_limit: bool
) -> None:
    assert ProcessOpenFiles(pid=1, count=count, limit=limit).near_limit is near_limit


def test_host_process_open_files_is_empty_when_counts_are_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable daemon or a platform without descriptor counts reports nothing."""
    _install_process_tree(monkeypatch, _daemon_tree(soft_limit=256))
    assert open_file_limit.host_process_open_files(1, inherited_limit=None) == []

    denied_root = _daemon_tree(soft_limit=256)
    denied_root.error = psutil.AccessDenied(denied_root.pid)
    _install_process_tree(monkeypatch, denied_root)
    assert open_file_limit.host_process_open_files(4242, inherited_limit=None) == []

    monkeypatch.setattr(
        open_file_limit, "psutil", SimpleNamespace(Process=lambda pid: None, Error=psutil.Error)
    )
    assert open_file_limit.host_process_open_files(4242, inherited_limit=None) == []
