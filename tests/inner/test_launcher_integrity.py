"""The launcher must trust its Python runtime before it can install containment."""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
import venv
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.inner import sandbox as sb


def _policy(backend: str = "linux_bwrap", *, active: bool = True) -> sb.SandboxPolicy:
    return sb.SandboxPolicy(
        backend_type=backend,
        active=active,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=False,
    )


@pytest.fixture
def runtime_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    system = tmp_path / "system"
    runtime = tmp_path / "workspace" / ".venv"
    executable = system / "bin" / "python3"
    executable.parent.mkdir(parents=True)
    executable.touch()
    interpreter = runtime / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(executable)
    library = runtime / "lib" / "python3.12"
    stdlib = system / "lib" / "python3.12"
    packages = library / "site-packages"
    framework = tmp_path / "framework"
    dependency = tmp_path / "dependency" / "psutil"
    monkeypatch.setattr(
        sb,
        "sys",
        SimpleNamespace(
            executable=str(interpreter),
            prefix=str(runtime),
            exec_prefix=str(runtime),
            base_prefix=str(system),
            base_exec_prefix=str(system),
            path=[str(stdlib), str(packages)],
        ),
    )
    monkeypatch.setattr(
        sb,
        "sysconfig",
        SimpleNamespace(
            get_paths=lambda: {
                "stdlib": str(stdlib),
                "platstdlib": str(library),
                "purelib": str(packages),
                "platlib": str(packages),
            }
        ),
    )
    monkeypatch.setattr(sb, "site", SimpleNamespace(getsitepackages=lambda: [str(packages)]))
    monkeypatch.setattr(sb, "_isolated_launcher_import_roots", lambda _: (stdlib, packages))
    monkeypatch.setattr(sb, "__file__", str(framework / "omnigent" / "inner" / "sandbox.py"))
    monkeypatch.setattr(
        sb,
        "importlib",
        SimpleNamespace(
            util=SimpleNamespace(
                find_spec=lambda _: SimpleNamespace(
                    origin=str(dependency / "__init__.py"),
                    submodule_search_locations=[str(dependency)],
                )
            )
        ),
    )
    return SimpleNamespace(
        system=system,
        runtime=runtime,
        interpreter=interpreter,
        executable=executable,
        library=library,
        stdlib=stdlib,
        packages=packages,
        framework=framework,
        dependency=dependency,
    )


@pytest.mark.parametrize("backend", ["linux_bwrap", "darwin_seatbelt"])
@pytest.mark.parametrize("grant_kind", ["write_roots", "write_files"])
@pytest.mark.parametrize(
    "location",
    [
        "interpreter",
        "executable",
        "runtime",
        "workspace",
        "pyvenv.cfg",
        "startup-hook",
        "stdlib-module",
        "libpython",
        "framework-module",
        "dependency-module",
    ],
)
def test_launcher_rejects_runtime_writes(
    runtime_layout: SimpleNamespace, backend: str, grant_kind: str, location: str
) -> None:
    layout = runtime_layout
    locations = {
        "interpreter": layout.interpreter,
        "executable": layout.executable,
        "runtime": layout.runtime,
        "workspace": layout.runtime.parent,
        "pyvenv.cfg": layout.runtime / "pyvenv.cfg",
        "startup-hook": layout.packages / "poison.pth",
        "stdlib-module": layout.stdlib / "subprocess.py",
        "libpython": layout.system / "lib" / "libpython3.12.so",
        "framework-module": layout.framework / "omnigent" / "inner" / "sandbox.py",
        "dependency-module": layout.dependency / "__init__.py",
    }
    policy = _policy(backend)
    setattr(policy, grant_kind, [locations[location]])
    with pytest.raises(OSError, match="conflicts with trusted launcher runtime"):
        sb.create_exec_launcher("/bin/true", policy)


def test_launcher_protects_lexical_venv_even_when_prefix_is_system(
    runtime_layout: SimpleNamespace,
) -> None:
    # A symlink resolves to the system binary but startup still consults the venv.
    sb.sys.prefix = sb.sys.base_prefix
    sb.sys.exec_prefix = sb.sys.base_exec_prefix
    policy = _policy()
    for path in (
        runtime_layout.runtime / "pyvenv.cfg",
        runtime_layout.packages / "sitecustomize.py",
    ):
        policy.write_files = [path]
        with pytest.raises(OSError, match="trusted launcher runtime"):
            sb.create_exec_launcher("/bin/true", policy)


