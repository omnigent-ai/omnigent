"""
Provider-agnostic sandbox bootstrap for Omnigent hosts.

Composes a :class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher`
into the full host-bootstrap flow: build the Omnigent wheels locally,
ship + install them in the sandbox, run the Databricks Apps OAuth flow
*inside the sandbox* (driving the browser step from the local machine
over a forwarded callback port), and register the sandbox as a host by
holding ``omnigent host`` open in it. The end state is a sandbox whose
sessions are reachable from the Omnigent server's UI, TUI, and
``omnigent resume``.

The OAuth token is minted and stored by the sandbox's own CLI rather
than shipped from the laptop. Shipping a laptop-minted token was
fundamentally broken: modern CLIs store the live token in the OS
keyring (so the shippable file was stale), the local and in-sandbox CLI
versions disagree on the token-cache layout, and U2M refresh tokens are
single-use — laptop and sandbox can't both hold the same one. Logging
in inside the sandbox makes it the sole token holder and sidesteps all
three.

Everything provider-specific (transport, image quirks, pip flags) lives
behind the launcher; see
:mod:`omnigent.onboarding.sandboxes.lakebox` for the reference
implementation.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import click
import httpx

if TYPE_CHECKING:
    from collections.abc import Iterable

    from omnigent.onboarding.sandboxes.base import RemoteProcess, SandboxLauncher


# ── Constants ──────────────────────────────────────────

WHEEL_PACKAGE_PATHS: tuple[str, ...] = ("sdks/python-client", "sdks/ui", ".")
"""Repo-relative paths of the three packages we bundle for sandbox
install: the python client SDK, the UI SDK, and the omnigent package
itself (which path-depends on the first two)."""

DEFAULT_WHEELS_TGZ: str = "/tmp/oa-wheels.tgz"
"""Local staging path for the packed wheel tarball. Rebuilt fresh on
every bootstrap so the sandbox always gets exactly the current
checkout's code."""

DEFAULT_BUILD_LOG: str = "/tmp/lakebox-build.log"
"""Default ``uv build`` log location."""

DEFAULT_SANDBOX_NAME: str = "omnigent-host"
"""Default label used when ``omnigent sandbox create`` provisions a
new sandbox."""

_REMOTE_WHEELS_TGZ: str = "/tmp/oa-wheels.tgz"
"""Where :func:`ship_wheels` places the wheel tarball inside the
sandbox before unpacking it."""

# Matches ANSI CSI escape sequences. In-sandbox `databricks auth login`
# output arrives over a PTY, so URL lines may be wrapped in color/cursor
# codes that must be stripped before parsing.
_ANSI_ESCAPE_PATTERN: re.Pattern[str] = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


# ── Wheel build ────────────────────────────────────────


def build_wheels(
    repo_root: Path,
    *,
    tgz_path: Path = Path(DEFAULT_WHEELS_TGZ),
    build_log: Path = Path(DEFAULT_BUILD_LOG),
    pypi_proxy: str | None = None,
) -> None:
    """
    Build the Omnigent wheels and pack them into a single tarball.

    Builds the three packages in :data:`WHEEL_PACKAGE_PATHS` via
    ``uv build --wheel`` into a staging directory, then tars the staging
    directory into *tgz_path*. The python-client and ui SDKs are
    path-deps of the root package, so all three must be built fresh in
    the same pass.

    Always builds fresh: the sandbox must end up running exactly the
    code in *repo_root*. (An earlier existence-based tarball cache made
    users reason about staleness via a --rebuild-wheels flag, with
    silently shipping old code as the failure mode.)

    :param repo_root: Path to the omnigent repo checkout, e.g.
        ``Path("/home/me/omnigent")``.
    :param tgz_path: Output path for the packed tarball.
    :param build_log: Path to write the combined ``uv build`` log to.
    :param pypi_proxy: ``UV_INDEX_URL`` override, or ``None`` to use
        ambient uv configuration. Launchers supply this via
        ``SandboxLauncher.wheel_build_index_url`` when the build machine
        sits on a network that can't reach public PyPI.
    :raises click.ClickException: If ``uv`` is not on PATH or any
        package's ``uv build`` exits non-zero.
    """
    if shutil.which("uv") is None:
        raise click.ClickException(
            "`uv` is required to build wheels. Install via "
            "`curl -LsSf https://astral.sh/uv/install.sh | sh` and retry."
        )

    click.echo(f"▸ Building Omnigent wheels → {tgz_path}")
    env = os.environ.copy()
    if pypi_proxy is not None:
        env.setdefault("UV_INDEX_URL", pypi_proxy)

    build_log.write_text("", encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="oa-wheels-") as stage_str:
        stage = Path(stage_str)
        for pkg in WHEEL_PACKAGE_PATHS:
            click.echo(f"  → {pkg}")
            _uv_build_wheel(repo_root / pkg, pkg, stage=stage, build_log=build_log, env=env)
        wheel_count = _pack_wheels(stage, tgz_path)

    click.echo(f"  → packed {wheel_count} wheels at {tgz_path}")


