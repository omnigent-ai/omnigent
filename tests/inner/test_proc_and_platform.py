"""Tests for the cross-platform process/platform primitives and Windows backend.

Covers omnigent._platform, omnigent.inner._proc, the harness IPC endpoint
abstraction, and the windows_jobobject sandbox backend. The platform-specific
assertions are gated with ``posix_only`` / ``windows_only`` markers so the
file runs on both Linux CI and a Windows box.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from omnigent import _platform
from omnigent.inner import _proc


def _spin_cmd() -> list[str]:
    """A short-lived child process that does nothing but sleep."""
    if os.name == "nt":
        return ["cmd", "/c", "ping -n 30 127.0.0.1 >NUL"]
    return ["sleep", "30"]


# --------------------------------------------------------------------------
# _platform
# --------------------------------------------------------------------------


def test_platform_flags_are_mutually_consistent() -> None:
    assert (os.name == "nt") == _platform.IS_WINDOWS
    assert (os.name == "posix") == _platform.IS_POSIX
    # Exactly one OS family is true.
    assert _platform.IS_WINDOWS != _platform.IS_POSIX


def test_default_shell_argv_runs_an_echo() -> None:
    argv = _platform.default_shell_argv("echo omnigent-shell-ok")
    out = subprocess.run(argv, capture_output=True, text=True, check=True)
    assert "omnigent-shell-ok" in out.stdout


@pytest.mark.posix_only
def test_default_interactive_shell_honors_known_shell_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``$SHELL`` naming a known shell that resolves on PATH is kept.

    Returns the basename (not the ``$SHELL`` path) so it stays resolvable
    when the terminal launches under a runner on a different host.
    """
    # Patch shutil.which on the shared module so the function's local
    # ``import shutil`` sees the fake; make every known shell "installed".
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    for shell_path, expected in (
        ("/bin/zsh", "zsh"),
        ("/usr/local/bin/fish", "fish"),
        ("/bin/bash", "bash"),
    ):
        monkeypatch.setenv("SHELL", shell_path)
        assert _platform.default_interactive_shell() == expected


@pytest.mark.posix_only
def test_default_interactive_shell_falls_back_to_bash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset / unknown / not-on-PATH ``$SHELL`` all fall back to bash."""
    # Unset $SHELL → bash (known shells resolve on PATH here).
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.delenv("SHELL", raising=False)
    assert _platform.default_interactive_shell() == "bash"
    # An unknown shell name is never honored, even if on PATH.
    monkeypatch.setenv("SHELL", "/opt/weird/nushell")
    assert _platform.default_interactive_shell() == "bash"
    # A known shell that does not resolve on PATH also falls back.
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert _platform.default_interactive_shell() == "bash"


@pytest.mark.posix_only
def test_installed_interactive_shells_lists_default_first_then_alternatives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default ($SHELL) leads; installed alternatives follow, deduped."""
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    # zsh is the default → first; bash/fish follow in offer order; no dupes.
    assert _platform.installed_interactive_shells() == ["zsh", "bash", "fish"]


@pytest.mark.posix_only
def test_installed_interactive_shells_skips_uninstalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alternatives that don't resolve on PATH are omitted."""
    # Only bash and zsh installed; fish absent.
    monkeypatch.setattr(
        "shutil.which", lambda name: f"/usr/bin/{name}" if name in {"bash", "zsh"} else None
    )
    monkeypatch.setenv("SHELL", "/bin/bash")
    assert _platform.installed_interactive_shells() == ["bash", "zsh"]


@pytest.mark.posix_only
def test_installed_interactive_shells_always_nonempty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With nothing resolvable the bash fallback is still offered alone."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.delenv("SHELL", raising=False)
    assert _platform.installed_interactive_shells() == ["bash"]


def test_stable_user_id_is_stable_and_path_safe() -> None:
    uid = _platform.stable_user_id()
    assert uid == _platform.stable_user_id()
    assert uid and not set(uid) & set("/\\: ")


