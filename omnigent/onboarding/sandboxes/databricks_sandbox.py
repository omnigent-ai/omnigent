"""
Databricks Sandbox launcher over the SDK's Sandbox service.

Implements :class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher` for
`Databricks Sandbox <https://docs.databricks.com/aws/en/compute/serverless/sandbox>`_
through ``databricks-sdk``'s ``WorkspaceClient.sandbox`` service
(``databricks-sdk>=0.153``): create / get / start / delete for lifecycle,
``execute_command_sync`` for remote commands, and the streaming
``execute_command`` for :meth:`~DatabricksSandboxLauncher.stream_exec` and
:meth:`~DatabricksSandboxLauncher.exec_foreground`. Using the SDK client means
a server-side contract change is absorbed by an SDK bump.

There is no SSH, no CLI subprocess, and no gateway port to reach, so the
server works anywhere that can call the workspace API over HTTPS
(including Databricks Apps compute, whose DNS stubs the SSH gateway).

Identity. By default the launcher acts as one deployment-wide identity (a
config profile, or ``DATABRICKS_*`` env such as a Databricks App's service
principal). With ``identity="owner"`` it instead acts *as the user who owns the
host*: the server binds a resolver for that user's connected Databricks
credential (:meth:`bind_owner_credential`, fed by Databricks Connect), and every
create / get / start / stop / delete / exec call — plus the proxy bearer written
into the sandbox — uses that user's own token. Each sandbox is therefore owned
by, and only holds a credential for, its owner. A user who has not connected
Databricks is refused with :class:`OwnerCredentialMissingError`; there is no
fallback to the server identity.

The SDK retries a request after a connection error or timeout, so an exec can
run twice. Commands are therefore safe to repeat or fail loudly (a re-run clone
hits its existing directory), and the host launch holds a lock
(:data:`_HOST_LOCK`) so a replayed launch exits instead of starting a second
host. The HTTP timeout sits above the longest execution timeout so a slow
command is not cut off and retried client-side.
"""

from __future__ import annotations

import secrets
import shlex
import threading
import time
from typing import TYPE_CHECKING, Any, ClassVar, Literal

import click

from omnigent.host.identity import HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR, HOST_TOKEN_ENV_VAR
from omnigent.onboarding.sandboxes.base import (
    OwnerCredentialMissingError,
    RemoteCommandResult,
    RemoteProcess,
    SandboxGoneError,
    SandboxLauncher,
    render_host_config_write_command,
    supervise_host_command,
)
from omnigent.onboarding.sandboxes.types import SandboxCapabilities, clone_dir_names
from omnigent.util.proxy_bearer import PROXY_BEARER_FILE_ENV_VAR

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from databricks.sdk.service.sandbox import SandboxState

    from omnigent.onboarding.sandboxes.types import RepoWorkspace

# Longest single remote command (git clone of a large repo, a bootstrap
# install). The server clamps execution_timeout to its own ceiling.
_EXEC_TIMEOUT_S: int = 1800
# Client HTTP timeout for exec calls: above _EXEC_TIMEOUT_S so a still-running
# command is not cut off client-side (which the SDK would retry).
_EXEC_HTTP_TIMEOUT_S: int = _EXEC_TIMEOUT_S + 120
_STATE_TIMEOUT_S: float = 600.0
_STATE_POLL_S: float = 1.0

# Under $HOME, the only sandbox storage that survives a stop / resume cycle.
_HOST_LOG_DIR: str = "$HOME/.omnigent"
_HOST_LOG: str = f"{_HOST_LOG_DIR}/host.log"
# Held by the running host supervisor; a replayed launch fails to take it and exits.
_HOST_LOCK: str = f"{_HOST_LOG_DIR}/host.lock"

_SDK_TOO_OLD = (
    "the 'databricks' sandbox provider needs databricks-sdk[sandbox]>=0.153 "
    "(pip install -U 'omnigent[databricks]')"
)