def _uv_build_wheel(
    pkg_dir: Path,
    pkg: str,
    *,
    stage: Path,
    build_log: Path,
    env: dict[str, str],
) -> None:
    """
    Run ``uv build --wheel`` for one package into the staging directory.

    :param pkg_dir: Absolute package directory to build from.
    :param pkg: Repo-relative package label for log/error messages,
        e.g. ``"sdks/python-client"``.
    :param stage: Staging directory uv writes the wheel into.
    :param build_log: Combined build log to append uv's output to.
    :param env: Environment for the uv subprocess (carries the
        ``UV_INDEX_URL`` override when set).
    :raises click.ClickException: If ``uv build`` exits non-zero.
    """
    with build_log.open("a", encoding="utf-8") as log:
        log.write(f"\n=== uv build {pkg} ===\n")
        log.flush()
        result = subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(stage)],
            cwd=pkg_dir,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            check=False,
        )
    if result.returncode != 0:
        raise click.ClickException(f"`uv build` for {pkg} failed — see {build_log}")


def _pack_wheels(stage: Path, tgz_path: Path) -> int:
    """
    Tar every wheel in the staging directory into *tgz_path*.

    :param stage: Directory holding the freshly-built ``*.whl`` files.
    :param tgz_path: Output path for the packed tarball.
    :returns: Number of wheels packed.
    """
    wheels = sorted(stage.glob("*.whl"))
    with tarfile.open(tgz_path, "w:gz") as tar:
        for wheel in wheels:
            tar.add(wheel, arcname=wheel.name)
    return len(wheels)


# ── Wheel install ──────────────────────────────────────


def ship_wheels(
    launcher: SandboxLauncher,
    sandbox_id: str,
    *,
    wheels_tgz: Path,
) -> None:
    """
    Install Omnigent wheels into a sandbox.

    Performs three remote operations, in order: ship wheels.tgz →
    pip install (the launcher supplies the image-appropriate flags) →
    PATH-export persistence. Credentials are *not* shipped — the
    sandbox mints its own OAuth token via
    :func:`login_app_oauth_in_sandbox`.

    :param launcher: The provider's launcher.
    :param sandbox_id: Target sandbox, e.g. ``"lovable-wattlebird-1530"``.
    :param wheels_tgz: Local path to the packed wheel tarball.
    :raises click.ClickException: If any of the three steps fail.
    """
    click.echo("▸ Shipping wheels into the sandbox")

    click.echo("  → wheels")
    launcher.put(sandbox_id, wheels_tgz, _REMOTE_WHEELS_TGZ)

    click.echo("  → pip install")
    launcher.run(sandbox_id, launcher.wheel_install_command(_REMOTE_WHEELS_TGZ))

    click.echo("  → PATH persistence in sandbox")
    launcher.run(
        sandbox_id,
        "for f in ~/.bashrc ~/.bash_profile; do "
        'grep -q ".local/bin" "$f" 2>/dev/null || '
        'echo "export PATH=\\$HOME/.local/bin:\\$PATH" >> "$f"; '
        "done",
    )


# ── App OAuth (minted inside the sandbox) ──────────────


