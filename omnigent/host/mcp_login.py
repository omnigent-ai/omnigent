"""Run a harness's own ``mcp login`` on the host so the web UI can sign in to a server.

The harness keeps the resulting tokens in its own credential store; only the
authorization page address and a lifecycle state leave this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import Literal, TypedDict
from urllib.parse import SplitResult, parse_qs, urlsplit

from omnigent._platform import IS_POSIX, resolve_cli_binary
from omnigent.host.mcp_inventory import ConfiguredMcpServer
from omnigent.inner._proc import remember_process_group, spawn_kwargs
from omnigent.inner._subprocess_lifecycle import terminate_subprocess
from omnigent.runner.identity import strip_runner_auth_secrets

_logger = logging.getLogger(__name__)

LOGIN_HARNESSES = ("claude", "codex")
LOGIN_TIMEOUT_SECONDS = 600.0
URL_WAIT_SECONDS = 30.0
STATUS_TIMEOUT_SECONDS = 5.0
FINISHED_RETENTION_SECONDS = 300.0
MAX_ACTIVE_LOGINS = 4
MAX_CALLBACK_URL = 4096
_MAX_OUTPUT = 64 * 1024
_BINARY_ENV = {"claude": "OMNIGENT_CLAUDE_PATH", "codex": "OMNIGENT_CODEX_PATH"}
_LOOPBACK_NAMES = {"localhost"}
# OSC 8 hyperlinks wrap the URL Claude Code prints; other escapes are cursor movement.
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# A URL only counts once a delimiter follows it, so a partial read is never accepted.
_URL = re.compile(r"https?://[^\s\x00-\x1f\x7f\"'<>]+(?=[\s\x00-\x1f])")

SignInState = Literal["signed_in", "available"]
LoginState = Literal["pending", "succeeded", "failed", "expired", "cancelled"]


class LoginStatus(TypedDict):
    login_id: str
    state: LoginState
    authorization_url: str | None
    error: str | None


class LoginError(Exception):
    """A login request the host refused; ``status`` is the frame status to report."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


def harness_server_name(server: ConfiguredMcpServer) -> str | None:
    """The name the harness CLI uses for *server*, or ``None`` if it can't log in to it."""
    summary = server.summary
    harness = summary["harness"]
    if harness not in LOGIN_HARNESSES or summary["transport"] != "http":
        return None
    name = summary["name"]
    plugin = summary.get("plugin")
    if plugin is not None:
        if harness != "claude":
            return None
        name = f"plugin:{plugin}:{name}"
    if name.startswith("-") or any(not ch.isprintable() for ch in name):
        return None
    return name


def _binary(harness: str) -> str | None:
    return resolve_cli_binary(harness, env_var=_BINARY_ENV[harness])


def _cli_env(harness: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in strip_runner_auth_secrets(os.environ).items()
        if not key.startswith("OMNIGENT_")
    }
    if harness == "codex":
        from omnigent.inner.codex_executor import _codex_home_config_source_from_env

        # Sign in to the same Codex home whose config listed the server.
        env["CODEX_HOME"] = str(_codex_home_config_source_from_env())
    return env


def _login_target(server: ConfiguredMcpServer) -> tuple[str, str] | None:
    if not IS_POSIX:
        return None
    name = harness_server_name(server)
    if name is None:
        return None
    executable = _binary(server.summary["harness"])
    if executable is None:
        return None
    return executable, name


async def _run_status_command(argv: list[str], harness: str) -> tuple[int, str] | None:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=_cli_env(harness),
        cwd=str(Path.home()),
        **spawn_kwargs(),
    )
    remember_process_group(process)
    try:
        async with asyncio.timeout(STATUS_TIMEOUT_SECONDS):
            stdout, _ = await process.communicate()
    except TimeoutError:
        return None
    finally:
        await terminate_subprocess(
            process, label="MCP sign-in status", terminate_timeout=0.5, kill_timeout=0.5
        )
    assert process.returncode is not None
    return process.returncode, stdout.decode(errors="replace")


