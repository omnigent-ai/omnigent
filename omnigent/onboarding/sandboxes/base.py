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

import shlex
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

import click

from omnigent.host.identity import HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR, HOST_TOKEN_ENV_VAR

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
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

    # Whether this provider can resume a stopped sandbox IN PLACE
    # (reattaching its persistent volume) rather than only provisioning a
    # fresh one. The server's managed-host wake path checks this BEFORE
    # attempting a resume: providers with a stop/resume lifecycle + a
    # persistent volume override it to ``True``; providers whose sandboxes
    # are ephemeral (Modal — no volume to reattach) leave it ``False``, so a
    # dormant host there stays gone (the user starts a new session) instead
    # of being silently revived onto an empty workspace.
    can_resume: ClassVar[bool] = False

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
        Create a new sandbox and return its id.

        Exec-model providers create the box here. Entrypoint-as-host providers
        (whose sandbox boots running the host) may instead just RESERVE the id
        and defer materialization to :meth:`start_host` — which lets the server
        register the launch token against the id before the box exists, closing
        the host dial-back race by construction.

        :param name: Human-readable label for the sandbox, e.g.
            ``"omnigent-host"``.
        :returns: The provider-assigned (or reserved) sandbox id, e.g.
            ``"lovable-wattlebird-1530"``.
        :raises click.ClickException: If provisioning fails.
        """

    def start_host(
        self,
        sandbox_id: str,
        *,
        token: str,
        host_id: str,
        host_name: str,
        server_url: str,
        repo_url: str | None = None,
        repo_branch: str | None = None,
        repo_name: str | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> str:
        """
        Start ``omnigent host`` in the sandbox and return the workspace path.

        The default is the EXEC model: probe ``$HOME``, create
        ``<HOME>/workspace``, optionally materialize the repository into it (via
        :meth:`materialize_workspace`, which clones by default), and start the
        host detached (``setsid``-backgrounded, identity + token in the process
        environment) — all driven through :meth:`run` / :meth:`run_background`.
        It is shared by every provider whose sandbox is a bare box the server
        execs into (Modal, Daytona, …); entrypoint-as-host providers (e.g.
        Kubernetes, whose Pod boots running the host) override it. A provider
        that only needs to change how the repository is obtained (resolve a local
        checkout instead of cloning) overrides :meth:`materialize_workspace`
        alone.

        The launch token is registered before this call, so the host
        authenticates the moment it dials back. The ``repo_*`` arguments arrive
        as primitives (not the server's ``RepoWorkspace``) so this
        onboarding-layer method carries no server dependency.

        :param sandbox_id: The sandbox from :meth:`provision`.
        :param token: The raw launch token the host authenticates with.
        :param host_id: Server-chosen host identity, e.g. ``"host_a1b2c3d4..."``.
        :param host_name: Server-chosen host display name, e.g.
            ``"managed-a1b2c3d4"``.
        :param server_url: URL of this server the host dials back to.
        :param repo_url: Repository clone URL, or ``None`` for an empty
            workspace.
        :param repo_branch: Branch to clone, or ``None`` for the default branch.
        :param repo_name: Directory the clone lands in under the workspace, or
            ``None`` when *repo_url* is ``None``.
        :param on_stage: Progress observer invoked with ``"cloning"`` before the
            clone (when *repo_url* is set) and ``"starting"`` before the host
            launches. Runs on this (worker) thread, so it must be thread-safe.
            ``None`` disables progress reporting.
        :returns: The absolute in-sandbox workspace path (the cloned repository
            directory when *repo_url* is set).
        :raises click.ClickException: If a sandbox command fails, the clone
            fails, or the sandbox's ``$HOME`` cannot be resolved.
        """
        # The image (and the user it runs as) is operator-supplied, so the home
        # directory isn't knowable statically — ask the sandbox.
        home = self.run(sandbox_id, 'printf %s "$HOME"').stdout.strip()
        if not home:
            raise click.ClickException(
                f"could not resolve $HOME inside sandbox '{sandbox_id}' — "
                "the configured image must provide a usable shell environment"
            )
        workspace = f"{home}/workspace"
        self.run(sandbox_id, f"mkdir -p {shlex.quote(workspace)}")
        if repo_url is not None:
            workspace = self.materialize_workspace(
                sandbox_id,
                workspace=workspace,
                repo_url=repo_url,
                repo_branch=repo_branch,
                repo_name=repo_name,
                on_stage=on_stage,
            )
        # "starting" covers from here through host registration — the caller's
        # online poll resolves it.
        if on_stage is not None:
            on_stage("starting")
        env_prefix = " ".join(
            f"{key}={shlex.quote(value)}"
            for key, value in (
                (HOST_TOKEN_ENV_VAR, token),
                (HOST_ID_ENV_VAR, host_id),
                (HOST_NAME_ENV_VAR, host_name),
            )
        )
        self.run_background(
            sandbox_id,
            f"{env_prefix} omnigent host --server {shlex.quote(server_url)}",
        )
        return workspace

    def materialize_workspace(
        self,
        sandbox_id: str,
        *,
        workspace: str,
        repo_url: str,
        repo_branch: str | None,
        repo_name: str | None,
        on_stage: Callable[[str], None] | None = None,
    ) -> str:
        """
        Materialize the requested repository into the sandbox and return the
        working directory the host should start in.

        Override point for how a repository *identity* becomes an on-disk
        checkout. The default is the EXEC model — ``git clone`` the URL into
        ``<workspace>/<repo_name>`` via :meth:`run` — shared by every provider
        whose sandbox is a bare box with outbound git access (Modal, Daytona,
        E2B, …). Providers whose sandbox already carries the repository (a
        pre-provisioned checkout, a local mirror, a cached worktree) override
        this to *resolve* the identity to that local path instead of cloning,
        without having to reimplement :meth:`start_host`. Called by
        :meth:`start_host` only when ``repo_url`` is set, after ``<workspace>``
        has been created and before the host launches.

        The ``repo_*`` arguments are the same repository identity
        :meth:`start_host` received (the server's ``RepoWorkspace`` unpacked into
        primitives, so this onboarding-layer method carries no server
        dependency). An override is free to interpret ``repo_url`` as an identity
        to map to a local checkout rather than a URL to fetch.

        :param sandbox_id: The sandbox from :meth:`provision`.
        :param workspace: The already-created workspace root, e.g.
            ``"/root/workspace"``.
        :param repo_url: Repository clone URL (or, for a resolving override, the
            repository identity), e.g. ``"https://github.com/org/repo"``.
        :param repo_branch: Branch to check out, or ``None`` for the default
            branch.
        :param repo_name: Directory the checkout lands in under *workspace*, or
            ``None``.
        :param on_stage: Progress observer; the default invokes it with
            ``"cloning"`` before the clone. Runs on this (worker) thread, so it
            must be thread-safe. ``None`` disables progress reporting.
        :returns: The absolute in-sandbox path the host should start in (the
            checkout directory).
        :raises click.ClickException: If materialization fails (e.g. the clone
            fails).
        """
        if on_stage is not None:
            on_stage("cloning")
        clone_dir = f"{workspace}/{repo_name}"
        branch_args = (
            f"--branch {shlex.quote(repo_branch)} --single-branch "
            if repo_branch is not None
            else ""
        )
        try:
            self.run(
                sandbox_id,
                f"git clone {branch_args}-- {shlex.quote(repo_url)} {shlex.quote(clone_dir)}",
            )
        except click.ClickException as exc:
            # Provider boundary: re-raise with the repository named so the
            # create-session 502 says WHAT failed to clone, not just that a
            # sandbox command exited non-zero.
            raise click.ClickException(
                f"failed to clone repository '{repo_url}'"
                f"{f' (branch {repo_branch!r})' if repo_branch else ''}: {exc.message}"
            ) from exc
        return clone_dir

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

        The default wraps the command in ``setsid nohup sh -c '…' & echo
        launched`` so it survives the exec session ending. The ``sh -c`` wrapper
        is load-bearing: callers pass env-prefixed commands (e.g.
        ``"ENV=val omnigent host …"``), and ``nohup`` does NOT honor shell
        ``VAR=val`` assignment syntax — ``nohup ENV=val cmd`` makes nohup try to
        exec a program literally named ``ENV=val`` ("No such file or directory").
        Re-parsing the command under ``sh -c`` lets the inner shell apply the
        assignments before running the program. Providers where backgrounded
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
            f"setsid nohup sh -c {shlex.quote(command)} "
            f"> {log_path} 2>&1 < /dev/null & echo launched",
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

    def resume(self, sandbox_id: str) -> None:
        """
        Resume a stopped sandbox in place, reattaching its persistent
        volume, so a dormant managed host can be revived under the SAME
        sandbox id.

        Optional capability: the default implementation raises
        :class:`SandboxCapabilityError`. Providers whose backend has a
        stop/resume lifecycle with a persistent volume override it AND set
        :attr:`can_resume` to ``True``. Used by the server's managed-host
        wake path; the host process itself is restarted separately (resume
        only brings the compute + volume back).

        :param sandbox_id: The stopped sandbox to resume, e.g.
            ``"sb-a1b2c3"``.
        :raises SandboxCapabilityError: When the provider cannot resume a
            stopped sandbox (ephemeral sandboxes / no persistent volume).
        :raises click.ClickException: If the resume fails.
        """
        raise self._capability_error("resume a stopped sandbox")

    def is_running(self, sandbox_id: str) -> bool | None:
        """
        Return whether the provider reports this sandbox as running.

        Optional capability: ``None`` means the launcher cannot cheaply answer
        and callers should preserve their existing liveness behavior.

        :param sandbox_id: The sandbox to inspect, e.g. ``"sb-a1b2c3"``.
        :returns: ``True`` when running, ``False`` when not running, or ``None``
            when the provider status is unknown.
        """
        del sandbox_id
        return None

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