#: Seconds an owner-identity launcher reuses one resolved owner token before
#: asking the server again. The server's resolver refreshes tokens well before
#: expiry, so a token cached this long is always still valid; the cache only
#: saves a store read (and a thread hop) per REST call while polling state.
_OWNER_TOKEN_CACHE_S: float = 60.0

#: The ``identity`` values :class:`DatabricksSandboxLauncher` accepts.
DatabricksIdentity = Literal["server", "owner"]

# Run before every command: commands start outside $HOME, so begin there.
_HOME_PRELUDE: str = (
    'cd "$HOME" || exit 1; '
    # User-level tool installs (e.g. a bootstrap `uv tool install omnigent`)
    # must shadow the image's baked /usr/local/bin/omnigent.
    'export PATH="$HOME/.local/bin:$PATH"; '
)


def _sandbox_sdk() -> Any:
    """The SDK's ``databricks.sdk.service.sandbox`` module, or an upgrade error."""
    try:
        from databricks.sdk.mixins.sandbox import StreamMode  # noqa: F401 - streaming, 0.153+
        from databricks.sdk.service import sandbox
    except ImportError as exc:
        raise click.ClickException(_SDK_TOO_OLD) from exc
    return sandbox


def _resource_name(sandbox_id: str) -> str:
    """The SDK resource name for *sandbox_id* (``sandboxes/<id>``)."""
    return f"sandboxes/{sandbox_id}"


def _duration(seconds: int) -> Any:
    """A protobuf ``Duration`` of *seconds*, as the Sandbox service expects."""
    from google.protobuf.duration_pb2 import Duration

    return Duration(seconds=seconds)


def _state_of(sandbox: Any) -> SandboxState | None:
    """Return a Sandbox's lifecycle state, or ``None`` when absent / unknown."""
    status = getattr(sandbox, "status", None)
    return getattr(status, "state", None)


def _is_not_found(exc: Exception) -> bool:
    """Whether an SDK error is a 404 / NOT_FOUND (REST or gRPC)."""
    from databricks.sdk.errors import NotFound

    if isinstance(exc, NotFound):
        return True
    code = getattr(exc, "code", None)  # grpc.RpcError from a streaming exec
    return callable(code) and getattr(code(), "name", "") == "NOT_FOUND"


def _reraise_owner_refusal(exc: Exception) -> None:
    """Re-raise an owner-credential refusal unwrapped, so its message reaches the user."""
    if isinstance(exc, OwnerCredentialMissingError):
        raise exc


def _normalize_host(host: str) -> str:
    """``https://host`` without a trailing slash, lower-cased, for comparisons."""
    value = host.strip().rstrip("/").lower()
    if value and "://" not in value:
        value = f"https://{value}"
    return value


_NOT_CONNECTED_MESSAGE = (
    "Connect Databricks first: this server runs Databricks Sandboxes as your own "
    "Databricks identity. Open Settings → Connections → Databricks, connect your "
    "workspace, then try again."
)


def _owner_token_strategy(token: Callable[[], str]) -> Any:
    """An SDK ``CredentialsStrategy`` that authenticates every request as the owner.

    *token* is called per request (it caches internally), so a long-lived
    client always sends the owner's current, server-refreshed token. Built
    here rather than at module scope so importing this module never needs
    ``databricks-sdk``.
    """
    from databricks.sdk.credentials_provider import CredentialsStrategy

    class _OwnerTokenStrategy(CredentialsStrategy):
        def auth_type(self) -> str:
            return "omnigent-owner-oauth"

        def __call__(self, cfg: Any) -> Callable[[], dict[str, str]]:
            del cfg
            return lambda: {"Authorization": f"Bearer {token()}"}

    return _OwnerTokenStrategy()