def _extract_oauth_url(line: str) -> str | None:
    """
    Pull a Databricks OAuth authorize URL out of one line of CLI output.

    ``databricks auth login`` prints the verification URL on its own
    line (``https://<host>/oidc/v1/authorize?...``). Sandbox output is
    wrapped in a PTY, so the line may carry ANSI codes and a trailing
    carriage return; both are stripped before matching.

    :param line: One line of combined stdout/stderr from the in-sandbox
        login process.
    :returns: The authorize URL if this line contains one, else
        ``None``.
    """
    clean = _ANSI_ESCAPE_PATTERN.sub("", line).strip()
    start = clean.find("https://")
    if start == -1 or "/oidc/v1/authorize" not in clean:
        return None
    return clean[start:]


def _loopback_port_from_authorize_url(url: str) -> int:
    """
    Extract the loopback callback port from an OAuth authorize URL.

    The authorize URL carries a ``redirect_uri`` query parameter such as
    ``http://localhost:8022``. The in-sandbox CLI binds that port
    dynamically (the first free loopback port at/above 8020), so the
    actual value must be read back from the URL to know which port to
    forward.

    :param url: The authorize URL, e.g. ``"https://example.databricks."
        "com/oidc/v1/authorize?...&redirect_uri=http%3A%2F%2Flocalhost"
        "%3A8022&..."``.
    :returns: The callback port, e.g. ``8022``.
    :raises click.ClickException: If no ``redirect_uri`` with a loopback
        port can be parsed (the login URL format changed).
    """
    redirect = parse_qs(urlparse(url).query).get("redirect_uri", [None])[0]
    port = urlparse(redirect).port if redirect else None
    if port is None:
        raise click.ClickException(
            f"Could not parse an OAuth callback port from the login URL: {url}"
        )
    return port


def _read_login_url(stream: Iterable[str]) -> str | None:
    """
    Read login-process output until the OAuth verification URL appears,
    echoing every non-URL line (ANSI-stripped) as it streams.

    The echo is load-bearing for debuggability: when the in-sandbox
    login dies before printing a URL, its own error message is the
    only evidence of why — swallowing it here would leave the user
    with nothing but an exit code.

    :param stream: Line iterator over the in-sandbox login process's
        combined output (``RemoteProcess.lines``, or a list of lines in
        tests).
    :returns: The authorize URL, or ``None`` when the stream ends
        without printing one — which is NOT necessarily an error:
        ``omnigent login`` reuses a cached workspace OAuth grant when
        one verifies against the server, completing without a browser
        step. The caller distinguishes success from failure by the
        process's exit code.
    """
    for line in stream:
        url = _extract_oauth_url(line)
        if url is not None:
            return url
        text = _ANSI_ESCAPE_PATTERN.sub("", line).rstrip()
        if text:
            click.echo(f"    {text}")
    return None


def _drain_login_output(stream: Iterable[str]) -> None:
    """
    Echo remaining login output (ANSI-stripped) until the stream ends.

    Called after the URL is revealed; the in-sandbox CLI prints a
    confirmation line when the user completes the browser flow, then
    closes the stream.

    :param stream: Line iterator over the login process's output.
    """
    for line in stream:
        text = _ANSI_ESCAPE_PATTERN.sub("", line).rstrip()
        if text:
            click.echo(f"    {text}")


def _probe_server(server_url: str) -> httpx.Response | None:
    """
    GET ``<server_url>/v1/me`` unauthenticated, without following
    redirects (the 302 IS the signal for Databricks Apps).

    :param server_url: The server URL, e.g.
        ``"https://myapp-123.aws.databricksapps.com"``.
    :returns: The probe response, or ``None`` when the server is
        unreachable — the caller treats that as "shape unknown" and
        lets the in-sandbox login surface the real connectivity error.
    """
    try:
        return httpx.get(f"{server_url}/v1/me", timeout=10.0)
    except httpx.HTTPError:
        return None


