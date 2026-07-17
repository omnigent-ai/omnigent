"""
Modal sandbox launcher.

Implements :class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher`
for `Modal <https://modal.com>`_ sandboxes. This module ships in the OSS
build; the Modal SDK itself is an optional dependency (``pip install
'omnigent[modal]'``) imported lazily, so the provider can be listed and
the module probed without it.

Platform constraints that shape this launcher:

- **24-hour lifetime.** Modal caps sandbox lifetime at 24 hours;
  :meth:`ModalSandboxLauncher.provision` requests that maximum and
  :meth:`ModalSandboxLauncher.keep_alive` can only restate the cap — a
  Modal-hosted Omnigent host must be re-created daily.
- **No inbound port forwarding.** Modal tunnels expose sandbox ports to
  the public internet but provide no local→sandbox path, so the
  in-sandbox App OAuth flow (which forwards the browser's callback port)
  is unsupported: ``supports_local_port_forward`` stays ``False`` and
  the CLI skips the in-sandbox App OAuth step automatically.
- **No kill handle on exec'd processes.** The SDK's process handle
  exposes wait/poll/streams only, so
  :meth:`ModalSandboxLauncher.exec_foreground` records the remote pid in
  a pidfile and kills it via a second exec when the local side is
  interrupted.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import click

from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    RemoteCommandResult,
    RemoteProcess,
    SandboxLauncher,
    foreground_kill_command,
    foreground_pidfile,
    foreground_record_prefix,
    host_image_wheel_install_command,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    import modal


# ── Constants ──────────────────────────────────────────

MODAL_APP_NAME: str = "omnigent-sandboxes"
"""Modal App that owns every sandbox this launcher creates. Sandboxes
must belong to an App; a single shared, lazily-created App keeps them
grouped in Modal's dashboard."""

MAX_SANDBOX_LIFETIME_S: int = 24 * 60 * 60
"""Sandbox ``timeout`` requested at creation — Modal's hard platform
maximum (24 hours). There is no way to extend a sandbox past it."""

HOST_IMAGE_ENV_VAR: str = "OMNIGENT_MODAL_HOST_IMAGE"
"""Environment variable overriding :data:`DEFAULT_HOST_IMAGE`, e.g. for
an org-internal copy of the host image
(``ghcr.io/<your-org>/omnigent-host:latest``)."""

REGISTRY_SECRET_ENV_VAR: str = "OMNIGENT_MODAL_REGISTRY_SECRET"
"""Environment variable naming a Modal secret
(https://modal.com/secrets) holding static registry credentials —
``REGISTRY_USERNAME`` / ``REGISTRY_PASSWORD`` (for GHCR: a PAT with
``read:packages``) — required when the host image lives in a private
registry. Unset means an anonymous pull."""

SANDBOX_SECRETS_ENV_VAR: str = "OMNIGENT_MODAL_SANDBOX_SECRETS"
"""Environment variable naming Modal secrets (comma-separated) whose
env vars are injected into every sandbox this launcher creates —
typically the harness LLM credentials (``ANTHROPIC_API_KEY``,
``OPENAI_API_KEY``, ``CLAUDE_CODE_OAUTH_TOKEN``, gateway base URLs, …)
that the in-sandbox host forwards to runners. Distinct from
:data:`REGISTRY_SECRET_ENV_VAR` (image-pull credentials): these become
the WORKLOAD's environment. The server's managed-host config
(``sandbox.modal.secrets``) takes precedence when set."""

# Resources for the sandbox. Modal's defaults (0.125 CPU cores) starve
# the Omnigent host's runner + harness processes; 2 vCPU / 4 GiB is
# enough for a host running one interactive session.
_SANDBOX_CPU: float = 2.0
_SANDBOX_MEMORY_MIB: int = 4096


def _ensure_sdk() -> None:
    """
    Verify the Modal SDK is importable, with an install hint when not.

    Called at the top of every launcher entry point because the SDK is
    an optional dependency — the base ``omnigent`` install does not
    pull it in.

    :raises click.ClickException: When the ``modal`` package is not
        installed.
    """
    try:
        import modal  # noqa: F401  # presence probe only
    except ImportError as exc:
        raise click.ClickException(
            "The Modal SDK is required for the 'modal' sandbox provider. "
            "Install it with `pip install 'omnigent[modal]'`, then "
            "authenticate with `modal token new`."
        ) from exc