class _DatabricksRemoteProcess(RemoteProcess):
    """:class:`RemoteProcess` over an SDK ``CommandProcess`` (stderr merged into stdout)."""

    def __init__(self, process: Any, kill: Callable[[int], None]) -> None:
        self._process = process
        self._kill = kill
        self._lines: Iterator[str] = iter(process.stdout)
        self._closed = False

    @property
    def lines(self) -> Iterator[str]:
        return self._lines

    def wait(self) -> int:
        return int(self._process.wait())

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # The SDK's close() only drops the client stream; the command keeps
        # running, so stop it by the pid its start event reported.
        if self._process.exit_code is None and self._process.pid:
            self._kill(int(self._process.pid))
        self._process.close()


class DatabricksSandboxLauncher(SandboxLauncher):
    """
    Managed-host launcher for Databricks Sandbox over the SDK Sandbox service.

    :param profile: Databricks config profile (``~/.databrickscfg``), e.g.
        ``"DEFAULT"``. ``None`` uses the SDK's default resolution
        (``DATABRICKS_*`` env, which is how a Databricks App's service
        principal authenticates).
    :param sandbox_id_prefix: Prefix for client-chosen sandbox ids, e.g.
        ``"omnigent"`` → ``omnigent-1a2b3c4d5e``.
    :param inactivity_timeout_s: Idle seconds before the platform stops the
        sandbox, or ``None`` for the workspace default. A stopped sandbox is
        resumed (and its host re-bootstrapped) on the session's next turn.
    :param bootstrap_command: Shell command run before every host start (first
        launch and every resume), e.g. a ``uv tool install`` that pins the
        in-sandbox omnigent to the server's version. ``None`` runs the
        image's baked ``omnigent``.
    :param proxy_bearer: Provision the launcher's own short-lived OAuth bearer
        into the sandbox (``~/.omnigent/proxy-bearer``, mode 600) and point the
        host at it (:data:`~omnigent.util.proxy_bearer.PROXY_BEARER_FILE_ENV_VAR`).
        Required when the server sits behind the Databricks Apps OAuth proxy,
        which rejects the sandbox's own PAT. Refreshed on every start, resume,
        and keepalive. With ``identity="owner"`` this is the owner's token.
    :param identity: ``"server"`` (default) acts as the deployment identity
        (*profile* / ``DATABRICKS_*`` env). ``"owner"`` acts as the host's owner
        through their connected Databricks credential (see the module
        docstring); *profile* must then be unset.
    :param workspace_host: With ``identity="owner"``, the only workspace an
        owner's connection may target, e.g.
        ``"https://my-workspace.cloud.databricks.com"``. A connection to any
        other workspace is refused (its sandbox could not reach this server's
        front door). ``None`` accepts the connection's workspace as-is.
    """

    provider: ClassVar[str] = "databricks"
    can_resume: ClassVar[bool] = True
    supports_cli_bootstrap: ClassVar[bool] = False

    def __init__(
        self,
        *,
        profile: str | None = None,
        sandbox_id_prefix: str | None = None,
        inactivity_timeout_s: int | None = None,
        bootstrap_command: str | None = None,
        proxy_bearer: bool = False,
        identity: DatabricksIdentity = "server",
        workspace_host: str | None = None,
    ) -> None:
        if identity not in ("server", "owner"):
            raise ValueError(f"identity must be 'server' or 'owner', got {identity!r}")
        if identity == "owner" and profile:
            raise ValueError("identity='owner' acts as the host owner; do not also set a profile")
        self._profile = profile
        self._proxy_bearer = proxy_bearer
        self._prefix = sandbox_id_prefix or "omnigent"
        self._inactivity_timeout_s = inactivity_timeout_s
        self._bootstrap_command = bootstrap_command
        self._identity: DatabricksIdentity = identity
        self._workspace_host = _normalize_host(workspace_host) if workspace_host else None
        self._owner_resolver: Callable[[], Mapping[str, object] | None] | None = None
        self._owner_token: tuple[str, float] | None = None
        self._owner_lock = threading.Lock()
        self._client: Any = None

    @property
    def capabilities(self) -> SandboxCapabilities:
        """Managed launch + resume + terminate + non-PTY streaming exec."""
        return SandboxCapabilities(
            cli_bootstrap=False,
            managed_launch=True,
            local_port_forward=False,
            resume_stopped=True,
            programmatic_terminate=True,
            file_copy=False,
            streaming_exec=True,
            foreground_exec=True,
            git_clone_options=True,
        )

    # ── Owner identity ──────────────────────────────────

    @property
    def owner_credential_provider(self) -> str | None:
        """``"databricks"`` in owner-identity mode, else ``None``."""
        return "databricks" if self._identity == "owner" else None

    def bind_owner_credential(self, resolve: Callable[[], Mapping[str, object] | None]) -> None:
        """Bind the resolver for the host owner's Databricks credential."""
        self._owner_resolver = resolve
        self._owner_token = None
        self._client = None

    def _resolve_owner(self) -> tuple[str, str]:
        """Return the owner's ``(access_token, workspace_host)`` or refuse.

        :raises OwnerCredentialMissingError: No resolver is bound (the server
            has no Databricks Connect), the owner has not connected, or their
            connection targets a workspace other than :attr:`_workspace_host`.
        """
        if self._owner_resolver is None:
            raise OwnerCredentialMissingError(
                "this server runs Databricks Sandboxes as each user's own identity "
                "(sandbox.databricks.identity: owner) but Databricks Connect is not "
                "configured — set OMNIGENT_DATABRICKS_CLIENT_ID / _CLIENT_SECRET"
            )
        payload = self._owner_resolver()
        token = payload.get("token") if payload else None
        host = payload.get("workspace_host") if payload else None
        if not isinstance(token, str) or not token or not isinstance(host, str) or not host:
            raise OwnerCredentialMissingError(_NOT_CONNECTED_MESSAGE)
        host = _normalize_host(host)
        if self._workspace_host is not None and host != self._workspace_host:
            raise OwnerCredentialMissingError(
                f"your Databricks connection is for {host}, but this server's sandboxes "
                f"run in {self._workspace_host}. Reconnect Databricks to that workspace "
                "(Settings → Connections → Databricks), then try again."
            )
        return token, host

    def _owner_access_token(self) -> str:
        """The owner's current token, re-resolved at most every 60s (thread-safe)."""
        with self._owner_lock:
            cached = self._owner_token
            if cached is not None and time.monotonic() - cached[1] < _OWNER_TOKEN_CACHE_S:
                return cached[0]
            token, _host = self._resolve_owner()
            self._owner_token = (token, time.monotonic())
            return token

    # ── SDK plumbing ────────────────────────────────────

    def _workspace_client(self) -> Any:
        """Return the SDK ``WorkspaceClient`` (built lazily, cached)."""
        if self._client is None:
            _sandbox_sdk()
            try:
                from databricks.sdk import WorkspaceClient
                from databricks.sdk.config import Config
            except ImportError as exc:  # pragma: no cover - extra not installed
                raise click.ClickException(
                    "the 'databricks' sandbox provider needs databricks-sdk "
                    "(pip install 'omnigent[databricks]')"
                ) from exc
            kwargs: dict[str, Any] = {"http_timeout_seconds": _EXEC_HTTP_TIMEOUT_S}
            if self._identity == "owner":
                # Resolve up front: refuses an unconnected owner before any call,
                # and pins the client to the owner's workspace. Explicit
                # host + credentials_strategy win over any DATABRICKS_* env (the
                # server's own identity), so nothing here can act as the server.
                token, host = self._resolve_owner()
                with self._owner_lock:
                    self._owner_token = (token, time.monotonic())
                kwargs["host"] = host
                kwargs["credentials_strategy"] = _owner_token_strategy(self._owner_access_token)
            elif self._profile:
                kwargs["profile"] = self._profile
            self._client = WorkspaceClient(config=Config(**kwargs))
        return self._client

    def _sandboxes(self) -> Any:
        """The SDK's Sandbox service (``WorkspaceClient.sandbox``)."""
        return self._workspace_client().sandbox

    def _get(self, sandbox_id: str) -> Any:
        try:
            return self._sandboxes().get_sandbox(_resource_name(sandbox_id))
        except Exception as exc:
            _reraise_owner_refusal(exc)
            if _is_not_found(exc):
                raise SandboxGoneError(
                    f"Databricks Sandbox '{sandbox_id}' no longer exists"
                ) from exc
            raise click.ClickException(
                f"could not read Databricks Sandbox '{sandbox_id}': {exc}"
            ) from exc

    def _wait_for(self, sandbox_id: str, target: SandboxState) -> float:
        """Poll until the sandbox reaches *target*; return seconds waited."""
        start = time.monotonic()
        last: SandboxState | None = None
        while time.monotonic() - start < _STATE_TIMEOUT_S:
            last = _state_of(self._get(sandbox_id))
            if last == target:
                return time.monotonic() - start
            time.sleep(_STATE_POLL_S)
        raise click.ClickException(
            f"Databricks Sandbox '{sandbox_id}' did not reach {target.value} within "
            f"{_STATE_TIMEOUT_S:.0f}s (last state {last.value if last else 'unknown'})"
        )

    # ── Lifecycle ───────────────────────────────────────

    def prepare(self) -> None:
        """Fail fast when credentials or the SDK's Sandbox API are missing."""
        try:
            # The listing is a lazy iterator; pulling one item forces the request.
            next(iter(self._sandboxes().list_sandboxes(page_size=1)), None)
        except click.ClickException:
            raise
        except Exception as exc:
            _reraise_owner_refusal(exc)
            raise click.ClickException(
                f"could not reach the Databricks Sandbox API "
                f"(profile={self._profile or '<default>'}): {exc}"
            ) from exc

    def provision(self, name: str) -> str:
        """Create a sandbox labelled *name* and wait for it to run."""
        sdk = _sandbox_sdk()
        sandbox_id = f"{self._prefix}-{secrets.token_hex(5)}"
        spec = None
        if self._inactivity_timeout_s is not None:
            timeout = _duration(self._inactivity_timeout_s)
            spec = sdk.SandboxSpec(compute=sdk.ComputeSpec(inactivity_timeout=timeout))
        click.echo(f"▸ Creating Databricks Sandbox '{sandbox_id}' ({name})")
        try:
            self._sandboxes().create_sandbox(
                sdk.Sandbox(display_name=name, spec=spec), sandbox_id=sandbox_id
            )
        except Exception as exc:
            _reraise_owner_refusal(exc)
            raise click.ClickException(f"could not create a Databricks Sandbox: {exc}") from exc
        try:
            waited = self._wait_for(sandbox_id, sdk.SandboxState.SANDBOX_STATE_RUNNING)
        except Exception:
            self.terminate(sandbox_id)
            raise
        click.echo(f"  → {sandbox_id} running after {waited:.1f}s")
        return sandbox_id

    def resume(self, sandbox_id: str) -> None:
        """Start a stopped (or stopping) sandbox in place and wait for RUNNING."""
        states = _sandbox_sdk().SandboxState
        state = _state_of(self._get(sandbox_id))
        if state == states.SANDBOX_STATE_RUNNING:
            return
        if state == states.SANDBOX_STATE_STOPPING:
            self._wait_for(sandbox_id, states.SANDBOX_STATE_STOPPED)
            state = states.SANDBOX_STATE_STOPPED
        if state == states.SANDBOX_STATE_STOPPED:
            click.echo(f"▸ Starting Databricks Sandbox '{sandbox_id}'")
            try:
                self._sandboxes().start_sandbox(_resource_name(sandbox_id))
            except Exception as exc:
                _reraise_owner_refusal(exc)
                if _is_not_found(exc):
                    raise SandboxGoneError(
                        f"Databricks Sandbox '{sandbox_id}' no longer exists"
                    ) from exc
                # A concurrent start already won; fall through to the wait.
                if "already active" not in str(exc):
                    raise click.ClickException(
                        f"could not start Databricks Sandbox '{sandbox_id}': {exc}"
                    ) from exc
        self._wait_for(sandbox_id, states.SANDBOX_STATE_RUNNING)

    def is_running(self, sandbox_id: str) -> bool | None:
        """Map the control-plane state onto running / not running / unknown."""
        states = _sandbox_sdk().SandboxState
        try:
            state = _state_of(self._get(sandbox_id))
        except SandboxGoneError:
            return False
        except click.ClickException:
            return None
        if state == states.SANDBOX_STATE_RUNNING:
            return True
        if state in (
            states.SANDBOX_STATE_STOPPED,
            states.SANDBOX_STATE_STOPPING,
            states.SANDBOX_STATE_PENDING,
        ):
            return False
        return None

    def terminate(self, sandbox_id: str) -> None:
        """Delete the sandbox; an already-absent sandbox counts as success."""
        try:
            self._sandboxes().delete_sandbox(_resource_name(sandbox_id))
        except Exception as exc:
            _reraise_owner_refusal(exc)
            if _is_not_found(exc):
                return
            raise click.ClickException(
                f"could not delete Databricks Sandbox '{sandbox_id}': {exc}"
            ) from exc

    # ── Transport ───────────────────────────────────────

    def run(
        self,
        sandbox_id: str,
        command: str,
        *,
        check: bool = True,
        envs: dict[str, str] | None = None,
    ) -> RemoteCommandResult:
        """
        Run *command* under ``bash -c`` via ``execute_command_sync``.

        :param envs: Extra environment for the remote process. Carried in the
            API request body, so secrets passed here never appear in the
            sandbox's process argv (unlike an ``ENV=value cmd`` prefix).
        """
        statuses = _sandbox_sdk().ExecuteCommandStatus
        try:
            response = self._sandboxes().execute_command_sync(
                _resource_name(sandbox_id),
                "/bin/bash",
                args=["-c", _HOME_PRELUDE + command],
                envs=envs or None,
                execution_timeout=_duration(_EXEC_TIMEOUT_S),
            )
        except Exception as exc:
            _reraise_owner_refusal(exc)
            if _is_not_found(exc):
                raise SandboxGoneError(
                    f"Databricks Sandbox '{sandbox_id}' no longer exists"
                ) from exc
            raise click.ClickException(
                f"command execution failed on Databricks Sandbox '{sandbox_id}': {exc}"
            ) from exc
        status = response.status
        stdout = response.stdout or ""
        stderr = response.stderr or ""
        if response.truncated:
            stderr += "\n[omnigent: output truncated by the sandbox exec API]\n"
        if status == statuses.EXECUTE_COMMAND_STATUS_TIMED_OUT:
            returncode = 124
            stderr += f"\n[omnigent: command timed out after {_EXEC_TIMEOUT_S}s]\n"
        elif response.exit_code is None:
            # An unrecognized status decodes to None; treat it as a failure.
            returncode = 0 if status == statuses.EXECUTE_COMMAND_STATUS_COMPLETED else 1
        else:
            returncode = int(response.exit_code)
        if check and returncode != 0:
            detail = (stderr.strip() or stdout.strip())[-2000:]
            raise click.ClickException(
                f"Remote command failed on Databricks Sandbox '{sandbox_id}' "
                f"(exit {returncode}, {status.value if status else 'unknown'}): {detail}"
            )
        return RemoteCommandResult(returncode=returncode, stdout=stdout, stderr=stderr)

    # ── Streaming exec ──────────────────────────────────

    def _kill_tree(self, sandbox_id: str, pid: int) -> None:
        """SIGTERM *pid* and its children (the shell may have forked the command)."""
        self.run(sandbox_id, f"pkill -TERM -P {pid}; kill -TERM {pid}", check=False)

    def stream_exec(self, sandbox_id: str, command: str, *, pty: bool = False) -> RemoteProcess:
        """Spawn *command* and stream its combined output; ``close()`` stops it.

        The SDK starts a streaming command exactly once (no retry), unlike
        :meth:`run`. The Sandbox API has no PTY, so ``pty=True`` is refused.
        """
        return self._stream(sandbox_id, command, pty=pty)

    def _stream(
        self,
        sandbox_id: str,
        command: str,
        *,
        pty: bool = False,
        envs: dict[str, str] | None = None,
    ) -> RemoteProcess:
        if pty:
            raise self._capability_error("stream a remote process with a PTY")
        from databricks.sdk.mixins.sandbox import StreamMode

        try:
            process = self._sandboxes().execute_command(
                _resource_name(sandbox_id),
                "/bin/bash",
                args=["-c", _HOME_PRELUDE + command],
                envs=envs,
                stderr=StreamMode.STDOUT,
            )
        except Exception as exc:
            _reraise_owner_refusal(exc)
            if _is_not_found(exc):
                raise SandboxGoneError(
                    f"Databricks Sandbox '{sandbox_id}' no longer exists"
                ) from exc
            raise click.ClickException(
                f"could not start a command on Databricks Sandbox '{sandbox_id}': {exc}"
            ) from exc
        return _DatabricksRemoteProcess(process, lambda pid: self._kill_tree(sandbox_id, pid))

    def exec_foreground(self, sandbox_id: str, command: str) -> int:
        """Run *command*, echoing its output locally until it exits; Ctrl-C stops it."""
        # Native harnesses spawn tmux, which refuses a dumb/unset TERM.
        process = self._stream(sandbox_id, f"exec {command}", envs={"TERM": "xterm-256color"})
        try:
            for line in process.lines:
                click.echo(line, nl=False)
            return process.wait()
        except KeyboardInterrupt:
            click.echo("\n  → detaching; stopping the remote process")
            raise
        finally:
            process.close()

    # ── Front-door bearer (Databricks Apps OAuth proxy) ──

    def _current_bearer(self) -> str:
        """Return the launcher identity's current access token (from its SDK auth)."""
        header = self._workspace_client().config.authenticate().get("Authorization", "")
        token = header.removeprefix("Bearer ").strip()
        if not token:
            raise click.ClickException("could not mint a bearer for the sandbox host")
        return token

    def _write_proxy_bearer(self, sandbox_id: str) -> str:
        """Atomically (re)write the proxy bearer file; return its absolute path."""
        result = self.run(
            sandbox_id,
            'umask 077; mkdir -p "$HOME/.omnigent" && '
            'printf %s "$OMNIGENT_PROXY_BEARER_VALUE" > "$HOME/.omnigent/proxy-bearer.tmp" && '
            'mv -f "$HOME/.omnigent/proxy-bearer.tmp" "$HOME/.omnigent/proxy-bearer" && '
            'printf %s "$HOME/.omnigent/proxy-bearer"',
            envs={"OMNIGENT_PROXY_BEARER_VALUE": self._current_bearer()},
        )
        return result.stdout.strip()

    def keep_alive(self, sandbox_id: str) -> bool | None:
        """
        Refresh the proxy bearer on a RUNNING sandbox (no-op otherwise).

        Never execs into a stopped sandbox: exec-sync auto-starts it, which
        would defeat the platform's idle stop. A stopped host is revived by the
        wake path, whose :meth:`start_host` writes a fresh bearer anyway.
        """
        if not self._proxy_bearer:
            return None
        states = _sandbox_sdk().SandboxState
        try:
            if _state_of(self._get(sandbox_id)) != states.SANDBOX_STATE_RUNNING:
                return None
            self._write_proxy_bearer(sandbox_id)
        except (click.ClickException, SandboxGoneError) as exc:
            click.echo(f"  → warning: could not refresh proxy bearer on {sandbox_id}: {exc}")
            return False
        return True

    def start_host(
        self,
        sandbox_id: str,
        *,
        token: str,
        host_id: str,
        host_name: str,
        server_url: str,
        repos: Sequence[RepoWorkspace] = (),
        host_config: dict[str, object] | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> str:
        """
        Re-bootstrap and start ``omnigent host``.

        Runs on first launch AND on every wake (the platform restarts the box
        but not its processes). Any previous host supervisor is stopped first
        so a wake racing a still-alive host never leaves two hosts flapping one
        registration; then the optional :attr:`bootstrap_command` runs, then the
        workspace is prepared and the host is started detached under the
        standard restart supervisor.
        """
        self.run(
            sandbox_id,
            # `[o]` keeps the pattern from matching this very bash -c argv.
            "pkill -f '[o]mnigent host' >/dev/null 2>&1; sleep 1; true",
            check=False,
        )
        launch_envs: dict[str, str] = {}
        if self._proxy_bearer:
            launch_envs[PROXY_BEARER_FILE_ENV_VAR] = self._write_proxy_bearer(sandbox_id)
        if self._bootstrap_command:
            if on_stage is not None:
                on_stage("starting")
            self.run(sandbox_id, self._bootstrap_command)
        workspace = self._prepare_workspace(sandbox_id, repos=repos, on_stage=on_stage)
        if on_stage is not None:
            on_stage("starting")
        # Resumable: always (re)write, so a removed host_config is cleaned up.
        self.run(sandbox_id, render_host_config_write_command(host_config or {}))
        # The launch token rides in the exec request's `envs`, inherited by the
        # detached supervisor and every host restart — never in argv, where any
        # process in the sandbox could read it from `ps`.
        supervised = supervise_host_command(f"omnigent host --server {shlex.quote(server_url)}")
        # exec-sync waits on every holder of its pipes, so the host must fully
        # detach (setsid, redirects, </dev/null). Paths are double-quoted (not
        # shlex-quoted) so the remote shell expands $HOME.
        launch = (
            f'mkdir -p "{_HOST_LOG_DIR}"; '
            f'setsid nohup flock -n "{_HOST_LOCK}" sh -c {shlex.quote(supervised)} '
            f'>> "{_HOST_LOG}" 2>&1 < /dev/null & echo launched'
        )
        self.run(
            sandbox_id,
            launch,
            envs={
                HOST_TOKEN_ENV_VAR: token,
                HOST_ID_ENV_VAR: host_id,
                HOST_NAME_ENV_VAR: host_name,
                **launch_envs,
            },
        )
        return workspace

    def _prepare_workspace(
        self,
        sandbox_id: str,
        *,
        repos: Sequence[RepoWorkspace],
        on_stage: Callable[[str], None] | None,
    ) -> str:
        """Create ``$HOME/workspace`` and clone *repos*; return the agent cwd."""
        home = self.run(sandbox_id, 'printf %s "$HOME"').stdout.strip()
        workspace = f"{home}/workspace"
        self.run(sandbox_id, f"mkdir -p {shlex.quote(workspace)}")
        if not repos:
            return workspace
        if on_stage is not None:
            on_stage("cloning")
        clone_dirs = [
            self.materialize_workspace(
                sandbox_id,
                workspace=workspace,
                repo_url=repo.url,
                repo_branch=repo.branch,
                repo_name=dirname,
                git_clone=repo.git_clone,
            )
            for repo, dirname in zip(repos, clone_dir_names(repos), strict=True)
        ]
        return clone_dirs[0] if len(clone_dirs) == 1 else workspace
