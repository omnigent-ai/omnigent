"""Setup helpers for invoking ucode from Omnigent."""

from __future__ import annotations

import contextlib
import logging
import os
import shlex
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms have no flock
    fcntl = None  # type: ignore[assignment]

import click

from omnigent.onboarding.databricks_config import normalize_workspace_url
from omnigent.onboarding.ucode_state import read_ucode_config_scope, read_ucode_state

_logger = logging.getLogger(__name__)

# Sandbox configure is best-effort and off the host's dial-back path; bound it so
# a hung ucode can't leak a thread for the life of the host.
_SANDBOX_CONFIGURE_TIMEOUT_S = 120

_UCODE_AGENT_NAMES: tuple[str, ...] = ("claude", "codex", "pi")
# Pin an immutable SHA (ucode v0.2.1, sudo-free ``configure --user``) so setup is
# reproducible and uvx reuses its cached build instead of tracking ``main``.
_UCODE_GIT_REF = "5f72236eea3ab01db2cc4ff5fd4f15ede7626c33"
_UCODE_UVX_SOURCE = f"git+https://github.com/databricks/ucode@{_UCODE_GIT_REF}"

# ucode refuses ``configure --user`` while Codex's machine-wide config exists.
_CODEX_MANAGED_CONFIG_PATH = Path("/etc/codex/managed_config.toml")
_MACOS_ADMIN_GROUP = "admin"


def model_gateway_workspace_urls() -> list[str]:
    """Return the workspaces ucode should configure for model serving.

    ucode's only job is wiring coding harnesses to the Unity Gateway,
    so it only needs the gateway workspace(s) — the ``is_model_gateway``
    profiles. MCP-only workspaces (e.g. Jira / Confluence) still get a
    Databricks profile during onboarding for MCP auth, but passing them to
    ``ucode configure`` would make ucode do wasted gateway-discovery work
    (and extra per-workspace token fetches) against workspaces that serve
    no models.

    :returns: Gateway workspace URLs, each stripped of a trailing slash.
    """
    # Lazy import: internal-beta workspace list, excluded from the OSS build.
    import omnigent.onboarding.internal_beta as internal_beta  # type: ignore[import-not-found]

    return [
        spec.host.rstrip("/") for spec in internal_beta.DEFAULT_PROFILES if spec.is_model_gateway
    ]


def build_ucode_configure_command(
    ucode_command: Sequence[str],
    *,
    workspace_url: str,
    agents: Sequence[str] = _UCODE_AGENT_NAMES,
    user_scope: bool = False,
) -> list[str]:
    """Build the ``ucode configure`` command Omnigent runs.

    Fable needs no flag: ucode records it in ``claude_models`` when the
    workspace serves it.

    :param ucode_command: Command prefix that invokes ucode, e.g.
        ``("/usr/bin/ucode",)`` or ``("uvx", "--from",
        "git+https://github.com/databricks/ucode@<sha>", "ucode")``.
    :param workspace_url: The one workspace URL to configure; ucode
        configures a single workspace per run.
    :param agents: ucode agent names to configure non-interactively,
        e.g. ``("claude", "codex", "pi")``.
    :param user_scope: Pass ``--user`` so ucode writes per-user settings
        only, never the machine-wide managed settings that need sudo.
    :returns: Command argv using ucode's ``--workspaces`` and
        comma-separated ``--agents`` options.
    """
    argv = [
        *ucode_command,
        "configure",
        "--workspaces",
        normalize_workspace_url(workspace_url),
        "--agents",
        ",".join(agents),
    ]
    if user_scope:
        argv.append("--user")
    return argv


def _lacks_macos_admin_rights() -> bool:
    """Return whether this is a macOS account that cannot approve a sudo prompt.

    :returns: ``True`` on macOS when the user is neither root nor a member
        of the ``admin`` group; ``False`` on every other platform.
    """
    if sys.platform != "darwin" or os.geteuid() == 0:
        return False
    import grp
    import pwd

    try:
        admin_gid = grp.getgrnam(_MACOS_ADMIN_GROUP).gr_gid
        user_name = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return False
    else:
        return admin_gid not in os.getgrouplist(user_name, os.getgid())


