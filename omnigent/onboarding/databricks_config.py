"""Helpers for resolving local Databricks profile metadata."""

from __future__ import annotations

import configparser
import contextlib
import importlib.util
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

_logger = logging.getLogger(__name__)

_DATABRICKSCFG_PATH = Path.home() / ".databrickscfg"


def normalize_workspace_url(raw: str) -> str:
    """Reduce a Databricks workspace URL to its bare ``scheme://host`` origin.

    Users routinely paste the URL straight from a browser address bar, which
    carries a path and query the workspace host does not — e.g.
    ``https://my-ws.cloud.databricks.com/browse?o=1234567890``. Both the
    ``~/.databrickscfg`` profile host and ``ucode configure --workspaces``
    need the bare origin: the Databricks CLI keys its OAuth token cache by
    host, so a path-laden value resolves to "no access token" and
    ``ucode configure`` then exits non-zero.

    :param raw: A workspace URL, possibly carrying a path/query/fragment
        and/or a trailing slash, e.g.
        ``"https://my-ws.cloud.databricks.com/browse?o=1"``.
    :returns: ``scheme://host`` with no path, query, fragment, or trailing
        slash (e.g. ``"https://my-ws.cloud.databricks.com"``). When *raw* has
        no parseable scheme+host (e.g. a bare ``"host/path"`` with no scheme),
        the input is returned trimmed of surrounding whitespace and a trailing
        slash — matching the prior ``rstrip("/")`` behavior so callers that
        pre-add a scheme never regress.
    """
    parsed = urlparse(raw.strip())
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return raw.strip().rstrip("/")


# The install command surfaced wherever a Databricks flow is gated on the
# `databricks` extra (the add-provider menu, `setup --internal-beta`).
# Matches the README's canonical `uv tool install` path: install from the
# package index, not git. Dev clones use `uv sync --extra databricks`
# instead, but the tool install is the path end users actually took.
DATABRICKS_EXTRA_INSTALL_HINT = 'uv tool install --force "omnigent[databricks]"'


def databricks_sdk_installed() -> bool:
    """Return whether ``databricks-sdk`` (the ``databricks`` extra) is present.

    The SDK is not part of the default install — it ships in the
    ``databricks`` (and ``all``) extras. The ``kind: databricks`` provider
    path needs it to mint workspace OAuth tokens at runtime
    (:mod:`omnigent.runtime.credentials.databricks`), so onboarding flows
    gate the Databricks option on this check and surface
    :data:`DATABRICKS_EXTRA_INSTALL_HINT` when it fails.

    Uses :func:`importlib.util.find_spec` so the check never pays the cost
    of actually importing the SDK.

    :returns: ``True`` when ``databricks.sdk`` is importable.
    """
    try:
        return importlib.util.find_spec("databricks.sdk") is not None
    except ModuleNotFoundError:
        # find_spec("databricks.sdk") imports the parent `databricks`
        # namespace package first; when even that is absent it raises
        # instead of returning None.
        return False


def list_databricks_profiles() -> list[str]:
    """Return the profile section names declared in ``~/.databrickscfg``.

    Used by ``omnigent setup --no-internal-beta`` to offer the user a pick-list
    when adding a ``kind: databricks`` provider, so they don't have to
    recall the exact profile name.

    :returns: Section names, e.g. ``["oss", "DEFAULT"]``. The ``DEFAULT``
        section is included only when it actually carries keys. Empty when
        the file is missing or unparseable.
    """
    if not _DATABRICKSCFG_PATH.exists():
        return []
    parser = configparser.ConfigParser()
    try:
        parser.read(_DATABRICKSCFG_PATH)
    except configparser.Error as exc:
        # Class-only: configparser errors can embed the offending line, which
        # in a credentials file may be a token.
        _logger.debug("Could not parse %s (%s)", _DATABRICKSCFG_PATH, type(exc).__name__)
        return []
    sections = [s for s in parser.sections() if s != "DEFAULT"]
    if parser.defaults():
        sections.append("DEFAULT")
    return sections


