"""The launcher must trust its Python runtime before it can install containment."""

from __future__ import annotations

import errno
import json
import os
import shlex
import shutil
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
            platform=sys.platform,
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
def test_launcher_rejects_writable_hardlink_to_startup_import(
    runtime_layout: SimpleNamespace, tmp_path: Path, grant_kind: str
) -> None:
    startup = runtime_layout.packages / "startup_module.py"
    startup.parent.mkdir(parents=True)
    startup.write_text("value = 'trusted'\n")
    original_identity = (startup.stat().st_dev, startup.stat().st_ino)
    workspace = tmp_path / "contained"
    workspace.mkdir()
    alias = workspace / "editable.py"
    os.link(startup, alias)
    alias.write_text("value = 'modified'\n")
    assert (startup.stat().st_dev, startup.stat().st_ino) == original_identity

    policy = _policy()
    setattr(policy, grant_kind, [workspace if grant_kind == "write_roots" else alias])
    with pytest.raises(OSError, match="hard-link alias"):
        sb.create_exec_launcher("/bin/true", policy)


def test_launcher_allows_unrelated_hardlinks(
    runtime_layout: SimpleNamespace, tmp_path: Path
) -> None:
    startup = runtime_layout.packages / "startup_module.py"
    startup.parent.mkdir(parents=True)
    startup.write_text("value = 'trusted'\n")
    unrelated = tmp_path / "unrelated.py"
    unrelated.write_text("value = 'workspace'\n")
    workspace = tmp_path / "contained"
    workspace.mkdir()
    os.link(unrelated, workspace / "editable.py")

    policy = _policy()
    policy.write_roots = [workspace]
    Path(sb.create_exec_launcher("/bin/true", policy)).unlink()


def test_launcher_rejects_root_level_import_alias(
    runtime_layout: SimpleNamespace, tmp_path: Path
) -> None:
    root = runtime_layout.framework
    root.mkdir(parents=True, exist_ok=True)
    shadow = root / "uuid.py"
    shadow.write_text("value = 'trusted'\n")
    workspace = tmp_path / "contained"
    workspace.mkdir()
    os.link(shadow, workspace / "editable.py")
    policy = _policy()
    policy.write_roots = [workspace]
    with pytest.raises(OSError, match="hard-link alias"):
        sb.create_exec_launcher("/bin/true", policy)


