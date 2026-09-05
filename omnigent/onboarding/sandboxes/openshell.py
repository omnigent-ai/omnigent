"""
NVIDIA OpenShell sandbox launcher.

Implements :class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher`
for `NVIDIA OpenShell <https://github.com/NVIDIA/openshell>`_ sandboxes
on top of the official ``openshell`` Python SDK. Same posture as the
Modal, Daytona, and CoreWeave launchers: the SDK is an optional
dependency (``uv pip install 'omnigent[openshell]'``) imported lazily, so
the provider can be listed and the module probed without it.

OpenShell is self-hosted: a gateway control plane manages sandbox
lifecycle on a configured compute driver (Docker, Podman, microVM,
Kubernetes), filling the gap for on-prem and air-gapped deployments
where cloud providers are unavailable.

Notes that shape this launcher:

- **gRPC, not REST.** OpenShell's control plane is a gRPC service; the
  ``openshell`` SDK wraps it. The launcher connects through
  :meth:`SandboxClient.from_active_cluster`, which resolves the active
  gateway (its endpoint, TLS material, and OIDC token) from the gateway
  selected by ``openshell gateway select`` — i.e. ``$OPENSHELL_GATEWAY``
  or ``~/.config/openshell/active_gateway``. There is no base-URL knob.
- **Custom host image.** Omnigent boots its prebaked host image, which
  rides in ``SandboxSpec.template.image``. The SDK's public ``create``
  takes only a ``SandboxSpec`` and does not re-export the spec
  protobufs, so the spec is built from the generated ``openshell._proto``
  module — the only path to a non-default image.
- **No file-transfer RPC.** OpenShell exposes command execution but no
  upload primitive, so :meth:`put` streams the file's bytes to ``cat``
  over the exec channel's stdin — the same approach NVIDIA's own
  LangChain backend uses.
- **No local port forwarding.** OpenShell has no local→sandbox port
  forward for the in-sandbox App OAuth callback, so the CLI skips that
  auth step automatically (``supports_local_port_forward = False``).
"""

from __future__ import annotations

import logging
import os
import shlex
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, ClassVar, TypeVar

import click

from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    RemoteCommandResult,
    SandboxLauncher,
    foreground_kill_command,
    foreground_pidfile,
    foreground_record_prefix,
    host_image_wheel_install_command,
    supervise_host_command,
)
from omnigent.onboarding.sandboxes.types import SandboxCapabilities

if TYPE_CHECKING:
    import threading
    from pathlib import Path

    from openshell import ExecResult

_logger = logging.getLogger(__name__)

HOST_IMAGE_ENV_VAR: str = "OMNIGENT_OPENSHELL_HOST_IMAGE"
"""Environment variable overriding :data:`DEFAULT_HOST_IMAGE` for
OpenShell sandboxes."""

SANDBOX_ENV_PASSTHROUGH_ENV_VAR: str = "OMNIGENT_OPENSHELL_SANDBOX_ENV"
"""Comma-separated server-process environment variable names injected
into created OpenShell sandboxes."""

GATEWAY_ENV_VAR: str = "OPENSHELL_GATEWAY"
"""Gateway name read by the SDK's :meth:`SandboxClient.from_active_cluster`;
overrides ``~/.config/openshell/active_gateway``."""

WORKSPACE_ENV_VAR: str = "OMNIGENT_OPENSHELL_WORKSPACE"
"""Workspace name passed to the OpenShell SDK for sandbox lifecycle
operations (create, get, delete, wait_ready). Defaults to ``"default"``."""

_DEFAULT_WORKSPACE: str = "default"

_READY_TIMEOUT_S = 300
_EXEC_TIMEOUT_S = 300
# A foreground host (`omnigent host`) is held open until Ctrl-C, so its
# exec stream must not hit a gRPC deadline mid-session — give it a long
# ceiling. The pidfile records the in-sandbox pid so Ctrl-C can kill the
# remote process (cancelling the local stream doesn't stop it).
_FOREGROUND_TIMEOUT_S = 7 * 24 * 3600

