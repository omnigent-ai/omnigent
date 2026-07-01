"""Process manager for a per-conversation ``opencode serve`` server.

Mirrors :mod:`omnigent.codex_native_app_server` but for OpenCode's HTTP +
SSE transport. The runner owns this server (and the SSE forwarder); the
harness-side executor injects web turns over REST using the loopback URL
and auth secret published in the bridge state.

Responsibilities:

- Resolve and version-check the ``opencode`` CLI.
- Allocate a loopback port and per-session XDG data/config roots.
- Launch ``opencode serve --hostname 127.0.0.1 --port <port>`` with a
  random ``OPENCODE_SERVER_PASSWORD`` and the per-session XDG dirs.
- Poll the HTTP API for readiness.
- Expose ``base_url``, ``auth_headers``, ``xdg_data_home`` /
  ``xdg_config_home``, and a process handle.
- Build the ``opencode attach`` argv + env for the terminal takeover (the
  Codex ``--remote`` analog).
- Terminate the process on session close / runner shutdown.

Security posture: bind to ``127.0.0.1`` only, random per-session password,
per-session XDG dirs (never the user's global OpenCode state). The server
is runner-internal — the web UI attaches to Omnigent terminal resources,
never to the OpenCode HTTP port.
"""

from __future__ import annotations

import asyncio
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

from omnigent.opencode_native_bridge import (
    OPENCODE_DEFAULT_USERNAME,
    OPENCODE_SERVER_PASSWORD_ENV_VAR,
    OPENCODE_SERVER_USERNAME_ENV_VAR,
    auth_headers_for_secret,
    ensure_auth_secret,
    xdg_config_home_for_bridge_dir,
    xdg_data_home_for_bridge_dir,
)
from omnigent.opencode_native_client import (
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
# OpenCode env vars that point the server at the user's GLOBAL config — they
# would defeat the per-session XDG isolation by re-introducing whatever
# config/model/permission settings the parent shell has set. Dropped from
# the passthrough even though they match the ``OPENCODE_`` prefix, so an
# isolated session never inherits unrelated global OpenCode config.
_ENV_OPENCODE_CONFIG_DENYLIST = frozenset(
    {
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_CONTENT",
    }
)

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+(?:[-.][0-9A-Za-z]+)*)")

# Escape hatch: set truthy to bypass the OpenCode CLI version gate (e.g. to
# try an as-yet-unvalidated 1.18+/v2 release). Mirrors OMNIGENT_NO_UPDATE_CHECK.
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
            "opencode CLI not found on PATH; install the 'opencode-ai' npm package"
        )
    return resolved


def parse_opencode_version(text: str) -> str | None:
    """
    Extract a semver string from ``opencode --version`` output.

    :param text: Raw CLI output, e.g. ``"opencode 1.17.7"`` or ``"1.17.7"``.
    :returns: The parsed version, e.g. ``"1.17.7"``, or ``None``.
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
            "Install a pinned 'opencode-ai' release."
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

    Always passes explicit ``--hostname``/``--port`` so config can't
    override them (the source default port is ``0``).

    :param hostname: Bind hostname, e.g. ``"127.0.0.1"``.
    :param port: Bind port.
    :param opencode_args: Extra pass-through args.
    :returns: Argv tail, e.g. ``["serve", "--hostname", "127.0.0.1",
        "--port", "49231"]``.
    """
    return ["serve", "--hostname", hostname, "--port", str(port), *opencode_args]


def build_opencode_attach_args(
    *,
    server_url: str,
    workspace: str,
    session_id: str | None,
    opencode_args: Sequence[str] = (),
) -> list[str]:
    """
    Build the ``opencode attach`` argv for a terminal takeover.

    Mirrors codex's ``--remote`` attach: the TUI attaches to the
    already-running server so the terminal, forwarder, and web-UI bridge
    all drive the same OpenCode session.

    :param server_url: The server URL, e.g. ``"http://127.0.0.1:49231"``.
    :param workspace: Directory the TUI runs in (``--dir``).
    :param session_id: OpenCode session id to attach (``--session``), or
        ``None`` to let the TUI choose.
    :param opencode_args: Extra pass-through args appended last.
    :returns: Argv tail after the executable.
    """
    args = ["attach", server_url, "--dir", workspace]
    if session_id:
        args.extend(["--session", session_id])
    args.extend(opencode_args)
    return args


def filtered_server_env(
    *,
    bridge_dir: Path,
    auth_secret: str,
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """
    Build the launch environment for ``opencode serve``.

    Per-session XDG dirs isolate OpenCode's state from the user's global
    config; ``OPENCODE_SERVER_PASSWORD`` secures the loopback server. Only
    provider/proxy env from the parent is passed through.

    :param bridge_dir: Native OpenCode bridge directory.
    :param auth_secret: Server password for basic auth.
    :param extra_env: Additional provider env (e.g. from Omnigent setup).
    :returns: The environment mapping for the server subprocess.
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _ENV_OPENCODE_CONFIG_DENYLIST:
            # Never inherit the parent's global OpenCode config — the
            # per-session XDG dirs are the only config source.
            continue
        if key in _ENV_PASSTHROUGH_KEYS or key.startswith(_ENV_PASSTHROUGH_PREFIXES):
            env[key] = value
    env.update(extra_env or {})
    env["XDG_DATA_HOME"] = str(xdg_data_home_for_bridge_dir(bridge_dir))
    env["XDG_CONFIG_HOME"] = str(xdg_config_home_for_bridge_dir(bridge_dir))
    env[OPENCODE_SERVER_PASSWORD_ENV_VAR] = auth_secret
    env[OPENCODE_SERVER_USERNAME_ENV_VAR] = OPENCODE_DEFAULT_USERNAME
    return env


def opencode_terminal_env(server: OpenCodeNativeServer) -> dict[str, str]:
    """
    Build terminal-process env for the native OpenCode TUI (``attach``).

    Keeping the password in the environment avoids leaking it on argv
    (``--password`` defaults to ``OPENCODE_SERVER_PASSWORD``).

    :param server: The running server wrapper.
    :returns: Environment variables for the attach terminal process.
    """
    return {
        OPENCODE_SERVER_PASSWORD_ENV_VAR: server.auth_secret,
        OPENCODE_SERVER_USERNAME_ENV_VAR: OPENCODE_DEFAULT_USERNAME,
        "XDG_DATA_HOME": str(server.xdg_data_home),
        "XDG_CONFIG_HOME": str(server.xdg_config_home),
    }


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
    ) -> None:
        self.bridge_dir = bridge_dir
        self.workspace = workspace
        self.hostname = hostname
        self._explicit_port = port
        self._extra_env = dict(extra_env or {})
        self._opencode_args = tuple(opencode_args)
        self._verify_version = verify_version
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
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await self._wait_until_ready()

    async def _wait_until_ready(self, *, attempts: int = 60, delay: float = 0.5) -> None:
        """
        Poll the HTTP API until the server answers or attempts run out.

        :param attempts: Maximum readiness polls.
        :param delay: Seconds between polls.
        :raises RuntimeError: When the server never becomes ready (or the
            process died early).
        """
        last_error: Exception | None = None
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
                    response = await client.get("/session")
                    if response.status_code < 500:
                        return
                except httpx.HTTPError as exc:
                    last_error = exc
                await asyncio.sleep(delay)
        raise RuntimeError(f"opencode serve did not become ready: {last_error!r}")

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

    async def close(self) -> None:  # pragma: no cover
        """Terminate the server subprocess if running."""
        process = self.process
        if process is None:
            return
        if process.poll() is None:
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
