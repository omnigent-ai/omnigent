"""Sandbox CLI commands: run an Omnigent host in a remote sandbox.

``omnigent sandbox …`` bootstraps an Omnigent host inside a sandbox
from one of the registered providers (``--provider``) so that sessions
on it are reachable from the server-hosted UI, TUI, and ``omnigent
resume``. Provider availability is build-dependent — the Databricks
Lakebox launcher ships only internally — so cli.py registers this group
only when at least one provider is available.

The provider-agnostic step implementations live in
:mod:`omnigent.onboarding.sandboxes`.
"""

from __future__ import annotations

from pathlib import Path

import click

from omnigent.inner import ui
from omnigent.onboarding.sandboxes import (
    SandboxLauncher,
    available_providers,
    get_launcher,
)


def _omnigent_repo_root() -> Path:
    """
    Locate the omnigent checkout that hosts the three packages
    we build wheels for (``sdks/python-client``, ``sdks/ui``, ``.``).

    Strategy:

    1. Walk up from the current working directory looking for a parent
       that contains both ``sdks/python-client`` and ``omnigent``.
       This succeeds when the user runs ``omnigent sandbox …`` from
       inside a checkout.
    2. Fall back to ``Path(__file__).resolve().parents[1]`` so an
       editable install (``pip install -e .``) still works when the
       user invokes from outside the checkout.

    :returns: Absolute path to the repo root.
    :raises click.ClickException: If neither strategy finds the expected
        ``sdks/python-client`` directory.
    """
    cwd = Path.cwd().resolve()
    candidates: list[Path] = [cwd, *cwd.parents]
    # ``cli_sandbox.py`` lives at ``<repo>/omnigent/cli_sandbox.py``;
    # parents[1] is the repo root that hosts ``sdks/`` and
    # ``omnigent/``. Editable installs (``pip install -e .``) point
    # ``__file__`` into the checkout, so this branch covers the "invoke
    # from outside cwd" case. Wheel installs land in site-packages where
    # the parent check below will (correctly) miss and we raise.
    candidates.append(Path(__file__).resolve().parents[1])
    for candidate in candidates:
        if (candidate / "sdks" / "python-client").is_dir() and (candidate / "omnigent").is_dir():
            return candidate
    raise click.ClickException(
        "Could not locate the omnigent repo root from "
        f"{cwd}. Pass --repo-root explicitly or run from inside a checkout."
    )


def _resolve_repo_root(repo_root: Path | None) -> Path:
    """
    Resolve a ``--repo-root`` option value to an absolute path.

    :param repo_root: User-supplied override, or ``None`` to autodetect.
    :returns: Absolute repo root path.
    :raises click.ClickException: If autodetection fails or the supplied
        path doesn't look like a checkout.
    """
    if repo_root is None:
        return _omnigent_repo_root()
    resolved = repo_root.resolve()
    if not (resolved / "sdks" / "python-client").is_dir():
        raise click.ClickException(
            f"--repo-root {resolved} doesn't contain sdks/python-client; "
            "point it at an omnigent checkout."
        )
    return resolved


def _require_cli_bootstrap(launcher: SandboxLauncher) -> None:
    """
    Reject managed-only providers up front with an actionable message.

    Some providers implement only the server-managed launch subset
    (``supports_cli_bootstrap`` is ``False``); reaching their missing
    file-shipping / streaming primitives mid-flow would surface as an
    opaque capability error after real work already ran.

    :param launcher: The resolved provider launcher.
    :raises click.ClickException: When the provider has no CLI
        bootstrap flow.
    """
    if not launcher.supports_cli_bootstrap:
        raise click.ClickException(
            f"The '{launcher.provider}' provider supports server-managed "
            "sessions only — create one with "
            '`POST /v1/sessions {"host_type": "managed"}` (or the Web '
            "UI's New Sandbox option) against a server configured with "
            f"`sandbox.provider: {launcher.provider}`."
        )


def _normalize_server_url(server_url: str) -> str:
    """
    Validate and normalize a ``--server`` value.

    Validation runs at the CLI boundary so a malformed URL fails
    BEFORE any sandbox work — without it, a scheme-less value (e.g.
    ``//myapp.databricksapps.com``, a paste artifact) sails through
    provisioning, wheel build, and ship, and only explodes at the
    final in-sandbox ``omnigent login`` step.

    :param server_url: Raw ``--server`` value, e.g.
        ``"https://myapp-123.aws.databricksapps.com/"``.
    :returns: The URL without its trailing slash (a trailing slash
        breaks server-side URL joins).
    :raises click.ClickException: If the value does not start with
        ``http://`` or ``https://``.
    """
    normalized = server_url.rstrip("/")
    if not normalized.startswith(("http://", "https://")):
        raise click.ClickException(
            f"--server must be a full URL including the scheme, e.g. "
            f"https://{normalized.lstrip('/')} — got {server_url!r}."
        )
    return normalized


