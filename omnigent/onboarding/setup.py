"""Databricks profile discovery and OAuth login helpers for Omnigent."""

from __future__ import annotations

import configparser
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from rich.console import Console

CONFLICTING_ENV_VARS: tuple[str, ...] = (
    "DATABRICKS_HOST",
    "DATABRICKS_TOKEN",
    "DATABRICKS_CONFIG_PROFILE",
    "DATABRICKS_AUTH_TYPE",
    "DATABRICKS_CLIENT_ID",
    "DATABRICKS_CLIENT_SECRET",
    "DATABRICKS_USERNAME",
    "DATABRICKS_PASSWORD",
)


@dataclass(frozen=True)
class ProfileSpec:
    """One Databricks profile to authenticate.

    :param name: Profile name written to ``~/.databrickscfg``,
        e.g. ``"oss"``.
    :param host: Workspace URL the profile points at.
    :param purpose: One-line description shown during onboarding.
    """

    name: str
    host: str
    purpose: str


# ── Env-var hygiene ────────────────────────────────────


def detect_conflicting_env_vars() -> list[str]:
    """
    Return the names of set ``DATABRICKS_*`` env vars (catalog order).

    These shadow profile lookup in the SDK (env beats profile beats
    defaults). Empty-string values are treated as unset.

    :returns: e.g. ``["DATABRICKS_TOKEN"]``.
    """
    return [var for var in CONFLICTING_ENV_VARS if os.environ.get(var)]


def find_databricks_cli() -> str | None:
    """Locate the ``databricks`` binary on ``$PATH``.

    :returns: Absolute path, or ``None`` if not found.
    """
    return shutil.which("databricks")


def _existing_profile_hosts() -> dict[str, str]:
    """
    Return ``{profile_name: host}`` from ``~/.databrickscfg``.

    Shells out to ``databricks auth profiles --output json``. Returns
    ``{}`` on any failure so callers can treat "no CLI" / "no config" /
    "bad config" identically.

    :returns: Mapping of profile name to workspace host.
    """
    cli = find_databricks_cli()
    if cli is None:
        return {}
    try:
        result = subprocess.run(
            [cli, "auth", "profiles", "--output", "json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    profiles = payload.get("profiles") if isinstance(payload, dict) else None
    if not isinstance(profiles, list):
        return {}
    out: dict[str, str] = {}
    for entry in profiles:
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("name"), str)
            and isinstance(entry.get("host"), str)
        ):
            out[entry["name"]] = entry["host"]
    return out


def _host_matches(a: str, b: str) -> bool:
    """Compare two workspace hosts ignoring a trailing slash."""
    return a.rstrip("/") == b.rstrip("/")


def _databrickscfg_path() -> Path:
    """Return the active Databricks config file path.

    Honors ``DATABRICKS_CONFIG_FILE`` so Databricks CLI profile operations
    and direct configparser writes target the same file.
    """
    env = os.environ.get("DATABRICKS_CONFIG_FILE")
    return Path(env).expanduser() if env else Path.home() / ".databrickscfg"


def _remove_profile_section(name: str) -> bool:
    """
    Remove ``[name]`` from ``~/.databrickscfg``. Atomic write.

    Necessary before ``databricks auth login --host X --profile name``
    when ``name`` already exists with a different host — the CLI
    refuses the host change otherwise.

    :param name: Profile section to drop, e.g. ``"oss"``.
    :returns: ``True`` if the section existed and was removed.
    """
    path = _databrickscfg_path()
    if not path.exists():
        return False
    cfg = configparser.ConfigParser()
    cfg.read(path)
    if name not in cfg:
        return False
    cfg.remove_section(name)
    tmp = path.with_name(path.name + ".write")
    with tmp.open("w") as f:
        cfg.write(f)
    tmp.replace(path)
    return True


# ── OAuth login ───────────────────────────────────────


_OAUTH_BETWEEN_LOGIN_SLEEP_SECONDS = 1.0


def _login_profile(cli: str, spec: ProfileSpec, console: Console) -> bool:
    """Run ``databricks auth login`` for a single profile.

    Sleeps briefly after each successful login so the IdP's
    session-scoped OAuth state drains before the next call's listener
    opens on the same ``localhost:8020`` redirect URI — without this
    pause, Okta SSO can deliver a previous flow's callback to the
    current listener and trip ``state mismatch in 3-legged-OAuth flow``.

    :param cli: Absolute path to the ``databricks`` binary.
    :param spec: Profile to set up.
    :param console: Rich console for surrounding messages.
    :returns: ``True`` on success.
    """
    console.print(f"\n  [bold]→ {spec.name}[/bold]  [dim]{spec.purpose}[/dim]")
    console.print(f"    [dim]host: {spec.host}[/dim]")
    try:
        result = subprocess.run(
            [cli, "auth", "login", "--host", spec.host, "--profile", spec.name],
            check=False,
        )
    except KeyboardInterrupt:
        console.print(f"  [yellow]cancelled {spec.name}[/yellow]")
        return False
    if result.returncode != 0:
        console.print(f"  [red]failed (exit {result.returncode})[/red]")
        return False
    console.print(f"  [green]✓ {spec.name}[/green]")
    time.sleep(_OAUTH_BETWEEN_LOGIN_SLEEP_SECONDS)
    return True