def _existing_machine_managed_settings(agents: Sequence[str]) -> list[Path]:
    """Return the machine-wide managed settings files the selected harnesses already have.

    :param agents: ucode agent names being configured, e.g. ``("claude",)``.
    :returns: Existing Claude Code managed-settings or Codex managed-config
        paths for the selected agents, e.g.
        ``[Path("/etc/codex/managed_config.toml")]``.
    """
    from omnigent.onboarding.ambient import CLAUDE_CODE_MANAGED_SETTINGS_PATHS

    paths: list[Path] = []
    if "claude" in agents:
        paths.extend(CLAUDE_CODE_MANAGED_SETTINGS_PATHS)
    if "codex" in agents:
        paths.append(_CODEX_MANAGED_CONFIG_PATH)
    return [path for path in paths if path.exists()]


def _ucode_user_scope_preferred(agents: Sequence[str] = _UCODE_AGENT_NAMES) -> bool:
    """Return whether ``ucode configure`` should run with ``--user``.

    ucode refuses ``--user`` over existing managed files, so they always keep
    the default scope, even over a saved user scope. Otherwise a ``--user``
    scope already recorded in ucode's state is kept, since a run without it
    resets ucode to machine-wide settings. A macOS account without admin rights
    also gets ``--user``, because ucode's default writes managed settings
    through sudo.

    :param agents: ucode agent names being configured, e.g.
        ``("claude", "codex", "pi")``.
    :returns: ``True`` when Omnigent should pass ``--user``.
    """
    if _existing_machine_managed_settings(agents):
        return False
    return read_ucode_config_scope() == "user" or _lacks_macos_admin_rights()


def run_ucode_configure(
    ucode_command: Sequence[str],
    workspace_url: str,
    *,
    agents: Sequence[str] = _UCODE_AGENT_NAMES,
) -> None:
    """Run ``ucode configure`` for one workspace in the user's terminal.

    :param ucode_command: Command prefix from :func:`find_ucode_command`.
    :param workspace_url: The workspace to configure, e.g.
        ``"https://example.databricks.com"``.
    :param agents: ucode agent names to configure non-interactively.
    :returns: None.
    :raises click.ClickException: If ``ucode configure`` exits non-zero.
    """
    user_scope = _ucode_user_scope_preferred(agents)
    scope_flag = " --user" if user_scope else ""
    click.echo(f"Running `ucode configure --workspaces {workspace_url}{scope_flag}`...")
    argv = build_ucode_configure_command(
        ucode_command, workspace_url=workspace_url, agents=agents, user_scope=user_scope
    )
    result = subprocess.run(argv, check=False)
    if result.returncode == 0:
        return
    message = (
        f"`ucode configure` exited with code {result.returncode}; "
        "see the command output above for details."
    )
    managed_paths = _existing_machine_managed_settings(agents)
    if managed_paths:
        message += (
            " ucode cannot use --user while machine-wide managed settings exist. "
            "If it could not update them, ask an administrator to update or remove "
            f"{', '.join(map(str, managed_paths))}, then rerun setup."
        )
    elif not user_scope:
        message += (
            " If it could not update machine-wide managed settings (that needs "
            "administrator rights), configure your user only, then rerun setup:\n"
            f"  {shlex.join([*argv, '--user'])}"
        )
    raise click.ClickException(message)