def _has_modal_credentials() -> bool:
    """
    Detect whether Modal credentials are available to the SDK.

    Mirrors the SDK's own resolution order: the
    ``MODAL_TOKEN_ID`` / ``MODAL_TOKEN_SECRET`` env-var pair takes
    precedence, then the token file (``MODAL_CONFIG_PATH`` override or
    the documented default ``~/.modal.toml``).

    :returns: ``True`` if either credential source is present.
    """
    if os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET"):
        return True
    config_path = os.environ.get("MODAL_CONFIG_PATH")
    token_file = Path(config_path) if config_path else Path.home() / ".modal.toml"
    return token_file.is_file()


def _lookup_sandbox(sandbox_id: str) -> modal.Sandbox:
    """
    Resolve a sandbox id to a live SDK handle.

    :param sandbox_id: Modal sandbox object id, e.g. ``"sb-a1b2c3"``.
    :returns: The sandbox handle.
    :raises click.ClickException: When no sandbox with that id exists
        (including ones already reaped after their 24h lifetime).
    """
    _ensure_sdk()
    import modal

    try:
        return modal.Sandbox.from_id(sandbox_id)
    except modal.exception.NotFoundError as exc:
        raise click.ClickException(
            f"Modal sandbox '{sandbox_id}' not found — it may have passed its "
            "24-hour lifetime. Create a fresh one with "
            "`omnigent sandbox create --provider modal`."
        ) from exc


def _build_sandbox_image(image_ref: str | None = None) -> modal.Image:
    """
    Resolve the sandbox image definition.

    Pulls the prebaked Omnigent host image — the full omnigent
    install plus the tools a host needs at runtime: ``git``
    (workspaces), ``tmux`` (terminal sessions spawned by native
    harnesses), ``curl`` + CA certificates. Booting from a prebaked
    image makes sandbox creation a pull instead of an in-sandbox
    dependency install; the CLI flow's locally-built wheels still
    overlay the baked install (see
    :meth:`ModalSandboxLauncher.wheel_install_command`).

    Resolution order for the image reference: the explicit *image_ref*
    (e.g. the server's managed-host ``sandbox.modal.image`` config) →
    :data:`HOST_IMAGE_ENV_VAR` → the official
    :data:`DEFAULT_HOST_IMAGE`.

    For images in private registries, :data:`REGISTRY_SECRET_ENV_VAR`
    names a Modal secret with ``REGISTRY_USERNAME`` /
    ``REGISTRY_PASSWORD`` keys (for GHCR: a PAT with ``read:packages``)
    — applied to explicit refs and env/default refs alike.

    :param image_ref: Explicit registry image reference, e.g.
        ``"docker.io/me/omnigent-host:latest"``, or ``None`` to
        resolve from the environment / official default.
    :returns: The (lazy) image definition; Modal pulls it on first use.
    """
    import modal

    resolved_ref = image_ref or os.environ.get(HOST_IMAGE_ENV_VAR) or DEFAULT_HOST_IMAGE
    secret_name = os.environ.get(REGISTRY_SECRET_ENV_VAR)
    secret = modal.Secret.from_name(secret_name) if secret_name else None
    return modal.Image.from_registry(resolved_ref, secret=secret)


def _echo_lines(stream: str) -> None:
    """
    Echo a captured remote output stream line-by-line, dropping
    pure-whitespace lines.

    :param stream: Captured stdout or stderr from a remote command.
    """
    for line in stream.splitlines():
        if line.strip():
            click.echo(line)