@pytest.mark.windows_only
def test_resolve_repo_symlink_dereferences_git_stub(tmp_path: Path) -> None:
    # Real target the Git symlink points at.
    target = tmp_path / "examples" / "polly"
    target.mkdir(parents=True)
    (target / "config.yaml").write_text("name: polly\n", encoding="utf-8")
    # Stub file Git leaves on a no-symlink Windows checkout: content is the
    # relative link target, no trailing newline.
    stub = tmp_path / "resources" / "polly"
    stub.parent.mkdir(parents=True)
    stub.write_text("../examples/polly", encoding="utf-8")

    resolved = _platform.resolve_repo_symlink(stub)
    assert resolved == target.resolve()


@pytest.mark.windows_only
def test_resolve_repo_symlink_leaves_real_specs_untouched(tmp_path: Path) -> None:
    # A genuine single-file YAML spec must not be mistaken for a symlink stub.
    spec = tmp_path / "agent.yaml"
    spec.write_text("name: hello\nharness: claude-sdk\n", encoding="utf-8")
    assert _platform.resolve_repo_symlink(spec) == spec
    # A real directory is returned unchanged.
    d = tmp_path / "bundle"
    d.mkdir()
    assert _platform.resolve_repo_symlink(d) == d


# --------------------------------------------------------------------------
# _proc
# --------------------------------------------------------------------------