async def harness_sign_in_state(server: ConfiguredMcpServer) -> SignInState | None:
    """Whether the harness holds a sign-in for *server* (``None``: it can't sign in).

    Only called for servers that answered Omnigent's probe with HTTP 401, so a
    status the CLI can't report still offers a sign-in attempt.
    """
    target = _login_target(server)
    if target is None:
        return None
    executable, name = target
    harness = server.summary["harness"]
    try:
        if harness == "claude":
            ran = await _run_status_command([executable, "mcp", "get", name], harness)
            if ran is None:
                return "available"
            code, output = ran
            if code != 0:
                return None
            status = next(
                (line for line in output.splitlines() if line.strip().startswith("Status:")), ""
            )
            return "signed_in" if "Connected" in status else "available"
        ran = await _run_status_command([executable, "mcp", "list", "--json"], harness)
        if ran is None:
            return "available"
        code, output = ran
        entries = json.loads(output) if code == 0 else None
    except (OSError, ValueError):
        return None
    if not isinstance(entries, list):
        return None
    entry = next((e for e in entries if isinstance(e, dict) and e.get("name") == name), None)
    if entry is None:
        return None
    auth = entry.get("auth_status")
    if auth == "o_auth":
        return "signed_in"
    if auth in {"not_logged_in", "unknown"}:
        return "available"
    return None


def _is_loopback(host: str | None) -> bool:
    if host is None:
        return False
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def authorization_redirect(url: str) -> SplitResult | None:
    """The loopback redirect address inside an authorization URL, if it is acceptable."""
    try:
        parts = urlsplit(url)
        redirect = parse_qs(parts.query).get("redirect_uri", [])
        if (
            not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or not (
                parts.scheme == "https"
                or (parts.scheme == "http" and _is_loopback(parts.hostname))
            )
            or len(redirect) != 1
        ):
            return None
        target = urlsplit(redirect[0])
        if target.scheme != "http" or not _is_loopback(target.hostname) or target.port is None:
            return None
    except ValueError:
        return None
    return target


def find_authorization_url(output: str) -> tuple[str, SplitResult] | None:
    """The first acceptable authorization URL in terminal *output*."""
    text = _CSI.sub("", _OSC.sub("", output))
    for match in _URL.finditer(text):
        redirect = authorization_redirect(match[0])
        if redirect is not None:
            return match[0], redirect
    return None


def valid_callback(callback_url: str, redirect: SplitResult) -> bool:
    """Whether a pasted address is the redirect the harness is waiting for."""
    if len(callback_url) > MAX_CALLBACK_URL or any(
        ch.isspace() or not ch.isprintable() for ch in callback_url
    ):
        return False
    try:
        parts = urlsplit(callback_url)
        query = parse_qs(parts.query)
        return (
            parts.scheme == redirect.scheme
            and parts.netloc == redirect.netloc
            and parts.path == redirect.path
            and ("code" in query or "error" in query)
        )
    except ValueError:
        return False


@dataclass
class _Login:
    login_id: str
    key: tuple[str, str]
    process: asyncio.subprocess.Process
    master_fd: int
    output: bytearray = field(default_factory=bytearray)
    url: str | None = None
    redirect: SplitResult | None = None
    state: LoginState = "pending"
    error: str | None = None
    finished_at: float | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    watcher: asyncio.Task[None] | None = None

    def status(self) -> LoginStatus:
        return {
            "login_id": self.login_id,
            "state": self.state,
            "authorization_url": self.url if self.state == "pending" else None,
            "error": self.error,
        }


