"""Unit tests for :mod:`omnigent.inner.os_env` helper-env construction."""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import tracemalloc
from pathlib import Path

import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import (
    _child_shell_env,
    _project_root,
    _read_impl,
    _select_default_shell,
    _shell_impl,
    build_helper_env,
    create_os_environment,
)
from omnigent.inner.sandbox import SandboxPolicy
from omnigent.runner.identity import (
    OMNIGENT_SESSION_ENV_VALUE,
    OMNIGENT_SESSION_ENV_VAR,
    RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
)


def _inactive_policy() -> SandboxPolicy:
    """A ``sandbox.type: none`` policy (user opted out of sandboxing).

    :returns: An inactive :class:`SandboxPolicy` whose ``build_helper_env``
        branch mirrors the parent environment.
    """
    return SandboxPolicy(
        backend_type="none",
        active=False,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=True,
    )


def _active_policy() -> SandboxPolicy:
    """An active policy that drives ``build_helper_env``'s allowlist branch.

    ``build_helper_env`` only consults ``active`` and ``env_passthrough``;
    the ``backend_type`` is never activated here, so ``"none"`` is fine.

    :returns: An active :class:`SandboxPolicy`.
    """
    return SandboxPolicy(
        backend_type="none",
        active=True,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=True,
    )


def test_build_helper_env_inactive_strips_binding_token() -> None:
    """``sandbox.type: none`` mirrors parent env MINUS the binding token.

    Opting out of sandboxing grants the agent broad
    file/network access, but it must NOT additionally leak the runner's
    control-plane auth secret. Asserts ``PATH`` survives (the opt-out
    still mirrors the parent env) while the token is dropped.

    :returns: None.
    """
    parent = {
        "PATH": "/usr/bin",
        RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR: "bug-binding-token-secret",
    }

    env = build_helper_env(parent, _inactive_policy())

    assert RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR not in env
    assert "bug-binding-token-secret" not in env.values()
    assert env["PATH"] == "/usr/bin"


def test_build_helper_env_active_drops_binding_token() -> None:
    """The active allowlist branch never admits the binding token.

    The deny-by-default allowlist excludes the token's name, so even if
    it is present in the parent env it does not reach the helper.

    :returns: None.
    """
    parent = {
        "PATH": "/usr/bin",
        RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR: "bug-binding-token-secret",
    }

    env = build_helper_env(parent, _active_policy())

    assert RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR not in env
    assert "bug-binding-token-secret" not in env.values()
    assert env["PATH"] == "/usr/bin"  # PATH is in the default allowlist


def test_build_helper_env_active_passes_omnigent_session_marker() -> None:
    """The ``OMNIGENT`` session marker survives the active allowlist.

    The marker (set once on the runner process) must reach an agent's
    sandboxed shell so code running there can detect it is inside an
    Omnigent session, the way ``CLAUDE_CODE`` / ``CODEX`` are visible in
    their own agents' shells.

    :returns: None.
    """
    parent = {
        "PATH": "/usr/bin",
        OMNIGENT_SESSION_ENV_VAR: OMNIGENT_SESSION_ENV_VALUE,
    }

    env = build_helper_env(parent, _active_policy())

    assert env[OMNIGENT_SESSION_ENV_VAR] == OMNIGENT_SESSION_ENV_VALUE


# ---------------------------------------------------------------------------
# _shell_impl — timeout result shape
# ---------------------------------------------------------------------------


@pytest.mark.posix_only
def test_shell_impl_timeout_includes_exit_code(tmp_path: Path) -> None:
    """Timed-out shell commands still return the documented result fields.

    POSIX-only: ``shutil.which("bash")`` resolves to the WSL launcher on Windows;
    the native Windows timeout path is covered by
    ``test_shell_impl_timeout_kills_child_tree_on_windows``.
    """
    shell_path = shutil.which("bash") or shutil.which("sh")
    assert shell_path is not None

    result = _shell_impl(
        command="sleep 2",
        timeout=1,
        shell_path=shell_path,
        cwd=tmp_path,
    )

    assert result["stdout"] == ""
    assert result["stderr"] == ""
    assert result["exit_code"] is None
    assert result["timed_out"] is True
    assert result["error"] == "Command timed out after 1 seconds"


# ---------------------------------------------------------------------------
# _read_impl — binary file handling
# ---------------------------------------------------------------------------

_BINARY = b"\x89PNG\r\n\x1a\n\x00\x01\x02\xff"


