"""Tests for the runner-side harness zygote client.

Drive a REAL zygote subprocess as the fork server (via the host-side
``ZygoteManager`` to spawn it), then connect a runner-side
:class:`HarnessZygoteClient` to a fresh control socket the zygote serves, and
exercise ``fork_harness`` + the ``ZygoteHarnessProc`` shim end to end. ``os.fork``
works on macOS and Linux; only the COW memory savings are Linux-only, so the
protocol paths are fully testable here.
"""

from __future__ import annotations

import asyncio
import os
import socket

import pytest

from omnigent.host.runner_zygote import ZygoteManager
from omnigent.runner._zygote import (
    _ZYGOTE_TEST_CHILD_EXIT_ENV_VAR,
    ZYGOTE_HARNESS_FD_ENV_VAR,
)
from omnigent.runtime.harnesses._harness_zygote_client import (
    HarnessZygoteClient,
    ZygoteHarnessProc,
    ZygoteHarnessUnavailable,
)

# os.fork / AF_UNIX socketpair are POSIX-only.
pytestmark = [pytest.mark.posix_only, pytest.mark.asyncio]


@pytest.fixture
def zygote(tmp_path):
    """A started zygote subprocess (via ZygoteManager), stopped on teardown.

    :param tmp_path: Temp dir for the zygote's own log.
    :returns: The started manager owning a live zygote.
    """
    mgr = ZygoteManager(log_path=tmp_path / "zygote.log")
    mgr.start()
    try:
        yield mgr
    finally:
        mgr.stop()


def _client_on(manager: ZygoteManager) -> HarnessZygoteClient:
    """Build a runner-side client on a dup of the manager's control socket.

    In production each runner gets its own socketpair to the zygote; for a unit
    test we dup the manager's daemon control fd (the zygote handles fork_harness
    on any socket) so the client speaks the real wire protocol to a real zygote.

    :param manager: A started manager.
    :returns: A client wired to the live zygote.
    """
    assert manager._sock is not None
    dup_fd = os.dup(manager._sock.fileno())
    return HarnessZygoteClient(dup_fd)


def _harness_env(exit_code: int, log_path) -> dict[str, str]:
    """A harness fork env whose child exits with *exit_code* via the seam.

    :param exit_code: Code the forked harness child exits with.
    :param log_path: Harness log path the child points stdout at.
    :returns: The harness subprocess env.
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        _ZYGOTE_TEST_CHILD_EXIT_ENV_VAR: str(exit_code),
        "OMNIGENT_PROCESS_LOG_FILE": str(log_path),
    }


async def test_from_env_absent_returns_none(monkeypatch) -> None:
    """Without the inherited fd env var, ``from_env`` yields ``None``.

    A direct ``Popen``-launched runner (no zygote) must get no client so the
    process manager falls back to a direct exec.

    :param monkeypatch: Env patch fixture.
    """
    monkeypatch.delenv(ZYGOTE_HARNESS_FD_ENV_VAR, raising=False)
    assert HarnessZygoteClient.from_env() is None


async def test_fork_harness_runs_and_reports_exit(zygote: ZygoteManager, tmp_path) -> None:
    """``fork_harness`` returns a live shim whose ``wait()`` yields the exit code.

    :param zygote: The started zygote fixture.
    :param tmp_path: Temp dir for the harness log.
    """
    client = _client_on(zygote)
    proc = await client.fork_harness(
        ["--harness", "x", "--conversation-id", "c"],
        _harness_env(0, tmp_path / "h.log"),
    )
    assert isinstance(proc, ZygoteHarnessProc)
    assert isinstance(proc.pid, int)
    assert await asyncio.wait_for(proc.wait(), timeout=10) == 0


async def test_fork_harness_nonzero_exit(zygote: ZygoteManager, tmp_path) -> None:
    """A non-zero harness exit is reported through the shim's ``wait()``.

    :param zygote: The started zygote fixture.
    :param tmp_path: Temp dir for the harness log.
    """
    client = _client_on(zygote)
    proc = await client.fork_harness(
        ["--harness", "x", "--conversation-id", "c"],
        _harness_env(5, tmp_path / "h.log"),
    )
    assert await asyncio.wait_for(proc.wait(), timeout=10) == 5


async def test_fork_harness_raises_when_channel_closed(zygote: ZygoteManager, tmp_path) -> None:
    """A closed control socket surfaces as ``ZygoteHarnessUnavailable``.

    Lets the process manager latch the disable flag and fall back to exec.

    :param zygote: The started zygote fixture.
    :param tmp_path: Unused; present for fixture symmetry.
    """
    # A client over a socketpair with no server end: the peer is closed, so the
    # first exchange hits EOF.
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    right.close()
    client = HarnessZygoteClient(left.fileno())
    with pytest.raises(ZygoteHarnessUnavailable):
        await client.fork_harness(["--harness", "x"], {"PATH": os.environ.get("PATH", "")})


async def test_wait_surfaces_failure_when_zygote_dies_and_harness_gone(
    zygote: ZygoteManager, tmp_path
) -> None:
    """A crashed harness whose code is unrecoverable reads as failure, not 0.

    If the zygote dies and the harness pid is also gone, ``wait()`` must return
    a non-zero sentinel — never ``0``. Returning ``0`` would let
    ``HarnessProcessManager`` treat a harness that never bound as a clean exit
    and hang / tear down believing it succeeded.

    :param zygote: The started zygote fixture.
    :param tmp_path: Temp dir for the harness log.
    """
    from omnigent.runtime.harnesses._harness_zygote_client import _ZYGOTE_LOST_EXIT_CODE

    client = _client_on(zygote)
    # A harness that stays alive so we control when it dies.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "OMNIGENT_RUNNER_ZYGOTE_TEST_CHILD_SLEEP": "30",
        "OMNIGENT_PROCESS_LOG_FILE": str(tmp_path / "h.log"),
    }
    proc = await client.fork_harness(["--harness", "x"], env)

    # Kill the zygote, then the harness: its code is now unrecoverable.
    assert zygote.pid is not None
    os.kill(zygote.pid, 9)
    os.waitpid(zygote.pid, 0)
    os.kill(proc.pid, 9)

    code = await asyncio.wait_for(proc.wait(), timeout=10)
    assert code == _ZYGOTE_LOST_EXIT_CODE
    assert code != 0
