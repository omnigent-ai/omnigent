"""Verify helper ownership survives request threads and ends at client teardown.

Linux Bubblewrap binds helper lifetime to its spawning thread. Cross-platform
checks verify owner lifetime; the Linux case also checks process and scratch-dir reuse.
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
    """Resolve a Bubblewrap policy granting workspace writes and package reads.

    The helper imports omnigent.inner.os_env from the project root.

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
    """Issue a helper read through the production request-thread hop.

    :param client: Helper client under test.
    :param path: File to read.
    :returns: The helper's response mapping.
    """
    return asyncio.run(run_sync_on_thread(client.request, {"op": "read", "path": str(path)}))


def test_helper_is_spawned_from_a_thread_that_outlives_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify owner-thread lifetime and process reuse without requiring Bubblewrap.

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

        # Replacing a dead helper must also retire its parked owner.
        client._proc.kill()
        client._proc.wait(timeout=5)

        assert "error" not in _request(client, target), "no helper after the previous one died"
        assert len(spawned_on) == 2, "the replacement helper reused the dead one's owner thread"
        assert not spawner.is_alive(), (
            "the dead helper's owner thread is still parked; these accumulate across restarts"
        )
        assert spawned_on[1].is_alive(), "the replacement's owner thread did not stay alive"
        assert client._proc.poll() is None, "the replacement helper is not running"
    finally:
        client.close()


def test_close_retires_the_owned_spawn_thread(tmp_path: Path) -> None:
    """Closing the client must not leave the parked spawn thread behind.

    The spawn thread parks after starting the helper (that is what keeps it alive
    as the PDEATHSIG parent), so ``close()`` has to release it — otherwise every
    stopped environment leaks a thread for the process lifetime.

    This asserts against *this* client's thread rather than every thread with the
    helper's name, so an unrelated live client cannot make it pass or fail.

    :returns: None.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("hello\n")

    owned: threading.Thread | None = None
    client = _HelperProcessClient(cwd=workspace, shell_path="/bin/sh", sandbox=_inactive_policy())
    try:
        assert "error" not in _request(client, target)
        owned = client._spawn_thread
        assert owned is not None, "no owned spawn thread is running for the helper"
        assert owned.name == _HELPER_SPAWN_THREAD_NAME
        assert owned.is_alive(), "the owned spawn thread is not running"
        assert client._spawn_thread_exit is not None, "the owner thread's exit event was lost"
    finally:
        client.close()

    assert not owned.is_alive(), "the helper's spawn thread outlived close()"
    assert client._spawn_thread is None, "close() left the retired owner thread published"


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


def test_failed_thread_start_leaves_the_client_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed helper-thread start must not poison the client, or its close().

    ``thread.start()`` raises under thread-resource exhaustion. If the client has
    already published that unstarted thread as its owner thread, the next
    retirement ``join()``s it and raises ``RuntimeError: cannot join thread before
    it is started`` — so every later request fails the same way, and ``close()``
    aborts before retiring the egress proxy and the scratch directory.

    :returns: None.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("hello\n")

    real_start = threading.Thread.start
    failed: list[threading.Thread] = []

    def _start_failing_once(thread: threading.Thread) -> None:
        if thread.name == _HELPER_SPAWN_THREAD_NAME and not failed:
            failed.append(thread)
            raise RuntimeError("can't start new thread")
        real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", _start_failing_once)

    client = _HelperProcessClient(cwd=workspace, shell_path="/bin/sh", sandbox=_inactive_policy())
    try:
        # The failure reaches the caller instead of being swallowed.
        with pytest.raises(RuntimeError, match="can't start new thread"):
            _request(client, target)

        # And nothing unstarted is left published for a later retirement to choke
        # on: this is the state that used to break every subsequent request.
        assert client._spawn_thread is None, "an unstarted thread was left as the owner thread"
        assert client._spawn_thread_exit is None, "a stale owner exit event was left published"

        # A retry starts a fresh owner thread and serves the request.
        assert "error" not in _request(client, target)
        assert client._proc is not None
        assert client._proc.poll() is None, "the helper is not running after the retry"
        assert client._spawn_thread is not None, "the retry did not start an owner thread"
        assert client._spawn_thread.is_alive(), "the retry's owner thread is not alive"
    finally:
        client.close()

    assert client._spawn_thread is None


def test_failure_inside_the_owner_thread_is_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Helper startup failing inside the owner thread must retire it, not park it.

    Distinct from failing to create the thread: here the owner thread runs, the
    helper's own startup raises, and the failure has to reach the requesting
    thread while the owner thread is retired instead of parked forever.

    :returns: None.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("hello\n")

    client = _HelperProcessClient(cwd=workspace, shell_path="/bin/sh", sandbox=_inactive_policy())

    def _boom() -> None:
        raise RuntimeError("helper argv rejected")

    monkeypatch.setattr(client, "_start_locked", _boom)
    try:
        with pytest.raises(RuntimeError, match="helper argv rejected"):
            _request(client, target)
        assert client._spawn_thread is None, "the failed owner thread was left parked"
        assert client._spawn_thread_exit is None, "the failed owner thread's exit event leaked"
    finally:
        client.close()