# OpenShell runs the agent as the non-root ``sandbox`` user (its image
# contract; see deploy/docker/Dockerfile), whose home is ``/sandbox``.
# The host image keeps ``WORKDIR /root`` for the root-based providers, so we
# pin every exec's cwd + ``$HOME`` to the sandbox user's writable home here
# rather than changing the shared image -- otherwise ``omnigent host`` resolves
# its config under ``/root`` (unreadable to the sandbox user) and crashes, and
# the managed flow's ``$HOME/workspace`` lands somewhere unwritable.
# ``/home/sandbox`` is denied by the k8s Landlock LSM policy; ``/sandbox`` is
# the permitted path.
_SANDBOX_HOME = "/sandbox"

_T = TypeVar("_T")


def _sdk_supports_resume() -> bool:
    """Whether the installed openshell SDK can drive an in-place resume.

    The gateway's resume primitive is ``SandboxClient.start`` (available
    from openshell 0.0.105). Returns ``False`` when the SDK is missing or
    predates it, so callers never advertise a wake they cannot perform.
    """
    try:
        from openshell import SandboxClient
    except ImportError:
        return False
    return callable(getattr(SandboxClient, "start", None))


def _ensure_sdk() -> None:
    """Verify the openshell SDK is importable, with an install hint when not."""
    try:
        import openshell  # noqa: F401
    except ImportError as exc:
        raise click.ClickException(
            "The openshell SDK is required for the 'openshell' sandbox provider. "
            "Install it with `uv pip install 'omnigent[openshell]'`, then select a "
            "gateway with `openshell gateway select <name>` (or set OPENSHELL_GATEWAY)."
        ) from exc


