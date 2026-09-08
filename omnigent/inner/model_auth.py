"""Trusted, shell-free authentication for the model signer."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit

from . import _proc
from .egress.rules import is_dns_safe_host

PROVIDER_AUTH_REQUIRED = "PROVIDER_AUTH_REQUIRED"

_MAX_TOKEN_BYTES = 8 * 1024
_TOKEN_TIMEOUT_SECONDS = 15.0
_PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class ProviderAuthRequired(RuntimeError):
    """A trusted provider credential is missing, expired, or malformed."""

    code = PROVIDER_AUTH_REQUIRED


def _validated_authority(host: str, profile: str) -> tuple[str, str]:
    parsed = urlsplit(host)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or not is_dns_safe_host(parsed.hostname)
    ):
        raise ValueError("trusted authentication host must be an HTTPS origin")
    if _PROFILE_RE.fullmatch(profile) is None:
        raise ValueError("trusted authentication profile is invalid")
    return f"https://{parsed.hostname.lower()}", profile


def _provider_auth_message(host: str, profile: str) -> str:
    host, profile = _validated_authority(host, profile)
    return (
        f"Provider authentication required for host {host} and profile {profile}. "
        "Run `ucode configure` or "
        f"`databricks auth login --host {host} --profile {profile}`, then Retry."
    )


def _resolve_ucode_executable() -> Path:
    candidate = shutil.which("ucode")
    if candidate is None:
        raise FileNotFoundError("trusted ucode executable is unavailable")
    resolved = Path(candidate).resolve(strict=True)
    mode = resolved.stat().st_mode
    if (
        not stat.S_ISREG(mode)
        or not os.access(resolved, os.X_OK)
        or mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise PermissionError("trusted ucode executable is unsafe")
    return resolved


def _ucode_auth_token_argv(executable: Path, *, host: str, profile: str) -> list[str]:
    """Build the fixed argv supported by ucode's hidden auth-token command."""
    host, profile = _validated_authority(host, profile)
    return [
        str(executable),
        "auth-token",
        "--host",
        host,
        "--profile",
        profile,
        "--force-refresh",
    ]


def _ucode_env() -> dict[str, str]:
    env = {
        name: value
        for name in ("HOME", "PATH", "LANG", "LC_ALL", "TZ")
        if (value := os.environ.get(name)) is not None
    }
    # requests honors NETRC. Pointing it at the null device prevents implicit
    # ~/.netrc discovery while retaining HOME for Databricks CLI credentials.
    env["NETRC"] = os.devnull
    return env


async def _collect_stdout(proc: asyncio.subprocess.Process) -> bytes:
    assert proc.stdout is not None
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await proc.stdout.read(min(4096, _MAX_TOKEN_BYTES + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > _MAX_TOKEN_BYTES:
            _proc.kill_tree(proc)
            break
    await proc.wait()
    return b"".join(chunks)


def _parse_token(stdout: bytes, returncode: int | None) -> str:
    if returncode != 0 or not stdout.endswith(b"\n") or len(stdout) > _MAX_TOKEN_BYTES:
        raise ValueError("credential helper failed")
    token_bytes = stdout[:-1]
    if not token_bytes or any(byte < 0x21 or byte > 0x7E for byte in token_bytes):
        raise ValueError("credential helper returned malformed output")
    return token_bytes.decode("ascii")


async def mint_ucode_token(*, host: str, profile: str) -> str:
    """Mint one opaque credential without accepting a command or a shell."""
    host, profile = _validated_authority(host, profile)
    message = _provider_auth_message(host, profile)
    proc: asyncio.subprocess.Process | None = None
    liveness_read_fd: int | None = None
    liveness_write_fd: int | None = None
    try:
        executable = _resolve_ucode_executable()
        helper_argv = _ucode_auth_token_argv(executable, host=host, profile=profile)
        pass_fds: tuple[int, ...] = ()
        if os.name == "posix":
            liveness_read_fd, liveness_write_fd = os.pipe()
            os.set_inheritable(liveness_read_fd, True)
            helper_argv = [
                sys.executable,
                str(Path(__file__).with_name("_liveness_exec.py")),
                "--liveness-fd",
                str(liveness_read_fd),
                *helper_argv,
            ]
            pass_fds = (liveness_read_fd,)
        proc = await asyncio.create_subprocess_exec(
            *helper_argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=_ucode_env(),
            pass_fds=pass_fds,
            **_proc.spawn_kwargs(),
        )
        if liveness_read_fd is not None:
            os.close(liveness_read_fd)
            liveness_read_fd = None
        _proc.remember_process_group(proc)
        stdout = await asyncio.wait_for(_collect_stdout(proc), timeout=_TOKEN_TIMEOUT_SECONDS)
        return _parse_token(stdout, proc.returncode)
    except asyncio.CancelledError:
        if proc is not None:
            _proc.terminate_tree(proc)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=2)
            _proc.kill_tree(proc)
            with contextlib.suppress(Exception):
                await proc.wait()
        raise
    except (OSError, ValueError, asyncio.TimeoutError):
        if proc is not None:
            _proc.terminate_tree(proc)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=2)
            _proc.kill_tree(proc)
            with contextlib.suppress(Exception):
                await proc.wait()
        raise ProviderAuthRequired(message) from None
    finally:
        for fd in (liveness_read_fd, liveness_write_fd):
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
