"""
Tenki sandbox launcher.

Implements :class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher`
for `Tenki <https://tenki.cloud>`_ sandboxes on top of the official
``tenki-sandbox`` Python SDK. Same posture as the Modal, Daytona, E2B,
and CoreWeave launchers: the SDK is an optional dependency
(``pip install 'omnigent[tenki]'``) imported lazily, so the provider can
be listed and the module probed without it.

Supports both server-managed hosts (``host_type="managed"`` sessions)
and the CLI bootstrap flow. The one unsupported primitive is
``forward_local_port``: Tenki only exposes sandbox ports OUTWARD via a
public URL (``sandbox.expose_port``) — there is no local→sandbox path —
so the in-sandbox Databricks App OAuth flow doesn't apply and managed
hosts authenticate with a server-minted launch token instead.

Notes that shape this launcher:

- **Prepared image required.** A Tenki session boots from a registry
  image reference (``<workspace>/<name>:tag``); the CLI bootstrap only
  overlays the locally-built wheels with ``pip install --no-deps`` and
  the managed flow ships no wheels at all, so the Omnigent host and its
  dependencies must already live in the image. Users build and publish a
  Tenki template with the host baked in (see ``deploy/tenki/README.md``)
  and name it via the ``image`` field / :data:`IMAGE_ENV_VAR`. Registry
  refs are workspace-scoped, so there is no universal default —
  :meth:`provision` fails fast when no image is configured.
- **Networking off by default (SDK).** Tenki's SDK does not enable
  networking unless asked, so :meth:`provision` passes
  ``allow_outbound=True`` explicitly (the host must dial back and clone)
  and ``allow_inbound=False`` (nothing here needs inbound).
- **API-key auth.** ``TENKI_API_KEY`` (or ``TENKI_AUTH_TOKEN``) is read
  from the CLI/server process environment by the SDK, 12-factor — like
  the other providers' keys.
"""

from __future__ import annotations

import contextlib
import os
import queue
import threading
from typing import TYPE_CHECKING, ClassVar

import click