@dataclass
class DerivedWorkspace:
    """
    Databricks workspace coordinates derived from unauthenticated
    probes of an Omnigent server URL.

    Consumed in two places: seeding the sandbox's ``~/.databrickscfg``
    before the in-sandbox login, and pinning the LOCAL ``databricks
    lakebox`` calls to the server's workspace (so the sandbox is
    created where the server lives, regardless of the ambient default
    profile).

    :param host: Workspace host fronting the server, e.g.
        ``"https://example.databricks.com"``.
    :param workspace_id: The workspace's numeric id, e.g.
        ``"4168070633950267"``, or ``None`` when the workspace didn't
        reveal it (the cfg's ``workspace_id`` line is then omitted).
    """

    host: str
    workspace_id: str | None


def _workspace_org_id(workspace_host: str) -> str | None:
    """
    Read a workspace's numeric id off an unauthenticated response.

    Databricks workspaces stamp ``x-databricks-org-id`` on responses
    even for anonymous requests; ``/login.html`` is a stable
    unauthenticated path to probe.

    :param workspace_host: Workspace host, e.g.
        ``"https://example.databricks.com"``.
    :returns: The id, e.g. ``"4168070633950167"``, or ``None`` when
        the header is absent or the workspace is unreachable.
    """
    try:
        response = httpx.get(f"{workspace_host}/login.html", timeout=10.0)
    except httpx.HTTPError:
        return None
    return response.headers.get("x-databricks-org-id")


def derive_workspace(server_url: str) -> DerivedWorkspace | None:
    """
    Return the Databricks workspace fronting *server_url*, if any.

    Runs the same unauthenticated detection ``omnigent login``
    performs — but from the LOCAL machine. Two consumers: the in-sandbox
    login step seeds the sandbox's ``~/.databrickscfg`` with the result,
    and the sandbox CLI commands pin their local ``databricks lakebox``
    calls to the derived workspace (so the sandbox is created and
    reached where the server lives).

    :param server_url: The server URL, e.g.
        ``"https://myapp-123.aws.databricksapps.com"``.
    :returns: The derived workspace, or ``None`` when the server is
        unreachable or not Databricks-fronted (accounts / OIDC /
        header-auth servers need no Databricks cfg).
    """
    # Deferred import: cli.py transitively imports this module at
    # startup, so a top-level import would be a cycle. The classifier
    # is cli-private; promoting it to a shared module is follow-up.
    from omnigent.cli import _databricks_workspace_login_target

    probe = _probe_server(server_url)
    if probe is None:
        return None
    host = _databricks_workspace_login_target(server_url, probe)
    if host is None:
        return None
    return DerivedWorkspace(host=host, workspace_id=_workspace_org_id(host))