def configure_ucode_for_workspace(
    workspace_url: str,
    *,
    agents: Sequence[str] = _UCODE_AGENT_NAMES,
) -> None:
    """Run ``ucode configure`` against a single model-serving workspace.

    This is the per-workspace counterpart to the legacy multi-workspace
    setup flow: instead of configuring every bundled profile at once, it
    wires the coding harnesses (Claude, Codex, Pi) to the Unity Gateway
    of exactly the one workspace the user supplied when adding a
    ``kind: databricks`` provider via ``omnigent setup --no-internal-beta``.
    ucode writes ``~/.ucode/state.json``, which Omnigent then reads for
    per-harness model defaults, base URLs, and the token-refresh command.

    :param workspace_url: The Databricks workspace URL whose model-serving
        gateway to configure, e.g.
        ``"https://example.databricks.com"``. A trailing slash
        is stripped by :func:`build_ucode_configure_command`.
    :param agents: ucode agent names to configure non-interactively,
        e.g. ``("claude", "codex", "pi")``. Defaults to all three.
    :returns: None.
    :raises click.ClickException: If ucode cannot be resolved (see
        :func:`find_ucode_command`) or ``ucode configure`` exits non-zero.
    """
    run_ucode_configure(find_ucode_command(), workspace_url, agents=agents)
    click.echo("ucode configuration complete. Omnigent will use state.json for harness setup.")


def ucode_workspace_exists(workspace_url: str) -> bool:
    """Return whether ucode state contains *workspace_url*.

    :param workspace_url: Workspace URL to check, e.g.
        ``"https://example.databricks.com"``.
    :returns: ``True`` when ``~/.ucode/state.json`` has a readable
        entry for the workspace.
    """
    return read_ucode_state(workspace_url) is not None


def find_ucode_command() -> list[str]:
    """Return a command prefix that invokes ``ucode``.

    Prefers an ephemeral ``uvx`` run pinned to a fixed ucode commit, so setup
    uses a known-good ucode rather than whatever (possibly stale) ``ucode`` the
    user installed long ago. A locally-installed binary is used only as a last
    resort when ``uvx`` is unavailable. This ordering matters: an old
    persistently-installed ``ucode`` predates options like
    ``configure --workspaces`` and would otherwise win and break setup.

    :returns: Command prefix, e.g.
        ``["uvx", "--from",
        "git+https://github.com/databricks/ucode@<sha>", "ucode"]`` or, when
        ``uvx`` is absent, ``["/usr/bin/ucode"]``.
    :raises click.ClickException: If neither ``uvx`` nor ``ucode`` is on PATH.
    """
    uvx = shutil.which("uvx")
    if uvx is not None:
        return [uvx, "--from", _UCODE_UVX_SOURCE, "ucode"]

    ucode = shutil.which("ucode")
    if ucode is None:
        raise click.ClickException(
            "uvx is not on PATH and ucode is not installed. Install uv, then retry:\n"
            "  uv tool install uv  # provides uvx"
        )
    return [ucode]


# Lock file shared by every sandbox ``ucode configure`` run, so they serialize
# their writes to ``~/.ucode/state.json``.
_CONFIGURE_LOCK_PATH = Path.home() / ".ucode" / ".omnigent-configure.lock"