class HostMcpLogins:
    """Interactive ``mcp login`` processes the web UI is driving on this host."""

    def __init__(self, on_success: Callable[[], None] | None = None) -> None:
        self._logins: dict[str, _Login] = {}
        self._on_success = on_success

    def _prune(self) -> None:
        cutoff = time.monotonic() - FINISHED_RETENTION_SECONDS
        for login_id, login in list(self._logins.items()):
            if login.finished_at is not None and login.finished_at < cutoff:
                del self._logins[login_id]

    def _get(self, login_id: str) -> _Login:
        self._prune()
        login = self._logins.get(login_id)
        if login is None:
            raise LoginError("not_found", "Sign-in not found")
        return login

    async def start(self, server: ConfiguredMcpServer) -> LoginStatus:
        """Start signing in to *server* and return once its authorization URL is known."""
        target = _login_target(server)
        if target is None:
            raise LoginError("invalid", "This harness can't sign in to this MCP server")
        executable, name = target
        key = (server.summary["harness"], name)
        self._prune()
        for earlier in [login for login in self._logins.values() if login.key == key]:
            await self._finish(earlier, "cancelled")
        if sum(login.state == "pending" for login in self._logins.values()) >= MAX_ACTIVE_LOGINS:
            raise LoginError("busy", "Too many sign-ins in progress")
        login = await self._spawn(executable, name, key)
        self._logins[login.login_id] = login
        try:
            async with asyncio.timeout(URL_WAIT_SECONDS):
                while login.url is None and login.state == "pending":
                    login.changed.clear()
                    await login.changed.wait()
        except TimeoutError:
            await self._finish(login, "failed", "The harness didn't offer a sign-in page")
        return login.status()

    async def _spawn(self, executable: str, name: str, key: tuple[str, str]) -> _Login:
        import fcntl
        import pty
        import struct
        import termios

        master_fd, slave_fd = pty.openpty()
        try:
            # Wide enough that the CLI never wraps the authorization URL.
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 1000, 0, 0))
            process = await asyncio.create_subprocess_exec(
                executable,
                "mcp",
                "login",
                name,
                "--no-browser",
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=_cli_env(key[0]),
                cwd=str(Path.home()),
                **spawn_kwargs(),
            )
        except BaseException:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)
        remember_process_group(process)
        os.set_blocking(master_fd, False)
        login = _Login(secrets.token_urlsafe(16), key, process, master_fd)
        asyncio.get_running_loop().add_reader(master_fd, self._read, login)
        login.watcher = asyncio.create_task(self._watch(login))
        return login

    def _read(self, login: _Login) -> None:
        try:
            chunk = os.read(login.master_fd, 4096)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if not chunk:
            asyncio.get_running_loop().remove_reader(login.master_fd)
            return
        if len(login.output) < _MAX_OUTPUT:
            login.output += chunk[: _MAX_OUTPUT - len(login.output)]
        if login.url is None:
            found = find_authorization_url(login.output.decode(errors="replace"))
            if found is not None:
                login.url, login.redirect = found
                login.changed.set()

    async def _watch(self, login: _Login) -> None:
        try:
            async with asyncio.timeout(LOGIN_TIMEOUT_SECONDS):
                code = await login.process.wait()
        except TimeoutError:
            await self._finish(login, "expired", "Sign-in timed out")
            return
        if login.state != "pending":
            return
        if code == 0:
            await self._finish(login, "succeeded")
            if self._on_success is not None:
                self._on_success()
        else:
            _logger.info("MCP sign-in for %s exited with status %s", login.key[0], code)
            await self._finish(login, "failed", "The harness couldn't finish signing in")

    async def _finish(self, login: _Login, state: LoginState, error: str | None = None) -> None:
        if login.state == "pending":
            login.state = state
            login.error = error
        login.finished_at = login.finished_at or time.monotonic()
        login.changed.set()
        if login.watcher is not None and login.watcher is not asyncio.current_task():
            login.watcher.cancel()
        await terminate_subprocess(
            login.process, label="MCP sign-in", terminate_timeout=1.0, kill_timeout=1.0
        )
        if login.master_fd >= 0:
            asyncio.get_running_loop().remove_reader(login.master_fd)
            os.close(login.master_fd)
            login.master_fd = -1

    def pids(self) -> set[int]:
        """Login processes whose exit status belongs to this manager."""
        return {
            login.process.pid
            for login in self._logins.values()
            if login.process.returncode is None
        }

    def status(self, login_id: str) -> LoginStatus:
        return self._get(login_id).status()

    async def submit(self, login_id: str, callback_url: str) -> LoginStatus:
        """Hand a pasted redirect address to the waiting harness CLI."""
        login = self._get(login_id)
        if login.state != "pending" or login.redirect is None or login.master_fd < 0:
            raise LoginError("invalid", "This sign-in is no longer waiting")
        if not valid_callback(callback_url, login.redirect):
            raise LoginError("invalid", "That isn't the address the sign-in page redirected to")
        with contextlib.suppress(OSError):
            os.write(login.master_fd, callback_url.encode() + b"\r")
        return login.status()

    async def cancel(self, login_id: str) -> LoginStatus:
        login = self._get(login_id)
        await self._finish(login, "cancelled")
        return login.status()

    async def close(self) -> None:
        """Stop every login; the web UI that drove them is gone."""
        for login in list(self._logins.values()):
            await self._finish(login, "cancelled")
        self._logins.clear()