def login_app_oauth_in_sandbox(
    launcher: SandboxLauncher,
    sandbox_id: str,
    *,
    server_url: str | None,
    workspace: DerivedWorkspace | None,
    skip: bool = False,
) -> None:
    """
    Log the sandbox in to *server_url* by running ``omnigent login``
    **inside the sandbox**, driving the browser step from the local
    machine.

    Databricks Apps front their HTTP/WS endpoints with an OAuth edge —
    workspace PATs (including the Lakebox image's baked credential)
    are rejected; only workspace OAuth tokens pass. ``omnigent
    login`` owns the credential inference used everywhere else: it
    probes *server_url*, discovers the fronting workspace
    from the probe response, mints a workspace OAuth grant (running
    ``databricks auth login --host <workspace>`` when no cached grant
    verifies), and stores a pointer record so ``omnigent host`` and
    runners mint fresh workspace tokens for this server automatically.
    No Databricks profile is created or consulted.

    The sandbox is headless, so when the login needs a browser this:

    1. runs ``omnigent login <server_url>`` inside the sandbox over a
       PTY;
    2. reads the dynamically-chosen loopback callback port back from the
       printed authorize URL;
    3. forwards ``localhost:<port>`` on the local machine into the
       sandbox (and waits for it to bind) so the browser's OAuth
       redirect reaches the in-sandbox listener; and
    4. opens the authorize URL in the local browser.

    When the in-sandbox login completes without printing an authorize
    URL (a cached workspace grant verified against the server), the
    browser steps are skipped and success is read from the exit code.

    The sandbox ends up the sole holder of its OAuth grant — nothing is
    shipped from the laptop — which sidesteps CLI-version cache-format
    skew, OS-keyring storage, and single-use refresh-token rotation.

    For Databricks-fronted servers (*workspace* is not ``None``), the
    sandbox's ``~/.databrickscfg`` is first RESET to exactly one
    ``[DEFAULT]`` entry naming the fronting workspace. Both halves
    matter: the image's baked PAT must go — host-keyed credential
    resolution prefers a host-matching cfg credential, and the Apps
    edge 302s PATs, so it shadows the OAuth grant the login mints —
    and a host entry must exist, because ``databricks auth login``
    stalls on an interactive profile-name prompt when the cfg has no
    entry for its host. The credential-less entry resolves through
    the ``databricks-cli`` token cache (the minted grant), which also
    serves in-sandbox workspace API calls, so nothing is lost with
    the PAT.

    :param launcher: The provider's launcher. Must support
        ``forward_local_port`` (providers without it raise
        ``SandboxCapabilityError`` naming the ``--no-auth`` escape
        hatch).
    :param sandbox_id: Target sandbox, e.g. ``"fast-tarantula-6030"``.
    :param server_url: Omnigent server URL to log in to, e.g.
        ``"https://myapp-123.aws.databricksapps.com"``. Required
        unless *skip*.
    :param workspace: The workspace fronting *server_url*, from
        :func:`derive_workspace` (the CLI derives once per command and
        threads it down — to here for the cfg seed, and to the lakebox
        launcher for its local workspace pin). ``None`` means the
        server is not Databricks-fronted — the cfg seed is skipped and
        the in-sandbox login runs the server's native (accounts /
        OIDC) flow.
    :param skip: When ``True``, skip authentication entirely (the
        ``--no-auth`` escape hatch).
    :raises click.ClickException: If *server_url* is missing, the
        forward fails to bind, or the in-sandbox login exits non-zero.
    """
    if skip:
        click.echo("▸ Skipping the in-sandbox server login")
        return
    # Fail fast for providers that can't bridge the OAuth callback port
    # (e.g. Modal) — BEFORE validating flags or touching the sandbox, so
    # the user gets the --no-auth hint instead of a misleading error
    # from a doomed in-sandbox login.
    if not launcher.supports_local_port_forward:
        raise launcher.forward_capability_error()
    if server_url is None:
        raise click.ClickException(
            "The in-sandbox login needs the server URL — pass --server, or --no-auth to skip."
        )

    click.echo(f"▸ Logging sandbox '{sandbox_id}' in to {server_url}")
    if workspace is not None:
        # Reset ~/.databrickscfg to exactly one [DEFAULT] entry shaped
        # like what `databricks auth login` itself writes (host +
        # auth_type + workspace_id) — see the docstring for why the
        # baked PAT must go AND a host entry must exist. auth_type =
        # databricks-cli pins the profile to the CLI token cache, so
        # credential resolution can't wander to another strategy and
        # miss the grant the login mints. Deterministic full reset
        # (not a surgical edit): leftover image keys would pin the
        # profile to the dead PAT instead. Idempotent; also replaces
        # the image's read-only-mount symlink with a real file.
        click.echo(f"  → seeding ~/.databrickscfg with workspace {workspace.host}")
        cfg_lines = [f"host = {workspace.host}", "auth_type = databricks-cli"]
        if workspace.workspace_id is not None:
            cfg_lines.append(f"workspace_id = {workspace.workspace_id}")
        cfg_body = "[DEFAULT]\n" + "\n".join(cfg_lines) + "\n"
        launcher.run(
            sandbox_id,
            f"rm -f ~/.databrickscfg && printf '%s' {shlex.quote(cfg_body)} > ~/.databrickscfg",
        )
    login = launcher.stream_exec(
        sandbox_id,
        f"omnigent login {shlex.quote(server_url)}",
        pty=True,
    )
    try:
        _complete_browser_login(launcher, sandbox_id, login)
    finally:
        login.close()


