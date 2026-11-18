"""Trusted ucode token adapter tests."""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.inner._proc import process_alive
from omnigent.inner.model_auth import (
    PROVIDER_AUTH_REQUIRED,
    ProviderAuthRequired,
    _auth_base_runtime_trees,
    _pin_auth_python_runtime,
    _profile_token_argv,
    _resolve_auth_executable,
    _TrustedExecutable,
    _validate_auth_runtime,
    mint_profile_token,
)
from omnigent.inner.sandbox import SandboxPolicy

_HOST = "https://workspace.cloud.databricks.com"
_PROFILE = "agent-profile"


@pytest.fixture
def trusted_python_helper(tmp_path: Path) -> tuple[_TrustedExecutable, Path, Path, Path]:
    # Disposable native-file stand-in: rejection tests must never modify host Python.
    binary_dir = tmp_path / "native-runtime"
    binary_dir.mkdir()
    binary = binary_dir / "python-real"
    binary.write_bytes(b"\x7fELF-disposable-runtime")
    binary.chmod(0o700)
    hop_dir = tmp_path / "interpreter-links"
    hop_dir.mkdir()
    hop = hop_dir / "python"
    hop.symlink_to(binary)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    runtime_bin = tmp_path / "runtime-bin"
    runtime_bin.mkdir()
    (runtime / "bin").symlink_to(runtime_bin, target_is_directory=True)
    interpreter = runtime / "bin/python"
    interpreter.symlink_to(hop)
    helper = tmp_path / "profile-auth"
    helper.write_text(f"#!{interpreter}\nprint('unused')\n")
    helper.chmod(0o700)
    info = helper.stat()
    return _TrustedExecutable(helper, (info.st_dev, info.st_ino)), interpreter, hop, binary


def test_pinned_interpreter_keeps_original_invocation_path(
    trusted_python_helper: tuple[_TrustedExecutable, Path, Path, Path],
) -> None:
    executable, interpreter, _, binary = trusted_python_helper
    pinned = _pin_auth_python_runtime(executable)

    assert pinned.interpreter == interpreter
    assert pinned.interpreter != binary
    info = binary.stat()
    assert pinned.interpreter_identity == (info.st_dev, info.st_ino)