@contextlib.contextmanager
def ucode_configure_lock() -> Iterator[None]:
    """Serialize concurrent ``ucode configure`` runs across processes and threads.

    At managed-connect boot the host runs ``ucode configure`` for all agents in a
    daemon thread; independently, opencode's launch can run a second
    ``ucode configure --agents opencode`` on demand. Both write
    ``~/.ucode/state.json``, so overlapping runs can interleave and drop an agent's
    entry or leave torn JSON. An advisory ``flock`` on a shared lock file
    serializes them (and concurrent host starts). Best-effort: if the lock file
    can't be created or the platform has no ``flock``, run unserialized rather than
    block the launch.
    """
    if fcntl is None:
        yield  # non-POSIX platform, no flock — degrade to unserialized
        return
    try:
        _CONFIGURE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(_CONFIGURE_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        yield  # can't create the lock file — degrade to unserialized
        return
    locked = False
    try:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_EX)
            locked = True
        yield
    finally:
        if locked:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def build_ucode_configure_command_for_profile(
    ucode_command: Sequence[str],
    *,
    profile: str,
    agents: Sequence[str] = _UCODE_AGENT_NAMES,
    use_pat: bool = False,
) -> list[str]:
    """Build a non-interactive ``ucode configure`` against an injected profile.

    The sandbox counterpart to :func:`build_ucode_configure_command`: that one
    takes ``--workspaces`` and drives an interactive OAuth login, which a headless
    managed sandbox can't do. Here the workspace is already present as a
    ``~/.databrickscfg`` profile, so ucode authenticates from it: ``use_pat``
    reads the profile's PAT (the lakebox control plane injects one), otherwise the
    caller exports ``DATABRICKS_BEARER_COMMAND`` so the credential broker mints per
    request (the OSS connect flow, nothing on disk). ``--skip-validate`` /
    ``--skip-upgrade`` keep host bring-up fast (no gateway round-trip or
    self-update mid-launch).
    """
    argv = [
        *ucode_command,
        "configure",
        "--profiles",
        profile,
        "--agents",
        ",".join(agents),
        "--skip-validate",
        "--skip-upgrade",
        "--skip-unavailable",
    ]
    if use_pat:
        argv.append("--use-pat")
    return argv


def configure_ucode_for_sandbox(
    profile: str,
    *,
    agents: Sequence[str] = _UCODE_AGENT_NAMES,
    use_pat: bool = False,
    extra_env: dict[str, str] | None = None,
) -> None:
    """Populate ``~/.ucode/state.json`` at managed-sandbox host boot, in the background.

    Runs ``ucode configure`` for the coding harnesses against an injected profile
    so the harness launch paths (which already call
    :func:`omnigent.onboarding.ucode_state.read_ucode_state`) pick up the
    workspace's base URLs and served models. A daemon thread keeps it off
    ``omnigent host``'s dial-back path, where a synchronous multi-second run would
    delay the runner connect; the harness launch falls back to its own hand-built
    gateway config until ``state.json`` exists, so the eventual-consistency window
    is safe. Best-effort: when ucode isn't available (a non-managed image) it
    no-ops rather than raising.

    Shared by both managed-sandbox callers: the OSS connect flow (this runs it
    from ``omnigent host`` boot with the broker command in ``extra_env``) and the
    lakebox launcher (``use_pat=True`` against its injected PAT).

    Fire-and-forget per boot (no resume-skip): configure is idempotent and cheap
    to repeat, and concurrent runs are serialized by :func:`ucode_configure_lock`.
    """
    try:
        ucode_command = find_ucode_command()
    except click.ClickException:
        return  # neither uvx nor ucode present → not a managed-sandbox image
    argv = build_ucode_configure_command_for_profile(
        ucode_command, profile=profile, agents=agents, use_pat=use_pat
    )
    env = {**os.environ, **(extra_env or {})}
    # ucode has no use for the host's launch token; don't hand it to the external
    # tool (it mints via DATABRICKS_BEARER_COMMAND, passed in extra_env).
    env.pop("OMNIGENT_HOST_TOKEN", None)

    def _run() -> None:
        try:
            with ucode_configure_lock():
                result = subprocess.run(
                    argv, capture_output=True, timeout=_SANDBOX_CONFIGURE_TIMEOUT_S, env=env
                )
        except (OSError, subprocess.SubprocessError) as exc:
            # A failed/timed-out configure silently forces the harness hand-built
            # fallback; log at WARNING so the field isn't blind.
            _logger.warning("ucode: sandbox configure failed (profile=%s): %r", profile, exc)
            return
        if result.returncode != 0:
            # Log the returncode, not ucode's stderr body — stderr could echo a
            # secret, and the code is enough to flag the fallback was taken.
            _logger.warning(
                "ucode: sandbox configure exit=%s (profile=%s)", result.returncode, profile
            )
        else:
            _logger.info("ucode: sandbox configure ok (profile=%s)", profile)

    threading.Thread(target=_run, name="ucode-configure", daemon=True).start()