from omnigent.inner import ui
from omnigent.onboarding.sandboxes.base import (
    RemoteCommandResult,
    RemoteProcess,
    SandboxLauncher,
    host_image_wheel_install_command,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence
    from pathlib import Path

    from tenki_sandbox import Client, Sandbox
    from tenki_sandbox.process import Process


# ── Constants ──────────────────────────────────────────

API_KEY_ENV_VAR: str = "TENKI_API_KEY"
"""Tenki API key, read from the CLI/server process environment by the SDK.
Create one at https://tenki.cloud."""

AUTH_TOKEN_ENV_VAR: str = "TENKI_AUTH_TOKEN"
"""Alternate credential the SDK accepts (checked before
:data:`API_KEY_ENV_VAR` in its resolution order)."""

BASE_URL_ENV_VAR: str = "TENKI_API_ENDPOINT"
"""Environment variable overriding the Tenki API endpoint. Read by the
SDK; the ``base_url`` constructor arg / ``sandbox.tenki.base_url`` config
takes precedence."""

IMAGE_ENV_VAR: str = "OMNIGENT_TENKI_IMAGE"
"""Environment variable naming the Tenki registry image reference
(``<workspace>/<name>:tag``) the Omnigent host was baked into (see
``deploy/tenki/README.md``). ``sandbox.tenki.image`` takes precedence;
there is no default because registry refs are workspace-scoped."""

PROJECT_ENV_VAR: str = "OMNIGENT_TENKI_PROJECT"
"""Environment variable naming the Tenki project id sessions are created
in. The service requires an explicit project unless the API key is itself
project-scoped, so this (or ``sandbox.tenki.project``, which takes
precedence) is needed when using an account-wide key."""

WORKSPACE_ENV_VAR: str = "OMNIGENT_TENKI_WORKSPACE"
"""Environment variable naming the Tenki workspace id sessions are created
in. Optional; ``sandbox.tenki.workspace`` takes precedence."""

SANDBOX_ENV_PASSTHROUGH_ENV_VAR: str = "OMNIGENT_TENKI_SANDBOX_ENV"
"""Comma-separated server-process environment variable NAMES whose
values are injected into every sandbox this launcher creates — typically
the harness LLM credentials (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``,
gateway base URLs, …) and ``GIT_TOKEN`` that the in-sandbox host forwards
to runners. Names, not values: the values are read from the server's own
environment at provision time, so secrets never live in config files.
The server's managed-host config (``sandbox.tenki.env``) takes precedence
when set."""

# Keep-alive extends a live session by this much (Tenki's extend() is
# additive). One day mirrors the other providers' generous lifetime and
# is re-applied on every reconnect through the bootstrap.
_KEEP_ALIVE_EXTEND_S: int = 24 * 60 * 60

# Bounded timeout (seconds) for reaping a streaming process on close(): a
# best-effort SDK wait plus a join of the reader threads, so a cancelled
# foreground attach doesn't return before teardown yet also can't hang on a
# stalled transport.
_CLOSE_REAP_TIMEOUT_S: float = 5.0


def _ensure_sdk() -> None:
    """
    Verify the Tenki SDK is importable, with an install hint when not.

    Called at the top of every launcher entry point because the SDK is
    an optional dependency — the base ``omnigent`` install does not
    pull it in.

    :raises click.ClickException: When the ``tenki-sandbox`` package is
        not installed.
    """
    try:
        import tenki_sandbox  # noqa: F401  # presence probe only
    except ImportError as exc:
        raise click.ClickException(
            "The Tenki SDK is required for the 'tenki' sandbox provider. "
            "Install it with `pip install 'omnigent[tenki]'`, then set "
            "TENKI_API_KEY (create a key at https://tenki.cloud)."
        ) from exc


def _echo_lines(stream: str, *, err: bool = False) -> None:
    """
    Echo a captured remote output stream line-by-line, dropping
    pure-whitespace lines.

    :param stream: Captured stdout or stderr from a remote command.
    :param err: When ``True``, write to stderr (used for the captured
        stderr stream).
    """
    for line in stream.splitlines():
        if line.strip():
            click.echo(line, err=err)


class _TenkiRemoteProcess(RemoteProcess):
    """
    Thread-backed :class:`RemoteProcess` over a Tenki streaming command.

    Tenki's :class:`~tenki_sandbox.process.Process` exposes stdout and
    stderr as two separate byte-chunk streams, so a reader thread per
    stream splits chunks into lines and feeds one queue — the queue is
    the combined-output stream the :class:`RemoteProcess` contract wants.
    A transport failure surfaces through :meth:`wait` (which re-raises the
    SDK error), so the drain threads swallow it and simply stop.
    """

    def __init__(self, process: Process) -> None:
        """
        Wrap a running streaming process.

        :param process: Handle returned by ``sandbox.start(...)``.
        """
        self._process = process
        self._lines: queue.Queue[str | None] = queue.Queue()
        # Materialize the iterator once so repeated `lines` reads resume
        # the same stream (the RemoteProcess contract).
        self._line_iter: Iterator[str] = self._iter_lines()
        self._pending_lock = threading.Lock()
        self._pending = 2  # one sentinel emitted once both readers finish
        self._readers = [
            threading.Thread(
                target=self._drain, args=(process.stdout,), name="tenki-remote-stdout", daemon=True
            ),
            threading.Thread(
                target=self._drain, args=(process.stderr,), name="tenki-remote-stderr", daemon=True
            ),
        ]
        for reader in self._readers:
            reader.start()

    @property
    def lines(self) -> Iterator[str]:
        """
        Iterator over the command's combined stdout/stderr lines (same
        object on every access).

        :returns: Line iterator draining the shared output queue.
        """
        return self._line_iter

    def wait(self) -> int:
        """
        Block until the command finishes and return its exit code.

        :returns: The command's exit code.
        :raises click.ClickException: When the command failed to run at
            the transport level (as opposed to merely exiting non-zero,
            which Tenki reports as a normal result).
        """
        for reader in self._readers:
            reader.join()
        try:
            result = self._process.wait()
        except Exception as exc:
            # A transport failure (session gone, daemon outage) surfaces
            # from wait(); the drain threads already stopped on it.
            raise click.ClickException(str(exc)) from exc
        return int(result.exit_code)

    def close(self) -> None:
        """
        Terminate the command if it is still running and reap it.

        Tenki's ``Process.kill`` only QUEUES a SIG_KILL that the SDK's
        pump thread delivers, so close() bounded-reaps after issuing it:
        a best-effort ``wait(timeout=…)`` on the SDK process (which joins
        the SDK's pump thread) plus a bounded join of the two stream
        readers. A caller that cancels mid-stream therefore doesn't
        return before the remote process and local threads are torn down,
        while the bounds prevent a hang on a stalled transport. Idempotent
        and best-effort: every step is suppressed / safe to repeat, and
        close() never raises.
        """
        with contextlib.suppress(Exception):
            self._process.kill()
        # Reap through the SDK's own wait() (it joins the pump thread); do
        # NOT call this wrapper's wait(), which joins the reader threads
        # first and could hang if transport shutdown stalls.
        with contextlib.suppress(Exception):
            self._process.wait(timeout=_CLOSE_REAP_TIMEOUT_S)
        for reader in self._readers:
            reader.join(timeout=_CLOSE_REAP_TIMEOUT_S)

    def _drain(self, stream: Iterable[bytes]) -> None:
        """Split one byte stream into lines, feeding them into the queue."""
        buffer = b""
        try:
            for chunk in stream:
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    self._lines.put(line.decode(errors="replace") + "\n")
        except Exception:
            # A transport error is surfaced by process.wait(); stop draining.
            pass
        finally:
            if buffer:
                self._lines.put(buffer.decode(errors="replace") + "\n")
            with self._pending_lock:
                self._pending -= 1
                if self._pending == 0:
                    self._lines.put(None)

    def _iter_lines(self) -> Iterator[str]:
        """Yield queued output lines until the terminating sentinel."""
        while True:
            item = self._lines.get()
            if item is None:
                return
            yield item


class TenkiSandboxLauncher(SandboxLauncher):
    """
    :class:`SandboxLauncher` for Tenki sandboxes, over the
    ``tenki-sandbox`` SDK.

    All transport rides the SDK: ``Client.create`` / ``Client.get`` /
    ``Sandbox.close`` for lifecycle, ``sandbox.exec`` for commands (a
    ``bash -lc`` wrap applies login PATH), ``sandbox.fs.upload`` for file
    shipping, and ``sandbox.start`` for the foreground attach. Handles are
    cached per sandbox id to avoid a server round-trip on every primitive.
    """

    provider: ClassVar[str] = "tenki"
    # Tenki exposes sandbox ports OUTWARD only (expose_port → public URL);
    # there is no local→sandbox path for the App OAuth callback.
    supports_local_port_forward: ClassVar[bool] = False

    def __init__(
        self,
        *,
        image: str | None = None,
        env: Sequence[str] | None = None,
        base_url: str | None = None,
        project: str | None = None,
        workspace: str | None = None,
        vcpus: int | None = None,
        memory_mb: int | None = None,
        disk_gb: int | None = None,
    ) -> None:
        """
        Initialize the launcher.

        :param image: Tenki registry image reference the Omnigent host
            was baked into (``<workspace>/<name>:tag``) — the server's
            managed-host ``sandbox.tenki.image`` config. ``None`` resolves
            :data:`IMAGE_ENV_VAR`; there is no built-in default (registry
            refs are workspace-scoped), so :meth:`provision` fails fast
            when neither is set.
        :param env: Optional names of server-process environment variables
            to inject into every sandbox, e.g. ``["OPENAI_API_KEY",
            "GIT_TOKEN"]`` — the server's managed-host ``sandbox.tenki.env``
            config. ``None`` resolves :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR`
            (comma-separated) and falls back to no injected env.
        :param base_url: Optional Tenki API endpoint override. ``None``
            lets the SDK resolve :data:`BASE_URL_ENV_VAR` / its default.
        :param project: Optional Tenki project id sessions are created in
            — the server's ``sandbox.tenki.project`` config. Required when
            the API key is not itself project-scoped (the service rejects
            create with "project_id is required"). ``None`` resolves
            :data:`PROJECT_ENV_VAR`.
        :param workspace: Optional Tenki workspace id — the server's
            ``sandbox.tenki.workspace`` config. ``None`` resolves
            :data:`WORKSPACE_ENV_VAR`.
        :param vcpus: Optional vCPU count (Tenki bounds: 1–16).
        :param memory_mb: Optional memory allocation in MiB (128–65536).
        :param disk_gb: Optional root disk size in GiB (5–100).
        """
        self._image_ref = image
        self._env_names = tuple(env) if env is not None else None
        self._base_url = base_url
        self._project = project
        self._workspace = workspace
        self._vcpus = vcpus
        self._memory_mb = memory_mb
        self._disk_gb = disk_gb
        self._client: Client | None = None
        self._sandboxes: dict[str, Sandbox] = {}

    def _client_lazy(self) -> Client:
        """
        Return the shared Tenki client, constructing it on first use.

        :returns: The client (auth + base URL resolved from the
            constructor / environment by the SDK).
        :raises click.ClickException: When the SDK is missing or no
            credentials are available.
        """
        _ensure_sdk()
        if self._client is None:
            from tenki_sandbox import Client
            from tenki_sandbox.errors import MissingAuthTokenError

            try:
                self._client = Client(base_url=self._base_url)
            except MissingAuthTokenError as exc:
                raise click.ClickException(
                    "No Tenki credentials found. Create an API key at "
                    "https://tenki.cloud and set TENKI_API_KEY."
                ) from exc
        return self._client

    def _resolved_image(self) -> str | None:
        """
        Resolve the Tenki image reference: explicit constructor value
        wins, then :data:`IMAGE_ENV_VAR`. No built-in default.

        :returns: The image reference, or ``None`` when none is
            configured.
        """
        return self._image_ref or os.environ.get(IMAGE_ENV_VAR) or None

    def _resolved_project(self) -> str | None:
        """Resolve the Tenki project id: ctor value → :data:`PROJECT_ENV_VAR`."""
        return self._project or os.environ.get(PROJECT_ENV_VAR) or None

    def _resolved_workspace(self) -> str | None:
        """Resolve the Tenki workspace id: ctor value → :data:`WORKSPACE_ENV_VAR`."""
        return self._workspace or os.environ.get(WORKSPACE_ENV_VAR) or None

    def _resolve(self, sandbox_id: str) -> Sandbox:
        """
        Return the cached handle for *sandbox_id*, fetching it on first
        use.

        :param sandbox_id: Tenki session id.
        :returns: The sandbox handle.
        :raises click.ClickException: When the SDK is not installed or the
            session does not exist (e.g. terminated).
        """
        _ensure_sdk()
        from tenki_sandbox.errors import SessionNotFoundError

        handle = self._sandboxes.get(sandbox_id)
        if handle is None:
            client = self._client_lazy()
            try:
                handle = client.get(sandbox_id)
            except SessionNotFoundError as exc:
                raise click.ClickException(
                    f"Tenki sandbox '{sandbox_id}' not found — it may have been "
                    "terminated. Managed sessions provision a replacement on the "
                    "next message; for a CLI host create a fresh one with "
                    "`omnigent sandbox create --provider tenki`."
                ) from exc
            self._sandboxes[sandbox_id] = handle
        return handle

    def _resolve_sandbox_env(self) -> dict[str, str]:
        """
        Resolve the env vars to inject into created sandboxes.

        Explicit constructor names win; otherwise
        :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR` (comma-separated)
        applies; an empty resolution injects nothing. Values come from
        the server's own environment — a configured name that is unset
        there fails loud (an operator listed a credential the deployment
        never provided; silently launching without it would surface much
        later as an opaque harness auth failure).

        :returns: Name → value mapping for ``Client.create(env=…)``.
        :raises click.ClickException: When a configured name is not set
            in the server process environment.
        """
        if self._env_names is not None:
            names: Sequence[str] = self._env_names
        else:
            names = [
                name.strip()
                for name in os.environ.get(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, "").split(",")
                if name.strip()
            ]
        resolved: dict[str, str] = {}
        for name in names:
            value = os.environ.get(name)
            if value is None:
                raise click.ClickException(
                    f"sandbox env passthrough names '{name}' but it is not set "
                    "in the server's environment — set it (or remove it from "
                    f"sandbox.tenki.env / {SANDBOX_ENV_PASSTHROUGH_ENV_VAR})."
                )
            resolved[name] = value
        return resolved

    def prepare(self) -> None:
        """
        Local preflight: the Tenki SDK must be installed and a credential
        available.

        :raises click.ClickException: When the SDK is missing or neither
            ``TENKI_API_KEY`` nor ``TENKI_AUTH_TOKEN`` is set.
        """
        _ensure_sdk()
        if not (os.environ.get(API_KEY_ENV_VAR) or os.environ.get(AUTH_TOKEN_ENV_VAR)):
            raise click.ClickException(
                "No Tenki credentials found. Create an API key at "
                "https://tenki.cloud and set TENKI_API_KEY."
            )

    def provision(self, name: str) -> str:
        """
        Create a new Tenki sandbox from the configured host image.

        The session is created with outbound networking enabled (the host
        dials back and clones) and inbound disabled, and creation waits
        for the ``RUNNING`` state. The sandbox lives until the managed-
        session machinery terminates it.

        :param name: Human-readable label, e.g. ``"managed-a1b2c3d4"``.
        :returns: The Tenki session id.
        :raises click.ClickException: If no image is configured or
            provisioning fails.
        """
        image = self._resolved_image()
        if image is None:
            raise click.ClickException(
                "No Tenki host image configured. The Omnigent host must be baked "
                "into a Tenki template/registry image first (see "
                "deploy/tenki/README.md), then named via sandbox.tenki.image or "
                f"{IMAGE_ENV_VAR}."
            )
        _ensure_sdk()
        from tenki_sandbox.errors import SandboxError

        env_vars = self._resolve_sandbox_env()
        client = self._client_lazy()
        click.echo(f"▸ Creating Tenki sandbox '{name}' from image '{image}'")
        try:
            sandbox = client.create(
                name=name,
                image=image,
                project_id=self._resolved_project(),
                workspace_id=self._resolved_workspace(),
                cpu_cores=self._vcpus,
                memory_mb=self._memory_mb,
                disk_size_gb=self._disk_gb,
                env=env_vars or None,
                allow_outbound=True,
                allow_inbound=False,
                wait=True,
            )
        except SandboxError as exc:
            # SDK boundary: surface the provider's reason (quota, bad
            # resources, missing image, …) as the launcher-contract error
            # type so the managed-launch 502 carries it verbatim.
            raise click.ClickException(f"Tenki sandbox creation failed: {exc}") from exc
        sandbox_id = str(sandbox.id)
        self._sandboxes[sandbox_id] = sandbox
        click.echo(f"  → created {sandbox_id}")
        return sandbox_id

    def attach(self, sandbox_id: str) -> None:
        """
        Validate that an existing sandbox is still running.

        :param sandbox_id: The sandbox to attach to.
        :raises click.ClickException: When the sandbox is missing or is
            not in the ``RUNNING`` state.
        """
        click.echo(f"▸ Reusing existing Tenki sandbox '{sandbox_id}'")
        sandbox = self._resolve(sandbox_id)
        if sandbox.state != "RUNNING":
            raise click.ClickException(
                f"Tenki sandbox '{sandbox_id}' is not running (state {sandbox.state}). "
                "Create a fresh one with `omnigent sandbox create --provider tenki`."
            )

    def keep_alive(self, sandbox_id: str) -> None:
        """
        Extend the sandbox lifetime so long agent runs don't lose their
        host.

        Tenki's ``extend`` is additive; soft-fail per the launcher
        contract — a rejected extension (e.g. a session with no lifetime
        cap to extend) warns rather than aborting the bootstrap.

        :param sandbox_id: The sandbox to configure.
        """
        _ensure_sdk()
        from tenki_sandbox.errors import SandboxError

        sandbox = self._resolve(sandbox_id)
        try:
            sandbox.extend(_KEEP_ALIVE_EXTEND_S)
        except SandboxError as exc:
            ui.console.print(
                f"  → warning: could not extend the lifetime of '{sandbox_id}' "
                f"({exc}); the sandbox will stop at its current timeout.",
                style="omni.warning",
                markup=False,
            )
        else:
            click.echo(f"  → requested a {_KEEP_ALIVE_EXTEND_S // 3600}h lifetime extension.")

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """
        Run a shell command in the sandbox and capture its output.

        ``bash -lc`` wraps the command so login PATH applies, and no
        per-command timeout is set (``timeout=None``) so installs / clones
        aren't killed mid-run.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely.
        :param check: When ``True``, raise on non-zero exit.
        :returns: Exit code plus captured stdout/stderr.
        :raises click.ClickException: If the command could not be
            executed, or *check* is ``True`` and it exited non-zero.
        """
        _ensure_sdk()
        from tenki_sandbox.errors import SandboxError

        sandbox = self._resolve(sandbox_id)
        try:
            # timeout=None disables any per-command cap; exec returns the
            # result even on non-zero exit (it does not raise), so check is
            # applied below.
            result = sandbox.exec("bash", "-lc", command, timeout=None)
        except SandboxError as exc:
            # SDK boundary: a stopped/deleted sandbox or daemon outage must
            # surface its provider reason through the launcher contract.
            raise click.ClickException(
                f"Remote command failed to execute on sandbox '{sandbox_id}': {exc}"
            ) from exc
        returncode = result.exit_code
        stdout, stderr = result.stdout_text, result.stderr_text
        _echo_lines(stdout)
        _echo_lines(stderr, err=True)
        if check and returncode != 0:
            raise click.ClickException(
                f"Remote command failed on sandbox '{sandbox_id}' (exit {returncode}): {command}"
            )
        return RemoteCommandResult(returncode=returncode, stdout=stdout, stderr=stderr)

    def put(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        """
        Copy a local file into the sandbox via the SDK's filesystem API.

        :param sandbox_id: Target sandbox.
        :param local_path: Local file to read.
        :param remote_path: Absolute destination path on the sandbox,
            e.g. ``"/tmp/oa-wheels.tgz"``.
        :raises click.ClickException: If the transfer fails.
        """
        _ensure_sdk()
        from tenki_sandbox.errors import SandboxError

        sandbox = self._resolve(sandbox_id)
        try:
            sandbox.fs.upload(str(local_path), remote_path)
        except SandboxError as exc:
            raise click.ClickException(
                f"File upload to sandbox '{sandbox_id}' failed: {exc}"
            ) from exc

    def stream_exec(self, sandbox_id: str, command: str, *, pty: bool = False) -> RemoteProcess:
        """
        Spawn a command in the sandbox and stream its combined output
        line by line.

        Tenki delivers stdout and stderr as separate byte streams; the
        wrapping :class:`_TenkiRemoteProcess` routes both into one queue,
        so the *pty* flag is unused — the output is already combined
        either way.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely.
        :param pty: Accepted for the contract; unused (see above).
        :returns: Handle over the streaming process.
        :raises click.ClickException: When the command cannot be started.
        """
        del pty  # unused (see docstring)
        _ensure_sdk()
        from tenki_sandbox.errors import SandboxError

        sandbox = self._resolve(sandbox_id)
        try:
            process = sandbox.start("bash", "-lc", command)
        except SandboxError as exc:
            raise click.ClickException(
                f"Could not start a streaming command on sandbox '{sandbox_id}': {exc}"
            ) from exc
        return _TenkiRemoteProcess(process)

    def exec_foreground(self, sandbox_id: str, command: str) -> int:
        """
        Run *command* in the sandbox, echoing its output to the local
        terminal until it exits; Ctrl-C kills the remote process and
        re-raises.

        ``TERM`` is forced to ``xterm-256color`` for the same reason as
        the other launchers: native harnesses spawn tmux, which refuses
        to start under a dumb/unset TERM. ``exec`` replaces the wrapping
        shell so the streamed command's own exit code is reported.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely, e.g.
            ``"omnigent host --server https://…"``.
        :returns: The remote command's exit code.
        :raises KeyboardInterrupt: Re-raised after killing the remote
            process when the user detaches with Ctrl-C.
        """
        process = self.stream_exec(sandbox_id, f"TERM=xterm-256color exec {command}", pty=True)
        try:
            for line in process.lines:
                click.echo(line, nl=False)
            return process.wait()
        except KeyboardInterrupt:
            click.echo("\n  → detaching; stopping the remote process")
            process.close()
            raise

    def wheel_install_command(self, remote_tgz_path: str) -> str:
        """
        Remote command that overlays the shipped wheels onto the host
        image — see
        :func:`~omnigent.onboarding.sandboxes.base.host_image_wheel_install_command`
        for the flag rationale. Applies because the Tenki image is built
        FROM the prebaked host image (omnigent ``0.1.0`` baked).

        :param remote_tgz_path: Sandbox path of the shipped tarball,
            e.g. ``"/tmp/oa-wheels.tgz"``.
        :returns: Shell command string for :meth:`run`.
        """
        return host_image_wheel_install_command(remote_tgz_path)

    def terminate(self, sandbox_id: str) -> None:
        """
        Terminate a sandbox, releasing its compute.

        Idempotent from the caller's perspective: a sandbox that no longer
        exists or is already terminating/terminated is treated as success
        — the desired end state holds.

        :param sandbox_id: The sandbox to terminate.
        :raises click.ClickException: If termination fails for a reason
            other than the sandbox already being gone.
        """
        _ensure_sdk()
        from tenki_sandbox.errors import (
            InvalidStateError,
            SandboxError,
            SessionNotFoundError,
            SessionTerminatedError,
        )

        handle = self._sandboxes.get(sandbox_id)
        try:
            # Resolve directly (not via _resolve, which wraps not-found in a
            # CLI-facing hint) so an already-gone sandbox is a clean no-op.
            if handle is None:
                handle = self._client_lazy().get(sandbox_id)
            handle.close_if_open()
        except (SessionNotFoundError, SessionTerminatedError, InvalidStateError):
            pass  # already gone / terminating — success
        except SandboxError as exc:
            raise click.ClickException(
                f"Tenki sandbox termination failed for '{sandbox_id}': {exc}"
            ) from exc
        finally:
            self._sandboxes.pop(sandbox_id, None)
