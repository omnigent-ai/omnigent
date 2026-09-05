"""Unit tests for the leaked-process reaper.

Spawns real, disposable child processes (plain ``sleep`` loops whose
command line or environment carries the attribution markers) so the
matcher and the TERM→KILL escalation are exercised against live pids —
no server, no daemon.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from omnigent.testing.process_reaper import (
    find_leaked_omnigent_processes,
    reap_leaked_omnigent_processes,
)

# A child that lives until reaped. The argv embeds marker tokens so the
# matcher can attribute it by command line.
_SLEEP_CHILD = "import time\ntime.sleep(120)\n"

# A child that ignores SIGTERM, forcing the reaper's KILL escalation.
_STUBBORN_CHILD = (
    "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(120)\n"
)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A fake session data dir used as the attribution key."""
    d = tmp_path / "omnigent-pytest-abc123"
    d.mkdir()
    return d


def _spawn(code: str, *argv_markers: str, env: dict[str, str] | None = None) -> subprocess.Popen:
    """Spawn a disposable python child carrying attribution markers.

    :param code: Python source the child runs.
    :param argv_markers: Extra argv tokens (visible in its command line).
    :param env: Optional environment override for the child.
    :returns: The child process handle.
    """
    return subprocess.Popen(
        [sys.executable, "-c", code, *argv_markers],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _kill_quietly(proc: subprocess.Popen) -> None:
    """Best-effort cleanup for a test child."""
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)


def test_matches_by_cmdline_reference(data_dir: Path) -> None:
    """A process whose argv references the data dir is attributed."""
    child = _spawn(_SLEEP_CHILD, "omnigent-server", f"sqlite:///{data_dir}/chat.db")
    try:
        found = find_leaked_omnigent_processes(data_dir)
        assert child.pid in {p.pid for p in found}
    finally:
        _kill_quietly(child)


def test_matches_by_environment(data_dir: Path) -> None:
    """A process whose OMNIGENT_DATA_DIR env equals the data dir is attributed."""
    child = _spawn(
        _SLEEP_CHILD,
        "omnigent-daemon-marker",
        env={"OMNIGENT_DATA_DIR": str(data_dir), "PATH": "/usr/bin:/bin"},
    )
    try:
        found = find_leaked_omnigent_processes(data_dir)
        assert child.pid in {p.pid for p in found}
    finally:
        _kill_quietly(child)


def test_ignores_processes_without_omnigent_in_cmdline(data_dir: Path) -> None:
    """Matching the data dir alone is not enough — 'omnigent' must appear in argv.

    Uses ``/bin/sleep`` rather than ``sys.executable``: a repo-venv
    interpreter path can itself contain "omnigent", which would defeat
    the point of this negative case.
    """
    child = subprocess.Popen(
        ["sleep", "120"],
        env={"OMNIGENT_DATA_DIR": str(data_dir), "PATH": "/usr/bin:/bin"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        found = find_leaked_omnigent_processes(data_dir)
        assert child.pid not in {p.pid for p in found}
    finally:
        _kill_quietly(child)


def test_ignores_pytest_processes(data_dir: Path) -> None:
    """A concurrent pytest carrying the data dir is never attributed.

    xdist workers and nested pytest runs are exec'd with the parent's
    environment (their conftest re-points ``os.environ`` only after
    exec), so a live pytest can legitimately reference this session's
    data dir without being a leak.
    """
    child = _spawn(
        _SLEEP_CHILD,
        "-m",
        "pytest",
        "omnigent-suite",
        env={"OMNIGENT_DATA_DIR": str(data_dir), "PATH": "/usr/bin:/bin"},
    )
    try:
        found = find_leaked_omnigent_processes(data_dir)
        assert child.pid not in {p.pid for p in found}
    finally:
        _kill_quietly(child)


def test_pytest_guard_is_token_based_not_substring(data_dir: Path) -> None:
    """An orphan whose data-dir NAME contains 'pytest' is still attributed.

    The real data dirs are named ``omnigent-pytest-<rand>``, and a leaked
    server's ``--database-uri`` embeds that name — a substring guard
    would exempt exactly the orphans this module exists to reap.
    """
    child = _spawn(_SLEEP_CHILD, "omnigent-server", f"sqlite:///{data_dir}/chat.db")
    try:
        assert "pytest" in str(data_dir)  # the fixture mimics the real prefix
        found = find_leaked_omnigent_processes(data_dir)
        assert child.pid in {p.pid for p in found}
    finally:
        _kill_quietly(child)


def test_ignores_sibling_dir_with_matching_prefix(data_dir: Path) -> None:
    """A sibling session dir that merely PREFIX-matches is never attributed.

    ``mkdtemp`` names are random-suffixed, so one session's dir can be a
    string prefix of another's (``omnigent-pytest-a`` vs
    ``omnigent-pytest-abcd``). A substring match on the command line would
    attribute — and kill — the sibling session's process.
    """
    sibling = Path(f"{data_dir}bcd")  # e.g. .../omnigent-pytest-abc123bcd
    child = _spawn(_SLEEP_CHILD, "omnigent-server", f"sqlite:///{sibling}/chat.db")
    try:
        found = find_leaked_omnigent_processes(data_dir)
        assert child.pid not in {p.pid for p in found}
    finally:
        _kill_quietly(child)


def test_blank_data_dir_matches_nothing() -> None:
    """A degenerate attribution key must not sweep the whole machine."""
    assert find_leaked_omnigent_processes("") == []
    assert find_leaked_omnigent_processes("/") == []


def test_reap_terminates_leaked_process(data_dir: Path) -> None:
    """reap TERMs an attributed process and reports its command line."""
    child = _spawn(_SLEEP_CHILD, "omnigent-daemon-marker", f"{data_dir}/chat.db")
    try:
        reaped, survivors = reap_leaked_omnigent_processes(data_dir, timeout=10)
        assert any("omnigent-daemon-marker" in cmd for cmd in reaped)
        assert survivors == []
        child.wait(timeout=10)
        assert not psutil.pid_exists(child.pid) or child.poll() is not None
    finally:
        _kill_quietly(child)


def test_reap_escalates_to_kill(data_dir: Path) -> None:
    """A TERM-ignoring orphan is KILLed after the grace period."""
    child = _spawn(_STUBBORN_CHILD, "omnigent-daemon-marker", f"{data_dir}/chat.db")
    try:
        # Give the child a beat to install its SIGTERM handler.
        time.sleep(1.0)
        reaped, survivors = reap_leaked_omnigent_processes(data_dir, timeout=2)
        assert any("omnigent-daemon-marker" in cmd for cmd in reaped)
        assert survivors == []
        child.wait(timeout=10)
        assert child.poll() is not None
    finally:
        _kill_quietly(child)


def test_reap_with_no_leaks_returns_empty(data_dir: Path) -> None:
    """No attributed processes → no-op, empty report."""
    assert reap_leaked_omnigent_processes(data_dir, timeout=1) == ([], [])