@pytest.mark.parametrize("target", ["helper", "interpreter"])
def test_auth_runtime_rejects_writable_hardlink_alias(
    trusted_python_helper: tuple[_TrustedExecutable, Path, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    from omnigent.inner import sandbox

    executable, _, _, binary = trusted_python_helper
    pinned = _pin_auth_python_runtime(executable)
    trusted = pinned.path if target == "helper" else binary
    original_identity = (trusted.stat().st_dev, trusted.stat().st_ino)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = workspace / "editable"
    os.link(trusted, alias)
    alias.write_bytes(b"modified in place")
    assert (trusted.stat().st_dev, trusted.stat().st_ino) == original_identity

    monkeypatch.setattr(sandbox, "_validate_launcher_runtime", lambda _policy: None)
    monkeypatch.setattr(sandbox, "_isolated_launcher_import_roots", lambda _python: ())
    policy = SandboxPolicy("linux_bwrap", True, None, [workspace], [], False)
    with pytest.raises(ValueError, match="outside sandbox-writable paths"):
        _validate_auth_runtime(pinned, policy)


def test_auth_runtime_rejects_copied_interpreter_base_stdlib_alias_before_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.inner import sandbox

    base_bin = tmp_path / "base" / "bin"
    base_bin.mkdir(parents=True)
    base_python = base_bin / "python"
    base_python.write_bytes(b"\x7fELF-disposable-runtime")
    base_python.chmod(0o700)
    stdlib = base_bin.parent / "lib" / "python3.12"
    (stdlib / "json").mkdir(parents=True)
    (stdlib / "json" / "__init__.py").write_text("# disposable stdlib\n")
    site = stdlib / "site.py"
    site.write_text("# disposable stdlib\n")
    runtime = tmp_path / "auth-venv"
    (runtime / "bin").mkdir(parents=True)
    interpreter = runtime / "bin" / "python"
    shutil.copy2(base_python, interpreter)
    (runtime / "pyvenv.cfg").write_text(f"home = {base_bin}\nversion = 3.12.0\n")
    helper = tmp_path / "profile-auth"
    helper.write_text(f"#!{interpreter}\nprint('unused')\n")
    helper.chmod(0o700)
    info = helper.stat()
    pinned = _pin_auth_python_runtime(_TrustedExecutable(helper, (info.st_dev, info.st_ino)))
    assert not interpreter.is_symlink()
    assert interpreter.stat().st_ino != base_python.stat().st_ino

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = workspace / "editable.py"
    os.link(site, alias)
    alias.write_text("# worker can change base site import\n")
    probe = Mock(side_effect=AssertionError("auth interpreter probe ran"))
    monkeypatch.setattr(sandbox, "_validate_launcher_runtime", lambda _policy: None)
    monkeypatch.setattr(sandbox, "_isolated_launcher_import_roots", probe)

    policy = SandboxPolicy("linux_bwrap", True, None, [workspace], [], False)
    with pytest.raises(ValueError, match="outside sandbox-writable paths"):
        _validate_auth_runtime(pinned, policy)
    probe.assert_not_called()


def test_auth_base_runtime_accepts_venv_without_optional_version(tmp_path: Path) -> None:
    base_bin = tmp_path / "base" / "bin"
    base_bin.mkdir(parents=True)
    stdlib = base_bin.parent / "lib" / "python3.12"
    (stdlib / "json").mkdir(parents=True)
    (stdlib / "json" / "__init__.py").write_text("# disposable stdlib\n")
    (stdlib / "site.py").write_text("# disposable stdlib\n")
    interpreter = tmp_path / "runtime" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"\x7fELF-disposable-runtime")
    (interpreter.parent.parent / "pyvenv.cfg").write_text(f"home = {base_bin}\n")

    assert base_bin.parent / "lib" in _auth_base_runtime_trees(interpreter)


@pytest.mark.parametrize(
    "config_text",
    ["version = 3.12.0\n", "home = relative/bin\nversion = 3.12.0\n"],
)
def test_auth_runtime_rejects_indeterminate_venv_home_before_probe(
    trusted_python_helper: tuple[_TrustedExecutable, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    config_text: str,
) -> None:
    from omnigent.inner import sandbox

    executable, interpreter, _, _ = trusted_python_helper
    (interpreter.parent.parent / "pyvenv.cfg").write_text(config_text)
    probe = Mock(side_effect=AssertionError("auth interpreter probe ran"))
    monkeypatch.setattr(sandbox, "_validate_launcher_runtime", lambda _policy: None)
    monkeypatch.setattr(sandbox, "_isolated_launcher_import_roots", probe)

    policy = SandboxPolicy("linux_bwrap", True, None, [], [], False)
    with pytest.raises(ValueError, match="outside sandbox-writable paths"):
        _validate_auth_runtime(_pin_auth_python_runtime(executable), policy)
    probe.assert_not_called()


@pytest.mark.parametrize("stage", ["pin", "mint"])
@pytest.mark.parametrize(
    "component", ["directory-target", "hop-parent", "resolved-parent", "leaf"]
)
async def test_interpreter_rejects_writable_provenance_before_launch(
    trusted_python_helper: tuple[_TrustedExecutable, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    component: str,
) -> None:
    executable, interpreter, hop, binary = trusted_python_helper
    if stage == "mint":
        executable = _pin_auth_python_runtime(executable)
    unsafe = {
        "directory-target": interpreter.parent.resolve(),
        "hop-parent": hop.parent,
        "resolved-parent": binary.parent,
        "leaf": binary,
    }[component]
    unsafe.chmod(0o777)
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    if stage == "pin":
        with pytest.raises(PermissionError, match="unsafe"):
            _pin_auth_python_runtime(executable)
    else:
        with pytest.raises(ProviderAuthRequired):
            await mint_profile_token(host=_HOST, profile=_PROFILE, _executable=executable)
    spawn.assert_not_awaited()


@pytest.mark.parametrize("component", ["symlink", "leaf"])
def test_interpreter_rejects_foreign_owned_leaf_and_symlink(
    trusted_python_helper: tuple[_TrustedExecutable, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    component: str,
) -> None:
    executable, _, hop, binary = trusted_python_helper
    foreign = hop if component == "symlink" else binary
    original_lstat = Path.lstat

    def foreign_lstat(path: Path) -> os.stat_result:
        info = original_lstat(path)
        if path == foreign:
            fields = list(info)
            fields[4] = os.getuid() + 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "lstat", foreign_lstat)
    with pytest.raises(
        PermissionError, match="trusted authentication executable has unsafe ownership"
    ):
        _pin_auth_python_runtime(executable)


@pytest.mark.parametrize("change", ["replace-binary", "retarget-link", "missing-identity"])
async def test_interpreter_identity_rechecked_before_mint(
    trusted_python_helper: tuple[_TrustedExecutable, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    executable, interpreter, hop, binary = trusted_python_helper
    if change == "missing-identity":
        executable = _TrustedExecutable(executable.path, executable.identity, interpreter)
    else:
        executable = _pin_auth_python_runtime(executable)
        replacement = binary.with_name("replacement")
        replacement.write_bytes(binary.read_bytes())
        replacement.chmod(0o700)
        if change == "replace-binary":
            replacement.replace(binary)
        else:
            hop.unlink()
            hop.symlink_to(replacement)
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    with pytest.raises(ProviderAuthRequired):
        await mint_profile_token(host=_HOST, profile=_PROFILE, _executable=executable)
    spawn.assert_not_awaited()


def _write_ucode(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_ucode_argv_is_fixed_and_contains_only_validated_authority(tmp_path: Path) -> None:
    executable = _write_ucode(tmp_path / "ucode", "exit 0\n").resolve()

    assert _profile_token_argv(executable, host=_HOST, profile=_PROFILE) == [
        str(executable),
        "auth-token",
        "--host",
        _HOST,
        "--profile",
        _PROFILE,
        "--force-refresh",
    ]


def test_ucode_resolution_rejects_writable_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    unsafe_bin = tmp_path / "unsafe-bin"
    unsafe_bin.mkdir(mode=0o755)
    unsafe_bin.chmod(0o777)
    _write_ucode(unsafe_bin / "ucode", "printf 'opaque-token-value\\n'\n")
    monkeypatch.setenv("PATH", str(unsafe_bin))

    with pytest.raises(PermissionError, match="unsafe"):
        _resolve_auth_executable()


def test_ucode_resolution_rejects_unsafe_symlink_chain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trusted_bin = tmp_path / "trusted-bin"
    trusted_bin.mkdir(mode=0o755)
    real = _write_ucode(trusted_bin / "ucode-real", "printf 'opaque-token-value\\n'\n")
    unsafe_links = tmp_path / "unsafe-links"
    unsafe_links.mkdir(mode=0o777)
    unsafe_links.chmod(0o777)
    link = unsafe_links / "ucode"
    link.symlink_to(real)
    monkeypatch.setenv("PATH", str(unsafe_links))

    with pytest.raises(PermissionError, match="unsafe"):
        _resolve_auth_executable()


async def test_ucode_replacement_after_validation_fails_before_exec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = _write_ucode(tmp_path / "ucode", "printf 'first-token\\n'\n")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}/usr/bin:/bin")
    original_resolve = _resolve_auth_executable

    def _resolve_then_replace() -> _TrustedExecutable:
        resolved = original_resolve()
        replacement = _write_ucode(tmp_path / "replacement", "printf 'second-token\\n'\n")
        replacement.replace(executable)
        return resolved

    monkeypatch.setattr(
        "omnigent.inner.model_auth._resolve_auth_executable", _resolve_then_replace
    )

    with pytest.raises(ProviderAuthRequired):
        await mint_profile_token(host=_HOST, profile=_PROFILE)


@pytest.mark.parametrize(
    ("host", "profile"),
    [
        ("http://workspace.cloud.databricks.com", _PROFILE),
        ("https://workspace.cloud.databricks.com/path", _PROFILE),
        ("https://user@workspace.cloud.databricks.com", _PROFILE),
        (_HOST, ""),
        (_HOST, "profile; touch /tmp/pwned"),
        (_HOST, "../profile"),
    ],
)
async def test_ucode_adapter_rejects_untrusted_host_and_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    host: str,
    profile: str,
) -> None:
    _write_ucode(tmp_path / "ucode", "printf 'should-not-run\\n'\n")
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(ValueError):
        await mint_profile_token(host=host, profile=profile)


async def test_ucode_adapter_uses_hermetic_environment_and_fixed_argv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    args_file = tmp_path / "args"
    env_file = tmp_path / "env"
    script = f"""
printf '%s\\n' "$@" > {args_file}
env > {env_file}
printf 'opaque-token-value\\n'
"""
    _write_ucode(tmp_path / "ucode", script)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}/usr/bin:/bin")
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "CURL_HOME",
        "COOKIE",
        "DATABRICKS_CONFIG_FILE",
    ):
        monkeypatch.setenv(name, "attacker-controlled")

    assert await mint_profile_token(host=_HOST, profile=_PROFILE) == "opaque-token-value"
    assert args_file.read_text(encoding="utf-8").splitlines() == [
        "auth-token",
        "--host",
        _HOST,
        "--profile",
        _PROFILE,
        "--force-refresh",
    ]
    child_env = env_file.read_text(encoding="utf-8")
    assert "attacker-controlled" not in child_env
    assert f"NETRC={os.devnull}" in child_env


async def test_ucode_helper_gets_an_owned_process_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_ucode(tmp_path / "ucode", "printf 'opaque-token-value\\n'\n")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}/usr/bin:/bin")
    original = asyncio.create_subprocess_exec
    observed_kwargs: dict[str, object] = {}

    async def _spawn(*argv: str, **kwargs: object) -> asyncio.subprocess.Process:
        observed_kwargs.update(kwargs)
        return await original(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)

    assert await mint_profile_token(host=_HOST, profile=_PROFILE) == "opaque-token-value"
    if os.name == "posix":
        assert observed_kwargs["start_new_session"] is True
    else:
        assert "creationflags" in observed_kwargs


async def test_cancelled_ucode_mint_terminates_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    started = tmp_path / "started"
    _write_ucode(
        tmp_path / "ucode",
        f": > {started}\ntrap 'exit 0' TERM\nwhile true; do sleep 1; done\n",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}/usr/bin:/bin")

    mint = asyncio.create_task(mint_profile_token(host=_HOST, profile=_PROFILE))
    while not started.exists():
        await asyncio.sleep(0)
    mint.cancel()

    with pytest.raises(asyncio.CancelledError):
        await mint


@pytest.mark.skipif(os.name != "posix", reason="forked helper acceptance is POSIX-only")
async def test_nonzero_helper_leader_exit_does_not_leave_descendants(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    child_pid_path = tmp_path / "child.pid"
    _write_ucode(
        tmp_path / "ucode",
        f"(trap '' TERM; while true; do sleep 1; done) >/dev/null 2>&1 &\n"
        f"printf '%s' \"$!\" > {child_pid_path}\n"
        "exit 19\n",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}/usr/bin:/bin")

    with pytest.raises(ProviderAuthRequired):
        await mint_profile_token(host=_HOST, profile=_PROFILE)

    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    for _ in range(100):
        if not process_alive(child_pid):
            break
        await asyncio.sleep(0.02)
    assert not process_alive(child_pid)


@pytest.mark.parametrize(
    "stdout",
    [
        b"",
        b"token",
        b"token\nextra\n",
        b"token\x00\n",
        b"token with-space\n",
        b" token\n",
        b"token\tvalue\n",
        b"token\r\n",
        b"token\x1f\n",
    ],
)
async def test_ucode_adapter_rejects_noncanonical_stdout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stdout: bytes,
) -> None:
    encoded = "".join(f"\\{byte:03o}" for byte in stdout)
    _write_ucode(tmp_path / "ucode", f"printf '{encoded}'\n")
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(ProviderAuthRequired) as raised:
        await mint_profile_token(host=_HOST, profile=_PROFILE)

    assert raised.value.code == PROVIDER_AUTH_REQUIRED
    assert "token" not in str(raised.value).lower()


async def test_ucode_adapter_bounds_stdout_and_hides_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_ucode(
        tmp_path / "ucode",
        "printf 'SECRET_HELPER_ERROR' >&2\n"
        'i=0; while [ "$i" -lt 9000 ]; do printf x; i=$((i + 1)); done\n',
    )
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(ProviderAuthRequired) as raised:
        await mint_profile_token(host=_HOST, profile=_PROFILE)

    message = str(raised.value)
    assert raised.value.code == PROVIDER_AUTH_REQUIRED
    assert "SECRET_HELPER_ERROR" not in message
    assert "xxxx" not in message
    assert "ucode configure" in message
    assert f"databricks auth login --host {_HOST} --profile {_PROFILE}" in message
