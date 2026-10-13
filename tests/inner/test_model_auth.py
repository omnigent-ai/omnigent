"""Trusted ucode token adapter tests."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from omnigent.inner._proc import process_alive
from omnigent.inner.model_auth import (
    PROVIDER_AUTH_REQUIRED,
    ProviderAuthRequired,
    _resolve_ucode_executable,
    _TrustedExecutable,
    _ucode_auth_token_argv,
    mint_ucode_token,
)

_HOST = "https://workspace.cloud.databricks.com"
_PROFILE = "agent-profile"


def _write_ucode(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_ucode_argv_is_fixed_and_contains_only_validated_authority(tmp_path: Path) -> None:
    executable = _write_ucode(tmp_path / "ucode", "exit 0\n").resolve()

    assert _ucode_auth_token_argv(executable, host=_HOST, profile=_PROFILE) == [
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
        _resolve_ucode_executable()


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
        _resolve_ucode_executable()


async def test_ucode_replacement_after_validation_fails_before_exec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = _write_ucode(tmp_path / "ucode", "printf 'first-token\\n'\n")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}/usr/bin:/bin")
    original_resolve = _resolve_ucode_executable

    def _resolve_then_replace() -> _TrustedExecutable:
        resolved = original_resolve()
        replacement = _write_ucode(tmp_path / "replacement", "printf 'second-token\\n'\n")
        replacement.replace(executable)
        return resolved

    monkeypatch.setattr(
        "omnigent.inner.model_auth._resolve_ucode_executable", _resolve_then_replace
    )

    with pytest.raises(ProviderAuthRequired):
        await mint_ucode_token(host=_HOST, profile=_PROFILE)


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
        await mint_ucode_token(host=host, profile=profile)


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
    ):
        monkeypatch.setenv(name, "attacker-controlled")

    assert await mint_ucode_token(host=_HOST, profile=_PROFILE) == "opaque-token-value"
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

    assert await mint_ucode_token(host=_HOST, profile=_PROFILE) == "opaque-token-value"
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

    mint = asyncio.create_task(mint_ucode_token(host=_HOST, profile=_PROFILE))
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
        await mint_ucode_token(host=_HOST, profile=_PROFILE)

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
        await mint_ucode_token(host=_HOST, profile=_PROFILE)

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
        await mint_ucode_token(host=_HOST, profile=_PROFILE)

    message = str(raised.value)
    assert raised.value.code == PROVIDER_AUTH_REQUIRED
    assert "SECRET_HELPER_ERROR" not in message
    assert "xxxx" not in message
    assert "ucode configure" in message
    assert f"databricks auth login --host {_HOST} --profile {_PROFILE}" in message