def test_launcher_rejects_alias_and_intermediate_interpreter_hops(
    runtime_layout: SimpleNamespace, tmp_path: Path
) -> None:
    layout = runtime_layout
    alias = tmp_path / "alias"
    alias.symlink_to(layout.packages, target_is_directory=True)
    policy = _policy()
    policy.write_files = [alias / "sitecustomize.py"]
    with pytest.raises(OSError, match="trusted launcher runtime"):
        sb.create_exec_launcher("/bin/true", policy)

    hop = tmp_path / "python-hop"
    hop.symlink_to(layout.executable)
    layout.interpreter.unlink()
    layout.interpreter.symlink_to(hop)
    policy.write_files = [hop]
    with pytest.raises(OSError, match="trusted launcher runtime"):
        sb.create_exec_launcher("/bin/true", policy)


@pytest.mark.parametrize("grant_kind", ["write_roots", "write_files"])
def test_launcher_allows_child_workspace_under_system_prefix(
    runtime_layout: SimpleNamespace, grant_kind: str
) -> None:
    layout = runtime_layout
    policy = _policy()
    # A broad system prefix is a protected entry, not an all-descendants ban.
    setattr(
        policy,
        grant_kind,
        [layout.system / "workspace" / ".venv", layout.system / "library-project"],
    )
    path = sb.create_exec_launcher("/bin/true", policy)
    Path(path).unlink()


@pytest.mark.parametrize("backend,active", [("none", False), ("linux_bwrap", False)])
def test_inactive_launcher_remains_compatible(
    runtime_layout: SimpleNamespace, backend: str, active: bool
) -> None:
    policy = _policy(backend, active=active)
    policy.write_roots = [runtime_layout.runtime.parent, runtime_layout.framework]
    path = sb.create_exec_launcher("/bin/true", policy)
    Path(path).unlink()


@pytest.mark.parametrize("grant_kind", ["write_roots", "write_files"])
def test_runner_added_parent_import_path_remains_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, grant_kind: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.syspath_prepend(str(workspace))
    policy = _policy()
    setattr(policy, grant_kind, [workspace if grant_kind == "write_roots" else workspace / "file"])
    path = sb.create_exec_launcher("/bin/true", policy)
    Path(path).unlink()


