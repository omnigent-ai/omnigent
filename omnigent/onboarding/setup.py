"""
Databricks profile onboarding for Omnigent.

The internal-beta onboarding configures a fixed set of Databricks profiles in
``~/.databrickscfg`` (the specific workspaces live in
:mod:`omnigent.onboarding.internal_beta`, which is excluded from the public
OSS build). This module provides the generic profile machinery:

- Discovers existing profiles via ``databricks auth profiles``
- Silently aliases existing profiles that already point at the right
  workspace under a different name (the OAuth token cache is host-keyed,
  so an alias inherits the login)
- Walks the user through ``databricks auth login`` for whatever's left
- Strips ``DATABRICKS_*`` env vars that would shadow profile lookup so
  stale tokens from a previous workspace can't override the requested
  profile

No persistent state: source of truth is ``~/.databrickscfg`` itself, so
every invocation re-derives "what needs to happen" from one filesystem
read.
"""

from __future__ import annotations

import configparser
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from rich.console import Console

SKIP_ENV_VAR = "OMNIGENT_SKIP_ONBOARD"

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

_CONFLICTING_ENV_VARS = CONFLICTING_ENV_VARS


@dataclass(frozen=True)
class ProfileSpec:
    """
    One Databricks profile Omnigent depends on.

    :param name: Profile name written to ``~/.databrickscfg``,
        e.g. ``"oss"``.
    :param host: Workspace URL the profile points at.
    :param purpose: One-line description shown during onboarding.
    :param is_model_gateway: Whether this workspace serves the Unity AI
        gateway that ucode configures coding harnesses against. Only
        gateway workspaces are passed to ``ucode configure``; MCP-only
        workspaces (e.g. Jira / Confluence) still get a Databricks profile
        for MCP auth but have no role in model serving, so configuring
        ucode against them is wasted work.
    """

    name: str
    host: str
    purpose: str
    is_model_gateway: bool


# ``DEFAULT_PROFILES`` (the internal-beta workspace hosts) lives in
# :mod:`omnigent.onboarding.internal_beta`, which is excluded from the public
# OSS build. The functions below import it lazily so this module stays free of
# internal hostnames and imports cleanly in the OSS build (where the only setup
# path is ``--no-internal-beta``, which never touches DEFAULT_PROFILES).


# ── Env-var hygiene ────────────────────────────────────


def detect_conflicting_env_vars() -> list[str]:
    """
    Return the names of set ``DATABRICKS_*`` env vars (catalog order).

    These shadow profile lookup in the SDK (env beats profile beats
    defaults). Empty-string values are treated as unset.

    :returns: e.g. ``["DATABRICKS_TOKEN"]``.
    """
    return [v for v in _CONFLICTING_ENV_VARS if os.environ.get(v)]


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


# ── Aliasing ───────────────────────────────────────────


def _alias_source_for(host: str, target: str, existing: dict[str, str]) -> str | None:
    """
    Find an existing profile pointing at ``host`` we can alias from.

    The Databricks CLI keys its OAuth token cache by host, so two
    profile sections pointing at the same workspace share the login —
    aliasing avoids a redundant browser OAuth dance.

    :param host: Target workspace URL.
    :param target: The profile name we're trying to create.
    :param existing: Output of :func:`_existing_profile_hosts`.
    :returns: A source profile name, or ``None`` if no match.
    """
    for name, existing_host in existing.items():
        if name != target and _host_matches(existing_host, host):
            return name
    return None


def _databrickscfg_path() -> Path:
    """Return the active Databricks config file path.

    Honors ``DATABRICKS_CONFIG_FILE`` so callers that want to operate on
    a temporary copy (e.g. during ``omnigent setup``) can redirect all
    direct configparser writes without touching the user's real file.
    """
    env = os.environ.get("DATABRICKS_CONFIG_FILE")
    return Path(env).expanduser() if env else Path.home() / ".databrickscfg"