class _ModalRemoteProcess(RemoteProcess):
    """
    :class:`RemoteProcess` over a Modal container process handle.

    The handle's stdout stream carries the combined output (the spawn
    site merges stderr in-shell or via a PTY).
    """

    def __init__(self, process: modal.container_process.ContainerProcess[str]) -> None:
        """
        Wrap a running exec'd process.

        :param process: Handle returned by ``Sandbox.exec``.
        """
        self._process = process
        # Materialize the line iterator once so repeated `lines` reads
        # resume the same stream (the RemoteProcess contract).
        self._lines: Iterator[str] = iter(process.stdout)

    @property
    def lines(self) -> Iterator[str]:
        """
        The process's combined-output line iterator (same object on
        every access).

        :returns: Line iterator over the process's stdout.
        """
        return self._lines

    def wait(self) -> int:
        """
        Block until the process exits.

        :returns: The process's exit code.
        """
        return self._process.wait()

    def close(self) -> None:
        """
        Best-effort cleanup. DEVIATION from the base contract: Modal's
        SDK exposes no way to kill an exec'd process (the handle has
        only wait/poll/streams), so a still-running process is left to
        die with the sandbox (at most 24 hours). In practice this path
        is unreachable: the only ``stream_exec`` consumer is the OAuth
        login flow, which the bootstrap blocks on Modal before any
        process is spawned.
        """
        # Nothing to reap if the process already exited; nothing we CAN
        # do if it hasn't (see docstring).
        self._process.poll()


