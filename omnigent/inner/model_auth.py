"""Trusted, shell-free authentication for the model signer."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import stat
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from . import _proc
from ._subprocess_lifecycle import close_subprocess_transport, terminate_subprocess
from .egress.rules import is_dns_safe_host

if TYPE_CHECKING:
    from .sandbox import SandboxPolicy

PROVIDER_AUTH_REQUIRED = "PROVIDER_AUTH_REQUIRED"

_MAX_TOKEN_BYTES = 8 * 1024
_TOKEN_TIMEOUT_SECONDS = 15.0
_PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class ProviderAuthRequired(RuntimeError):
    """A trusted provider credential is missing, expired, or malformed."""

    code = PROVIDER_AUTH_REQUIRED
    title = "Databricks authentication required"
    cause = "Databricks authentication for the selected workspace/profile is missing or expired."

    def __init__(
        self,
        message: str,
        *,
        host: str | None = None,
        profile: str | None = None,
    ) -> None:
        super().__init__(message)
        self.remediation = "ucode configure" if host is not None and profile is not None else None

    @classmethod
    def for_authority(cls, host: str, profile: str) -> ProviderAuthRequired:
        host, profile = _validated_authority(host, profile)
        return cls(_provider_auth_message(host, profile), host=host, profile=profile)


@dataclass(frozen=True)
class _TrustedExecutable:
    path: Path
    identity: tuple[int, int]
    interpreter: Path | None = None
    interpreter_identity: tuple[int, int] | None = None


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


def _validate_provenance_chain(path: Path) -> None:
    """Reject executable paths that can be replaced by another local user."""
    current = path
    while True:
        info = current.lstat()
        if info.st_uid not in (0, os.getuid()):
            raise PermissionError("trusted authentication executable has unsafe ownership")
        writable = info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        # Root-owned sticky temporary roots are safe traversal boundaries:
        # another user cannot replace this user's private child directory.
        sticky_temp_root = (
            stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
        )
        if writable and not sticky_temp_root and not stat.S_ISLNK(info.st_mode):
            raise PermissionError(
                "trusted authentication executable has unsafe writable path component"
            )
        if current.parent == current:
            return
        current = current.parent


def _is_beneath(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _resolve_auth_executable() -> _TrustedExecutable:
    candidate = shutil.which("ucode")
    if candidate is None:
        raise FileNotFoundError("trusted ucode executable is unavailable")
    candidate_path = Path(candidate).absolute()
    executable = _validate_auth_executable_path(candidate_path)
    workspace = Path.cwd().resolve(strict=True)
    if _is_beneath(candidate_path, workspace) or _is_beneath(executable.path, workspace):
        raise PermissionError("trusted authentication executable must not be workspace-controlled")
    return executable


def _validate_auth_executable_path(path: Path) -> _TrustedExecutable:
    """Validate every lexical and resolved component, including the executable leaf."""
    from .sandbox import _launcher_path_variants

    try:
        variants = _launcher_path_variants(path)
    except RuntimeError as exc:
        raise PermissionError("trusted authentication executable has a symlink loop") from exc
    for variant in variants:
        _validate_provenance_chain(variant)
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    mode = info.st_mode
    if (
        not stat.S_ISREG(mode)
        or not os.access(resolved, os.X_OK)
        or mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise PermissionError("trusted authentication executable is unsafe")
    return _TrustedExecutable(path=resolved, identity=(info.st_dev, info.st_ino))


def _pin_auth_python_runtime(executable: _TrustedExecutable) -> _TrustedExecutable:
    """Pin a supported interpreter without executing the credential helper."""
    with executable.path.open("rb") as source:
        shebang = source.readline(4096).decode("utf-8", errors="replace").strip()
    words = shebang[2:].split() if shebang.startswith("#!") else []
    if (
        len(words) != 1
        or not Path(words[0]).is_absolute()
        or re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(words[0]).name) is None
    ):
        raise ValueError("profile authentication requires an absolute Python interpreter shebang")
    interpreter = Path(words[0])
    target = _validate_auth_executable_path(interpreter)
    with interpreter.open("rb") as source:
        if source.read(2) == b"#!":
            raise ValueError("profile authentication requires a native Python interpreter")
    # Keep the invocation path: resolving a venv symlink would bypass its site-packages.
    return replace(executable, interpreter=interpreter, interpreter_identity=target.identity)


def _auth_base_runtime_trees(interpreter: Path) -> set[Path]:
    """Locate the base stdlib from venv metadata before starting Python."""
    from .sandbox import _launcher_path_variants

    configs: list[Path] = []
    for path in (interpreter.parent / "pyvenv.cfg", interpreter.parent.parent / "pyvenv.cfg"):
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        configs.append(path)
    if len(configs) > 1:
        raise OSError("Cannot determine authentication Python base runtime")

    homes: set[Path] = set()
    version: str | None = None
    if configs:
        config = configs[0]
        for variant in _launcher_path_variants(config):
            _validate_provenance_chain(variant)
        if not config.is_file():
            raise OSError("Cannot read authentication Python venv configuration")
        values: dict[str, str] = {}
        try:
            lines = config.read_text(encoding="utf-8").splitlines()
        except UnicodeError as exc:
            raise OSError("Cannot read authentication Python venv configuration") from exc
        for line in lines:
            key, separator, value = line.partition("=")
            key = key.strip().lower()
            if key not in ("home", "version", "version_info", "executable"):
                continue
            if not separator or key in values or not value.strip():
                raise OSError("Ambiguous authentication Python venv configuration")
            values[key] = value.strip()
        home = Path(values.get("home", ""))
        if not home.is_absolute() or not home.is_dir():
            raise OSError("Cannot determine authentication Python base runtime")
        homes.add(home)
        executable_value = values.get("executable")
        if executable_value is not None:
            base_executable = Path(executable_value)
            if not base_executable.is_absolute() or not base_executable.is_file():
                raise OSError("Cannot determine authentication Python base runtime")
            for variant in _launcher_path_variants(base_executable):
                _validate_provenance_chain(variant)
            homes.add(base_executable.parent)
            homes.add(base_executable.resolve(strict=True).parent)
        versions: set[str] = set()
        for key in ("version", "version_info"):
            if (value := values.get(key)) is None:
                continue
            match = re.match(r"(\d+\.\d+)(?:\.|\Z)", value)
            if match is None:
                raise OSError("Cannot determine authentication Python base runtime")
            versions.add(match.group(1))
        if len(versions) > 1:
            raise OSError("Cannot determine authentication Python base runtime")
        version = versions.pop() if versions else None
    else:
        homes.add(interpreter.resolve(strict=True).parent)

    prefixes: set[Path] = set()
    for home in homes:
        for variant in _launcher_path_variants(home):
            _validate_provenance_chain(variant)
            prefixes.add(variant.parent)

    trees = {
        prefix / library for prefix in prefixes for library in ("lib", "lib64", "Lib", "DLLs")
    }
    stdlibs: set[Path] = set()
    for tree in trees:
        if not tree.is_dir():
            continue
        candidates = [tree / f"python{version}"] if version is not None else tree.glob("python*")
        stdlibs.update(candidate for candidate in candidates if candidate.is_dir())
    if not any(
        (stdlib / "site.py").is_file() and (stdlib / "json" / "__init__.py").is_file()
        for stdlib in stdlibs
    ):
        raise OSError("Cannot determine authentication Python base stdlib")
    return trees


def _validate_auth_runtime(executable: _TrustedExecutable, policy: SandboxPolicy) -> None:
    """Reject grants that can replace helper code or either Python startup runtime."""
    from .sandbox import (
        _isolated_launcher_import_roots,
        _launcher_path_variants,
        _validate_launcher_runtime,
        _validate_launcher_write_grants,
        _validate_trusted_hardlink_aliases,
    )

    entries = _launcher_path_variants(executable.path)
    trees: set[Path] = set()
    if executable.interpreter is None:
        raise ValueError("profile authentication requires a pinned Python runtime")
    for interpreter in _launcher_path_variants(executable.interpreter):
        entries.add(interpreter)
        entries.add(interpreter.parent / "pyvenv.cfg")
        entries.add(interpreter.parent.parent / "pyvenv.cfg")
        trees.update(interpreter.parent.parent / name for name in ("lib", "lib64"))
    entries = {variant for path in entries for variant in _launcher_path_variants(path)}
    trees = {variant for path in trees for variant in _launcher_path_variants(path)}
    try:
        _validate_launcher_write_grants(policy, entries, trees)
        _validate_trusted_hardlink_aliases(policy, entries, trees)
        base_trees = {
            variant
            for path in _auth_base_runtime_trees(executable.interpreter)
            for variant in _launcher_path_variants(path)
        }
        _validate_launcher_write_grants(policy, set(), base_trees)
        _validate_trusted_hardlink_aliases(policy, set(), base_trees)
        # The liveness supervisor also starts Python in the trusted parent.
        _validate_launcher_runtime(policy)
        roots = _isolated_launcher_import_roots(str(executable.interpreter))
        import_trees = {variant for root in roots for variant in _launcher_path_variants(root)}
        _validate_launcher_write_grants(policy, set(), import_trees)
        _validate_trusted_hardlink_aliases(policy, set(), import_trees)
    except OSError as exc:
        raise ValueError(
            "model auth executable and runtime must stay outside sandbox-writable paths"
        ) from exc


def _profile_token_argv(executable: Path, *, host: str, profile: str) -> list[str]:
    """Build the fixed argv for the profile token authentication command."""
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


def _auth_env(config_file: Path | None = None) -> dict[str, str]:
    env = {
        name: value
        for name in ("HOME", "PATH", "LANG", "LC_ALL", "TZ")
        if (value := os.environ.get(name)) is not None
    }
    # requests honors NETRC. Pointing it at the null device prevents implicit
    # ~/.netrc discovery while retaining HOME for Databricks CLI credentials.
    env["NETRC"] = os.devnull
    if config_file is not None:
        env["DATABRICKS_CONFIG_FILE"] = str(config_file)
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


async def mint_profile_token(
    *,
    host: str,
    profile: str,
    _executable: _TrustedExecutable | None = None,
    _config_file: Path | None = None,
) -> str:
    """Mint one opaque credential without accepting a command or a shell."""
    host, profile = _validated_authority(host, profile)
    proc: asyncio.subprocess.Process | None = None
    liveness_read_fd: int | None = None
    liveness_write_fd: int | None = None
    try:
        executable = _executable or _resolve_auth_executable()
        current = executable.path.stat()
        if (current.st_dev, current.st_ino) != executable.identity:
            raise PermissionError("trusted ucode executable changed before launch")
        helper_argv = _profile_token_argv(executable.path, host=host, profile=profile)
        if executable.interpreter is not None:
            target = _validate_auth_executable_path(executable.interpreter)
            if target.identity != executable.interpreter_identity:
                raise PermissionError("trusted authentication interpreter changed before launch")
            helper_argv = [str(executable.interpreter), "-I", *helper_argv]
        pass_fds: tuple[int, ...] = ()
        if os.name == "posix":
            liveness_read_fd, liveness_write_fd = os.pipe()
            os.set_inheritable(liveness_read_fd, True)
            helper_argv = [
                sys.executable,
                "-I",
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
            env=_auth_env(_config_file),
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
            # Deliver TERM synchronously before the first cancellation point.
            _proc.terminate_tree(proc)
            cleanup_task = asyncio.create_task(
                terminate_subprocess(
                    proc,
                    terminate_timeout=2,
                    kill_timeout=1,
                    label="ucode credential helper",
                )
            )
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    continue
            with contextlib.suppress(Exception):
                cleanup_task.result()
            close_subprocess_transport(proc)
        raise
    except (TimeoutError, OSError, ValueError):
        if proc is not None:
            with contextlib.suppress(Exception):
                await terminate_subprocess(
                    proc,
                    terminate_timeout=2,
                    kill_timeout=1,
                    label="ucode credential helper",
                )
            close_subprocess_transport(proc)
        raise ProviderAuthRequired.for_authority(host, profile) from None
    finally:
        for fd in (liveness_read_fd, liveness_write_fd):
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