def _print_ready_banner(provider: str, sandbox_id: str, server_url: str) -> None:
    """
    Print the final "sandbox ready" instructions after a create.

    :param provider: The provider the sandbox was created with.
    :param sandbox_id: The sandbox the host is running in.
    :param server_url: Server URL for the connect hint (``--server``
        is required on create, so it is always known here).
    """
    ui.console.print()
    ui.success("Sandbox ready.")
    ui.console.print()
    ui.kv("Sandbox", f"{sandbox_id}  (provider: {provider})")
    ui.kv("Server", server_url)
    ui.console.print()
    click.echo("To register the sandbox as a host with your server:")
    click.echo(
        f"  omnigent sandbox connect --provider {provider} --sandbox-id {sandbox_id} "
        f"--server {server_url}\n"
    )


@click.group("sandbox")
def sandbox() -> None:
    """
    Run an Omnigent host inside a remote sandbox.

    \b
    Subcommands:
      create   Provision a sandbox + bootstrap Omnigent into it.
      connect  Register the sandbox as a host with your server (runs
               `omnigent host` in the sandbox).

    \b
    Provider notes:
      modal    Sandboxes live at most 24 hours (platform cap). Needs
               `pip install 'omnigent[modal]'` + `modal token new`.
      daytona  No lifetime cap (idle auto-stop is disabled). Needs
               `pip install 'omnigent[daytona]'` + DAYTONA_API_KEY.
               Free-tier (Tier 1/2) orgs only reach allowlisted
               domains, so `connect` needs an allowlisted --server
               (see deploy/daytona/README.md).
      islo     Uses the built-in HTTP client. Needs ISLO_API_KEY
               (and optionally ISLO_BASE_URL for non-default API
               endpoints).

    For provider-side sandbox lifecycle (list / status / delete /
    start / stop), use the provider's own CLI or dashboard directly
    (e.g. `modal sandbox list`).
    """


@sandbox.command("create")
@click.option(
    "--provider",
    type=click.Choice(available_providers()),
    required=True,
    help="Sandbox provider to use.",
)
@click.option(
    "--sandbox-id",
    "sandbox_id",
    default=None,
    # Lakebox-flow option (re-ship code into a long-lived sandbox, dodging
    # its slow provisioning + per-sandbox OAuth dance) — hidden from --help
    # pending removal. Disposable-sandbox providers just create a new one.
    hidden=True,
    help="Attach to an existing sandbox by id (skip provisioning).",
)
@click.option(
    "--name",
    "sandbox_name",
    default=None,
    help="Label for the new sandbox.",
)
@click.option(
    "--server",
    "server_url",
    required=True,
    help=(
        "Server URL the sandbox will register with. Determines the "
        "Databricks workspace the sandbox is created in (same "
        "inference as `omnigent login`), and the bootstrap finishes "
        "by logging the sandbox in to it (`omnigent login` inside the "
        "sandbox — one browser step)."
    ),
)
@click.option(
    "--repo-root",
    "repo_root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Path to the omnigent checkout.",
)
@click.option(
    "--no-auth",
    "skip_auth",
    is_flag=True,
    default=False,
    # Databricks-flow option — hidden from --help pending removal.
    hidden=True,
    help=(
        "Skip the in-sandbox server login. Providers that can't "
        "forward the callback port (e.g. modal) skip it automatically."
    ),
)
def sandbox_create(
    provider: str,
    sandbox_id: str | None,
    sandbox_name: str | None,
    server_url: str,
    repo_root: Path | None,
    skip_auth: bool,
) -> None:
    """
    Provision a sandbox and ship Omnigent into it.

    The server's workspace is derived from ``--server`` (the same
    unauthenticated probe ``omnigent login`` uses), and for lakebox
    the sandbox is created IN that workspace — so the sandbox always
    lives where the server lives, regardless of the local default
    profile. Builds the Omnigent wheels from your local checkout,
    installs them into the fresh sandbox, and finishes by logging the
    sandbox in to the server (``omnigent login`` runs inside the
    sandbox; the browser step is driven from this machine). Sandboxes
    are disposable — when your code changes, just create a new one.

    After this finishes, run ``omnigent sandbox connect`` to register
    the sandbox as a host with your server.
    """
    from omnigent.onboarding.sandboxes import (
        DEFAULT_SANDBOX_NAME,
        bootstrap_sandbox_host,
        derive_workspace,
    )

    app_url = _normalize_server_url(server_url)
    workspace = derive_workspace(app_url)
    launcher = get_launcher(
        provider, workspace_host=workspace.host if workspace is not None else None
    )
    _require_cli_bootstrap(launcher)
    # The in-sandbox login only exists for providers that can forward
    # the browser's callback port — others skip it automatically, no
    # --no-auth acknowledgement required.
    if not launcher.supports_local_port_forward:
        skip_auth = True
    sandbox_id = bootstrap_sandbox_host(
        launcher,
        sandbox_id=sandbox_id,
        sandbox_name=sandbox_name or DEFAULT_SANDBOX_NAME,
        server_url=app_url,
        workspace=workspace,
        repo_root=_resolve_repo_root(repo_root),
        skip_auth=skip_auth,
    )
    _print_ready_banner(provider, sandbox_id, app_url)