def test_read_impl_binary_descriptor_for_agent(tmp_path: Path) -> None:
    """With no byte cap (agent ``sys_os_read`` path) binary is not inlined.

    The base64 payload would be useless to the model and could saturate the
    context window, so only a descriptor is returned.

    :returns: None.
    """
    f = tmp_path / "logo.png"
    f.write_bytes(_BINARY)

    result = _read_impl(f, offset=1, limit=2_000)

    assert result["encoding"] == "base64"
    assert result["content"] == ""
    assert result["total_bytes"] == len(_BINARY)
    # Not truncated — the payload was deliberately omitted, not cut short.
    assert result["truncated"] is False
    assert "note" in result


def test_read_impl_binary_inlined_within_cap(tmp_path: Path) -> None:
    """A byte cap larger than the file inlines the whole payload, untruncated.

    :returns: None.
    """
    f = tmp_path / "logo.png"
    f.write_bytes(_BINARY)

    result = _read_impl(f, offset=1, limit=2_000, max_binary_bytes=10 * 1024 * 1024)

    assert result["encoding"] == "base64"
    assert base64.b64decode(result["content"]) == _BINARY
    assert result["total_bytes"] == len(_BINARY)
    assert result["truncated"] is False


def test_read_impl_binary_truncated_at_cap(tmp_path: Path) -> None:
    """A byte cap smaller than the file truncates and flags it.

    :returns: None.
    """
    f = tmp_path / "logo.png"
    f.write_bytes(_BINARY)

    result = _read_impl(f, offset=1, limit=2_000, max_binary_bytes=4)

    assert base64.b64decode(result["content"]) == _BINARY[:4]
    assert result["returned_bytes"] == 4
    assert result["total_bytes"] == len(_BINARY)
    assert result["truncated"] is True


def _make_large_binary(path: Path, size: int) -> None:
    """Write a sparse file with a binary prefix and a logical size of *size*.

    The 8 KB binary prefix forces the prefix-sniff to classify it binary; the
    ``truncate`` extends the (sparse) file to *size* without writing the bytes,
    so the test stays cheap while exercising a large logical file.

    :returns: None.
    """
    with path.open("wb") as fh:
        fh.write(b"\xff\xfe\x00\x01" * 2_048)  # 8 KB of non-UTF-8 bytes
        fh.truncate(size)


def test_read_impl_binary_descriptor_does_not_read_whole_file(tmp_path: Path) -> None:
    """The descriptor path is O(1): it stats the size, never reading content.

    Regression guard for inlining the whole file (``path.read_bytes()``) just
    to compute ``total_bytes`` — which would OOM on large workspace blobs.

    :returns: None.
    """
    size = 256 * 1024 * 1024  # 256 MB logical, only ~8 KB on disk
    f = tmp_path / "big.bin"
    _make_large_binary(f, size)

    tracemalloc.start()
    try:
        result = _read_impl(f, offset=1, limit=2_000)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result["total_bytes"] == size
    assert result["content"] == ""
    # A full read would have allocated ~256 MB; bounded reads stay tiny.
    assert peak < 10 * 1024 * 1024


def test_read_impl_binary_cap_reads_only_the_cap(tmp_path: Path) -> None:
    """The byte-capped path reads at most ``max_binary_bytes``, not the file.

    :returns: None.
    """
    size = 256 * 1024 * 1024
    f = tmp_path / "big.bin"
    _make_large_binary(f, size)

    tracemalloc.start()
    try:
        result = _read_impl(f, offset=1, limit=2_000, max_binary_bytes=16)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result["returned_bytes"] == 16
    assert result["total_bytes"] == size
    assert result["truncated"] is True
    assert peak < 10 * 1024 * 1024


def test_read_impl_multibyte_char_straddling_sniff_boundary_is_text(tmp_path: Path) -> None:
    """A multi-byte char split across the 8 KB sniff boundary stays text.

    The incremental decoder must treat the truncated trailing sequence as
    *incomplete*, not invalid — otherwise valid UTF-8 would be misread as
    binary purely because of where the prefix happened to be cut.

    :returns: None.
    """
    # 8 KB sniff window cuts the 3-byte '€' (0xE2 0x82 0xAC) at byte 8191.
    text = "a" * 8_190 + "€" + "tail\n"
    f = tmp_path / "wide.txt"
    f.write_text(text, encoding="utf-8")

    result = _read_impl(f, offset=1, limit=2_000)

    assert result["encoding"] == "utf-8"
    assert result["content"] == text