def test_launcher_rejects_hardlink_moved_out_of_workspace_during_scan(
    runtime_layout: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = runtime_layout.packages / "startup.py"
    trusted.parent.mkdir(parents=True)
    trusted.write_text("value = 'trusted'\n")
    workspace = tmp_path / "grant"
    workspace.mkdir()
    alias = workspace / "editable.py"
    os.link(trusted, alias)
    hidden = tmp_path / "moved.py"
    original_scandir = os.scandir

    def move_on_trusted_scan(path: str | os.PathLike[str]):
        if Path(path) == runtime_layout.packages:
            alias.rename(hidden)
        return original_scandir(path)

    monkeypatch.setattr(sb.os, "scandir", move_on_trusted_scan)
    policy = _policy()
    policy.write_roots = [workspace]
    with pytest.raises(OSError, match="hard-link alias"):
        sb._validate_trusted_hardlink_aliases(policy, set(), {runtime_layout.packages})
    assert hidden.exists(), "the alias moved before the trusted file was checked"
    assert not alias.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux root-owned package compatibility")
def test_root_owned_readonly_package_hardlinks_are_allowed(
    runtime_layout: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = runtime_layout.packages / "module.py"
    trusted.parent.mkdir(parents=True)
    trusted.write_text("value = 'trusted'\n")
    os.link(trusted, tmp_path / "cache-copy.py")
    original_stat = Path.stat
    original_access = os.access
    info = trusted.stat()
    trusted_identity = (info.st_dev, info.st_ino)

    def is_trusted_inode(path: Path | str) -> bool:
        try:
            info = original_stat(Path(path))
        except FileNotFoundError:
            return False
        return (info.st_dev, info.st_ino) == trusted_identity

    def root_owned_stat(path: Path, *args: object, **kwargs: object) -> os.stat_result:
        info = original_stat(path, *args, **kwargs)
        if (info.st_dev, info.st_ino) == trusted_identity:
            fields = list(info)
            fields[4] = 0
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "stat", root_owned_stat)
    monkeypatch.setattr(
        os,
        "access",
        lambda path, mode: False if is_trusted_inode(path) else original_access(path, mode),
    )
    policy = _policy()
    policy.write_roots = [tmp_path / "grant"]
    Path(sb.create_exec_launcher("/bin/true", policy)).unlink()

    trusted.chmod(0o664)
    with pytest.raises(OSError, match="hard-link alias"):
        sb.create_exec_launcher("/bin/true", policy)


def test_trusted_inode_scan_follows_directory_symlinks_without_loop(tmp_path: Path) -> None:
    trusted_dir = tmp_path / "trusted"
    trusted_dir.mkdir()
    trusted = trusted_dir / "trusted.py"
    trusted.write_text("value = 'trusted'\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (trusted_dir / "external").symlink_to(external, target_is_directory=True)
    (external / "cycle").symlink_to(trusted_dir, target_is_directory=True)
    policy = _policy()
    policy.write_roots = [workspace]
    sb._validate_trusted_hardlink_aliases(policy, set(), {trusted_dir})

    os.link(trusted, external / "editable.py")
    with pytest.raises(OSError, match="hard-link alias"):
        sb._validate_trusted_hardlink_aliases(policy, set(), {trusted_dir})


@pytest.mark.parametrize("target_kind", ["intermediate", "dangling"])
def test_launcher_rejects_nested_runtime_symlink_hops(
    runtime_layout: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_kind: str,
) -> None:
    workspace = tmp_path / "workspace"
    startup = runtime_layout.packages / "startup.py"
    startup.parent.mkdir(parents=True)
    if target_kind == "intermediate":
        harmless = tmp_path / "harmless.py"
        harmless.write_text("value = 'trusted'\n")
        hop = workspace / "hop.py"
        hop.symlink_to(harmless)
        startup.symlink_to(hop)
    else:
        startup.symlink_to(workspace / "missing.py")
    monkeypatch.setattr(
        sb,
        "_isolated_launcher_import_roots",
        lambda _interpreter: pytest.fail("startup probe ran before symlink validation"),
    )
    policy = _policy()
    policy.write_roots = [workspace]

    with pytest.raises(OSError, match="trusted launcher runtime"):
        sb.create_exec_launcher("/bin/true", policy)


def test_trusted_inode_scan_fails_closed_on_large_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for number in range(4):
        (tmp_path / f"file-{number}.py").write_text("value = 'trusted'\n")
    monkeypatch.setattr(sb, "_MAX_TRUSTED_RUNTIME_ENTRIES", 3)

    with pytest.raises(OSError, match="Cannot validate trusted runtime"):
        sb._validate_trusted_hardlink_aliases(_policy(), set(), {tmp_path})


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt")
def test_seatbelt_denies_new_hardlink_from_readonly_source(tmp_path: Path) -> None:
    from omnigent.inner.seatbelt_sandbox import _build_profile

    sandbox_exec = Path("/usr/bin/sandbox-exec")
    if not sandbox_exec.exists():
        pytest.skip("sandbox-exec is unavailable")
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    source = trusted / "startup.py"
    source.write_text("value = 'trusted'\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = workspace / "editable.py"
    policy = _policy("darwin_seatbelt")
    policy.read_roots = [trusted]
    policy.write_roots = [workspace]
    profile = _build_profile(policy, workspace)
    copied = workspace / "copy.py"
    subprocess.run(
        [str(sandbox_exec), "-p", profile, "/bin/cp", str(source), str(copied)],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert copied.read_text() == source.read_text()

    result = subprocess.run(
        [str(sandbox_exec), "-p", profile, "/bin/ln", str(source), str(alias)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert result.stderr.startswith("ln: ")
    assert "Operation not permitted" in result.stderr
    assert not alias.exists()
    os.link(source, alias)
    assert alias.exists(), "the filesystem itself must permit the link"


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux bubblewrap")
def test_bwrap_denies_new_hardlink_from_readonly_source(tmp_path: Path) -> None:
    from omnigent.inner.bwrap_sandbox import BwrapSandboxBackend

    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap is unavailable")
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    source = trusted / "startup.py"
    source.write_text("value = 'trusted'\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = workspace / "editable.py"
    policy = _policy()
    policy.read_roots = [trusted]
    policy.write_roots = [workspace]
    copied = workspace / "copy.py"
    copy_argv = BwrapSandboxBackend().wrap_launcher_argv(
        ["/bin/cp", str(source), str(copied)], policy, workspace
    )
    subprocess.run(
        copy_argv, cwd=workspace, capture_output=True, text=True, check=True, timeout=10
    )
    assert copied.read_text() == source.read_text()
    code = (
        "import os, sys\n"
        "try:\n"
        "    os.link(sys.argv[1], sys.argv[2])\n"
        "except OSError as error:\n"
        "    print(error.errno)\n"
    )
    argv = BwrapSandboxBackend().wrap_launcher_argv(
        [sys.executable, "-I", "-c", code, str(source), str(alias)], policy, workspace
    )
    result = subprocess.run(argv, cwd=workspace, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(errno.EXDEV)
    assert not alias.exists()
    os.link(source, alias)
    assert alias.exists(), "the filesystem itself must permit the link"


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
    # Read startup roots without running .pth hooks.
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
    assert not marker.exists(), "startup root checks must not execute sitecustomize"


@pytest.mark.skipif(os.name == "nt", reason="POSIX disposable venv")
def test_external_pth_import_is_rejected_before_startup_code_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(runtime)
    interpreter = runtime / "bin/python"
    packages = (
        runtime
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    external = tmp_path / "external"
    external.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "external-import-ran"
    module = external / "_distutils_hack.py"
    module.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\ndef add_shim(): pass\n"
    )
    os.link(module, workspace / "editable.py")
    (packages / "a_external.pth").write_text(str(external) + "\n")
    (packages / "distutils-precedence.pth").write_text(
        "import os; var = 'SETUPTOOLS_USE_DISTUTILS'; enabled = os.environ.get(var, 'local') "
        "== 'local'; enabled and __import__('_distutils_hack').add_shim(); \n"
    )
    monkeypatch.setattr(sys, "executable", str(interpreter))
    policy = _policy()
    policy.write_roots = [workspace]

    with pytest.raises(OSError, match="hard-link alias"):
        sb.create_exec_launcher("/bin/true", policy)
    assert not marker.exists(), "validation must not execute .pth imports"


@pytest.mark.skipif(os.name == "nt", reason="POSIX disposable venv")
def test_unsupported_pth_startup_form_fails_closed(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(runtime)
    interpreter = runtime / "bin/python"
    packages = (
        runtime
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    marker = tmp_path / "startup-ran"
    (packages / "dynamic.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).touch()\n"
    )

    with pytest.raises(OSError, match=r"unsupported \.pth startup form"):
        sb._isolated_launcher_import_roots(str(interpreter))
    assert not marker.exists()


@pytest.mark.parametrize("config_location", ["adjacent", "venv-root"])
def test_isolated_pth_roots_follow_cpython_venv_config_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_location: str
) -> None:
    runtime = tmp_path / "runtime"
    interpreter = runtime / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    base_site = tmp_path / "base-site"
    base_site.mkdir()
    venv_site = tmp_path / "venv-site"
    venv_site.mkdir()
    external = tmp_path / "external-imports"
    external.mkdir()
    (base_site / "external.pth").write_text(str(external) + "\n")
    if config_location == "adjacent":
        (runtime / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
        (interpreter.parent / "pyvenv.cfg").write_text("include-system-site-packages = true\n")
    else:
        (runtime / "pyvenv.cfg").write_text("home = /trusted/bin\n")

    metadata = {
        "paths": [str(tmp_path / "stdlib")],
        "base_sites": [str(base_site)],
        "venv_sites": [str(venv_site)],
    }
    monkeypatch.setattr(
        sb.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=json.dumps(metadata)),
    )

    roots = sb._isolated_launcher_import_roots(str(interpreter))
    assert external in roots
    assert base_site in roots
    assert venv_site in roots


def test_editable_finder_must_match_installed_template(tmp_path: Path) -> None:
    site_dir = tmp_path / "site-packages"
    template_path = site_dir / "setuptools" / "command" / "editable_wheel.py"
    template_path.parent.mkdir(parents=True)
    template = (
        "MAPPING = {mapping!r}\n"
        "NAMESPACES = {namespaces!r}\n"
        "PATH_PLACEHOLDER = {name!r} + '.__path_hook__'\n"
        "def install():\n    pass\n"
    )
    template_path.write_text(f"_FINDER_TEMPLATE = {template!r}\n")
    finder = site_dir / "__editable___sample_finder.py"
    finder.write_text(
        template.format(name="__editable__.sample.finder", mapping={}, namespaces={})
    )
    pth = site_dir / "__editable__.sample.pth"
    line = "import __editable___sample_finder; __editable___sample_finder.install()"
    assert sb._trusted_pth_import_roots(pth, line) == set()

    finder.write_text(finder.read_text() + "import pathlib; pathlib.Path('/tmp/marker').touch()\n")
    with pytest.raises(OSError, match=r"unsupported \.pth editable finder"):
        sb._trusted_pth_import_roots(pth, line)


@pytest.mark.skipif(os.name == "nt", reason="POSIX disposable venv")
def test_isolated_pth_roots_are_read_again_after_change(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(runtime)
    interpreter = runtime / "bin/python"
    packages = (
        runtime
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    startup = packages / "external.pth"
    first = tmp_path / "first"
    second = tmp_path / "second"
    startup.write_text(str(first) + "\n")
    assert first in sb._isolated_launcher_import_roots(str(interpreter))
    startup.write_text(str(second) + "\n")
    roots = sb._isolated_launcher_import_roots(str(interpreter))
    assert second in roots
    assert first not in roots


@pytest.mark.parametrize("name", ["COVERAGE_PROCESS_START", "COVERAGE_PROCESS_CONFIG"])
def test_launcher_rejects_staged_coverage_startup(
    runtime_layout: SimpleNamespace, name: str
) -> None:
    with pytest.raises(OSError, match="coverage startup environment"):
        sb.create_exec_launcher("/bin/true", _policy(), env_overrides={name: "config"})


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher shell")
def test_wrapped_launcher_clears_inherited_coverage_before_python_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(runtime)
    interpreter = runtime / "bin/python"
    packages = (
        runtime
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    marker = tmp_path / "coverage-ran"
    (packages / "coverage.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    )
    (packages / "a1_coverage.pth").write_text(
        'import sys; exec(\'import os\\n\\nif os.getenv("COVERAGE_PROCESS_START") or '
        'os.getenv("COVERAGE_PROCESS_CONFIG"):\\n try:\\n  import coverage\\n '
        'except:\\n  pass\\n else:\\n  coverage.process_startup(slug="pth")\')\n'
    )
    monkeypatch.setattr(sys, "executable", str(interpreter))
    monkeypatch.setattr(sb, "_launcher_inline_source", lambda *_args, **_kwargs: "print('ok')")
    path = Path(sb.create_exec_launcher("/bin/true", _policy()))
    try:
        result = subprocess.run(
            [str(path)],
            env={
                "PATH": os.defpath,
                "COVERAGE_PROCESS_START": str(tmp_path / "coverage-config"),
                "COVERAGE_PROCESS_CONFIG": str(tmp_path / "coverage-config"),
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "ok"
        assert not marker.exists()
    finally:
        path.unlink()


def test_isolated_import_snapshot_failure_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("isolated startup", 10)

    monkeypatch.setattr(sb.subprocess, "run", fail)
    with pytest.raises(OSError, match="Cannot validate sandbox launcher isolated startup imports"):
        sb._isolated_launcher_import_roots(str(tmp_path / "uncached-interpreter"))


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

    alias_dir = tmp_path / "contained"
    alias_dir.mkdir()
    os.link(startup, alias_dir / "editable.py")
    policy.write_roots = [alias_dir]
    policy.write_files = []
    with pytest.raises(OSError, match="hard-link alias"):
        sb.create_exec_launcher("/bin/true", policy)
    assert not marker.exists(), "the isolated startup probe must not run poisoned .pth code"


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
