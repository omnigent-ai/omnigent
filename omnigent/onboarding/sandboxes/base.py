"""
Provider-agnostic interface for running Omnigent hosts in remote sandboxes.

A *sandbox launcher* wraps one sandbox provider (Databricks Lakebox, Modal,
Daytona, …) behind the small set of transport / lifecycle primitives that the
generic bootstrap flow in :mod:`omnigent.onboarding.sandboxes.bootstrap`
composes: provision a sandbox, run commands in it, ship files into it, stream
a PTY-backed process out of it, forward a local port into it, and hold a
foreground process open. Everything provider-specific (CLI bootstrap, SSH
quirks, image contents, pip flags) lives behind a :class:`SandboxLauncher`
implementation; everything provider-agnostic (wheel builds, the in-sandbox
App OAuth dance, host registration) lives in ``bootstrap``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

import click

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


DEFAULT_HOST_IMAGE: str = "ghcr.io/omnigent-ai/omnigent-host:latest"
"""Default sandbox image across providers: the official prebaked
Omnigent host image, published by CI from the ``host`` target of
``deploy/docker/Dockerfile`` (``:latest`` tracks main; ``:sha-<short>``
pins a commit). It bakes the full omnigent install plus git / tmux /
curl and the coding-harness CLIs, so sandbox creation skips the
in-sandbox dependency install. Providers layer their own override
mechanisms (env var / server config) on top of this default."""


def host_image_wheel_install_command(remote_tgz_path: str) -> str:
    """
    Build the remote shell command that overlays locally-built wheels
    onto a sandbox booted from the prebaked host image
    (:data:`DEFAULT_HOST_IMAGE`).

    Shared by every launcher whose sandboxes boot from that image
    (Modal, Daytona): the right pip flags are a property of the image,
    not the provider.

    ``--force-reinstall`` is required because the host image bakes
    omnigent at the same ``0.1.0`` version. Without it, pip sees the
    version satisfied and silently skips, leaving the sandbox on the
    baked code while the CLI reports success.

    ``--no-deps`` skips the (already baked) dependency tree, so the
    overlay is just the three local wheels. A local checkout that adds
    a brand-new dependency surfaces as ImportError at runtime until
    the official image rebuilds with it (next main commit) — one-time
    manual pip-install of that package per affected sandbox in the
    meantime.

    The image's venv pip is first on PATH, so the install lands in the
    venv and entry points stay in ``/opt/venv/bin``.

    :param remote_tgz_path: Sandbox path of the shipped tarball, e.g.
        ``"/tmp/oa-wheels.tgz"``.
    :returns: Shell command string for :meth:`SandboxLauncher.run`.
    """
    return (
        "cd /tmp && rm -rf oa-wheels && mkdir oa-wheels && "
        f"tar xzf {remote_tgz_path} -C oa-wheels --warning=no-unknown-keyword && "
        "pip install --quiet --force-reinstall --no-deps "
        "--no-warn-script-location oa-wheels/*.whl"
    )


class SandboxCapabilityError(click.ClickException):
    """
    Raised when a launcher does not support an optional primitive.

    The only optional primitive today is
    :meth:`SandboxLauncher.forward_local_port` — providers without a
    local-to-sandbox forwarding path (e.g. Modal) raise this, and the
    OAuth flow surfaces the message (which should name the ``--no-auth``
    escape hatch) to the user.
    """


@dataclass
class RemoteCommandResult:
    """
    Outcome of a command run inside a sandbox via
    :meth:`SandboxLauncher.run`.

    :param returncode: The remote command's exit code, e.g. ``0``.
    :param stdout: Captured standard output. Providers that merge the
        two streams put the combined output here.
    :param stderr: Captured standard error; empty for providers that
        merge streams into ``stdout``.
    """

    returncode: int
    stdout: str
    stderr: str


class RemoteProcess(ABC):
    """
    A streaming remote process spawned by
    :meth:`SandboxLauncher.stream_exec`.

    Callers interleave reads of :attr:`lines` with control calls — the
    OAuth flow reads lines until it finds the verification URL, opens a
    port forward, then keeps reading the same stream until the process
    exits.
    """

    @property
    @abstractmethod
    def lines(self) -> Iterator[str]:
        """
        Line iterator over the process's combined stdout/stderr.

        Repeated accesses MUST return the same underlying iterator so a
        caller can consume a few lines, do other work, and resume the
        stream where it left off.

        :returns: Iterator yielding output lines (trailing newlines
            included, matching ``subprocess.Popen`` text-mode streams).
        """

    @abstractmethod
    def wait(self) -> int:
        """
        Block until the process exits.

        :returns: The process's exit code, e.g. ``0``.
        """

    @abstractmethod
    def close(self) -> None:
        """
        Terminate the process if it is still running and reap it.

        Idempotent: safe to call after :meth:`wait` returned or after a
        prior ``close``.
        """


class SandboxLauncher(ABC):
    """
    Transport + lifecycle primitives for one sandbox provider.

    Implementations exist per provider (``LakeboxLauncher``, …) and are
    resolved by name through
    :func:`omnigent.onboarding.sandboxes.get_launcher`. All methods
    raise ``click.ClickException`` (with a remediation hint) on failure
    so CLI callers surface clean errors without extra wrapping.
    """

    # Short provider name used in CLI ``--provider`` choices and error
    # messages, e.g. ``"lakebox"``.
    provider: ClassVar[str]

    # Package index URL exported as ``UV_INDEX_URL`` for the local wheel
    # build, or ``None`` to use ambient uv configuration. Providers tied
    # to networks where public PyPI is unreachable (Lakebox on the
    # Databricks corp network) override this.
    wheel_build_index_url: ClassVar[str | None] = None

    # Whether this provider can bridge a local port into the sandbox
    # (``ssh -L`` semantics). The in-sandbox App OAuth flow requires it;
    # the bootstrap checks this flag BEFORE doing any remote work so
    # providers without the capability (e.g. Modal) fail fast with the
    # ``--no-auth`` hint instead of erroring mid-flow.
    supports_local_port_forward: ClassVar[bool] = False

    # Whether this provider supports the CLI bootstrap flow
    # (``omnigent sandbox create`` / ``connect``: wheel shipping via
    # ``put`` + ``wheel_install_command``, streaming attach via
    # ``stream_exec`` / ``exec_foreground``). Managed-only providers
    # (e.g. Daytona) implement just the server-managed subset
    # (``prepare`` / ``provision`` / ``run`` / ``terminate``); the CLI
    # checks this flag up front so they fail with a pointer to
    # ``host_type="managed"`` instead of a mid-flow capability error.
    supports_cli_bootstrap: ClassVar[bool] = True

    @abstractmethod
    def prepare(self) -> None:
        """
        Run local preflight: install/verify provider tooling and
        credentials on the machine invoking the CLI.

        Idempotent — called at the start of every bootstrap.

        :raises click.ClickException: When required local tooling or
            credentials are missing and cannot be installed.
        """

    @abstractmethod
    def provision(self, name: str) -> str:
        """
        Create a new sandbox.

        :param name: Human-readable label for the sandbox, e.g.
            ``"omnigent-host"``.
        :returns: The provider-assigned sandbox id, e.g.
            ``"lovable-wattlebird-1530"``.
        :raises click.ClickException: If provisioning fails.
        """

    def attach(self, sandbox_id: str) -> None:
        """
        Validate / refresh access to an existing sandbox so subsequent
        primitives can resolve it.

        CLI-bootstrap capability — the server's managed-host flow never
        attaches to pre-existing sandboxes, so launchers that exist
        only for managed launches (e.g. a deployment-injected custom
        launcher) need not override the raising default.

        :param sandbox_id: The sandbox to attach to, e.g.
            ``"lovable-wattlebird-1530"``.
        :raises SandboxCapabilityError: When the provider does not
            support attaching.
        :raises click.ClickException: If the sandbox cannot be resolved.
        """
        raise self._capability_error("attach to an existing sandbox")

    def keep_alive(self, sandbox_id: str) -> None:
        """
        Configure the sandbox to survive idle periods (disable idle
        autostop / maximize lifetime), so long agent runs don't lose
        their host. Soft-fail: implementations should warn rather than
        raise when the provider rejects the setting.

        CLI-bootstrap capability — managed-only launchers need not
        override the raising default.

        :param sandbox_id: The sandbox to configure.
        :raises SandboxCapabilityError: When the provider does not
            support keep-alive configuration.
        """
        raise self._capability_error("configure keep-alive")

    @abstractmethod
    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """
        Run a shell command inside the sandbox and capture its output.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely, e.g.
            ``"pip install --user /tmp/pkg.whl"``. Quote paths yourself
            if they must survive the remote shell.
        :param check: When ``True``, raise on non-zero exit.
        :returns: The completed command's exit code and output.
        :raises click.ClickException: If *check* is ``True`` and the
            command exits non-zero.
        """

    def run_background(
        self, sandbox_id: str, command: str, *, log_path: str = "/tmp/omnigent-host.log"
    ) -> RemoteCommandResult:
        """
        Start *command* as a detached background process in the sandbox.

        The default wraps the command in ``setsid nohup … & echo launched``
        so it survives the exec session ending. Providers where backgrounded
        processes are reaped on exec return (e.g. OpenShell) override this
        to hold the exec stream open instead.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to background, e.g.
            ``"ENV=val omnigent host --server https://…"``.
        :param log_path: Where stdout/stderr of the backgrounded process
            are redirected inside the sandbox.
        :returns: A synthetic result with ``stdout="launched\\n"`` on success.
        :raises click.ClickException: If the launch command fails.
        """
        return self.run(
            sandbox_id,
            f"setsid nohup {command} > {log_path} 2>&1 < /dev/null & echo launched",
        )

    def put(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        """
        Copy a local file into the sandbox.

        CLI-bootstrap capability (wheel shipping) — managed-only
        launchers need not override the raising default.

        :param sandbox_id: Target sandbox.
        :param local_path: Path on the local machine to read from.
        :param remote_path: Destination path on the sandbox, e.g.
            ``"/tmp/oa-wheels.tgz"``.
        :raises SandboxCapabilityError: When the provider does not
            support file shipping.
        :raises click.ClickException: If the transfer fails.
        """
        raise self._capability_error("ship files into the sandbox")

    def stream_exec(self, sandbox_id: str, command: str, *, pty: bool = False) -> RemoteProcess:
        """
        Spawn a command in the sandbox and stream its output line by
        line.

        CLI-bootstrap capability (the in-sandbox OAuth login) —
        managed-only launchers need not override the raising default.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely, e.g.
            ``"databricks auth login --host https://… --profile oss"``.
        :param pty: When ``True``, allocate a remote PTY. Required for
            CLIs that suppress output when not attached to a terminal.
        :returns: A handle streaming the process's combined output.
        :raises SandboxCapabilityError: When the provider does not
            support streaming execs.
        """
        raise self._capability_error("stream a remote process")

    def forward_capability_error(self) -> SandboxCapabilityError:
        """
        Build the error explaining that this provider cannot forward a
        local port into the sandbox (and therefore cannot run the
        in-sandbox App OAuth flow).

        Single source for the message: raised both by the default
        :meth:`forward_local_port` and by the bootstrap's fail-fast
        check on :attr:`supports_local_port_forward`.

        :returns: The capability error, naming the ``--no-auth`` escape
            hatch.
        """
        return SandboxCapabilityError(
            f"The '{self.provider}' provider cannot forward a local port into the "
            "sandbox, which the in-sandbox Databricks App auth flow requires — "
            "use this provider with servers that don't need App auth."
        )

    def forward_local_port(self, sandbox_id: str, port: int) -> AbstractContextManager[None]:
        """
        Forward ``localhost:<port>`` on the local machine into the
        sandbox (``ssh -L`` semantics), yielding once the local port is
        bound and tearing the forward down on exit.

        Optional capability: the default implementation raises
        :class:`SandboxCapabilityError`. Providers with an inbound
        forwarding path (Lakebox over SSH) override it AND set
        :attr:`supports_local_port_forward` to ``True``.

        :param sandbox_id: Target sandbox.
        :param port: Local + remote loopback port to bridge, e.g.
            ``8022``.
        :returns: Context manager holding the forward open.
        :raises SandboxCapabilityError: When the provider has no
            local-to-sandbox forwarding path.
        """
        raise self.forward_capability_error()

    def terminate(self, sandbox_id: str) -> None:
        """
        Terminate a sandbox, releasing its compute.

        Optional capability: the default implementation raises
        :class:`SandboxCapabilityError` — providers whose SDK exposes
        programmatic termination override it. Used by the server's
        managed-host cleanup when a managed session is deleted.

        :param sandbox_id: The sandbox to terminate, e.g.
            ``"sb-a1b2c3"``.
        :raises SandboxCapabilityError: When the provider has no
            programmatic termination path — delete the sandbox with
            the provider's own tooling instead.
        """
        raise SandboxCapabilityError(
            f"The '{self.provider}' provider does not support programmatic "
            "sandbox termination — delete the sandbox with the provider's "
            "own tooling."
        )

    def exec_foreground(self, sandbox_id: str, command: str) -> int:
        """
        Run a command in the sandbox with stdio inherited from the
        current terminal, blocking until it exits (Ctrl-C detaches and
        tears the remote process down).

        Used to hold ``omnigent host`` open while the sandbox is
        registered with the App. CLI-bootstrap capability —
        managed-only launchers need not override the raising default.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely, e.g.
            ``"omnigent host --server https://… --profile oss"``.
        :returns: The remote command's exit code.
        :raises SandboxCapabilityError: When the provider does not
            support foreground execs.
        """
        raise self._capability_error("run a foreground process")

    def wheel_install_command(self, remote_tgz_path: str) -> str:
        """
        Build the remote shell command that unpacks the shipped wheel
        tarball and pip-installs the wheels.

        Provider-specific because the right pip flags depend on the
        sandbox image (e.g. the Lakebox image bakes omnigent and its
        deps, requiring ``--force-reinstall --no-deps``).
        CLI-bootstrap capability — managed-only launchers run from
        pre-baked images and need not override the raising default.

        :param remote_tgz_path: Where :func:`~omnigent.onboarding.
            sandboxes.bootstrap.ship_wheels` placed the tarball, e.g.
            ``"/tmp/oa-wheels.tgz"``.
        :returns: A shell command string for :meth:`run`.
        :raises SandboxCapabilityError: When the provider does not
            support wheel installs.
        """
        raise self._capability_error("install shipped wheels")

    def _capability_error(self, action: str) -> SandboxCapabilityError:
        """
        Build the error for an optional primitive this provider lacks.

        :param action: Human phrase for the unsupported primitive,
            e.g. ``"ship files into the sandbox"``.
        :returns: The capability error to raise.
        """
        return SandboxCapabilityError(
            f"The '{self.provider}' provider does not support the ability to {action}."
        )
