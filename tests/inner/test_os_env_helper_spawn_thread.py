"""The OS-env helper must outlive the thread that started it (#8956).

Under ``linux_bwrap`` the sandbox argv carries ``--die-with-parent``, which sets
``PR_SET_PDEATHSIG``: the kernel kills the helper when the *thread* that spawned it
exits. ``run_sync_on_thread`` runs every helper request on a fresh short-lived
thread, so spawning inline killed the helper as soon as the first request returned.
The next request paid a whole bwrap + interpreter startup in a new
``omnigent-osenv-*`` scratch directory, and ``start_in_scratch`` state did not
survive from one request to the next.

These tests pin the fix: the helper is started from a thread the client owns, and
that thread stays alive until ``close()`` / ``_stop_locked()`` retires it.

- The first test is platform-neutral and asserts the mechanism itself: the
  ``Popen`` happens on a thread that outlives the request, and consecutive
  requests reuse the one helper process.
- The second is the Linux/bwrap regression the issue asks for: two consecutive
  requests keep the same helper process (same pid, same scratch dir).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import omnigent.inner.os_env as os_env_module
from omnigent.inner.async_utils import run_sync_on_thread
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import _HelperProcessClient, _project_root
from omnigent.inner.sandbox import SandboxPolicy, _get_backend

_HELPER_SPAWN_THREAD_NAME = "omnigent-os-env-helper-spawn"


def _inactive_policy() -> SandboxPolicy:
    """An inactive (``sandbox.type: none``) policy: the helper runs unwrapped.

    :returns: A :class:`SandboxPolicy` whose activation is off, so this test
        exercises the helper lifecycle without needing bubblewrap.
    """
    return SandboxPolicy(
        backend_type="none",
        active=False,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=True,
    )


def _bwrap_policy(workspace: Path) -> SandboxPolicy:
    """Resolve an active ``linux_bwrap`` policy a real spec would resolve to.

    The helper is started as ``python -m omnigent.inner.os_env`` with the project
    root on ``PYTHONPATH`` (see ``_HelperProcessClient._start_locked``), so the
    sandbox has to be able to *read* that tree: bwrap mounts only ``/usr`` and
    friends, whatever the cwd is, and the granted roots. Without the grant the
    helper exits 1 with ``ModuleNotFoundError: omnigent.inner.os_env`` before the
    request ever reaches it.

    :param workspace: Directory the helper may write into.
    :returns: The resolved :class:`SandboxPolicy`.
    """
    spec = OSEnvSpec(
        type="caller_process",
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap",
            write_paths=["."],
            read_paths=[str(_project_root())],
        ),
    )
    return _get_backend("linux_bwrap").resolve(spec, workspace)


def _request(client: _HelperProcessClient, path: Path) -> dict:
    """Issue one helper read request through the production thread hop.

    ``run_sync_on_thread`` is what the async surfaces use, and it is the thread it
    creates — not the client — that a PDEATHSIG-bound helper would die with.

    :param client: Helper client under test.
    :param path: File to read.
    :returns: The helper's response mapping.
    """
    return asyncio.run(run_sync_on_thread(client.request, {"op": "read", "path": str(path)}))


def test_helper_is_spawned_from_a_thread_that_outlives_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The helper's Popen must not happen on the request's short-lived thread.

    On the base commit the helper is started from inside ``request()``, i.e. on
    the ``run_sync_on_thread`` worker, so bwrap's ``--die-with-parent`` kills it
    the moment that worker exits. Recording the spawning thread catches that
    without needing bwrap on the test host: the recorded thread must still be
    alive after the request returned, and a second request must reuse the same
    helper process instead of starting a third thread.

    :returns: None.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("hello\n")

    real_popen = subprocess.Popen
    spawned_on: list[threading.Thread] = []

    def _recording_popen(*args: object, **kwargs: object) -> subprocess.Popen:
        spawned_on.append(threading.current_thread())
        return real_popen(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os_env_module.subprocess, "Popen", _recording_popen)

    client = _HelperProcessClient(cwd=workspace, shell_path="/bin/sh", sandbox=_inactive_policy())
    try:
        first = _request(client, target)
        assert "error" not in first, f"first helper request failed: {first}"
        assert spawned_on, "the helper process was never started"

        spawner = spawned_on[0]
        assert spawner.is_alive(), (
            "the thread that started the helper exited with the request; under bwrap "
            "(--die-with-parent / PR_SET_PDEATHSIG) that kills the helper"
        )
        assert spawner is not threading.current_thread()

        assert client._proc is not None
        first_pid = client._proc.pid
        second = _request(client, target)
        assert "error" not in second, f"second helper request failed: {second}"

        assert len(spawned_on) == 1, "a second request started another helper process"
        assert client._proc.pid == first_pid, "the helper was replaced between requests"
        assert client._proc.poll() is None, "the helper is no longer running"
    finally:
        client.close()


def test_close_retires_the_owned_spawn_thread(tmp_path: Path) -> None:
    """Closing the client must not leave the parked spawn thread behind.

    The spawn thread parks after starting the helper (that is what keeps it alive
    as the PDEATHSIG parent), so ``close()`` has to release it — otherwise every
    stopped environment leaks a thread for the process lifetime.

    :returns: None.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("hello\n")

    client = _HelperProcessClient(cwd=workspace, shell_path="/bin/sh", sandbox=_inactive_policy())
    try:
        assert "error" not in _request(client, target)
        assert any(t.name == _HELPER_SPAWN_THREAD_NAME for t in threading.enumerate()), (
            "no owned spawn thread is running for the helper"
        )
    finally:
        client.close()

    live = [
        t for t in threading.enumerate() if t.name == _HELPER_SPAWN_THREAD_NAME and t.is_alive()
    ]
    assert not live, "the helper's spawn thread outlived close()"


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bwrap") is None,
    reason="requires Linux with bubblewrap (bwrap) installed",
)
def test_consecutive_requests_reuse_one_helper_under_bwrap(tmp_path: Path) -> None:
    """Two consecutive requests must keep one helper (pid, process, scratch dir).

    This is the reported symptom: with ``start_in_scratch`` the first request
    wrote its scratch file into a directory that no longer existed by the second
    request, because the helper had been SIGKILLed and restarted in a fresh
    ``omnigent-osenv-*`` tmpdir.

    :returns: None.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("hello\n")

    client = _HelperProcessClient(
        cwd=workspace,
        shell_path="/bin/sh",
        sandbox=_bwrap_policy(workspace),
        start_in_scratch=True,
    )
    try:
        assert "error" not in _request(client, target)
        assert client._proc is not None
        first_pid = client._proc.pid
        first_scratch = client._tmpdir

        assert client._proc.poll() is None, "the helper was already gone after the first request"

        assert "error" not in _request(client, target)
        assert client._proc.pid == first_pid, "the second request restarted the helper"
        assert client._tmpdir == first_scratch, "the helper's scratch directory changed"
        assert client._proc.poll() is None, "the helper exited before the second request returned"
    finally:
        client.close()
