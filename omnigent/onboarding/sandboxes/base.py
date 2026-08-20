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

Injected host config uses the loader's ``OMNIGENT_CONFIG_HOME`` resolution,
atomically replaces its config and ownership-marker files, and removes a
previously injected value only while it remains unchanged by the user.
"""

from __future__ import annotations

import base64
import json
import secrets
import shlex
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

import click

from omnigent.host import HOST_FATAL_EXIT_CODE, HOST_SIGTERM_EXIT_CODE
from omnigent.host.identity import HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR, HOST_TOKEN_ENV_VAR
from omnigent.onboarding.sandboxes import types as _sandbox_types

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

# Ceiling for the in-sandbox host restart backoff, so a host that crashes on
# every attempt settles into a slow retry instead of a hot loop.
_RESTART_MAX_DELAY_S: int = 30


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


# Prefix for the private dir a launcher creates under world-writable ``/tmp``
# to record the pid of an exec'd foreground process (several providers' SDKs
# expose no kill handle for exec'd processes, so the pid is recorded and a
# second exec signals it on detach). The dir carries an unpredictable random
# suffix and is created mode 700 with a bare ``mkdir`` that fails closed if
# the path already exists, so a co-tenant on the sandbox can't pre-create,
# symlink-redirect, or read the pidfile in ``/tmp``.
_FOREGROUND_RUNDIR_PREFIX: str = "/tmp/oa-foreground-"


def foreground_pidfile() -> tuple[str, str]:
    """Allocate a private, unpredictably-named pidfile under ``/tmp``.

    Used by :meth:`SandboxLauncher.exec_foreground` implementations whose
    SDK cannot kill an exec'd process through its handle (Modal,
    CoreWeave, OpenShell). The caller records the remote pid with
    :func:`foreground_record_prefix` and tears it down with
    :func:`foreground_kill_command`, both of which operate on the paths
    returned here.

    The pidfile lives in a fresh dir created ``mode 700`` with a bare
    ``mkdir`` (no ``-p``) so it **fails closed** if the path already
    exists — a co-tenant can't pre-seed a symlink we'd write through, nor
    read our pid back, in the world-writable ``/tmp`` it lives under.

    :returns: A ``(run_dir, pidfile)`` pair of absolute ``/tmp`` paths.
        ``run_dir`` is ``/tmp/oa-foreground-<32 hex chars>`` and
        ``pidfile`` is ``<run_dir>/pid``.
    """
    run_dir = f"{_FOREGROUND_RUNDIR_PREFIX}{secrets.token_hex(16)}"
    return run_dir, f"{run_dir}/pid"


def foreground_record_prefix(pidfile: str) -> str:
    """Shell prefix that creates the run dir and records the shell pid.

    ``mkdir -m 700`` (no ``-p``) fails closed if the path exists, and
    ``echo $$`` writes the shell pid before the caller swaps in the real
    command via ``exec`` (which keeps the pid across the swap). Both
    paths are :func:`shlex.quote`-d before interpolation so the function
    stays safe even if a future caller passes a non-hex path; the
    standard hex paths from :func:`foreground_pidfile` quote harmlessly.

    :param pidfile: The pidfile path returned by :func:`foreground_pidfile`.
    :returns: A shell fragment such as ``"mkdir -m 700 /tmp/… && echo $$ > /tmp/…/pid && "``
        to prepend before the foreground command.
    """
    run_dir = pidfile.rsplit("/", 1)[0]
    q_dir = shlex.quote(run_dir)
    q_pid = shlex.quote(pidfile)
    return f"mkdir -m 700 {q_dir} && echo $$ > {q_pid} && "


def foreground_kill_command(pidfile: str) -> str:
    """Shell command that signals the recorded pid and drops the run dir.

    Only a fully-numeric pid read back from the private pidfile is ever
    signalled — the ``case`` rejects empty and non-numeric content, so
    unvalidated file contents never reach ``kill``. The run dir is then
    removed so a successful foreground run leaves nothing behind in
    ``/tmp``.

    :param pidfile: The pidfile path returned by :func:`foreground_pidfile`.
    :returns: A self-contained shell command string for a second exec.
    """
    run_dir = pidfile.rsplit("/", 1)[0]
    q_dir = shlex.quote(run_dir)
    q_pid = shlex.quote(pidfile)
    return (
        f"pid=$(cat {q_pid} 2>/dev/null); "
        f'case "$pid" in ""|*[!0-9]*) ;; *) kill "$pid" 2>/dev/null ;; esac; '
        f"rm -rf {q_dir}"
    )


# In-sandbox write of an injected host config, run via ``python3 -c``.
# Self-contained on purpose (stdlib + yaml, both baked into any image that can
# run ``omnigent host``): importing merge logic from the sandbox's installed
# omnigent package would tie the feature to the IMAGE's package version, and
# operator-supplied images may predate it. ``__PAYLOAD__`` is replaced with a
# base64 Python literal — its alphabet has no quote or shell metacharacter, so
# arbitrary YAML content can never break out of the script.
#
# A marker records the previous payload. Each run removes exactly the names it
# injected last time — the server OWNS the names/keys it injects, so a renamed
# gateway or a removed block never strands a stale entry that could collide on a
# ``default`` scope. User-created entries under OTHER names are never in the
# marker and so are never touched. A missing or corrupt marker skips removal
# entirely — never delete without evidence of what was injected.
_HOST_CONFIG_WRITE_SCRIPT: str = """\
import base64, json, os, tempfile, yaml

