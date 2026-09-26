"""Process manager for a per-conversation ``opencode serve`` server.

Mirrors :mod:`omnigent.harnesses.codex_native.app_server` but for OpenCode's HTTP +
SSE transport. The runner owns this server (and the SSE forwarder); the
harness-side executor injects web turns over REST using the loopback URL
and auth secret published in the bridge state.

Responsibilities:

- Resolve and version-check the ``opencode`` CLI.
- Allocate a loopback port and per-session XDG data/config roots.
- Launch ``opencode serve --hostname 127.0.0.1 --port <port>`` with a
  random ``OPENCODE_PASSWORD`` and the per-session XDG dirs.
- Poll the HTTP API for readiness.
- Expose ``base_url``, ``auth_headers``, ``xdg_data_home`` /
  ``xdg_config_home``, and a process handle.
- Build the ``opencode --server <url> --session <id>`` argv + env for the
  terminal TUI (the Codex ``--remote`` analog).
- Terminate the process on session close / runner shutdown.

Security posture: bind to ``127.0.0.1`` only, random per-session password,
per-session XDG dirs (never the user's global OpenCode state). The server
is runner-internal — the web UI attaches to Omnigent terminal resources,
never to the OpenCode HTTP port.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import socket
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
from packaging.version import InvalidVersion, Version

from omnigent.harnesses.opencode_native.bridge import (
    OPENCODE_DB_ENV_VAR,
    OPENCODE_PASSWORD_ENV_VAR,
    OPENCODE_SERVER_PASSWORD_ENV_VAR,
    auth_headers_for_secret,
    ensure_auth_secret,
    opencode_db_path_for_bridge_dir,
    xdg_config_home_for_bridge_dir,
    xdg_data_home_for_bridge_dir,
)
from omnigent.harnesses.opencode_native.client import (
    OPENCODE_MAX_VERSION_EXCLUSIVE,
    OPENCODE_MIN_VERSION,
    OpenCodeClient,
)

_logger = logging.getLogger(__name__)

# Env vars the OpenCode server inherits from the parent that are safe and
# useful (provider creds + proxy). Everything else is filtered out so the
# server runs against a clean, per-session environment.
_ENV_PASSTHROUGH_PREFIXES = (
    "OPENAI_",
    "ANTHROPIC_",
    "OPENCODE_",
    "DATABRICKS_",
    "GEMINI_",
    "GOOGLE_",
    "HTTP_",
    "HTTPS_",
    "NO_PROXY",
    "ALL_PROXY",
)
_ENV_PASSTHROUGH_KEYS = (
    "PATH",
    "HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "no_proxy",
    "http_proxy",
    "https_proxy",
)
_RUNNER_ENV_PASSTHROUGH_ENV_VAR = "OMNIGENT_RUNNER_ENV_PASSTHROUGH"
# OpenCode env the parent must never leak into the isolated per-session server
# (global config, a foreign SQLite store, another server's password). Dropped
# despite matching the ``OPENCODE_`` passthrough prefix; the launcher sets its own below.
_ENV_OPENCODE_DENYLIST = frozenset(
    {
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_CONTENT",
        "OPENCODE_CONFIG_DIR",
        "OPENCODE_DB",
        "OPENCODE_PASSWORD",
        "OPENCODE_SERVER_PASSWORD",
    }
)
# How long a ``--stdio`` server gets to exit after its stdin closes.
_STDIN_CLOSE_GRACE_S = 3.0

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+(?:[-.][0-9A-Za-z]+)*)")

# Escape hatch: set truthy to bypass the OpenCode CLI version gate (e.g. to
# try an as-yet-unvalidated 3.x release). Mirrors OMNIGENT_NO_UPDATE_CHECK.
_SKIP_VERSION_CHECK_ENV = "OMNIGENT_OPENCODE_SKIP_VERSION_CHECK"


class OpenCodeVersionError(RuntimeError):
    """Raised when the installed ``opencode`` CLI is an unsupported version."""


class OpenCodeCliNotFoundError(RuntimeError):
    """Raised when no ``opencode`` executable can be resolved on ``PATH``."""


def find_opencode_cli(opencode_path: str | None = None) -> str:
    """
    Resolve the ``opencode`` executable.

    :param opencode_path: Explicit path override; ``None`` searches ``PATH``.
    :returns: Absolute path to the ``opencode`` binary.
    :raises OpenCodeCliNotFoundError: When no binary can be resolved.
    """
    if opencode_path:
        if os.path.isabs(opencode_path) and os.access(opencode_path, os.X_OK):
            return opencode_path
        resolved = shutil.which(opencode_path)
        if resolved:
            return resolved
        raise OpenCodeCliNotFoundError(f"opencode executable not found: {opencode_path!r}")
    resolved = shutil.which("opencode")
    if not resolved:
        raise OpenCodeCliNotFoundError(
            "opencode CLI not found on PATH; install the '@opencode/cli' npm package"
        )
    return resolved


def parse_opencode_version(text: str) -> str | None:
    """
    Extract a semver string from ``opencode --version`` output.

    :param text: Raw CLI output, e.g. ``"opencode v2.0.18"`` or ``"2.0.18"``.
    :returns: The parsed version, e.g. ``"2.0.18"``, or ``None``.
    """
    match = _VERSION_RE.search(text or "")
    return match.group(1) if match else None


def check_opencode_version(
    version: str,
    *,
    minimum: str = OPENCODE_MIN_VERSION,
    maximum_exclusive: str = OPENCODE_MAX_VERSION_EXCLUSIVE,
) -> None:
    """
    Validate an OpenCode version against the supported range.

    :param version: Version string, e.g. ``"1.17.7"``.
    :param minimum: Inclusive lower bound.
    :param maximum_exclusive: Exclusive upper bound.
    :raises OpenCodeVersionError: When *version* is unparsable or outside
        ``[minimum, maximum_exclusive)``.
    """
    try:
        parsed = Version(version)
        low = Version(minimum)
        high = Version(maximum_exclusive)
    except InvalidVersion as exc:
        raise OpenCodeVersionError(f"Unparsable OpenCode version {version!r}: {exc}") from exc
    if parsed < low or parsed >= high:
        raise OpenCodeVersionError(
            f"Unsupported OpenCode version {version}: requires >={minimum},<{maximum_exclusive}. "
            "Install a pinned '@opencode/cli' release."
        )


def resolve_opencode_version(opencode_path: str) -> str:
    """
    Run ``opencode --version`` and return the parsed version.

    :param opencode_path: Path to the ``opencode`` binary.
    :returns: Parsed version string, e.g. ``"1.17.7"``.
    :raises OpenCodeVersionError: When the version cannot be determined.
    """
    try:
        completed = subprocess.run(
            [opencode_path, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OpenCodeVersionError(f"Could not run 'opencode --version': {exc}") from exc
    output = f"{completed.stdout}\n{completed.stderr}"
    version = parse_opencode_version(output)
    if version is None:
        raise OpenCodeVersionError(f"Could not parse OpenCode version from: {output!r}")
    return version


def allocate_loopback_port() -> int:
    """
    Allocate an ephemeral loopback TCP port.

    :returns: A free port number on ``127.0.0.1``.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_opencode_serve_args(
    *,
    hostname: str,
    port: int,
    opencode_args: Sequence[str] = (),
) -> list[str]:
    """
    Build the ``opencode serve`` argv tail (after the executable).

    Always passes explicit ``--hostname``/``--port``. ``--stdio`` ties the
    server's lifetime to its stdin: it exits when the launcher closes the pipe,
    so a crashed runner never orphans it.

    :param hostname: Bind hostname, e.g. ``"127.0.0.1"``.
    :param port: Bind port.
    :param opencode_args: Extra pass-through args.
    :returns: Argv tail, e.g. ``["serve", "--hostname", "127.0.0.1",
        "--port", "49231", "--stdio"]``.
    """
    return ["serve", "--hostname", hostname, "--port", str(port), "--stdio", *opencode_args]