def _alias_profile(source: str, target: str) -> None:
    """
    Copy the ``[source]`` section of ``~/.databrickscfg`` to ``[target]``.

    Atomic write via tempfile + rename. Raises if ``source`` isn't in
    the file (which would be a programmer error — callers must check
    that ``source`` exists first).

    :param source: Existing profile name to copy from.
    :param target: New profile name to create.
    """
    path = _databrickscfg_path()
    cfg = configparser.ConfigParser()
    cfg.read(path)
    if source not in cfg:
        raise ValueError(f"alias source {source!r} not in {path}")
    cfg[target] = dict(cfg[source])
    tmp = path.with_name(path.name + ".write")
    with tmp.open("w") as f:
        cfg.write(f)
    tmp.replace(path)


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


# ── Compute actions ───────────────────────────────────


@dataclass(frozen=True)
class _Actions:
    """
    What :func:`maybe_run_onboarding` needs to do for the current state.

    :param ready: Profiles already correctly configured.
    :param aliasable: Tuples of ``(source_profile, target_spec)`` we can
        alias without OAuth.
    :param oauth: Profiles that need a fresh ``databricks auth login``.
    :param wrong_host: Profiles where the alpha name (e.g. ``oss``)
        exists but points at the wrong workspace. We can't auto-alias
        these without clobbering the user's existing config, so they
        need explicit confirmation before we overwrite.
    """

    ready: tuple[ProfileSpec, ...]
    aliasable: tuple[tuple[str, ProfileSpec], ...]
    oauth: tuple[ProfileSpec, ...]
    wrong_host: tuple[tuple[ProfileSpec, str], ...]


def _compute_actions(existing: dict[str, str]) -> _Actions:
    """Classify each :data:`DEFAULT_PROFILES` entry by what it needs.

    :param existing: Current ``{profile: host}`` from the CLI.
    :returns: Bucketed actions.
    """
    # Lazy import: the internal-beta workspace list is excluded from the OSS
    # build. This function is only reached via ``setup --internal-beta``.
    from omnigent.onboarding.internal_beta import DEFAULT_PROFILES

    ready: list[ProfileSpec] = []
    aliasable: list[tuple[str, ProfileSpec]] = []
    oauth: list[ProfileSpec] = []
    wrong_host: list[tuple[ProfileSpec, str]] = []
    for spec in DEFAULT_PROFILES:
        host = existing.get(spec.name)
        if host is not None and _host_matches(host, spec.host):
            ready.append(spec)
            continue
        if host is not None:
            # Profile name matches but the host doesn't — needs an
            # explicit "overwrite or skip" decision from the user.
            wrong_host.append((spec, host))
            continue
        source = _alias_source_for(spec.host, spec.name, existing)
        if source is not None:
            aliasable.append((source, spec))
        else:
            oauth.append(spec)
    return _Actions(tuple(ready), tuple(aliasable), tuple(oauth), tuple(wrong_host))


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


# ── Top-level entry points ────────────────────────────


def _apply_silent_aliases(aliasable: tuple[tuple[str, ProfileSpec], ...], console: Console) -> int:
    """Create aliases for every entry; return how many landed."""
    n = 0
    for source, spec in aliasable:
        try:
            _alias_profile(source, spec.name)
        except (ValueError, OSError) as exc:
            console.print(f"  [yellow]could not alias {spec.name} from {source}: {exc}[/yellow]")
            continue
        n += 1
    return n