def test_read_impl_nul_byte_file_classified_binary(tmp_path: Path) -> None:
    """A NUL byte marks a file binary even though ``\\x00`` is valid UTF-8.

    UTF-16/NUL-laden files decode cleanly as UTF-8, so without an explicit NUL
    check they'd be misread as text and line-windowed into garbage.

    :returns: None.
    """
    # UTF-16-LE-style ASCII: every byte is valid UTF-8, but the interleaved
    # NULs make this binary.
    f = tmp_path / "utf16.bin"
    f.write_bytes(b"H\x00e\x00l\x00l\x00o\x00")

    result = _read_impl(f, offset=1, limit=2_000)

    assert result["encoding"] == "base64"
    assert result["total_bytes"] == 10


# ---------------------------------------------------------------------------
# _child_shell_env — omnigent's own package root must not leak onto the
# PYTHONPATH of agent shell commands (it would shadow the project's packages).
# ---------------------------------------------------------------------------


def test_child_shell_env_strips_project_root_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """omnigent's project root is removed; a project entry is preserved.

    The helper prepends its project root to ``PYTHONPATH`` so it can import
    omnigent at startup. Commands the agent runs must not inherit that entry,
    or omnigent's ``site-packages`` shadows the project venv's own packages.

    :returns: None.
    """
    project_entry = "/opt/venvs/proj/lib/python3.13/site-packages"
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(_project_root()), project_entry]))

    env = _child_shell_env()

    assert env["PYTHONPATH"] == project_entry


def test_child_shell_env_drops_var_when_only_project_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the sole entry is omnigent's root, ``PYTHONPATH`` is unset.

    Leaving an empty ``PYTHONPATH`` would put the shell command's cwd on
    ``sys.path``; dropping the var entirely avoids that surprise.

    :returns: None.
    """
    monkeypatch.setenv("PYTHONPATH", str(_project_root()))

    env = _child_shell_env()

    assert "PYTHONPATH" not in env


def test_child_shell_env_noop_without_pythonpath(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``PYTHONPATH`` in the parent env means nothing to strip.

    :returns: None.
    """
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")

    env = _child_shell_env()

    assert "PYTHONPATH" not in env
    assert env["PATH"] == "/usr/bin"


# ---------------------------------------------------------------------------
# End-to-end: the real helper must not leak omnigent's package root into a
# sys_os_shell command's PYTHONPATH. Guards the wiring in _shell_impl, not
# just _child_shell_env in isolation.
# ---------------------------------------------------------------------------