def _derive_workspace_profile_name(workspace_url: str, existing: dict[str, str]) -> str:
    """Pick the ``~/.databrickscfg`` profile name to use for a workspace URL.

    Reusing an existing login is the priority so an already-authenticated
    profile for this host avoids an extra browser sign-in window:

    1. An existing profile in ``~/.databrickscfg`` already pointing at this
       host — reuse its name, so the host-keyed OAuth token cache is reused
       and no second ``databricks auth login`` is triggered.
    2. Otherwise derive a readable name from the workspace host's first DNS
       label, e.g. ``https://my-ws.cloud.databricks.com`` → ``"my-ws"``.

    :param workspace_url: The workspace URL, trailing slash already
        stripped, e.g. ``"https://my-ws.cloud.databricks.com"``.
    :param existing: ``{profile_name: host}`` from
        :func:`_existing_profile_hosts`.
    :returns: The profile section name to create or reuse, e.g. ``"my-ws"``.
    """
    for name, host in existing.items():
        if _host_matches(host, workspace_url):
            return name
    netloc = urlparse(workspace_url).netloc
    # The first DNS label is the most human-recognizable handle; fall back
    # to the full netloc, then a constant, so we never return an empty name.
    first_label = netloc.split(".")[0] if netloc else ""
    return first_label or netloc or "databricks"


def login_databricks_workspace(workspace_url: str, *, console: Console | None = None) -> str:
    """Ensure ``~/.databrickscfg`` has an authed profile for *workspace_url*.

    Runs ``databricks auth login --host <workspace_url> --profile <name>``
    for a single workspace and returns the profile name, so the caller can
    persist a ``kind: databricks`` provider keyed on it. This is the only
    place Omnigent triggers a Databricks CLI login: it fires solely when
    a user explicitly adds a Databricks provider in ``omnigent setup``, never
    on a bare ``omnigent run``.

    Idempotent: when a profile already points at this host (the OAuth token
    cache is host-keyed, so the login is still valid), it is reused without
    a fresh browser sign-in. If the derived profile name already exists but
    points at a *different* host, that stale section is dropped first — the
    CLI refuses ``--host`` against a name already bound to another host.

    :param workspace_url: The Databricks workspace URL to authenticate,
        e.g. ``"https://my-workspace.cloud.databricks.com"``. A trailing
        slash is stripped.
    :param console: Rich console for progress output; a fresh
        :class:`~rich.console.Console` is created when ``None``.
    :returns: The ``~/.databrickscfg`` profile name now authenticated for
        the workspace, e.g. ``"oss"``.
    :raises click.ClickException: If the ``databricks`` CLI is not on PATH,
        or the login subprocess fails.
    """
    import click
    from rich.console import Console

    from omnigent.onboarding.databricks_config import normalize_workspace_url

    console = console or Console()
    workspace_url = normalize_workspace_url(workspace_url)
    cli = find_databricks_cli()
    if cli is None:
        installer = (
            "curl -fsSL "
            "https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh "
            "| sh"
        )
        raise click.ClickException(
            "`databricks` CLI not on PATH — required to authenticate the workspace.\n"
            f"  Install with:\n    {installer}"
        )

    existing = _existing_profile_hosts()
    profile = _derive_workspace_profile_name(workspace_url, existing)

    current_host = existing.get(profile)
    if current_host is not None and _host_matches(current_host, workspace_url):
        console.print(f"  [green]✓ using existing Databricks profile `{profile}`[/green]")
        return profile
    if current_host is not None:
        # Name collides with a profile bound to a different host; the CLI
        # won't rebind --host onto an existing name, so drop it first.
        _remove_profile_section(profile)

    spec = ProfileSpec(
        name=profile,
        host=workspace_url,
        purpose="Databricks model serving (Unity Gateway)",
    )
    if not _login_profile(cli, spec, console):
        raise click.ClickException(
            f"`databricks auth login` failed for {workspace_url}; see the output above."
        )
    return profile