def test_spawn_kwargs_shape_matches_platform() -> None:
    kw = _proc.spawn_kwargs()
    if os.name == "nt":
        assert kw == {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        assert kw == {"start_new_session": True}


def test_process_alive_is_a_nondestructive_probe() -> None:
    proc = subprocess.Popen(_spin_cmd(), **_proc.spawn_kwargs())
    try:
        assert _proc.process_alive(proc.pid) is True
        # Probing repeatedly must NOT kill the process (the os.kill(pid, 0)
        # bug on Windows would terminate it here).
        for _ in range(3):
            assert _proc.process_alive(proc.pid) is True
    finally:
        _proc.kill_tree(proc)
        proc.wait(timeout=5)
    assert _proc.process_alive(proc.pid) is False


def test_process_alive_false_for_bogus_pid() -> None:
    assert _proc.process_alive(2_000_000_000) is False
    assert _proc.process_alive(-1) is False


def test_terminate_tree_stops_the_process() -> None:
    proc = subprocess.Popen(_spin_cmd(), **_proc.spawn_kwargs())
    _proc.terminate_tree(proc, grace=5)
    proc.wait(timeout=5)
    assert _proc.process_alive(proc.pid) is False


# --------------------------------------------------------------------------
# Harness IPC endpoint abstraction
# --------------------------------------------------------------------------


def test_endpoint_uds_variant_shape() -> None:
    from omnigent.runtime.harnesses.process_manager import _HarnessEndpoint

    ep = _HarnessEndpoint(socket_path=Path("/tmp/x/conv.sock"))
    assert ep.is_uds is True
    assert ep.spawn_args() == ["--socket", str(Path("/tmp/x/conv.sock"))]
    assert ep.base_url == "http://harness.local"


def test_endpoint_tcp_variant_shape() -> None:
    from omnigent.runtime.harnesses.process_manager import _HarnessEndpoint

    ep = _HarnessEndpoint(host="127.0.0.1", port=54321)
    assert ep.is_uds is False
    assert ep.spawn_args() == ["--bind", "127.0.0.1:54321"]
    assert ep.base_url == "http://127.0.0.1:54321"


def test_endpoint_create_picks_platform_transport() -> None:
    from omnigent.runtime.harnesses.process_manager import _HarnessEndpoint

    ep = _HarnessEndpoint.create(Path("/tmp/inst"), "conv_x")
    assert ep.is_uds == (os.name != "nt")


# --------------------------------------------------------------------------
# windows_jobobject backend
# --------------------------------------------------------------------------


@pytest.mark.windows_only
def test_windows_jobobject_is_platform_default() -> None:
    from omnigent.inner import sandbox

    assert sandbox._default_sandbox_for_platform().type == "windows_jobobject"


@pytest.mark.windows_only
def test_windows_jobobject_kill_on_close_terminates_tree() -> None:
    from omnigent.inner.sandbox import SandboxPolicy
    from omnigent.inner.windows_jobobject_sandbox import WindowsJobObjectSandboxBackend

    backend = WindowsJobObjectSandboxBackend()
    policy = SandboxPolicy(
        backend_type="windows_jobobject",
        active=True,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=True,
    )
    proc = subprocess.Popen(_spin_cmd())
    handle = backend.post_spawn(policy, proc.pid)
    assert handle is not None
    assert _proc.process_alive(proc.pid) is True
    # Closing the job handle must terminate the contained process.
    handle.close()
    time.sleep(0.5)
    assert _proc.process_alive(proc.pid) is False
    if _proc.process_alive(proc.pid):
        proc.kill()


@pytest.mark.windows_only
def test_explicit_bwrap_errors_loudly_on_windows() -> None:
    from omnigent.inner import sandbox
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    sandbox._ensure_builtin_backends()
    backend = sandbox.get_backend("linux_bwrap")
    with pytest.raises(OSError):
        backend.resolve(OSEnvSpec(sandbox=OSEnvSandboxSpec(type="linux_bwrap")), Path("."))


@pytest.mark.posix_only
def test_posix_default_sandbox_is_not_jobobject() -> None:
    from omnigent.inner import sandbox

    assert sandbox._default_sandbox_for_platform().type in {"linux_bwrap", "darwin_seatbelt"}


@pytest.mark.windows_only
def test_helper_env_keeps_systemroot_so_child_can_import_asyncio() -> None:
    # Regression: a filtered (active-sandbox) helper env that drops SYSTEMROOT
    # makes any spawned `python -m omnigent...` die at `import asyncio` with
    # WinError 10106 (Winsock loads providers from %SystemRoot%). The os_env
    # allowlist must carry the Windows system vars.
    from omnigent.inner.os_env import build_helper_env
    from omnigent.inner.sandbox import SandboxPolicy

    policy = SandboxPolicy(
        backend_type="windows_jobobject",
        active=True,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=True,
    )
    env = build_helper_env(os.environ, policy)
    assert "SYSTEMROOT" in env
    result = subprocess.run(
        [__import__("sys").executable, "-c", "import asyncio"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.windows_only
def test_parent_death_watchdog_does_not_false_fire_on_windows() -> None:
    # Regression: on Windows os.getppid() does not match the spawner (the venv
    # launcher breaks the parent link), so the getppid-based orphan check
    # reported the runner orphaned the instant it started and tore it down
    # (clean exit 0). With a live parent_pid the runner must NOT be orphaned.
    from omnigent.runner._entry import _parent_is_orphaned

    assert _parent_is_orphaned(os.getpid()) is False
    assert _parent_is_orphaned(2_000_000_000) is True


@pytest.mark.windows_only
def test_host_runner_env_lets_child_import_asyncio_and_resolve_home() -> None:
    # Regression: the host->runner env allowlist dropped SYSTEMROOT (asyncio /
    # WinError 10106) and USERPROFILE (Path.home() -> "Could not determine home
    # directory"). Both must pass through so a spawned runner can boot.
    import sys

    from omnigent.host.connect import _build_runner_env

    env = _build_runner_env(
        base_env=os.environ,
        server_url="http://x",
        runner_id="r1",
        binding_token="t",
        workspace=".",
        parent_pid=os.getpid(),
    )
    assert "SYSTEMROOT" in env
    assert "USERPROFILE" in env
    result = subprocess.run(
        [sys.executable, "-c", "import asyncio; from pathlib import Path; Path.home()"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