def _complete_browser_login(
    launcher: SandboxLauncher,
    sandbox_id: str,
    login: RemoteProcess,
) -> None:
    """
    Drive the browser half of an in-sandbox ``omnigent login``.

    Reads the authorize URL off the login process's output, bridges the
    URL's dynamically-chosen loopback callback port into the sandbox,
    opens the URL in the local browser, and waits for the login to
    finish. A login that completes without printing an authorize URL
    (cached workspace grant verified) skips the browser steps entirely
    — success vs. failure is then read from the exit code alone.

    :param launcher: The provider's launcher (supplies the port
        forward).
    :param sandbox_id: Sandbox the login process is running in.
    :param login: The streaming in-sandbox login process; the caller
        owns its cleanup.
    :raises click.ClickException: If the forward fails or the login
        exits non-zero.
    """
    url = _read_login_url(login.lines)
    if url is None:
        # No browser needed: `omnigent login` verified a cached
        # workspace grant against the server (or failed before the
        # browser step — the exit code tells which).
        returncode = login.wait()
        if returncode != 0:
            raise click.ClickException(
                f"`omnigent login` inside sandbox '{sandbox_id}' exited "
                f"with code {returncode} before printing a verification "
                "URL. Run it inside the sandbox manually to debug."
            )
        click.echo("  → cached workspace credentials accepted; no browser login needed")
        return
    port = _loopback_port_from_authorize_url(url)
    # Stand up (and confirm) the forward BEFORE revealing the URL so
    # the browser redirect can't race ahead of the tunnel.
    with launcher.forward_local_port(sandbox_id, port):
        click.echo("  → Opening the OAuth URL in your browser. If it doesn't open, visit:")
        click.echo(f"    {url}")
        click.echo(
            "  → On a headless host (Arca)? Forward the callback port to your "
            f"laptop too: `ssh -L {port}:localhost:{port} -N <this-host>`."
        )
        webbrowser.open(url)
        _drain_login_output(login.lines)
        returncode = login.wait()
        if returncode != 0:
            raise click.ClickException(
                f"`omnigent login` inside sandbox '{sandbox_id}' exited with code {returncode}."
            )


# ── Host registration ──────────────────────────────────


def set_sandbox_host_name(launcher: SandboxLauncher, sandbox_id: str, host_name: str) -> None:
    """
    Update the sandbox's ``~/.omnigent/config.yaml`` to use a
    specific host name.

    The host's ``host_id`` is preserved across the edit — only the
    ``name`` field is rewritten. If config.yaml doesn't exist yet,
    a minimal one is created with a fresh host_id; the next
    ``omnigent host`` will load that file as-is.

    Implementation note: the edit runs as a Python one-liner inside
    the sandbox (instead of ``sed``) so it survives YAML quirks
    (quoting, multi-doc, etc.) and produces well-formed output.

    :param launcher: The provider's launcher.
    :param sandbox_id: Target sandbox.
    :param host_name: New host name to write into config.yaml.
    :raises click.ClickException: If the remote command fails.
    """
    click.echo(f"  → setting host name to '{host_name}' in ~/.omnigent/config.yaml")
    # Quote the host name in single quotes for the python literal,
    # then escape any single quotes the user passed in.
    safe_name = host_name.replace("'", "\\'")
    py = (
        "import os, uuid, yaml; "
        "p=os.path.expanduser('~/.omnigent/config.yaml'); "
        "os.makedirs(os.path.dirname(p), exist_ok=True); "
        "cfg=yaml.safe_load(open(p)) if os.path.exists(p) else {}; "
        "cfg=cfg or {}; "
        f"h=cfg.get('host') or {{}}; h['name']='{safe_name}'; "
        "h.setdefault('host_id', uuid.uuid4().hex); "
        "cfg['host']=h; "
        "yaml.safe_dump(cfg, open(p,'w'), default_flow_style=False, sort_keys=True)"
    )
    launcher.run(sandbox_id, f'python3 -c "{py}"')