def build_tui_command(
    opencode_path: str,
    *,
    base_url: str,
    session_id: str,
    workspace: str,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """
    Build the full argv for the OpenCode TUI bound to this session's server.

    The TUI connects to the runner-owned ``opencode serve`` (``--server``) and
    opens the Omnigent-owned session, so the terminal, forwarder, and web UI
    drive one OpenCode session. The password travels in the environment (see
    :func:`opencode_terminal_env`), never on argv.

    :param opencode_path: Path to the ``opencode`` binary.
    :param base_url: Server URL, e.g. ``"http://127.0.0.1:49231"``.
    :param session_id: OpenCode session id, e.g. ``"ses_abc123"``.
    :param workspace: Directory the TUI starts in (positional argument).
    :param extra_args: User pass-through args appended last.
    :returns: ``[opencode, "--server", url, "--session", id, workspace, *extra]``.
    """
    return [
        opencode_path,
        "--server",
        base_url,
        "--session",
        session_id,
        workspace,
        *extra_args,
    ]


def filtered_server_env(
    *,
    bridge_dir: Path,
    auth_secret: str,
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """
    Build the launch environment for ``opencode serve``.

    Per-session XDG dirs isolate OpenCode's state from the user's global
    config; ``OPENCODE_PASSWORD`` (plus the legacy ``OPENCODE_SERVER_PASSWORD``)
    secures the loopback server. Only provider/proxy env and operator-declared
    runner passthrough vars from the parent are passed through.

    :param bridge_dir: Native OpenCode bridge directory.
    :param auth_secret: Server password for basic auth.
    :param extra_env: Additional provider env (e.g. from Omnigent setup).
    :returns: The environment mapping for the server subprocess.
    """
    extra_names = {
        name.strip()
        for name in os.environ.get(_RUNNER_ENV_PASSTHROUGH_ENV_VAR, "").split(",")
        if name.strip()
    }
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _ENV_OPENCODE_DENYLIST:
            # Never inherit the parent's OpenCode config, DB, or password.
            continue
        if (
            key in _ENV_PASSTHROUGH_KEYS
            or key.startswith(_ENV_PASSTHROUGH_PREFIXES)
            or key in extra_names
        ):
            env[key] = value
    env.update(extra_env or {})
    env["XDG_DATA_HOME"] = str(xdg_data_home_for_bridge_dir(bridge_dir))
    env["XDG_CONFIG_HOME"] = str(xdg_config_home_for_bridge_dir(bridge_dir))
    env[OPENCODE_DB_ENV_VAR] = str(opencode_db_path_for_bridge_dir(bridge_dir))
    env[OPENCODE_PASSWORD_ENV_VAR] = auth_secret
    env[OPENCODE_SERVER_PASSWORD_ENV_VAR] = auth_secret
    return env


def opencode_terminal_env(
    secret: str,
    *,
    xdg_data_home: Path | None = None,
    xdg_config_home: Path | None = None,
) -> dict[str, str]:
    """
    Build the environment for the OpenCode TUI terminal process.

    :param secret: The per-session server password.
    :param xdg_data_home: Per-session ``XDG_DATA_HOME`` so TUI-local state stays
        out of the user's global OpenCode data dir; ``None`` leaves it unset.
    :param xdg_config_home: Per-session ``XDG_CONFIG_HOME``; ``None`` leaves it
        unset.
    :returns: Env carrying the password as ``OPENCODE_PASSWORD`` and the legacy
        ``OPENCODE_SERVER_PASSWORD``.
    """
    env = {
        OPENCODE_PASSWORD_ENV_VAR: secret,
        OPENCODE_SERVER_PASSWORD_ENV_VAR: secret,
    }
    if xdg_data_home is not None:
        env["XDG_DATA_HOME"] = str(xdg_data_home)
    if xdg_config_home is not None:
        env["XDG_CONFIG_HOME"] = str(xdg_config_home)
    return env


class OpenCodeNativeServer:
    """
    A managed ``opencode serve`` subprocess bound to one conversation.

    :param bridge_dir: Native OpenCode bridge directory.
    :param workspace: Working directory for the server.
    :param opencode_path: Path to the ``opencode`` binary; ``None``
        searches ``PATH``.
    :param hostname: Bind hostname (always loopback).
    :param port: Explicit port; ``None`` allocates an ephemeral one.
    :param extra_env: Provider env merged into the launch environment.
    :param opencode_args: Extra ``serve`` pass-through args.
    :param verify_version: Whether to version-check the CLI on start.
    :param user_data_store: Reserved for the session-import server, which runs
        against the user's real OpenCode data store; unused by per-session
        servers.
    """

    def __init__(
        self,
        *,
        bridge_dir: Path,
        workspace: Path,
        opencode_path: str | None = None,
        hostname: str = "127.0.0.1",
        port: int | None = None,
        extra_env: Mapping[str, str] | None = None,
        opencode_args: Sequence[str] = (),
        verify_version: bool = True,
        user_data_store: bool = False,
    ) -> None:
        self.bridge_dir = bridge_dir
        self.workspace = workspace
        self.hostname = hostname
        self._explicit_port = port
        self._extra_env = dict(extra_env or {})
        self._opencode_args = tuple(opencode_args)
        self._verify_version = verify_version
        # Reserved for the import server; per-session servers always isolate.
        self.user_data_store = user_data_store
        self.opencode_path = find_opencode_cli(opencode_path)
        self.auth_secret = ensure_auth_secret(bridge_dir)
        self.xdg_data_home = xdg_data_home_for_bridge_dir(bridge_dir)
        self.xdg_config_home = xdg_config_home_for_bridge_dir(bridge_dir)
        self.port: int | None = port
        self.process: subprocess.Popen[bytes] | None = None
        self.version: str | None = None

    @property
    def base_url(self) -> str:
        """:returns: The server base URL once a port is bound."""
        if self.port is None:
            raise RuntimeError("OpenCode server has no port yet; call start() first")
        return f"http://{self.hostname}:{self.port}"

    @property
    def auth_headers(self) -> dict[str, str]:
        """:returns: Basic-auth headers for the server."""
        return auth_headers_for_secret(self.auth_secret)

    @property
    def env(self) -> dict[str, str]:
        """:returns: The launch environment for the server process."""
        return filtered_server_env(
            bridge_dir=self.bridge_dir,
            auth_secret=self.auth_secret,
            extra_env=self._extra_env,
        )

    def build_argv(self) -> list[str]:
        """
        Build the full server argv for the resolved port.

        :returns: ``[opencode, serve, --hostname, ..., --port, ...]``.
        :raises RuntimeError: When no port has been allocated.
        """
        if self.port is None:
            raise RuntimeError("OpenCode server port not allocated")
        return [
            self.opencode_path,
            *build_opencode_serve_args(
                hostname=self.hostname,
                port=self.port,
                opencode_args=self._opencode_args,
            ),
        ]

    async def start(self) -> None:
        """
        Launch the server subprocess and wait until it is ready.

        :raises OpenCodeVersionError: When the CLI version is unsupported.
        :raises RuntimeError: When the server does not become ready.
        """
        if self._verify_version:
            self.version = resolve_opencode_version(self.opencode_path)
            if os.environ.get(_SKIP_VERSION_CHECK_ENV):
                _logger.warning(
                    "%s set; skipping OpenCode version gate (got %s, supported >=%s,<%s)",
                    _SKIP_VERSION_CHECK_ENV,
                    self.version,
                    OPENCODE_MIN_VERSION,
                    OPENCODE_MAX_VERSION_EXCLUSIVE,
                )
            else:
                check_opencode_version(self.version)
        if self.port is None:
            self.port = self._explicit_port or allocate_loopback_port()
        argv = self.build_argv()
        _logger.info(
            "Launching opencode serve: port=%s workspace=%s xdg_data=%s",
            self.port,
            self.workspace,
            self.xdg_data_home,
        )
        self.process = subprocess.Popen(
            argv,
            cwd=str(self.workspace),
            env=self.env,
            # ``--stdio`` serves until stdin closes; keep the pipe open.
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            await self._wait_until_ready()
        except BaseException:
            # No caller owns the child until startup succeeds.
            await self.close()
            raise

    async def _wait_until_ready(self, *, attempts: int = 60, delay: float = 0.5) -> None:
        """
        Poll ``GET /api/info`` until the server reports ready.

        The server answers 503 while booting and 200 once its routes are live;
        the ready body's ``version`` is recorded on :attr:`version`.

        :param attempts: Maximum readiness polls.
        :param delay: Seconds between polls.
        :raises RuntimeError: When the server rejects the password, exits early,
            or never becomes ready.
        """
        last_error = "no response"
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self.auth_headers,
            timeout=httpx.Timeout(5.0, connect=2.0),
        ) as client:
            for _ in range(attempts):
                if self.process is not None and self.process.poll() is not None:
                    raise RuntimeError(
                        f"opencode serve exited early with code {self.process.returncode}"
                    )
                try:
                    response = await client.get("/api/info")
                except httpx.HTTPError as exc:
                    last_error = repr(exc)
                else:
                    if response.status_code == 200:
                        self._record_info(response)
                        return
                    if response.status_code == 401:
                        raise RuntimeError("opencode serve rejected the per-session password")
                    last_error = f"HTTP {response.status_code}"
                await asyncio.sleep(delay)
        raise RuntimeError(f"opencode serve did not become ready: {last_error}")

    def _record_info(self, response: httpx.Response) -> None:
        """
        Record the server-reported version from a ready ``/api/info`` response.

        :param response: The 200 response; its body is the bare ``ServerInfo``
            object ``{version, pid, urls, paths}``.
        """
        try:
            body = response.json()
        except ValueError:
            _logger.debug("opencode serve /api/info body was not JSON")
            return
        version = body.get("version") if isinstance(body, dict) else None
        if isinstance(version, str) and version:
            self.version = version
        else:
            _logger.debug("opencode serve /api/info body had no version: %r", body)

    def client(self, *, directory: str | None = None) -> OpenCodeClient:
        """
        Build an :class:`OpenCodeClient` bound to this server.

        :param directory: Optional workspace directory routing header.
        :returns: A new client (caller owns closing it).
        """
        return OpenCodeClient(
            self.base_url,
            headers=self.auth_headers,
            directory=directory or str(self.workspace),
        )

    async def close(self) -> None:
        """Stop the server: close stdin (graceful ``--stdio`` exit), then escalate."""
        process = self.process
        if process is None:
            return
        if process.stdin is not None:
            with contextlib.suppress(OSError):
                process.stdin.close()
        if process.poll() is None:
            try:
                await asyncio.to_thread(process.wait, _STDIN_CLOSE_GRACE_S)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, 10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait)
        self.process = None


def client_for_state(
    *,
    base_url: str,
    auth_secret: str | None,
    directory: str | None = None,
) -> OpenCodeClient:
    """
    Build an :class:`OpenCodeClient` from persisted bridge state.

    Used by the harness-side executor, which never owns the server process
    — it only has the URL + auth secret from bridge state.

    :param base_url: Server base URL.
    :param auth_secret: Server password, or ``None``.
    :param directory: Optional workspace routing header.
    :returns: A new client.
    """
    return OpenCodeClient(
        base_url,
        headers=auth_headers_for_secret(auth_secret),
        directory=directory,
    )