# The auth flow is Databricks-specific (in-sandbox server login) —
# hidden from --help pending removal; still invocable for lakebox users.
@sandbox.command("auth", hidden=True)
@click.option(
    "--provider",
    type=click.Choice(available_providers()),
    required=True,
    help="Sandbox provider to use.",
)
@click.option(
    "--sandbox-id", "sandbox_id", required=True, help="Sandbox to re-authenticate inside."
)
@click.option(
    "--server",
    "server_url",
    required=True,
    help=(
        "Server URL to log the sandbox in to. The in-sandbox "
        "`omnigent login` infers the fronting Databricks workspace "
        "from it automatically."
    ),
)
def sandbox_auth(
    provider: str,
    sandbox_id: str,
    server_url: str,
) -> None:
    """
    Run the server login inside the sandbox (``omnigent login``).

    Use this when the runner inside the sandbox starts failing because
    its cached OAuth grant expired (~90 days). Strictly faster than
    ``omnigent sandbox create --sandbox-id`` because it skips wheel
    build / ship / pip install — it only re-authenticates.
    """
    from omnigent.onboarding.sandboxes import derive_workspace, login_app_oauth_in_sandbox

    app_url = _normalize_server_url(server_url)
    workspace = derive_workspace(app_url)
    launcher = get_launcher(
        provider, workspace_host=workspace.host if workspace is not None else None
    )
    _require_cli_bootstrap(launcher)
    login_app_oauth_in_sandbox(
        launcher,
        sandbox_id,
        server_url=app_url,
        workspace=workspace,
    )
    ui.console.print()
    ui.success("Sandbox logged in.")
    ui.console.print()


@sandbox.command("connect")
@click.option(
    "--provider",
    type=click.Choice(available_providers()),
    required=True,
    help="Sandbox provider to use.",
)
@click.option("--sandbox-id", "sandbox_id", required=True, help="Sandbox to register as a host.")
@click.option(
    "--server",
    "server_url",
    required=True,
    help="Server URL the sandbox will register with.",
)
@click.option(
    "--host-name",
    "host_name",
    default=None,
    help=(
        "Name to register the sandbox as. Defaults to the sandbox's hostname. "
        "The server's hosts table is keyed on (owner, name), so sandboxes "
        "sharing a hostname collide; pass a unique value per sandbox."
    ),
)
def sandbox_connect(
    provider: str,
    sandbox_id: str,
    server_url: str,
    host_name: str | None,
) -> None:
    """
    Register the sandbox as a host with your server.

    Runs ``omnigent host --server <url>`` inside the sandbox — the
    host resolves its own credentials (a stored ``omnigent login``
    token, or the sandbox's ambient Databricks credentials such as the
    Lakebox image's baked workspace PAT). The remote command holds a
    WebSocket open until interrupted — Ctrl-C tears down the
    foreground transport and the remote process.

    Pass ``--host-name <label>`` when registering multiple sandboxes —
    sandboxes that share a hostname collide on the server's
    (owner, name) primary key.
    """
    from omnigent.onboarding.sandboxes import connect_sandbox_host, derive_workspace

    app_url = _normalize_server_url(server_url)
    # The sandbox lives in the server's workspace (create pinned it
    # there) — the local `lakebox ssh` transport must resolve through
    # the same workspace to find it.
    workspace = derive_workspace(app_url)
    launcher = get_launcher(
        provider, workspace_host=workspace.host if workspace is not None else None
    )
    _require_cli_bootstrap(launcher)
    connect_sandbox_host(
        launcher,
        sandbox_id,
        server_url=app_url,
        host_name=host_name,
    )


# Internal lakebox alias — hidden from the top-level --help pending
# removal; still fully invocable.
@click.group("lakebox", hidden=True)
@click.pass_context
def lakebox(ctx: click.Context) -> None:
    """
    Alias for ``omnigent sandbox … --provider lakebox``.

    Kept so existing muscle memory and scripts keep working. The
    subcommands (``create`` / ``auth`` / ``connect``) are the exact
    ``omnigent sandbox`` commands with ``--provider lakebox``
    pre-filled.
    """
    # Pre-fill --provider for the shared sandbox subcommands so
    # `omnigent lakebox <sub>` ≡ `omnigent sandbox <sub> --provider
    # lakebox`. default_map values satisfy the (required) --provider
    # option without redeclaring it on these aliased commands.
    ctx.default_map = {
        "create": {"provider": "lakebox"},
        "auth": {"provider": "lakebox"},
        "connect": {"provider": "lakebox"},
    }


# Reuse the same command objects (Click allows a command to live in more
# than one group); the default_map above fixes their provider to lakebox.
lakebox.add_command(sandbox_create, "create")
lakebox.add_command(sandbox_auth, "auth")
lakebox.add_command(sandbox_connect, "connect")