def test_shell_command_does_not_see_omnigent_project_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shell command's ``PYTHONPATH`` drops omnigent's root, keeps the rest.

    Spawns a real ``caller_process`` helper (``sandbox: none`` so it runs on
    every platform) with omnigent's root pre-seeded on ``PYTHONPATH`` — the
    same shape the helper spawn produces — and asserts the agent's command
    sees the sibling project entry but not omnigent's, so project subprocesses
    resolve their own packages.

    :returns: None.
    """
    project_entry = "/opt/venvs/proj/site-packages"
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(_project_root()), project_entry]))

    os_env = create_os_environment(
        OSEnvSpec(type="caller_process", sandbox=OSEnvSandboxSpec(type="none"))
    )
    assert os_env is not None
    try:
        result = asyncio.run(os_env.shell("echo PP=$PYTHONPATH"))
    finally:
        os_env.close()

    out = result.get("stdout", "")
    assert project_entry in out
    assert str(_project_root()) not in out


class TestSelectDefaultShell:
    """`_select_default_shell` must never pick the WSL bash on Windows."""

    def test_windows_uses_comspec_not_wsl_bash(self, monkeypatch) -> None:
        # On Windows, shutil.which("bash") is the WSL launcher; the selector
        # must ignore it and use the native cmd.exe via %COMSPEC%.
        monkeypatch.setattr(
            "omnigent.inner.os_env.shutil.which",
            lambda name: r"C:\Windows\System32\bash.exe" if name == "bash" else None,
        )
        monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
        assert _select_default_shell(True) == r"C:\Windows\System32\cmd.exe"

    def test_windows_defaults_to_cmd_when_no_comspec(self, monkeypatch) -> None:
        monkeypatch.delenv("COMSPEC", raising=False)
        assert _select_default_shell(True) == "cmd.exe"

    def test_posix_prefers_bash(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "omnigent.inner.os_env.shutil.which",
            lambda name: "/usr/bin/bash" if name == "bash" else None,
        )
        assert _select_default_shell(False) == "/usr/bin/bash"

    def test_posix_falls_back_to_sh(self, monkeypatch) -> None:
        monkeypatch.setattr("omnigent.inner.os_env.shutil.which", lambda name: None)
        assert _select_default_shell(False) == "/bin/sh"


@pytest.mark.windows_only
def test_shell_impl_preserves_quotes_on_windows(tmp_path: Path) -> None:
    """`_shell_impl` must pass a quoted command to cmd.exe verbatim.

    Regression for list-based argv serialization escaping the command's own
    quotes as ``\\"`` (so ``echo "a b"`` produced ``\\"a b\\"``) and for
    ``cmd.exe /c`` stripping the outer quotes of a quote-leading command.
    """
    comspec = os.environ.get("COMSPEC", "cmd.exe")
    result = _shell_impl(
        command='echo "a b"',
        timeout=15,
        shell_path=comspec,
        cwd=tmp_path,
    )
    assert result["exit_code"] == 0, result
    assert "\\" not in result["stdout"], result["stdout"]
    assert "a b" in result["stdout"]


@pytest.mark.windows_only
def test_shell_impl_runs_multiline_command_on_windows(tmp_path: Path) -> None:
    """Regression: multi-line commands must run every line, not silently line 1."""
    comspec = os.environ.get("COMSPEC", "cmd.exe")
    result = _shell_impl(
        command="echo AAA\necho BBB", timeout=15, shell_path=comspec, cwd=tmp_path
    )
    assert result["exit_code"] == 0, result
    assert "AAA" in result["stdout"] and "BBB" in result["stdout"], result


@pytest.mark.windows_only
def test_shell_impl_timeout_kills_child_tree_on_windows(tmp_path: Path) -> None:
    """Regression: a timeout kills the whole tree, not just cmd.exe.

    A ~5s ping child under a 1s timeout must return promptly rather than waiting
    for the orphaned child to finish.
    """
    import time

    comspec = os.environ.get("COMSPEC", "cmd.exe")
    start = time.monotonic()
    result = _shell_impl(
        command="ping -n 6 127.0.0.1 >NUL", timeout=1, shell_path=comspec, cwd=tmp_path
    )
    elapsed = time.monotonic() - start
    assert result["timed_out"] is True, result
    assert elapsed < 3, f"took {elapsed:.1f}s; child tree not killed"


@pytest.mark.windows_only
def test_shell_impl_nonzero_exit_on_windows(tmp_path: Path) -> None:
    """A non-zero exit returns the code without crashing (regression for an
    UnboundLocalError that referenced an undefined `completed` on Windows)."""
    comspec = os.environ.get("COMSPEC", "cmd.exe")
    result = _shell_impl(command="exit /b 7", timeout=15, shell_path=comspec, cwd=tmp_path)
    assert result["exit_code"] == 7, result
    assert result.get("error"), result


@pytest.mark.windows_only
def test_shell_impl_timeout_kills_orphaned_descendant_on_windows(tmp_path: Path) -> None:
    """A grandchild orphaned by a launcher that exits is still killed on timeout.

    The Job Object contains the whole tree; a parent-walk (psutil/taskkill) can't
    reach a descendant whose intermediate parent has already exited.
    """
    import sys
    import time

    marker = tmp_path / "MARKER"
    comspec = os.environ.get("COMSPEC", "cmd.exe")
    # Detach a worker that writes the marker after 6s, then keep cmd busy so the
    # 1s timeout fires; the whole tree (incl. the detached worker) must die.
    cmd = (
        f'start "" /b "{sys.executable}" -c '
        f"\"import time; time.sleep(6); open(r'{marker}', 'w').close()\"\n"
        "ping -n 10 127.0.0.1 >NUL"
    )
    result = _shell_impl(command=cmd, timeout=1, shell_path=comspec, cwd=tmp_path)
    assert result["timed_out"] is True, result
    time.sleep(8)
    assert not marker.exists(), "orphaned worker survived the timeout"


@pytest.mark.windows_only
def test_tree_kill_job_close_spares_but_terminate_kills() -> None:
    """The containment job has no kill-on-close: closing the handle leaves the
    process running (a detached server survives a successful command), while
    ``terminate()`` kills it (the timeout path). Tested at the job API directly
    to avoid cmd.exe quoting in the assertion.
    """
    import subprocess
    import sys
    import time

    from omnigent.inner.windows_jobobject_sandbox import create_tree_kill_job

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        job = create_tree_kill_job()
        assert job is not None and job.assign(sleeper.pid)
        job.close()  # no kill-on-close
        time.sleep(1)
        assert sleeper.poll() is None, "close() killed the process (kill-on-close regression)"
    finally:
        sleeper.kill()
        sleeper.wait(timeout=10)

    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        job = create_tree_kill_job()
        assert job is not None and job.assign(victim.pid)
        job.terminate()  # timeout path: tear the tree down
        victim.wait(timeout=10)
        assert victim.poll() is not None, "terminate() did not kill the process"
    finally:
        if victim.poll() is None:
            victim.kill()
            victim.wait(timeout=10)
        job.close()


@pytest.mark.windows_only
def test_tree_kill_job_terminate_reaches_grandchild(tmp_path: Path) -> None:
    """``terminate()`` kills the whole tree, including a grandchild orphaned by an
    intermediate process — the reach a parent-walk (psutil/taskkill) can't match.
    Helper scripts avoid all shell quoting.
    """
    import subprocess
    import sys
    import time

    from omnigent.inner.windows_jobobject_sandbox import create_tree_kill_job

    marker = tmp_path / "gc_marker.txt"
    gc_py = tmp_path / "gc.py"
    gc_py.write_text(
        f"import time\ntime.sleep(8)\nopen({str(marker)!r}, 'w').close()\n",
        encoding="utf-8",
    )
    parent_py = tmp_path / "parent.py"
    parent_py.write_text(
        f"import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(gc_py)!r}])\n"
        f"time.sleep(30)\n",
        encoding="utf-8",
    )
    parent = subprocess.Popen([sys.executable, str(parent_py)])
    try:
        job = create_tree_kill_job()
        assert job is not None and job.assign(parent.pid)
        time.sleep(2)  # let the parent spawn the grandchild (both now in the job)
        job.terminate()
        parent.wait(timeout=10)
        assert parent.poll() is not None
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)
        job.close()
    time.sleep(10)  # past the grandchild's 8s sleep — its marker must never appear
    assert not marker.exists(), "terminate() did not reach the grandchild"


@pytest.mark.windows_only
def test_shell_impl_job_creation_failure_never_spawns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing containment job fails before a suspended child is created."""
    import omnigent.inner.os_env as os_env_module
    import omnigent.inner.windows_jobobject_sandbox as jobs

    monkeypatch.setattr(jobs, "create_tree_kill_job", lambda: None)

    def _unexpected_popen(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("cmd.exe was spawned without a containment job")

    monkeypatch.setattr(os_env_module.subprocess, "Popen", _unexpected_popen)
    with pytest.raises(OSError, match="could not create a Windows Job Object"):
        os_env_module._run_windows_cmd_shell(
            "echo MUST_NOT_RUN",
            timeout=10,
            shell_path=os.environ.get("COMSPEC", "cmd.exe"),
            cwd=tmp_path,
        )


@pytest.mark.windows_only
def test_shell_impl_job_assignment_failure_never_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected Job assignment kills the suspended cmd.exe instead of running it."""
    import omnigent.inner.os_env as os_env_module
    import omnigent.inner.windows_jobobject_sandbox as jobs
    from omnigent.inner._proc import process_alive

    marker = tmp_path / "must_not_run.txt"
    assigned_pid: list[int] = []
    job_events: list[str] = []

    class _RejectingJob:
        def assign(self, pid: int) -> bool:
            job_events.append("assign")
            assigned_pid.append(pid)
            return False

        def terminate(self) -> None:
            job_events.append("terminate")

        def close(self) -> None:
            job_events.append("close")

    monkeypatch.setattr(jobs, "create_tree_kill_job", _RejectingJob)

    def _unexpected_resume(_pid: int) -> bool:
        raise AssertionError("an uncontained cmd.exe was resumed")

    monkeypatch.setattr(jobs, "resume_process_threads", _unexpected_resume)
    with pytest.raises(OSError, match="could not assign"):
        os_env_module._run_windows_cmd_shell(
            f'echo escaped>"{marker}"',
            timeout=10,
            shell_path=os.environ.get("COMSPEC", "cmd.exe"),
            cwd=tmp_path,
        )

    assert assigned_pid
    assert job_events == ["assign", "terminate", "close"]
    assert not process_alive(assigned_pid[0])
    assert not marker.exists()


@pytest.mark.windows_only
def test_shell_impl_abnormal_exit_terminates_and_reaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation after resume tears down and reaps the contained process tree."""
    import subprocess

    import omnigent.inner.os_env as os_env_module
    from omnigent.inner._proc import process_alive

    real_popen = subprocess.Popen
    created_pid: list[int] = []

    class _InterruptOnce:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._proc = real_popen(*args, **kwargs)
            self._first_communicate = True
            created_pid.append(self._proc.pid)

        @property
        def pid(self) -> int:
            return self._proc.pid

        @property
        def returncode(self) -> int | None:
            return self._proc.returncode

        def communicate(self, *args: object, **kwargs: object) -> tuple[str, str]:
            if self._first_communicate:
                self._first_communicate = False
                raise KeyboardInterrupt
            return self._proc.communicate(*args, **kwargs)

        def kill(self) -> None:
            self._proc.kill()

        def wait(self, *args: object, **kwargs: object) -> int:
            return self._proc.wait(*args, **kwargs)

    monkeypatch.setattr(os_env_module.subprocess, "Popen", _InterruptOnce)
    with pytest.raises(KeyboardInterrupt):
        os_env_module._run_windows_cmd_shell(
            "ping -n 30 127.0.0.1 >nul",
            timeout=30,
            shell_path=os.environ.get("COMSPEC", "cmd.exe"),
            cwd=tmp_path,
        )

    assert created_pid
    assert not process_alive(created_pid[0])


@pytest.mark.windows_only
def test_resume_thread_failure_uses_unsigned_dword_sentinel() -> None:
    """Win32 ``ResumeThread`` failure must compare equal to ``DWORD(-1)``."""
    import ctypes
    import ctypes.wintypes as wintypes

    from omnigent.inner.windows_jobobject_sandbox import _RESUME_THREAD_FAILED

    kernel32 = ctypes.windll.kernel32
    assert kernel32.ResumeThread.restype is wintypes.DWORD
    assert kernel32.ResumeThread(wintypes.HANDLE(-1)) == _RESUME_THREAD_FAILED


@pytest.mark.windows_only
def test_shell_impl_normal_completion_spares_detached_child(tmp_path: Path) -> None:
    """A truly detached child survives the normal close-without-terminate path."""
    import sys
    import time

    marker = tmp_path / "detached_survived.txt"
    worker = tmp_path / "detached_worker.py"
    worker.write_text(
        f"import time\ntime.sleep(2)\nopen({str(marker)!r}, 'w', encoding='utf-8').write('ok')\n",
        encoding="utf-8",
    )
    launcher = tmp_path / "detached_launcher.py"
    launcher.write_text(
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, {str(worker)!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL, close_fds=True, "
        "creationflags=0x00000008 | 0x00000200)\n",
        encoding="utf-8",
    )

    result = _shell_impl(
        command=f'"{sys.executable}" "{launcher}"',
        timeout=10,
        shell_path=os.environ.get("COMSPEC", "cmd.exe"),
        cwd=tmp_path,
    )
    assert result["exit_code"] == 0, result
    assert result["timed_out"] is False, result
    assert not marker.exists(), "detached worker completed before the shell returned"

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists(), "normal Job close killed the detached child"


@pytest.mark.windows_only
def test_shell_impl_runs_from_hostile_temp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A %TEMP% containing spaces, ``&``, and ``%NAME%`` must still run.

    The batch path is expanded once from a private environment variable, so cmd
    does not reinterpret percent sequences inside the resulting path.
    """
    import tempfile

    hostile = tmp_path / "a b & %OMNIGENT_EXPAND_ME% c"
    hostile.mkdir()
    monkeypatch.setenv("OMNIGENT_EXPAND_ME", "expanded")
    monkeypatch.setattr(tempfile, "tempdir", str(hostile))
    comspec = os.environ.get("COMSPEC", "cmd.exe")
    result = _shell_impl(command="echo spacey", timeout=15, shell_path=comspec, cwd=tmp_path)
    assert result["exit_code"] == 0, result
    assert "spacey" in result["stdout"], result


@pytest.mark.windows_only
def test_shell_impl_batch_for_loop_uses_double_percent(tmp_path: Path) -> None:
    """Commands run in batch context, so a ``for`` loop variable is ``%%i`` (not
    the interactive ``%i``). Documents/locks the cmd.exe semantic that the
    batch-file approach requires."""
    comspec = os.environ.get("COMSPEC", "cmd.exe")
    result = _shell_impl(
        command="for %%i in (1 2 3) do @echo N%%i",
        timeout=15,
        shell_path=comspec,
        cwd=tmp_path,
    )
    assert result["exit_code"] == 0, result
    assert "N1" in result["stdout"] and "N3" in result["stdout"], result