config_home = os.environ.get("OMNIGENT_CONFIG_HOME")
config_dir = config_home if config_home else os.path.join(os.path.expanduser("~"), ".omnigent")
path = os.path.join(config_dir, "config.yaml")
marker = os.path.join(config_dir, ".injected_host_config.json")
existing = {}
if os.path.exists(path):
    with open(path) as f:
        loaded = yaml.safe_load(f)
    if isinstance(loaded, dict):
        existing = loaded
previous = {}
try:
    with open(marker) as f:
        loaded = json.load(f)
    if isinstance(loaded, dict):
        previous = loaded
except (OSError, ValueError):
    pass
for key, value in previous.items():
    if key == "providers" and isinstance(value, dict):
        current = existing.get(key)
        if isinstance(current, dict):
            for name in value:
                current.pop(name, None)
            if not current:
                existing.pop(key, None)
    else:
        existing.pop(key, None)
injected = json.loads(base64.b64decode(__PAYLOAD__).decode())
for key, value in injected.items():
    current = existing.get(key)
    if key == "providers" and isinstance(current, dict) and isinstance(value, dict):
        existing[key] = {**current, **value}
    else:
        existing[key] = value

def atomic_write(path, dump):
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=os.path.dirname(path), delete=False) as f:
            temp_path = f.name
            dump(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    except BaseException:
        if temp_path is not None:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        raise

if injected or previous:
    os.makedirs(config_dir, exist_ok=True)
    atomic_write(
        path,
        lambda f: yaml.safe_dump(existing, f, default_flow_style=False, sort_keys=True),
    )
if injected:
    atomic_write(marker, lambda f: json.dump(injected, f))
elif previous:
    os.remove(marker)
"""


def render_host_config_write_command(
    host_config: dict[str, object],
    *,
    python_executable: str = "python3",
) -> str:
    """
    Build the remote command that installs *host_config* into the
    sandbox's config directory before ``omnigent host`` starts. The directory
    is ``$OMNIGENT_CONFIG_HOME`` when truthy, otherwise ``~/.omnigent``, exactly
    matching :func:`omnigent.onboarding.provider_config._config_path`.

    Server-managed replacement semantics: the server OWNS the names/keys it
    injects. Entries recorded in the previous marker are removed first BY NAME,
    then the current payload merges in with
    ``omnigent.cli._save_global_config``'s
    ``deep_merge_keys=("providers",)`` semantics — ``providers`` one
    level deep and every other top-level key wholesale. Removing by name (rather
    than only when unchanged) is deliberate: a renamed gateway must not leave
    its old entry behind, since two entries claiming the same ``default`` scope
    is a sandbox load error. User-created config under names the server never
    injects is never in the marker and so always survives; a name the server
    injects is server-managed, and an in-sandbox edit to it does not persist
    across the next replacement. An empty *host_config* renders a pure cleanup
    command. A missing or corrupt marker skips removal rather than guessing
    ownership. Shared by both launch seams — the exec-model
    :meth:`SandboxLauncher.start_host` and the Kubernetes init container — so the
    behavior cannot drift between providers.

    Both config and marker writes use a fully-written, fsynced temporary file
    in the destination directory followed by :func:`os.replace`, so an
    interrupted write cannot expose a truncated destination file. A pre-existing
    ``config.yaml`` symlink is replaced by a real file — durability of the write
    is favored over following the link, which an internal sandbox config never
    relies on.

    The payload travels as base64-encoded JSON substituted into a fixed
    Python script, and the whole script is ``shlex.quote``-wrapped —
    operator-supplied YAML content (quotes, ``$``, newlines) never
    reaches shell or Python quoting.

    :param host_config: The validated ``sandbox.host_config`` mapping
        (see :func:`omnigent.server.managed_hosts.parse_sandbox_config`),
        or ``{}`` to only remove previously injected entries.
    :param python_executable: Python interpreter available in the sandbox.
        Providers with an isolated host environment can point this at that
        environment so the script sees Omnigent's PyYAML dependency.
    :returns: A ``python3 -c '<script>'`` shell command, safe to pass to
        :meth:`SandboxLauncher.run` or embed in a larger shell script.
    """
    payload = base64.b64encode(json.dumps(host_config).encode()).decode()
    script = _HOST_CONFIG_WRITE_SCRIPT.replace("__PAYLOAD__", repr(payload))
    return f"{shlex.quote(python_executable)} -c {shlex.quote(script)}"


class SandboxCapabilityError(click.ClickException, _sandbox_types.SandboxError):
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


class SandboxLifecycle(ABC):
    """
    Lifecycle + capability primitives for one sandbox provider.

    Every provider — exec-model or entrypoint-as-host — implements this
    base. It carries the provider identity, capability flags, and the
    provisioning / teardown / resume lifecycle. Transport (``run``, ``put``,
    …) lives on :class:`SandboxExecTransport`; the default host launch lives
    on :class:`ExecModelHostLauncher`.
    """

    # Short provider name used in CLI ``--provider`` choices and error
    # messages, e.g. ``"lakebox"``.
    provider: ClassVar[str]

    # Package index URL exported as ``UV_INDEX_URL`` for the local wheel
    # build, or ``None`` to use ambient uv configuration. Providers tied
    # to networks where public PyPI is unreachable (Lakebox on the
    # Databricks corp network) override this.
    wheel_build_index_url: ClassVar[str | None] = None

    # Legacy class vars — kept for backward compat while providers migrate
    # to explicit ``capabilities`` objects. New providers should override
    # the ``capabilities`` property directly instead of setting these.
    supports_local_port_forward: ClassVar[bool] = False
    supports_cli_bootstrap: ClassVar[bool] = True
    can_resume: ClassVar[bool] = False
    supports_managed_launch: ClassVar[bool] = True

    @property
    def capabilities(self) -> _sandbox_types.SandboxCapabilities:
        """
        Feature flags this provider declares.

        The returned object is derived from the provider's class vars and
        from which optional transport methods it has overridden. It is a
        transition shim: providers override this property with an explicit
        :class:`~omnigent.onboarding.sandboxes.types.SandboxCapabilities`
        object directly.
        """
        return _sandbox_types.SandboxCapabilities(
            cli_bootstrap=self.supports_cli_bootstrap,
            managed_launch=self.supports_managed_launch,
            local_port_forward=self.supports_local_port_forward,
            resume_stopped=self.can_resume,
            programmatic_terminate=self._is_capability_overridden("terminate"),
            file_copy=self._is_capability_overridden("put"),
            streaming_exec=self._is_capability_overridden("stream_exec"),
            foreground_exec=self._is_capability_overridden("exec_foreground"),
        )

    def _is_capability_overridden(self, name: str) -> bool:
        """
        Return whether this provider overrides the named optional method.

        Used while the refactor is in transition so the capability object
        can reflect overridden methods without provider authors touching it.
        """
        self_method = getattr(type(self), name, None)
        if self_method is None:
            return False
        base_method = getattr(ExecModelHostLauncher, name, None)
        if base_method is None:
            return False
        return self_method is not base_method

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


def supervise_host_command(command: str) -> str:
    """
    Wrap a host launch in a restart loop so a crash does not strand the sandbox.

    The sandbox container outlives the host process: PID 1 is a placeholder
    (``sleep infinity``) or the provider's own init, so a dead host leaves a
    healthy, still-billing sandbox with nothing running in it. The only recovery
    is the server re-provisioning a fresh sandbox on the next message, which
    discards the workspace — restarting in place keeps the clone and the
    installed dependencies.

    The loop stands down on a clean exit, on
    :data:`~omnigent.host.HOST_FATAL_EXIT_CODE` (a credential / version failure
    that can never succeed), and on SIGTERM (a deliberate stop). Anything else
    is a crash, retried with a doubling delay. A signal-kill of the host alone
    (SIGKILL → 137) counts as a crash on purpose: that is what an OOM kill looks
    like, and restarting is the wanted response.

    A path that means to STOP the host must therefore signal the supervisor too,
    not just the host — otherwise the loop faithfully restarts it. Both in-sandbox
    stop paths already do: ``foreground_kill_command`` signals the process the
    pidfile recorded (the supervisor, which is what ``exec``s under it), and
    islo's preserved-daemon stop matches ``"omnigent host"`` against full argv,
    which the supervisor's own ``sh -c <script>`` argv contains.

    The attempt counter in the restart log makes a persistently-crashing host
    observable — the loop never gives up, so a wedged box would otherwise be
    silent apart from indistinguishable repeats.

    :param command: The host launch, e.g. ``"OMNIGENT_HOST_TOKEN=… omnigent
        host --server https://…"``. Env prefixes are re-applied per attempt.
    :returns: A POSIX ``sh`` script ending in ``done``, so callers can append
        redirections to it directly.
    """
    stop_codes = f"0|{HOST_FATAL_EXIT_CODE}|{HOST_SIGTERM_EXIT_CODE}"
    return (
        "delay=1\n"
        "attempt=0\n"
        "while :; do\n"
        f"  {command}\n"
        "  rc=$?\n"
        f'  case "$rc" in {stop_codes}) exit "$rc";; esac\n'
        "  attempt=$((attempt + 1))\n"
        '  echo "omnigent host exited ($rc); attempt $attempt; '
        'restarting in ${delay}s" >&2\n'
        '  sleep "$delay"\n'
        f'  delay=$((delay * 2)); [ "$delay" -gt {_RESTART_MAX_DELAY_S} ] '
        f"&& delay={_RESTART_MAX_DELAY_S}\n"
        "done"
    )


class SandboxExecTransport(SandboxLifecycle):
    """
    Exec-transport primitives for providers that exec into a running sandbox.

    Providers whose sandbox is a bare box the server execs into (Modal,
    Daytona, E2B, …) inherit this via :class:`ExecModelHostLauncher`.
    Entrypoint-as-host providers (e.g. Kubernetes, whose Pod boots running the
    host) inherit :class:`SandboxHostLauncher` instead and need NOT implement
    any of these methods.
    """

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
        Start *command* under a supervisor as a detached background process.

        The command is wrapped in :func:`supervise_host_command` (restart on
        crash) and then in ``setsid nohup sh -c '…' & echo launched`` so it
        survives the exec session ending. The ``sh -c`` wrapper is load-bearing:
        callers pass env-prefixed commands (e.g. ``"ENV=val omnigent host …"``),
        and ``nohup`` does NOT honor shell ``VAR=val`` assignment syntax —
        ``nohup ENV=val cmd`` makes nohup try to exec a program literally named
        ``ENV=val`` ("No such file or directory"). Re-parsing the command under
        ``sh -c`` lets the inner shell apply the assignments before running the
        program. Providers where backgrounded processes are reaped on exec
        return (e.g. OpenShell) override this to hold the exec stream open
        instead — they supervise too, just without the detach.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to background, e.g.
            ``"ENV=val omnigent host --server https://…"``.
        :param log_path: Where stdout/stderr of the supervisor and every host
            attempt are redirected inside the sandbox.
        :returns: A synthetic result with ``stdout="launched\\n"`` on success.
        :raises click.ClickException: If the launch command fails.
        """
        return self.run(
            sandbox_id,
            f"setsid nohup sh -c {shlex.quote(supervise_host_command(command))} "
            f">> {log_path} 2>&1 < /dev/null & echo launched",
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


class SandboxHostLauncher(SandboxLifecycle):
    """
    Lifecycle + host-launch contract for a sandbox provider.

    Every managed-host provider — exec-model or entrypoint-as-host — implements
    this. :meth:`start_host` is abstract here; the exec-model default lives on
    :class:`ExecModelHostLauncher`. Entrypoint-as-host providers (e.g.
    Kubernetes) inherit this class directly and override :meth:`start_host`
    without needing any exec transport.
    """

    @abstractmethod
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
        host_config: dict[str, object] | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> str:
        """
        Start ``omnigent host`` in the sandbox and return the workspace path.

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
        :param host_config: Deployment-supplied ``~/.omnigent/config.yaml``
            content installed into the sandbox's config BEFORE the host starts.
        :param on_stage: Progress observer invoked with ``"cloning"`` and
            ``"starting"``.
        :returns: The absolute in-sandbox workspace path.
        """


class ExecModelHostLauncher(SandboxHostLauncher, SandboxExecTransport):
    """
    Default exec-model host launcher for providers that exec into a running
    sandbox (Modal, Daytona, E2B, Islo, OpenShell, Boxlite, …).

    Provides the default :meth:`start_host`, :meth:`run_background`, and
    :meth:`materialize_workspace` that compose :meth:`run` into the full
    managed-host bootstrap. A provider that only needs to change how the
    repository is obtained overrides :meth:`materialize_workspace` alone.

    Entrypoint-as-host providers (e.g. Kubernetes) inherit
    :class:`SandboxHostLauncher` directly and do NOT need ``run()`` or any
    exec transport.
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
        host_config: dict[str, object] | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> str:
        """
        Start ``omnigent host`` in the sandbox and return the workspace path.

        The default is the EXEC model: probe ``$HOME``, create
        ``<HOME>/workspace``, optionally materialize the repository into it (via
        :meth:`materialize_workspace`, which clones by default), merge any
        *host_config* into ``~/.omnigent/config.yaml``, and start the host
        detached (``setsid``-backgrounded, identity + token in the process
        environment) — all driven through :meth:`run` / :meth:`run_background`.

        :returns: The absolute in-sandbox workspace path.
        """
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
        if on_stage is not None:
            on_stage("starting")
        if host_config is not None or self.capabilities.resume_stopped:
            self.run(sandbox_id, render_host_config_write_command(host_config or {}))
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

        The default is ``git clone`` into ``<workspace>/<repo_name>``. Override
        to resolve a local checkout instead of cloning.
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
            raise click.ClickException(
                f"failed to clone repository '{repo_url}'"
                f"{f' (branch {repo_branch!r})' if repo_branch else ''}: {exc.message}"
            ) from exc
        return clone_dir


# Backward-compat alias: existing providers inherit ``SandboxLauncher``, which
# is now ``ExecModelHostLauncher``. Kubernetes and future entrypoint-as-host
# providers inherit ``SandboxHostLauncher`` directly.
SandboxLauncher = ExecModelHostLauncher
