"""Tests for the cross-platform process/platform primitives and Windows backend.

Covers omnigent._platform, omnigent.inner._proc, the harness IPC endpoint
abstraction, and the windows_jobobject sandbox backend. The platform-specific
assertions are gated with ``posix_only`` / ``windows_only`` markers so the
file runs on both Linux CI and a Windows box.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path

import psutil
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
    """Unset / unknown / uninstalled ``$SHELL`` all fall back to bash."""
    # Resolution is fully hermetic: neither PATH nor the standard shell dirs
    # yield anything, so only $SHELL's own absolute path could rescue a value.
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(_platform, "_resolve_interactive_shell", lambda name: None)
    monkeypatch.setattr(os.path, "isabs", lambda path: False)
    # Unset $SHELL → bash.
    monkeypatch.delenv("SHELL", raising=False)
    assert _platform.default_interactive_shell() == "bash"
    # An unknown shell name is never honored, even if it resolved.
    monkeypatch.setenv("SHELL", "/opt/weird/nushell")
    assert _platform.default_interactive_shell() == "bash"
    # A known shell that resolves nowhere (not on PATH, not in a standard dir,
    # $SHELL not usable as an absolute path) also falls back.
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert _platform.default_interactive_shell() == "bash"


def test_normalize_interactive_shells_filters_and_deduplicates() -> None:
    """Host and server accept only supported shell basenames."""
    assert _platform.normalize_interactive_shells(["zsh", "bash", "zsh", "not-a-shell", 42]) == [
        "zsh",
        "bash",
    ]
    assert _platform.normalize_interactive_shells("bash") == []


@pytest.mark.posix_only
def test_default_interactive_shell_trusts_shell_absolute_path_off_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A known ``$SHELL`` whose absolute path is executable is honored even
    when it does not resolve on a stripped ``PATH``.

    Reproduces the frozen-PATH host daemon (GUI/launchd launch): ``$SHELL`` is
    ``/bin/zsh`` but ``PATH`` omits ``/bin``, so a bare ``shutil.which("zsh")``
    misses it. The login shell must still be honored.
    """
    zsh = tmp_path / "zsh"
    zsh.write_text("#!/bin/sh\n")
    zsh.chmod(0o755)
    # Nothing resolves on PATH or in the standard shell dirs.
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(_platform, "_INTERACTIVE_SHELL_DIRS", ())
    monkeypatch.setenv("SHELL", str(zsh))
    assert _platform.default_interactive_shell() == "zsh"