class _OpenShellClient:
    """Thin wrapper over the ``openshell`` gRPC SandboxClient.

    Owns the launcher's contact with the SDK: it builds the create
    spec, maps the public sandbox name back to the id ``exec`` needs,
    and translates SDK / gRPC errors into ``click.ClickException`` so
    the launcher surface stays clean.
    """

    @property
    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            cli_bootstrap=True,
            managed_launch=True,
            local_port_forward=False,
            # Only an SDK exposing the resume primitive can wake a stopped
            # sandbox; advertising it on an older SDK would promise a wake
            # that always fails.
            resume_stopped=_sdk_supports_resume(),
            # Flips once the gateway's agent-sandbox backend exposes its
            # suspend+snapshot restore through the SDK.
            snapshot_restore=False,
            programmatic_terminate=True,
            file_copy=True,
            streaming_exec=False,
            foreground_exec=True,
        )

    def __init__(self, *, cluster: str | None = None, workspace: str = _DEFAULT_WORKSPACE) -> None:
        _ensure_sdk()
        from openshell import SandboxClient, SandboxError

        try:
            self._client: SandboxClient = SandboxClient.from_active_cluster(cluster=cluster)
        except SandboxError as exc:
            raise click.ClickException(
                f"Could not connect to an OpenShell gateway: {exc}. Select one with "
                "`openshell gateway select <name>` (or set OPENSHELL_GATEWAY)."
            ) from exc
        self._workspace = workspace
        # Petname (public handle) -> opaque sandbox id, which exec needs.
        self._ids: dict[str, str] = {}
        # Daemon threads holding long-lived exec streams open (see
        # exec_background): OpenShell kills an exec's processes when the
        # ExecSandbox RPC returns, so a backgrounded host must be kept on
        # an open stream for its lifetime.
        self._bg_threads: list[threading.Thread] = []

    def close(self) -> None:
        """Release the gRPC channel and any bearer-auth resources."""
        self._client.close()

    def create_sandbox(self, *, image: str, env: dict[str, str]) -> str:
        """Create a sandbox from *image*, wait until ready, return its name."""
        from openshell._proto import openshell_pb2

        spec = openshell_pb2.SandboxSpec(
            template=openshell_pb2.SandboxTemplate(image=image),
            environment=env or {},
        )
        ws = self._workspace
        ref = self._guard(
            "OpenShell sandbox creation failed",
            lambda: self._client.create(workspace=ws, spec=spec),
        )
        ready = self._guard(
            "OpenShell sandbox did not become ready",
            lambda: self._client.wait_ready(
                ref.name, workspace=ws, timeout_seconds=_READY_TIMEOUT_S
            ),
        )
        sandbox_name: str = ready.name
        self._ids[sandbox_name] = ready.id
        return sandbox_name

    def execute(
        self,
        name: str,
        command: Sequence[str],
        *,
        stdin: bytes | None = None,
        timeout: int = _EXEC_TIMEOUT_S,
    ) -> ExecResult:
        """Run *command* (argv) in the sandbox and return its ExecResult."""
        sandbox_id = self._id_for(name)
        return self._guard(
            f"Remote command failed on OpenShell sandbox '{name}'",
            lambda: self._client.exec(
                sandbox_id,
                command,
                stdin=stdin,
                timeout_seconds=timeout,
                workdir=_SANDBOX_HOME,
                env={"HOME": _SANDBOX_HOME},
            ),
        )

    def run_foreground(self, name: str, command: Sequence[str], *, timeout: int) -> int:
        """Stream a command's output to the terminal; return its exit code."""
        import grpc
        from openshell import ExecChunk, ExecResult, SandboxError

        sandbox_id = self._id_for(name)
        exit_code = 0
        try:
            for item in self._client.exec_stream(
                sandbox_id,
                command,
                timeout_seconds=timeout,
                workdir=_SANDBOX_HOME,
                env={"HOME": _SANDBOX_HOME},
            ):
                if isinstance(item, ExecResult):
                    exit_code = int(item.exit_code)
                elif isinstance(item, ExecChunk):
                    click.echo(
                        item.data.decode("utf-8", errors="replace"),
                        nl=False,
                        err=item.stream != "stdout",
                    )
        except (grpc.RpcError, SandboxError) as exc:
            raise click.ClickException(
                f"Foreground command failed on OpenShell sandbox '{name}': {exc}"
            ) from exc
        return exit_code

    def exec_background(self, name: str, command: Sequence[str], *, timeout: int) -> None:
        """
        Start a long-lived foreground command, holding its exec stream open.

        OpenShell terminates an exec's process tree when the ``ExecSandbox``
        RPC returns, so the usual ``setsid nohup … &`` detach (which works on
        Modal/CoreWeave) is reaped immediately. Instead we run the command in
        the FOREGROUND of an ``exec_stream`` drained on a daemon thread: the
        stream stays open for the process's lifetime, so OpenShell keeps it
        alive. The thread ends when the process exits or the sandbox is
        deleted (the stream then errors out).
        """
        import threading

        sandbox_id = self._id_for(name)

        def _pump() -> None:
            try:
                for _ in self._client.exec_stream(
                    sandbox_id,
                    command,
                    timeout_seconds=timeout,
                    workdir=_SANDBOX_HOME,
                    env={"HOME": _SANDBOX_HOME},
                ):
                    pass
            except Exception:
                _logger.debug(
                    "exec_background stream ended for sandbox %s",
                    name,
                    exc_info=True,
                )

        self._bg_threads = [t for t in self._bg_threads if t.is_alive()]
        thread = threading.Thread(target=_pump, name=f"openshell-host-{name}", daemon=True)
        thread.start()
        self._bg_threads.append(thread)

    def resume_sandbox(self, name: str) -> None:
        """Resume a stopped sandbox in place and wait until it is ready again.

        The gateway delegates the actual restart to its compute driver; the
        sandbox keeps its name and persistent volume. The SDK names this
        primitive ``SandboxClient.start`` (available from openshell 0.0.105);
        an SDK predating it gets an actionable error — the server's wake path
        surfaces it without tearing the sandbox down.
        """
        start = getattr(self._client, "start", None)
        if start is None:
            raise click.ClickException(
                f"Could not resume OpenShell sandbox '{name}': the installed "
                "openshell SDK exposes no sandbox resume primitive "
                "(SandboxClient.start; requires openshell>=0.0.105). Upgrade the "
                "openshell package (`uv pip install --upgrade 'omnigent[openshell]'`)."
            )
        ws = self._workspace
        self._guard(
            f"Could not resume OpenShell sandbox '{name}'",
            lambda: start(name, workspace=ws),
        )
        ready = self._guard(
            f"OpenShell sandbox '{name}' did not become ready after resume",
            lambda: self._client.wait_ready(name, workspace=ws, timeout_seconds=_READY_TIMEOUT_S),
        )
        # The resumed instance may carry a fresh opaque id; recache it so
        # subsequent execs reach the live instance.
        self._ids[name] = ready.id

    def get_status(self, name: str) -> None:
        """Resolve a sandbox by name (validates access) and cache its id."""
        ws = self._workspace
        ref = self._guard(
            f"Could not resolve OpenShell sandbox '{name}'",
            lambda: self._client.get(name, workspace=ws),
        )
        self._ids[name] = ref.id

    def delete_sandbox(self, name: str) -> None:
        """Delete a sandbox; a missing sandbox is treated as already gone."""
        import grpc
        from openshell import SandboxError

        try:
            self._client.delete(name, workspace=self._workspace)
        except grpc.RpcError as exc:
            if isinstance(exc, grpc.Call) and exc.code() == grpc.StatusCode.NOT_FOUND:
                self._ids.pop(name, None)
                return
            raise click.ClickException(
                f"Failed to delete OpenShell sandbox '{name}': {exc}"
            ) from exc
        except SandboxError as exc:
            if "not found" in str(exc).lower():
                self._ids.pop(name, None)
                return
            raise click.ClickException(
                f"Failed to delete OpenShell sandbox '{name}': {exc}"
            ) from exc
        self._ids.pop(name, None)

    def _id_for(self, name: str) -> str:
        cached = self._ids.get(name)
        if cached is None:
            ws = self._workspace
            ref = self._guard(
                f"Could not resolve OpenShell sandbox '{name}'",
                lambda: self._client.get(name, workspace=ws),
            )
            cached = ref.id
            self._ids[name] = cached
        return cached

    def _guard(self, message: str, call: Callable[[], _T]) -> _T:
        """Run an SDK *call*, surfacing gRPC / SandboxError as ClickException."""
        import grpc
        from openshell import SandboxError

        try:
            return call()
        except (grpc.RpcError, SandboxError) as exc:
            raise click.ClickException(f"{message}: {exc}") from exc