def get_workspace_url_for_profile(profile: str) -> str | None:
    """Return the workspace host for a ``~/.databrickscfg`` profile.

    Reads the INI-style ``~/.databrickscfg`` directly with
    :mod:`configparser`, then falls back to Omnigent' built-in setup
    profile metadata for legacy names.

    :param profile: Profile section name, e.g. ``"<your-profile>"`` or
        ``"DEFAULT"``.
    :returns: The ``host`` value for the profile, stripped of trailing slash,
        or ``None`` when the profile cannot be resolved.
    """
    if _DATABRICKSCFG_PATH.exists():
        cfg = configparser.ConfigParser()
        try:
            cfg.read(_DATABRICKSCFG_PATH)
        except configparser.Error as exc:
            _logger.debug("Could not parse %s: %s", _DATABRICKSCFG_PATH, exc)
        else:
            host = None
            if cfg.has_section(profile):
                try:
                    host = cfg.get(profile, "host")
                except configparser.NoOptionError:
                    host = None
            elif profile.lower() == cfg.default_section.lower():
                host = cfg.defaults().get("host")
            if host:
                return host.rstrip("/")

    try:
        import omnigent.onboarding.internal_beta as internal_beta  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        # The internal-beta catalog is intentionally absent from the OSS
        # build; without it there are no bundled-profile fallbacks.
        return None

    for spec in internal_beta.DEFAULT_PROFILES:
        if spec.name == profile:
            host = spec.host
            if isinstance(host, str):
                return host.rstrip("/")
    return None


# ── OAuth callback port preflight ────────────────────────────────────────────

# Default U2M OAuth callback port of ``databricks auth login``.
_DATABRICKS_OAUTH_CALLBACK_PORT = 8020

# Databricks CLI v0.265.0+ falls back through ports 8021-8040 when 8020 is taken.
_DATABRICKS_CLI_PORT_FALLBACK_VERSION = (0, 265, 0)

_CLI_VERSION_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class _OAuthPortHolder:
    """Process info for the process holding the Databricks OAuth callback port."""

    pid: int | None
    name: str | None


def _oauth_callback_port_busy() -> bool:
    """Return whether the OAuth callback port is already bound on loopback.

    Binds with ``SO_REUSEADDR`` (mirrors Go's ``net.Listen``) so TIME_WAIT
    sockets are not false positives; only ``EADDRINUSE`` counts as busy.
    """
    import errno
    import socket as _socket

    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", _DATABRICKS_OAUTH_CALLBACK_PORT))
        except OSError as exc:
            return exc.errno == errno.EADDRINUSE
    return False


def _oauth_callback_port_holder() -> _OAuthPortHolder:
    """Best-effort lookup of the process listening on the OAuth callback port.

    :returns: Holder info; both fields are ``None`` when it can't be
        determined (e.g. ``psutil.AccessDenied`` without root on macOS).
    """
    try:
        import psutil

        for conn in psutil.net_connections(kind="tcp"):
            if conn.status == "LISTEN" and conn.laddr.port == _DATABRICKS_OAUTH_CALLBACK_PORT:  # type: ignore[union-attr]
                pid = conn.pid
                proc_name: str | None = None
                if pid is not None:
                    with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                        proc_name = psutil.Process(pid).name()
                return _OAuthPortHolder(pid=pid, name=proc_name)
    except Exception:
        pass
    return _OAuthPortHolder(pid=None, name=None)


def _databricks_cli_version(databricks_bin: str) -> tuple[int, int, int] | None:
    """Return the Databricks CLI version, or ``None`` when it can't be read.

    :param databricks_bin: Path to the ``databricks`` binary.
    :returns: ``(major, minor, patch)``; ``None`` on failure, unparseable
        output, or a ``0.0.0`` dev build.
    """
    try:
        result = subprocess.run(
            [databricks_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=_CLI_VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", result.stdout or "")
    if match is None:
        return None
    version = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return None if version == (0, 0, 0) else version


def databricks_login_port_conflict(databricks_bin: str) -> str | None:
    """Return a message when ``databricks auth login`` is known to fail on its port.

    Older CLIs hardcode port 8020 for the OAuth callback and fail when it is
    taken. Newer ones fall back to a free port, and an unknown version is
    never treated as a conflict.

    :param databricks_bin: Path to the ``databricks`` binary.
    :returns: An actionable plain-text message, or ``None`` when login can proceed.
    """
    if not _oauth_callback_port_busy():
        return None
    version = _databricks_cli_version(databricks_bin)
    if version is None or version >= _DATABRICKS_CLI_PORT_FALLBACK_VERSION:
        return None
    port = _DATABRICKS_OAUTH_CALLBACK_PORT
    holder = _oauth_callback_port_holder()
    who = ""
    if holder.pid is not None:
        who = f" by pid {holder.pid} ({holder.name})" if holder.name else f" by pid {holder.pid}"
    return (
        f"Port {port}, which Databricks CLI v{'.'.join(map(str, version))} needs for the "
        f"sign-in callback, is already in use{who}. "
        "Upgrade the Databricks CLI to v0.265.0 or newer, which falls back to a free port "
        "automatically (https://docs.databricks.com/dev-tools/cli/install.html), "
        f"or free port {port} if nothing else needs it "
        f"(`lsof -nP -iTCP:{port} -sTCP:LISTEN`)."
    )