def connect_sandbox_host(
    launcher: SandboxLauncher,
    sandbox_id: str,
    *,
    server_url: str,
    host_name: str | None = None,
) -> None:
    """
    Register the sandbox as a host by running ``omnigent host`` in it.

    The remote command holds a WebSocket open until interrupted —
    Ctrl-C tears down the foreground transport and the remote process.

    The remote command is always the bare ``omnigent host --server
    <url>``: ``omnigent host`` no longer takes a ``--profile`` flag
    — it resolves credentials itself, via a stored
    ``omnigent login`` token or the sandbox's ambient Databricks
    credentials (e.g. the Lakebox image's baked workspace PAT, which
    authenticates to servers in the sandbox's own workspace).

    When *host_name* is set, the sandbox's
    ``~/.omnigent/config.yaml`` is updated so the host registers
    with that name instead of the default ``socket.gethostname()``.
    This matters for Lakebox sandboxes because all of them share the
    hostname ``databricks``, and the server's ``hosts`` table is
    primary-keyed on ``(owner, name)`` — collisions overwrite each
    other and (with existing conversations) FK-violate. Pick a unique
    name per sandbox to avoid the clash.

    :param launcher: The provider's launcher.
    :param sandbox_id: Target sandbox.
    :param server_url: Omnigent App URL the runner registers with.
    :param host_name: Optional override for the host's registered
        name. ``None`` keeps whatever's already in the sandbox's
        config.yaml (usually ``socket.gethostname()``).
    :raises click.ClickException: If the remote command exits non-zero.
    """
    click.echo(f"▸ Registering sandbox '{sandbox_id}' as a host with {server_url}")
    if host_name is not None:
        set_sandbox_host_name(launcher, sandbox_id, host_name)
    click.echo("  → running `omnigent host` in the sandbox (Ctrl-C to detach)")
    returncode = launcher.exec_foreground(sandbox_id, f"omnigent host --server {server_url}")
    if returncode != 0:
        raise click.ClickException(
            f"`omnigent host` on sandbox '{sandbox_id}' exited with code {returncode}."
        )


# ── High-level orchestrator ────────────────────────────


def bootstrap_sandbox_host(
    launcher: SandboxLauncher,
    *,
    sandbox_id: str | None,
    sandbox_name: str,
    server_url: str | None,
    workspace: DerivedWorkspace | None,
    repo_root: Path,
    skip_auth: bool,
) -> str:
    """
    Run the full sandbox-host bootstrap end-to-end.

    Six steps: provider preflight → provision or attach sandbox →
    keep-alive → build wheels → ship wheels → ``omnigent login``
    inside the sandbox.

    :param launcher: The provider's launcher.
    :param sandbox_id: Existing sandbox id to attach to, or ``None`` to
        provision a new one.
    :param sandbox_name: Label for a new sandbox (ignored when
        *sandbox_id* is set).
    :param server_url: Omnigent server URL the sandbox logs in to.
        Required unless *skip_auth*.
    :param workspace: The workspace fronting *server_url*, from
        :func:`derive_workspace` (the CLI derives once per command).
        ``None`` when the server is not Databricks-fronted.
    :param repo_root: Path to the omnigent repo checkout.
    :param skip_auth: When ``True``, skip the in-sandbox login.
    :returns: The sandbox id (the one we created or attached to).
    :raises click.ClickException: Propagated from any failing step.
    :raises SandboxCapabilityError: Immediately (before any remote
        work) when auth is requested but the provider cannot forward
        the OAuth callback port — pass ``skip_auth`` for such
        providers (the CLI does this automatically).
    """
    # The login step is last; check its one hard capability requirement
    # up front so a misconfigured call fails before the wheel build and
    # ship already ran. (The CLI skips auth automatically for providers
    # without the capability; this backstops programmatic callers.)
    if not skip_auth and not launcher.supports_local_port_forward:
        raise launcher.forward_capability_error()
    launcher.prepare()
    if sandbox_id is None:
        sandbox_id = launcher.provision(sandbox_name)
    else:
        launcher.attach(sandbox_id)
    click.echo(f"  → sandbox_id={sandbox_id}")
    launcher.keep_alive(sandbox_id)
    wheels_tgz = Path(DEFAULT_WHEELS_TGZ)
    build_wheels(
        repo_root,
        tgz_path=wheels_tgz,
        pypi_proxy=launcher.wheel_build_index_url,
    )
    ship_wheels(launcher, sandbox_id, wheels_tgz=wheels_tgz)
    login_app_oauth_in_sandbox(
        launcher,
        sandbox_id,
        server_url=server_url,
        workspace=workspace,
        skip=skip_auth,
    )
    return sandbox_id