def run_onboarding() -> bool:
    """
    Interactive Databricks profile setup flow.

    1. Bail if ``databricks`` CLI is missing.
    2. Silently alias any same-host profiles.
    3. For the remaining gaps, run ``databricks auth login``.

    :returns: ``True`` if all three profiles are configured at the end.
    """
    from rich.console import Console
    from rich.panel import Panel

    # Lazy import: internal-beta workspace list, excluded from the OSS build.
    from omnigent.onboarding.internal_beta import DEFAULT_PROFILES

    console = Console()
    if os.environ.get(SKIP_ENV_VAR):
        console.print(f"  [dim]{SKIP_ENV_VAR} set; skipping.[/dim]")
        return False

    cli = find_databricks_cli()
    if cli is None:
        installer = (
            "curl -fsSL "
            "https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh "
            "| sh"
        )
        console.print(
            f"\n  [bold red]`databricks` CLI not on PATH.[/bold red]\n"
            f"  Install with:\n    [cyan]{installer}[/cyan]\n"
        )
        return False

    console.print(
        Panel(
            "[bold]Omnigent onboarding[/bold]\n\n"
            "Setting up the Databricks profiles Omnigent needs "
            f"({', '.join(f'`{s.name}`' for s in DEFAULT_PROFILES)}).",
            border_style="cyan",
            padding=(1, 2),
        )
    )

    existing = _existing_profile_hosts()
    actions = _compute_actions(existing)

    if actions.aliasable:
        n = _apply_silent_aliases(actions.aliasable, console)
        if n:
            console.print(f"\n  [green]✓ aliased {n} existing profile(s)[/green]")
        existing = _existing_profile_hosts()
        actions = _compute_actions(existing)

    for spec, current_host in actions.wrong_host:
        _handle_wrong_host(cli, spec, current_host, console)
    if actions.wrong_host:
        existing = _existing_profile_hosts()
        actions = _compute_actions(existing)

    if not actions.oauth and not actions.wrong_host:
        console.print("\n  [bold green]all profiles ready.[/bold green]\n")
        return True

    for spec in actions.oauth:
        _login_profile(cli, spec, console)

    actions = _compute_actions(_existing_profile_hosts())
    done = not actions.oauth and not actions.wrong_host
    if done:
        console.print("\n  [bold green]✓ onboarding complete.[/bold green]\n")
    else:
        names = [s.name for s in actions.oauth] + [s.name for s, _ in actions.wrong_host]
        console.print(f"\n  [yellow]still missing: {', '.join(names)}[/yellow]\n")
    return done


def _handle_wrong_host(cli: str, spec: ProfileSpec, current_host: str, console: Console) -> None:
    """
    Prompt the user about a profile whose name matches an alpha slot but
    points at a different workspace.

    Three escapes: overwrite (run ``databricks auth login`` for the
    alpha host, which the CLI handles by replacing the section), skip
    (leave the user's profile alone — they'll need to rename it manually
    and re-run), or default-skip on non-TTY / EOF.

    :param cli: Absolute path to the ``databricks`` binary.
    :param spec: The alpha profile spec we wanted to set up.
    :param current_host: Where the user's existing same-name profile
        currently points.
    :param console: Rich console for the surrounding messaging.
    """
    console.print(
        f"\n  [yellow]! `{spec.name}` exists but points at the wrong workspace:[/yellow]\n"
        f"    current:  {current_host}\n"
        f"    expected: {spec.host}\n"
        f"  [dim]Overwriting will REPLACE the existing `{spec.name}` section in "
        f"~/.databrickscfg. To keep your existing one, decline below and rename it "
        f"first (e.g. `databricks auth login --profile {spec.name}-personal "
        f"--host {current_host}`).[/dim]"
    )
    if not sys.stdin.isatty():
        console.print(
            f"  [dim](non-TTY — skipping. Rename your existing `{spec.name}` and re-run.)[/dim]"
        )
        return
    try:
        answer = input(f"  Overwrite `{spec.name}`? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print()
        return
    if answer not in ("y", "yes"):
        console.print(f"  [dim]Skipped `{spec.name}`.[/dim]")
        return
    # The Databricks CLI refuses ``--host X --profile NAME`` when NAME already
    # exists with a different host ("conflicts with --host"). Drop the existing
    # section first so the login starts from a clean slate.
    _remove_profile_section(spec.name)
    _login_profile(cli, spec, console)


def _derive_workspace_profile_name(workspace_url: str, existing: dict[str, str]) -> str:
    """Pick the ``~/.databrickscfg`` profile name to use for a workspace URL.

    Resolution order. Reusing an *existing* login is the priority — the
    whole point of the flow is to avoid extra browser sign-in windows — so
    an already-authenticated profile for this host wins even over the
    bundled canonical name:

    1. An existing profile in ``~/.databrickscfg`` already pointing at this
       host — reuse its name, so the host-keyed OAuth token cache is reused
       and no second ``databricks auth login`` is triggered. This wins even
       for the bundled OSS host: a user already logged in under their own
       profile name is never re-sent through a fresh ``--profile oss`` login.
    2. A bundled :data:`DEFAULT_PROFILES` spec whose host matches — use its
       canonical name (e.g. the OSS gateway URL → ``"oss"``) when the user
       has no existing profile for it, lining up with the routing fallbacks
       in
       :func:`omnigent.onboarding.databricks_config.get_workspace_url_for_profile`.
    3. Otherwise derive a readable name from the workspace host's first DNS
       label, e.g. ``https://my-ws.cloud.databricks.com`` → ``"my-ws"``.

    :param workspace_url: The workspace URL, trailing slash already
        stripped, e.g. ``"https://my-ws.cloud.databricks.com"``.
    :param existing: ``{profile_name: host}`` from
        :func:`_existing_profile_hosts`.
    :returns: The profile section name to create or reuse, e.g. ``"oss"``
        or ``"my-ws"``.
    """
    for name, host in existing.items():
        if _host_matches(host, workspace_url):
            return name
    # Lazy import: internal-beta workspace list, intentionally absent from
    # the OSS build — without it there are no bundled canonical names and
    # the derived-label fallback below applies.
    try:
        from omnigent.onboarding.internal_beta import DEFAULT_PROFILES
    except ModuleNotFoundError:
        DEFAULT_PROFILES = ()

    for spec in DEFAULT_PROFILES:
        if _host_matches(spec.host, workspace_url):
            return spec.name
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
    a user explicitly adds a Databricks provider in
    ``omnigent setup --no-internal-beta``, never on a bare ``omnigent run``.

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
        purpose="Databricks model serving (Unity AI Gateway)",
        # The user is adding this workspace specifically for model serving, so
        # it's a gateway workspace.
        is_model_gateway=True,
    )
    if not _login_profile(cli, spec, console):
        raise click.ClickException(
            f"`databricks auth login` failed for {workspace_url}; see the output above."
        )
    return profile