@pytest.mark.skipif(os.name == "nt", reason="POSIX disposable venv")
def test_isolated_pth_import_root_remains_protected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(runtime)
    interpreter = runtime / "bin" / "python"
    packages = (
        runtime
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    external = tmp_path / "startup-imports"
    external.mkdir()
    (packages / "external.pth").write_text(str(external) + "\n")
    assert str(external) not in sys.path
    # Snapshot while the installation is trusted, before any worker can run.
    roots = sb._isolated_launcher_import_roots(str(interpreter))
    assert external in roots
    marker = tmp_path / "startup-ran"
    (external / "sitecustomize.py").write_text(
        f"from pathlib import Path; Path({str(marker)!r}).touch()\n"
    )
    monkeypatch.setattr(sys, "executable", str(interpreter))
    policy = _policy()
    for grant_kind, grant in (
        ("write_roots", external),
        ("write_files", external / "sitecustomize.py"),
    ):
        policy.write_roots = []
        policy.write_files = []
        setattr(policy, grant_kind, [grant])
        with pytest.raises(OSError, match="trusted launcher runtime"):
            sb.create_exec_launcher("/bin/true", policy)
    assert not marker.exists(), "cached startup roots must not re-execute modified hooks"


def test_isolated_import_snapshot_failure_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("isolated startup", 10)

    monkeypatch.setattr(sb.subprocess, "run", fail)
    with pytest.raises(OSError, match="Cannot validate sandbox launcher isolated startup imports"):
        sb._isolated_launcher_import_roots(str(tmp_path / "uncached-interpreter"))


@pytest.mark.parametrize("alias", [False, True])
def test_launcher_rejects_tempfile_under_writable_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alias: bool
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    temporary = workspace
    if alias:
        temporary = tmp_path / "tmp-alias"
        temporary.symlink_to(workspace, target_is_directory=True)
    # tempfile caches its selected directory, so set the effective TMPDIR directly.
    monkeypatch.setattr(sb.tempfile, "tempdir", str(temporary))
    policy = _policy()
    policy.write_roots = [workspace]
    with pytest.raises(ValueError, match="exposes the host launcher directory"):
        sb.create_exec_launcher("/bin/true", policy, env_overrides={"TOKEN": "placeholder"})
    assert list(workspace.iterdir()) == [], "rejected launchers must be removed"


def test_launcher_snapshot_rejects_group_or_world_readable_file() -> None:
    path = sb.create_exec_launcher(
        "/bin/true", _policy("none", active=False), env_overrides={"TOKEN": "placeholder"}
    )
    try:
        Path(path).chmod(0o755)
        with pytest.raises(OSError, match="owner-only"):
            sb._read_launcher_env_overrides(path)
    finally:
        Path(path).unlink()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable launcher")
def test_isolated_launcher_ignores_hostile_python_startup(tmp_path: Path) -> None:
    marker = tmp_path / "startup-ran"
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    payload = f"from pathlib import Path; Path({str(marker)!r}).touch()\n"
    (hostile / "sitecustomize.py").write_text(payload)
    env = {"PATH": os.defpath, "HOME": str(tmp_path), "PYTHONPATH": str(hostile)}
    # Prove the injected startup hook is effective without isolated startup.
    subprocess.run([sys.executable, "-c", "pass"], env=env, check=True, timeout=30)
    assert marker.exists()
    marker.unlink()
    (hostile / "logging.py").write_text(payload + "raise RuntimeError('cwd import')\n")
    env["PYTHONHOME"] = str(hostile / "invalid-python-home")
    path = sb.create_exec_launcher("/bin/echo", _policy("none", active=False))
    try:
        result = subprocess.run(
            [path, "isolated"], cwd=hostile, env=env, capture_output=True, text=True, timeout=30
        )
    finally:
        Path(path).unlink()
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "isolated"
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX venv interpreter symlink")
def test_disposable_runtime_startup_poison_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "workspace" / ".venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(runtime)
    interpreter = runtime / "bin" / "python"
    packages = (
        runtime
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    marker = tmp_path / "host-startup-ran"
    startup = packages / "poison_startup.py"
    (packages / "poison.pth").write_text("import poison_startup\n")
    # Model the previous shell altering a writable runtime, using only disposable files.
    subprocess.run(
        ["/bin/sh", "-c", 'cat > "$1"', "sh", str(startup)],
        input=f"from pathlib import Path; Path({str(marker)!r}).touch()\n",
        text=True,
        check=True,
        timeout=30,
    )
    subprocess.run([str(interpreter), "-I", "-c", "pass"], check=True, timeout=30)
    assert marker.exists(), "-I still executes installed .pth startup hooks"
    marker.unlink()
    monkeypatch.setattr(sys, "executable", str(interpreter))
    policy = _policy()
    for grant_kind, path in (
        ("write_roots", runtime.parent),
        ("write_files", startup),
        ("write_files", runtime / "pyvenv.cfg"),
    ):
        policy.write_roots = []
        policy.write_files = []
        setattr(policy, grant_kind, [path])
        with pytest.raises(OSError, match="trusted launcher runtime"):
            sb.create_exec_launcher("/bin/true", policy)
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX fake CLI")
@pytest.mark.parametrize("allowlist", [None, ["PATH"]])
def test_launcher_snapshots_only_supplied_env_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allowlist: list[str] | None
) -> None:
    target = tmp_path / "cli"
    target.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$ANTHROPIC_AUTH_TOKEN" "$HOME" "$HTTP_PROXY" "$*"\n'
    )
    target.chmod(0o700)
    home = tmp_path / "private-home"
    home.mkdir()
    overrides = {
        "ANTHROPIC_AUTH_TOKEN": "proxy-placeholder",
        "HOME": str(home),
        "HTTP_PROXY": "http://127.0.0.1:49687",
    }
    original = dict(overrides)
    recorded_argv = tmp_path / "python-argv.json"
    recorder = (
        "import json,sys; from pathlib import Path; "
        f"Path({str(recorded_argv)!r}).write_text(json.dumps(sys.argv[1:]))"
    )
    interpreter = tmp_path / "python-shim"
    interpreter.write_text(
        f'#!/bin/sh\n{shlex.quote(sys.executable)} -I -c {shlex.quote(recorder)} "$@"\n'
        f'exec {shlex.quote(sys.executable)} "$@"\n'
    )
    interpreter.chmod(0o700)
    monkeypatch.setattr(sys, "executable", str(interpreter))
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "inherited-sensitive-sentinel")
    monkeypatch.setenv("OTHER_HOST_SECRET", "not-supplied-sentinel")
    monkeypatch.setenv("HOME", str(tmp_path / "host-home"))
    policy = _policy("none", active=False)
    policy.spawn_env_allowlist = allowlist
    path = sb.create_exec_launcher(str(target), policy, env_overrides=overrides)
    # Later mutations must not change this launcher's per-launch identity.
    overrides["ANTHROPIC_AUTH_TOKEN"] = "later-placeholder"
    overrides["HOME"] = str(tmp_path / "later-home")
    try:
        assert stat.S_IMODE(Path(path).stat().st_mode) == 0o700
        assert sb._read_launcher_env_overrides(path) == original
        for invocation in ("-v", "main"):
            result = subprocess.run(
                [path, invocation],
                env={
                    "PATH": os.defpath,
                    "HOME": str(tmp_path / "host-home"),
                    "ANTHROPIC_AUTH_TOKEN": "inherited-sensitive-sentinel",
                    "OTHER_HOST_SECRET": "not-supplied-sentinel",
                },
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stderr
            assert result.stdout.splitlines() == [
                "proxy-placeholder",
                str(home),
                original["HTTP_PROXY"],
                invocation,
            ]
            argv = json.loads(recorded_argv.read_text())
            assert argv[:2] == ["-I", "-c"]
            assert all(value not in "\0".join(argv) for value in original.values())
            assert sb._encode_json_arg(original) not in "\0".join(argv)
        source = Path(path).read_text()
    finally:
        Path(path).unlink()
    assert "inherited-sensitive-sentinel" not in source
    assert "not-supplied-sentinel" not in source
    assert "later-placeholder" not in source


@pytest.mark.skipif(os.name == "nt", reason="POSIX spawn-wrap re-exec")
@pytest.mark.parametrize("backend", ["linux_bwrap", "darwin_seatbelt"])
def test_reexec_preserves_isolation_and_env_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    private_home = str(tmp_path / "private-home")
    claude_tmpdir = str(tmp_path / "claude-tmp")
    overrides = {
        "HOME": private_home,
        "ANTHROPIC_AUTH_TOKEN": "proxy-placeholder",
        "XDG_RUNTIME_DIR": str(tmp_path / "stale-runtime"),
        "TMPDIR": str(tmp_path / "stale-tmp"),
        "CLAUDE_CODE_TMPDIR": claude_tmpdir,
        "HTTP_PROXY": "http://127.0.0.1:49687",
    }
    policy = _policy(backend)
    policy.spawn_env_allowlist = ["PATH"]
    target = tmp_path / "cli"
    target.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$ANTHROPIC_AUTH_TOKEN" "$HOME" "$*" '
        '"$XDG_RUNTIME_DIR" "$TMPDIR" "$CLAUDE_CODE_TMPDIR" "$HTTP_PROXY"\n'
    )
    target.chmod(0o700)
    environ = {"PATH": os.defpath, "HOME": "/host", "ANTHROPIC_AUTH_TOKEN": "host-sentinel"}
    captured: list[subprocess.CompletedProcess[str]] = []
    scratch: list[str] = []

    def wrap(argv, _policy, _cwd, **_kwargs):
        assert environ["HOME"] == private_home
        assert environ["ANTHROPIC_AUTH_TOKEN"] == "proxy-placeholder"
        scratch.append(environ[sb._LAUNCHER_PRIVATE_TMPDIR_ENV])
        assert environ["XDG_RUNTIME_DIR"] == scratch[0]
        assert environ["TMPDIR"] == scratch[0]
        return argv

    class Reexecuted(Exception):
        pass

    def execute(_file, argv):
        assert argv[:3] == [sys.executable, "-I", "-c"]
        assert all(value not in "\0".join(argv) for value in overrides.values())
        assert sb._encode_json_arg(overrides) not in "\0".join(argv)
        # The contained pass keeps the sanitized inherited values through pruning.
        child_env = dict(environ)
        inline = (
            f"import sys; sys.path.insert(0, {str(sb._project_root())!r}); "
            "from omnigent.inner import sandbox; sandbox.activate_sandbox = lambda _: None; "
            + argv[3]
        )
        captured.append(
            subprocess.run(
                [*argv[:3], inline, *argv[4:]],
                env=child_env,
                capture_output=True,
                text=True,
                timeout=30,
            )
        )
        raise Reexecuted

    monkeypatch.setattr(
        sb,
        "get_backend",
        lambda _: SimpleNamespace(
            prepare_launcher_policy=lambda policy: policy, wrap_launcher_argv=wrap
        ),
    )
    monkeypatch.setattr(
        sb, "os", SimpleNamespace(environ=environ, getcwd=os.getcwd, execvp=execute)
    )
    with pytest.raises(Reexecuted):
        sb.run_launcher(
            sb._encode_json_arg(policy.to_jsonable()),
            str(target),
            ["-v"],
            env_overrides=overrides,
        )
    assert len(captured) == 1
    result = captured[0]
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "proxy-placeholder",
        private_home,
        "-v",
        scratch[0],
        scratch[0],
        claude_tmpdir,
        overrides["HTTP_PROXY"],
    ]