class OpenShellSandboxLauncher(SandboxLauncher):
    """
    :class:`SandboxLauncher` for NVIDIA OpenShell, over the ``openshell`` SDK.

    Lifecycle and transport map onto the SDK's gRPC client: create /
    wait-ready / delete for the sandbox, command execution for running
    code, and an exec-over-stdin stream for shipping wheels.
    """

    provider: ClassVar[str] = "openshell"
    supports_local_port_forward: ClassVar[bool] = False

    @property
    def capabilities(self) -> SandboxCapabilities:
        """Feature flags, with in-place resume gated on the installed SDK.

        The gateway's compute backend keeps a stopped sandbox and its
        volume, so a dormant managed host is woken in place via
        :meth:`resume` — but only an SDK exposing the resume primitive
        (``SandboxClient.start``, openshell>=0.0.105) can drive the wake.
        Advertising ``resume_stopped`` on an older SDK would render dormant
        hosts as wakeable while every wake fails, so those installs keep
        the honest ``host_offline`` state instead.
        """
        return replace(super().capabilities, resume_stopped=_sdk_supports_resume())

    def __init__(
        self,
        *,
        image: str | None = None,
        env: Sequence[str] | None = None,
        cluster: str | None = None,
        workspace: str | None = None,
    ) -> None:
        """
        :param image: Registry image to provision from
            (``sandbox.openshell.image``); ``None`` resolves
            :data:`HOST_IMAGE_ENV_VAR` then the official host image.
        :param env: Server-process env var NAMES to inject into every
            sandbox (``sandbox.openshell.env``); ``None`` resolves
            :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR`.
        :param cluster: Gateway name to connect to
            (``sandbox.openshell.cluster``); ``None`` lets the SDK
            resolve the active gateway (``$OPENSHELL_GATEWAY`` or
            ``~/.config/openshell/active_gateway``).
        :param workspace: OpenShell workspace for sandbox lifecycle
            (``sandbox.openshell.workspace``); ``None`` resolves
            :data:`WORKSPACE_ENV_VAR` then ``"default"``.
        """
        self._image_ref = image
        self._env_names = tuple(env) if env is not None else None
        self._cluster = cluster
        self._workspace = workspace or os.environ.get(WORKSPACE_ENV_VAR) or _DEFAULT_WORKSPACE
        self._client: _OpenShellClient | None = None

    def prepare(self) -> None:
        """Preflight: the SDK must be installed and a gateway resolvable."""
        _ensure_sdk()
        # Constructing the client resolves the active gateway from on-disk
        # state, failing fast with a remediation hint before remote work.
        self._openshell()

    def provision(self, name: str) -> str:
        """Create a sandbox from the host image and wait until it is ready."""
        image = self._image_ref or os.environ.get(HOST_IMAGE_ENV_VAR) or DEFAULT_HOST_IMAGE
        env_vars = self._resolve_sandbox_env()
        click.echo(f"▸ Creating OpenShell sandbox from {image}")
        # OpenShell assigns its own petname; the requested `name` is advisory.
        sandbox_name = self._openshell().create_sandbox(image=image, env=env_vars)
        click.echo(f"  → created {sandbox_name}")
        return sandbox_name

    def attach(self, sandbox_id: str) -> None:
        """Validate access to an existing OpenShell sandbox by name."""
        click.echo(f"▸ Reusing existing OpenShell sandbox '{sandbox_id}'")
        self._openshell().get_status(sandbox_id)

    def keep_alive(self, sandbox_id: str) -> None:
        """No idle auto-stop management is exposed by the OpenShell API."""
        click.echo(f"  → OpenShell sandbox '{sandbox_id}' remains active until destroyed")

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """Run ``bash -lc <command>`` in the sandbox and capture its output."""
        result = self._openshell().execute(sandbox_id, ["bash", "-lc", command])
        if result.stdout:
            click.echo(result.stdout, nl=False)
        if result.stderr:
            click.echo(result.stderr, nl=False, err=True)
        if check and result.exit_code != 0:
            raise click.ClickException(
                f"Remote command failed on OpenShell sandbox '{sandbox_id}' "
                f"(exit {result.exit_code}): {command}"
            )
        return RemoteCommandResult(
            returncode=result.exit_code, stdout=result.stdout, stderr=result.stderr
        )

    def run_background(
        self, sandbox_id: str, command: str, *, log_path: str = "/tmp/omnigent-host.log"
    ) -> RemoteCommandResult:
        """Hold *command* on a long-lived exec stream instead of detaching.

        OpenShell kills an exec's processes when the RPC returns, so the
        base class's ``setsid nohup`` detach pattern doesn't work. Instead
        the command runs in the foreground of an ``exec_stream`` drained on
        a daemon thread — the stream stays open for the process's lifetime.

        The supervisor wrapper still applies, so a host crash restarts within
        the open stream. A dropped RPC remains fatal here — it kills the
        supervisor along with the host, which no in-sandbox loop can survive.
        """
        script = supervise_host_command(command)
        bg_command = f"{script} >> {log_path} 2>&1 < /dev/null"
        self._openshell().exec_background(
            sandbox_id, ["bash", "-lc", bg_command], timeout=_FOREGROUND_TIMEOUT_S
        )
        return RemoteCommandResult(returncode=0, stdout="launched\n", stderr="")

    def put(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        """Copy a local file into the sandbox by piping its bytes to ``cat``."""
        content = local_path.read_bytes()
        parent = shlex.quote(str(PurePosixPath(remote_path).parent))
        dest = shlex.quote(remote_path)
        result = self._openshell().execute(
            sandbox_id,
            ["bash", "-c", f"mkdir -p {parent} && cat > {dest}"],
            stdin=content,
        )
        if result.exit_code != 0:
            raise click.ClickException(
                f"File upload to OpenShell sandbox '{sandbox_id}' failed "
                f"(exit {result.exit_code}): {result.stderr.strip()}"
            )

    def exec_foreground(self, sandbox_id: str, command: str) -> int:
        """
        Run *command* in the sandbox, streaming its output, until it exits.

        Holds ``omnigent host`` open for ``omnigent sandbox connect``;
        Ctrl-C kills the remote process and re-raises ``KeyboardInterrupt``.
        """
        client = self._openshell()
        # Record the remote pid in a private, unpredictably-named dir under
        # world-writable /tmp: `mkdir -m 700` (no -p) fails closed if the path
        # already exists, so a co-tenant can't pre-seed a symlink we'd write
        # through, nor read our pid back. `echo $$ … && exec` keeps the pid
        # across the shell swap, so cancelling the local gRPC stream (which
        # stops our reads but not the remote command) can still kill it. See
        # :func:`foreground_pidfile` for the shared rationale.
        run_dir, pidfile = foreground_pidfile()
        remote = f"{foreground_record_prefix(pidfile)}exec {command}"
        try:
            rc = client.run_foreground(
                sandbox_id, ["bash", "-lc", remote], timeout=_FOREGROUND_TIMEOUT_S
            )
        except KeyboardInterrupt:
            click.echo("\n  → detaching; stopping the remote process")
            # Signal only a numeric pid read back from our own private pidfile,
            # then drop the dir; never feed unvalidated file contents to kill.
            client.execute(
                sandbox_id,
                ["bash", "-lc", foreground_kill_command(pidfile)],
            )
            raise
        # Normal exit: drop the run dir so we don't orphan a mode-700 dir in
        # /tmp. The interrupt path already cleans up via
        # :func:`foreground_kill_command`.
        client.execute(sandbox_id, ["bash", "-c", f"rm -rf {run_dir} 2>/dev/null"])
        return rc

    def wheel_install_command(self, remote_tgz_path: str) -> str:
        """Overlay shipped wheels onto the prebaked host image."""
        return host_image_wheel_install_command(remote_tgz_path)

    def resume(self, sandbox_id: str) -> None:
        """Resume a stopped OpenShell sandbox in place, keeping name + volume."""
        click.echo(f"▸ Resuming OpenShell sandbox '{sandbox_id}'")
        self._openshell().resume_sandbox(sandbox_id)
        click.echo(f"  → resumed {sandbox_id}")

    def terminate(self, sandbox_id: str) -> None:
        """Delete a sandbox, releasing its compute."""
        try:
            self._openshell().delete_sandbox(sandbox_id)
        finally:
            if self._client is not None:
                self._client.close()
                self._client = None

    def close(self) -> None:
        """Release the underlying gRPC channel, if one was opened."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def __del__(self) -> None:
        self.close()

    def _openshell(self) -> _OpenShellClient:
        if self._client is None:
            self._client = _OpenShellClient(cluster=self._cluster, workspace=self._workspace)
        return self._client

    def _resolve_sandbox_env(self) -> dict[str, str]:
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
                    f"sandbox env passthrough lists '{name}' but it is not set "
                    "in the server's environment — set it (or remove it from "
                    f"sandbox.openshell.env / {SANDBOX_ENV_PASSTHROUGH_ENV_VAR})."
                )
            resolved[name] = value
        return resolved