def maybe_run_onboarding() -> None:
    """
    Pre-flight onboarding for ``omnigent run``.

    Fast path on every call: read ``~/.databrickscfg``, classify
    profiles, and:

    - All three configured → no-op.
    - Some same-host profiles exist under different names → silently
      alias, print one-line note, no prompt.
    - Some still need OAuth → ``Y/n`` prompt; on ``Y`` delegate to
      :func:`run_onboarding`.

    Skipped when ``$OMNIGENT_SKIP_ONBOARD`` is set or stdin isn't a
    TTY (CI / piped input — can't safely launch OAuth).
    """
    if os.environ.get(SKIP_ENV_VAR):
        return
    if not sys.stdin.isatty():
        return

    existing = _existing_profile_hosts()
    actions = _compute_actions(existing)

    if not actions.aliasable and not actions.oauth and not actions.wrong_host:
        return

    from rich.console import Console

    console = Console()

    if actions.aliasable:
        n = _apply_silent_aliases(actions.aliasable, console)
        if n:
            console.print(
                f"omnigent: aliased {n} existing profile(s) "
                f"to Omnigent names: "
                f"{', '.join(spec.name for _, spec in actions.aliasable)}"
            )
        existing = _existing_profile_hosts()
        actions = _compute_actions(existing)

    if not actions.oauth and not actions.wrong_host:
        return

    missing_parts = [s.name for s in actions.oauth]
    missing_parts += [f"{s.name} (wrong host)" for s, _ in actions.wrong_host]
    console.print(
        f"\n  [yellow]Omnigent needs Databricks profiles: {', '.join(missing_parts)}[/yellow]"
    )
    try:
        answer = input("  Run onboarding now? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print()
        return
    if answer in ("", "y", "yes"):
        run_onboarding()
    else:
        console.print(
            "  [dim]run `omnigent setup --internal-beta` when ready "
            f"(or `{SKIP_ENV_VAR}=1` to silence).[/dim]"
        )