class ModalSandboxLauncher(SandboxLauncher):
    """
    :class:`SandboxLauncher` for Modal sandboxes.

    All transport rides the Modal SDK: ``Sandbox.exec`` for commands
    (argv-style ``bash -lc``, no remote quoting pitfalls),
    ``sandbox.filesystem`` for file shipping. Handles are cached per
    sandbox id to avoid a server round-trip on every primitive.
    """

    provider: ClassVar[str] = "modal"
    # Public PyPI is reachable from the local wheel build (no corp-proxy
    # requirement for Modal users); ambient uv config applies.
    wheel_build_index_url: ClassVar[str | None] = None
    # Modal tunnels are sandbox→public only; there is no local→sandbox
    # path, so the App OAuth flow is unsupported (use --no-auth).
    supports_local_port_forward: ClassVar[bool] = False

    def __init__(self, *, image: str | None = None, secrets: Sequence[str] | None = None) -> None:
        """
        Initialize the launcher.

        :param image: Optional registry image reference to provision
            sandboxes from, e.g. ``"docker.io/me/omnigent-host:latest"``
            — the server's managed-host ``sandbox.modal.image`` config.
            ``None`` resolves :data:`HOST_IMAGE_ENV_VAR` and falls back
            to the official :data:`DEFAULT_HOST_IMAGE` (see
            :func:`_build_sandbox_image`).
        :param secrets: Optional Modal secret names whose env vars are
            injected into every sandbox, e.g. ``["omnigent-llm"]`` —
            the server's managed-host ``sandbox.modal.secrets`` config.
            ``None`` resolves :data:`SANDBOX_SECRETS_ENV_VAR`
            (comma-separated) and falls back to no injected secrets.
        """
        self._image_ref = image
        self._secret_names = tuple(secrets) if secrets is not None else None
        self._sandboxes: dict[str, modal.Sandbox] = {}

    def _resolve_sandbox_secrets(self) -> list[modal.Secret]:
        """
        Resolve the Modal secrets to inject into created sandboxes.

        Explicit constructor names win; otherwise
        :data:`SANDBOX_SECRETS_ENV_VAR` (comma-separated) applies; an
        empty resolution injects nothing.

        :returns: Secret handles for ``Sandbox.create(secrets=…)``.
        """
        import modal

        if self._secret_names is not None:
            names: Sequence[str] = self._secret_names
        else:
            names = [
                name.strip()
                for name in os.environ.get(SANDBOX_SECRETS_ENV_VAR, "").split(",")
                if name.strip()
            ]
        return [modal.Secret.from_name(name) for name in names]

    def _resolve(self, sandbox_id: str) -> modal.Sandbox:
        """
        Return the cached handle for *sandbox_id*, looking it up on
        first use.

        :param sandbox_id: Modal sandbox object id, e.g. ``"sb-a1b2c3"``.
        :returns: The sandbox handle.
        :raises click.ClickException: When the sandbox does not exist.
        """
        handle = self._sandboxes.get(sandbox_id)
        if handle is None:
            handle = _lookup_sandbox(sandbox_id)
            self._sandboxes[sandbox_id] = handle
        return handle

    def prepare(self) -> None:
        """
        Local preflight: the Modal SDK must be installed and
        credentials available.

        :raises click.ClickException: When the SDK is missing or no
            credentials are found.
        """
        _ensure_sdk()
        if not _has_modal_credentials():
            raise click.ClickException(
                "No Modal credentials found. Run `modal token new` to "
                "authenticate (or set MODAL_TOKEN_ID and MODAL_TOKEN_SECRET)."
            )

    def provision(self, name: str) -> str:
        """
        Create a new Modal sandbox under the shared Omnigent App.

        The sandbox is created at Modal's maximum lifetime (24 hours)
        with a ``sleep infinity`` entrypoint so it stays up for the full
        window regardless of exec activity.

        :param name: Human-readable label, e.g. ``"omnigent-host"``.
            Modal sandbox *names* must be unique per App, so the label
            rides a tag instead; the returned id is the canonical
            reference.
        :returns: The sandbox object id, e.g. ``"sb-a1b2c3"``.
        """
        _ensure_sdk()
        import modal

        click.echo(f"▸ Creating Modal sandbox '{name}' (lives at most 24 hours)")
        app = modal.App.lookup(MODAL_APP_NAME, create_if_missing=True)
        image = _build_sandbox_image(self._image_ref)
        secrets = self._resolve_sandbox_secrets()
        handle = modal.Sandbox.create(
            # Entrypoint: hold the sandbox open for its full lifetime;
            # all real work arrives via exec.
            "sleep",
            "infinity",
            app=app,
            image=image,
            timeout=MAX_SANDBOX_LIFETIME_S,
            cpu=_SANDBOX_CPU,
            memory=_SANDBOX_MEMORY_MIB,
            # Workload env: the deployment's harness credentials (LLM
            # keys / gateway URLs) injected from named Modal secrets;
            # None keeps the SDK default when nothing is configured.
            secrets=secrets or None,
        )
        handle.set_tags({"omnigent-name": name})
        self._sandboxes[handle.object_id] = handle
        click.echo(f"  → created {handle.object_id}")
        return handle.object_id

    def attach(self, sandbox_id: str) -> None:
        """
        Validate that an existing sandbox is still running.

        :param sandbox_id: The sandbox to attach to.
        :raises click.ClickException: When the sandbox is missing or
            has terminated (Modal sandboxes live at most 24 hours).
        """
        click.echo(f"▸ Reusing existing Modal sandbox '{sandbox_id}'")
        handle = self._resolve(sandbox_id)
        # poll() returns None while the sandbox is running.
        if handle.poll() is not None:
            raise click.ClickException(
                f"Modal sandbox '{sandbox_id}' has terminated (sandboxes live "
                "at most 24 hours). Create a fresh one with "
                "`omnigent sandbox create --provider modal`."
            )

    def keep_alive(self, sandbox_id: str) -> None:
        """
        Nothing to configure: there is no idle autostop to disable, and
        lifetime is fixed at creation (provision already requests the
        24-hour platform maximum). Surfaces the cap so users aren't
        surprised when the host disappears.

        :param sandbox_id: The sandbox (unused beyond the message;
            present to satisfy the launcher contract).
        """
        click.echo(
            f"  → Modal caps sandbox lifetime at 24 hours; re-run `omnigent "
            f"sandbox create --provider modal` for a fresh host after "
            f"'{sandbox_id}' expires."
        )

    def terminate(self, sandbox_id: str) -> None:
        """
        Terminate a sandbox, releasing its compute.

        Idempotent from the caller's perspective: a sandbox that no
        longer exists (already terminated or aged past the 24h cap) is
        treated as success — the desired end state holds.

        :param sandbox_id: The sandbox to terminate, e.g.
            ``"sb-a1b2c3"``.
        """
        _ensure_sdk()
        import modal

        try:
            handle = modal.Sandbox.from_id(sandbox_id)
        except modal.exception.NotFoundError:
            return
        handle.terminate()
        self._sandboxes.pop(sandbox_id, None)

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """
        Run a shell command in the sandbox and capture its output.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely. ``bash -lc``
            wraps it so login PATH applies.
        :param check: When ``True``, raise on non-zero exit.
        :returns: Exit code plus captured stdout/stderr.
        :raises click.ClickException: If *check* is ``True`` and the
            command exits non-zero.
        """
        handle = self._resolve(sandbox_id)
        process = handle.exec("bash", "-lc", command)
        stdout = process.stdout.read()
        stderr = process.stderr.read()
        returncode = process.wait()
        _echo_lines(stdout)
        _echo_lines(stderr)
        if check and returncode != 0:
            raise click.ClickException(
                f"Remote command failed on sandbox '{sandbox_id}' (exit {returncode}): {command}"
            )
        return RemoteCommandResult(returncode=returncode, stdout=stdout, stderr=stderr)

    def put(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        """
        Copy a local file into the sandbox via Modal's filesystem API.

        :param sandbox_id: Target sandbox.
        :param local_path: Local file to read.
        :param remote_path: Absolute destination path on the sandbox,
            e.g. ``"/tmp/oa-wheels.tgz"`` (the filesystem API does not
            expand ``~``).
        """
        handle = self._resolve(sandbox_id)
        handle.filesystem.copy_from_local(str(local_path), remote_path)

    def stream_exec(self, sandbox_id: str, command: str, *, pty: bool = False) -> RemoteProcess:
        """
        Spawn a command in the sandbox and stream its output line by
        line.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely.
        :param pty: When ``True``, allocate a remote PTY (output arrives
            pre-merged on the terminal stream).
        :returns: Handle over the streaming process.
        """
        handle = self._resolve(sandbox_id)
        # Without a PTY, stdout/stderr arrive on separate streams and
        # the RemoteProcess contract wants combined output — merge
        # in-shell. A PTY already interleaves both.
        remote = command if pty else f"{command} 2>&1"
        process = handle.exec("bash", "-lc", remote, pty=pty)
        return _ModalRemoteProcess(process)

    def exec_foreground(self, sandbox_id: str, command: str) -> int:
        """
        Run *command* in the sandbox, echoing its output to the local
        terminal until it exits; Ctrl-C kills the remote process and
        re-raises.

        Modal's SDK cannot kill an exec'd process through its handle,
        so the remote command records its pid in a pidfile first
        (``echo $$`` then ``exec`` keeps the pid across the swap) and
        the interrupt path issues ``kill`` via a second exec.

        ``TERM`` is forced to ``xterm-256color`` for the same reason as
        the lakebox launcher: native harnesses spawn tmux, which refuses
        to start under a dumb/unset TERM.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely, e.g.
            ``"omnigent host --server https://…"``.
        :returns: The remote command's exit code.
        :raises KeyboardInterrupt: Re-raised after killing the remote
            process when the user detaches with Ctrl-C.
        """
        handle = self._resolve(sandbox_id)
        # Record the pid in a private, unpredictably-named dir under /tmp.
        # `mkdir -m 700` (no -p) fails closed if the path already exists, so a
        # co-tenant on the sandbox can't pre-seed a symlink we'd write through,
        # nor read our pid back, in world-writable /tmp. See
        # :func:`foreground_pidfile` for the shared rationale.
        run_dir, pidfile = foreground_pidfile()
        remote = f"{foreground_record_prefix(pidfile)}TERM=xterm-256color exec {command}"
        process = handle.exec("bash", "-lc", remote, pty=True)
        try:
            for line in process.stdout:
                click.echo(line, nl=False)
            rc = process.wait()
        except KeyboardInterrupt:
            click.echo("\n  → detaching; stopping the remote process")
            # Signal only a numeric pid read back from our own private pidfile,
            # then drop the dir; never feed unvalidated file contents to kill.
            handle.exec("bash", "-c", foreground_kill_command(pidfile)).wait()
            raise
        # Normal exit: drop the run dir so we don't orphan a mode-700
        # dir in /tmp. The interrupt path already cleans up via
        # :func:`foreground_kill_command`.
        handle.exec("bash", "-c", f"rm -rf {run_dir} 2>/dev/null").wait()
        return rc

    def wheel_install_command(self, remote_tgz_path: str) -> str:
        """
        Remote command that overlays the shipped wheels onto the
        prebaked host image — see
        :func:`~omnigent.onboarding.sandboxes.base.host_image_wheel_install_command`
        for the flag rationale.

        :param remote_tgz_path: Sandbox path of the shipped tarball,
            e.g. ``"/tmp/oa-wheels.tgz"``.
        :returns: Shell command string for :meth:`run`.
        """
        return host_image_wheel_install_command(remote_tgz_path)