@pytest.mark.posix_only
def test_resolve_interactive_shell_uses_matching_absolute_login_shell(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A Nix-style login shell remains executable under a stripped PATH."""
    fish = tmp_path / "profiles" / "default" / "bin" / "fish"
    fish.parent.mkdir(parents=True)
    fish.write_text("#!/bin/sh\n")
    fish.chmod(0o755)
    monkeypatch.setenv("SHELL", str(fish))
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr(_platform, "_INTERACTIVE_SHELL_DIRS", ())

    assert _platform.default_interactive_shell() == "fish"
    assert _platform._resolve_interactive_shell("fish") == str(fish)


@pytest.mark.parametrize("name", ["/bin/bash", "../bash", "unknown"])
def test_resolve_interactive_shell_rejects_paths_and_unknown_names(
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolver accepts allowlisted basenames only."""
    monkeypatch.setattr("shutil.which", lambda value: pytest.fail(f"resolved {value}"))
    assert _platform._resolve_interactive_shell(name) is None


def _fake_resolver(installed: set[str]):
    """A ``_resolve_interactive_shell`` stand-in: resolve only *installed*.

    Fully hermetic — the real helper probes both PATH and standard shell dirs,
    so patching it (not just ``shutil.which``) is what keeps a test from leaking
    whatever shells the host machine actually has installed.
    """
    return lambda name: f"/usr/bin/{name}" if name in installed else None


@pytest.mark.posix_only
def test_installed_interactive_shells_lists_default_first_then_alternatives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default ($SHELL) leads; installed alternatives follow, deduped."""
    monkeypatch.setattr(
        _platform, "_resolve_interactive_shell", _fake_resolver({"bash", "zsh", "fish"})
    )
    monkeypatch.setenv("SHELL", "/bin/zsh")
    # zsh is the default → first; bash/fish follow in offer order; no dupes.
    assert _platform.installed_interactive_shells() == ["zsh", "bash", "fish"]


@pytest.mark.posix_only
def test_installed_interactive_shells_skips_uninstalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alternatives that resolve nowhere are omitted."""
    # Only bash and zsh installed; fish absent.
    monkeypatch.setattr(_platform, "_resolve_interactive_shell", _fake_resolver({"bash", "zsh"}))
    monkeypatch.setenv("SHELL", "/bin/bash")
    assert _platform.installed_interactive_shells() == ["bash", "zsh"]


@pytest.mark.posix_only
def test_installed_interactive_shells_offers_shells_off_stripped_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shells in a standard dir are offered even when ``PATH`` omits it.

    Regression for the frozen-PATH host daemon: with ``SHELL=/bin/zsh`` but a
    ``PATH`` missing ``/bin``, ``shutil.which`` finds nothing, yet the standard
    shell dirs still resolve zsh/bash — so the picker must offer both instead of
    collapsing to a lone (phantom) ``bash``.
    """
    monkeypatch.setattr("shutil.which", lambda name: None)  # nothing on PATH

    # Both live in /bin, off the stripped PATH.
    def in_bin(path: str) -> bool:
        return os.path.basename(path) in {"zsh", "bash"} and path.startswith("/bin/")

    monkeypatch.setattr("os.path.isfile", in_bin)
    monkeypatch.setattr("os.access", lambda p, mode: True)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert _platform.installed_interactive_shells() == ["zsh", "bash"]


@pytest.mark.posix_only
def test_installed_interactive_shells_always_nonempty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With nothing resolvable anywhere the bash fallback is still offered alone."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(_platform, "_resolve_interactive_shell", lambda name: None)
    monkeypatch.setattr(os.path, "isabs", lambda path: False)
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
    # Bind to the live PID so psutil pins its creation time; a recycled PID
    # after teardown can't masquerade as the reaped child, so the final
    # liveness check is free of the process_alive(pid) TOCTOU race.
    handle = psutil.Process(proc.pid)
    try:
        assert _proc.process_alive(proc.pid) is True
        # Probing repeatedly must NOT kill the process (the os.kill(pid, 0)
        # bug on Windows would terminate it here).
        for _ in range(3):
            assert _proc.process_alive(proc.pid) is True
    finally:
        _proc.kill_tree(proc)
        proc.wait(timeout=5)
    # A reaped PID raises NoSuchProcess, which also means it isn't running.
    with contextlib.suppress(psutil.NoSuchProcess):
        assert not handle.is_running() or handle.status() == psutil.STATUS_ZOMBIE


def test_process_alive_false_for_bogus_pid() -> None:
    assert _proc.process_alive(2_000_000_000) is False
    assert _proc.process_alive(-1) is False


def test_killpg_refuses_the_broadcast_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """A target pgid of 1 must never be signalled.

    ``killpg(1, sig)`` is ``kill(-1, sig)`` -- a broadcast to every process
    the user can signal (a mocked pid that coerces to 1 killed the CI runner).
    """
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(_proc, "_getpgid_fn", lambda pid: 1 if pid else 54321)
    monkeypatch.setattr(_proc, "_killpg_fn", lambda pgid, sig: sent.append((pgid, sig)))
    assert _proc._killpg(1, signal.SIGTERM) is False
    assert sent == []


def test_terminate_tree_stops_the_process() -> None:
    proc = subprocess.Popen(_spin_cmd(), **_proc.spawn_kwargs())
    # Bind to the live PID so psutil pins its creation time; a recycled PID
    # after teardown can't masquerade as the reaped child, so the final
    # liveness check is free of the process_alive(pid) TOCTOU race.
    handle = psutil.Process(proc.pid)
    _proc.terminate_tree(proc, grace=5)
    proc.wait(timeout=5)
    # A reaped PID raises NoSuchProcess, which also means it isn't running.
    with contextlib.suppress(psutil.NoSuchProcess):
        assert not handle.is_running() or handle.status() == psutil.STATUS_ZOMBIE


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


# ---------------------------------------------------------------------------
# resolve_cli_binary — CLIs that may live off the daemon's frozen PATH
# ---------------------------------------------------------------------------


def test_resolve_cli_binary_prefers_path(monkeypatch):
    monkeypatch.delenv("OMNIGENT_TESTCLI_PATH", raising=False)
    monkeypatch.setattr(
        _platform.shutil, "which", lambda name: "/usr/bin/tool" if name == "tool" else None
    )
    monkeypatch.setattr(_platform, "_cli_fallback_dirs", lambda: ())
    assert _platform.resolve_cli_binary("tool", env_var="OMNIGENT_TESTCLI_PATH") == "/usr/bin/tool"


def test_resolve_cli_binary_env_override_wins(monkeypatch, tmp_path):
    override = tmp_path / "tool"
    override.write_text("#!/bin/sh\n")
    override.chmod(0o755)
    monkeypatch.setenv("OMNIGENT_TESTCLI_PATH", str(override))
    # PATH would resolve elsewhere, but the override takes precedence.
    monkeypatch.setattr(
        _platform.shutil, "which", lambda name: "/usr/bin/tool" if name == "tool" else None
    )
    assert _platform.resolve_cli_binary("tool", env_var="OMNIGENT_TESTCLI_PATH") == str(override)


def test_resolve_cli_binary_falls_back_to_global_dir(monkeypatch, tmp_path):
    """A binary off PATH is still found in a common global install dir.

    This is the nvm case: the host daemon's frozen PATH omits the bin dir,
    but the binary is present on disk.
    """
    fallback_dir = tmp_path / "bin"
    fallback_dir.mkdir()
    tool = fallback_dir / "tool"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    monkeypatch.delenv("OMNIGENT_TESTCLI_PATH", raising=False)
    monkeypatch.setattr(_platform.shutil, "which", lambda name: None)
    monkeypatch.setattr(_platform, "_cli_fallback_dirs", lambda: (fallback_dir,))
    assert _platform.resolve_cli_binary("tool", env_var="OMNIGENT_TESTCLI_PATH") == str(tool)


def test_resolve_cli_binary_returns_none_when_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("OMNIGENT_TESTCLI_PATH", raising=False)
    monkeypatch.setattr(_platform.shutil, "which", lambda name: None)
    monkeypatch.setattr(_platform, "_cli_fallback_dirs", lambda: (tmp_path / "empty",))
    assert _platform.resolve_cli_binary("tool", env_var="OMNIGENT_TESTCLI_PATH") is None


def test_resolve_cli_binary_warns_on_bad_override(monkeypatch, tmp_path, caplog):
    """A set-but-unresolvable override warns (so a misconfig surfaces) and then
    falls back to PATH rather than launching nothing."""
    monkeypatch.setenv("OMNIGENT_TESTCLI_PATH", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(
        _platform.shutil, "which", lambda name: "/usr/bin/tool" if name == "tool" else None
    )
    monkeypatch.setattr(_platform, "_cli_fallback_dirs", lambda: ())
    with caplog.at_level("WARNING"):
        resolved = _platform.resolve_cli_binary("tool", env_var="OMNIGENT_TESTCLI_PATH")
    assert resolved == "/usr/bin/tool"
    assert "OMNIGENT_TESTCLI_PATH" in caplog.text


def test_resolve_cli_binary_no_env_var(monkeypatch):
    """Without an env_var, resolution is PATH then fallback dirs only."""
    monkeypatch.setattr(_platform.shutil, "which", lambda name: "/usr/bin/tool")
    assert _platform.resolve_cli_binary("tool") == "/usr/bin/tool"


def test_cli_fallback_dirs_includes_nvm_version_bins(monkeypatch, tmp_path):
    """nvm keeps global bins under ~/.nvm/versions/node/<ver>/bin; the ladder
    must include those (newest first) since that's the reported nvm case.

    Ordering is by parsed numeric version, so double-digit majors (v10) beat
    single-digit ones (v9) — a lexicographic sort would get this backwards.
    """
    nvm = tmp_path / ".nvm" / "versions" / "node"
    for ver in ("v9.11.0", "v10.2.0", "v20.5.0"):
        (nvm / ver / "bin").mkdir(parents=True)
    monkeypatch.setattr(_platform.Path, "home", staticmethod(lambda: tmp_path))
    dirs = _platform._cli_fallback_dirs()
    for ver in ("v9.11.0", "v10.2.0", "v20.5.0"):
        assert nvm / ver / "bin" in dirs
    # newest → oldest, numerically: v20 > v10 > v9 (not the lexicographic
    # order where "v9" would sort after "v10"/"v20").
    order = [dirs.index(nvm / v / "bin") for v in ("v20.5.0", "v10.2.0", "v9.11.0")]
    assert order == sorted(order)


def test_cli_fallback_dirs_no_nvm_dir_is_safe(monkeypatch, tmp_path):
    """Absent ~/.nvm, the ladder still returns the static dirs without error."""
    monkeypatch.setattr(_platform.Path, "home", staticmethod(lambda: tmp_path))
    dirs = _platform._cli_fallback_dirs()
    assert tmp_path / ".local" / "bin" in dirs


def test_malloc_tuning_env_empty_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off Linux the tuning helper is a no-op so callers need no platform branch."""
    monkeypatch.setattr(_proc, "IS_LINUX", False)
    assert _proc.malloc_tuning_env() == {}


def test_malloc_tuning_env_defaults_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Linux the helper caps arenas and sets a trim threshold by default."""
    monkeypatch.setattr(_proc, "IS_LINUX", True)
    monkeypatch.delenv("OMNIGENT_RUNNER_MALLOC_ARENA_MAX", raising=False)
    monkeypatch.delenv("OMNIGENT_RUNNER_MALLOC_TRIM_THRESHOLD", raising=False)
    assert _proc.malloc_tuning_env() == {
        "MALLOC_ARENA_MAX": "2",
        "MALLOC_TRIM_THRESHOLD_": "134217728",
    }


def test_malloc_tuning_env_arena_max_zero_disables_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OMNIGENT_RUNNER_MALLOC_ARENA_MAX=0`` drops the arena cap (revert knob)."""
    monkeypatch.setattr(_proc, "IS_LINUX", True)
    monkeypatch.setenv("OMNIGENT_RUNNER_MALLOC_ARENA_MAX", "0")
    monkeypatch.delenv("OMNIGENT_RUNNER_MALLOC_TRIM_THRESHOLD", raising=False)
    env = _proc.malloc_tuning_env()
    assert "MALLOC_ARENA_MAX" not in env
    assert env["MALLOC_TRIM_THRESHOLD_"] == "134217728"


def test_malloc_tuning_env_honors_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operator overrides flow through to the child env values."""
    monkeypatch.setattr(_proc, "IS_LINUX", True)
    monkeypatch.setenv("OMNIGENT_RUNNER_MALLOC_ARENA_MAX", "1")
    monkeypatch.setenv("OMNIGENT_RUNNER_MALLOC_TRIM_THRESHOLD", "65536")
    assert _proc.malloc_tuning_env() == {
        "MALLOC_ARENA_MAX": "1",
        "MALLOC_TRIM_THRESHOLD_": "65536",
    }
