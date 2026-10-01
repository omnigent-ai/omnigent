"""
Databricks Sandbox launcher over the REST command-execution API.

Implements :class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher` for
`Databricks Sandbox <https://docs.databricks.com/aws/en/compute/serverless/sandbox>`_
using only the workspace REST API through ``databricks-sdk``
(``w.sandbox.*``): ``create_sandbox`` / ``get_sandbox`` / ``start_sandbox`` /
``delete_sandbox`` for lifecycle and ``execute_command_sync``
(``POST /api/2.0/sandbox-exec/sandboxes/<id>/exec-sync``) for every remote
command. There is no SSH, no CLI subprocess, and no gateway port to reach, so
the server works anywhere that can call the workspace API over HTTPS
(including Databricks Apps compute, whose DNS stubs the SSH gateway).

Platform behavior this launcher is built on (measured live, 2026-10-01):

- ``create_sandbox`` returns ``PENDING`` and the box is ``RUNNING`` within a
  few seconds; the image already ships ``omnigent``, ``git``, ``tmux``,
  ``uv``, ``node``, and the ``databricks`` CLI, with a Databricks token for the
  creating identity pre-provisioned (``DATABRICKS_CONFIG_FILE``).
- ``execute_command_sync`` runs as ``sandbox-agent`` but with ``HOME=/root``,
  which that user cannot write. Every command is therefore wrapped to export
  the passwd home directory first.
- Output is capped at 512 KiB per stream (``truncated=true`` past it) and the
  call blocks until every holder of the stdout/stderr pipes exits — a plain
  ``cmd &`` hangs the request. ``setsid nohup … >log 2>&1 </dev/null &``
  returns at once and the detached process outlives the call, which is what
  :meth:`~omnigent.onboarding.sandboxes.base.SandboxExecTransport.run_background`
  already emits.
- ``execution_timeout`` is honored (``TIMED_OUT`` status, no exit code).
- Stopping kills every process; ``start_sandbox`` does **not** restart the
  host. The managed wake path (:func:`omnigent.server.managed_hosts.
  resume_managed_host`) calls :meth:`resume` and then re-runs
  :meth:`start_host`, which is exactly the re-bootstrap this provider needs.
- An exec against a ``STOPPED`` sandbox transparently starts it.

The SDK's default HTTP client retries timed-out requests, which re-executes a
non-idempotent command. The exec client is therefore built with an HTTP
timeout above the longest execution timeout this launcher requests, so a long
command never trips the client-side timeout in the first place.
"""

from __future__ import annotations

import secrets
import time
from typing import TYPE_CHECKING, Any, ClassVar

import click

from omnigent.onboarding.sandboxes.base import (
    RemoteCommandResult,
    SandboxGoneError,
    SandboxLauncher,
)
from omnigent.onboarding.sandboxes.types import SandboxCapabilities

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from omnigent.onboarding.sandboxes.types import RepoWorkspace

# Longest single remote command (git clone of a large repo, a bootstrap
# install). The server clamps execution_timeout to its own ceiling.
_EXEC_TIMEOUT_S: int = 1800
# Client HTTP timeout for exec calls: above _EXEC_TIMEOUT_S so the SDK never
# times out (and retries, i.e. re-runs) a still-executing command.
_EXEC_HTTP_TIMEOUT_S: int = _EXEC_TIMEOUT_S + 120
_STATE_TIMEOUT_S: float = 600.0
_STATE_POLL_S: float = 1.0

_RUNNING = "SANDBOX_STATE_RUNNING"
_STOPPED = "SANDBOX_STATE_STOPPED"
_STOPPING = "SANDBOX_STATE_STOPPING"
_PENDING = "SANDBOX_STATE_PENDING"

# Exported before every command: exec-sync's environment carries HOME=/root,
# which the unprivileged sandbox user cannot write, so `omnigent host`, git,
# uv, and pip would all fail to persist state. Resolve the real home from
# passwd and start in it.
_HOME_PRELUDE: str = (
    'HOME="$(getent passwd "$(id -un)" | cut -d: -f6)"; export HOME; cd "$HOME" || exit 1; '
    # User-level tool installs (e.g. a bootstrap `uv tool install omnigent`)
    # must shadow the image's baked /usr/local/bin/omnigent.
    'export PATH="$HOME/.local/bin:$PATH"; '
)


def _state_of(sandbox: Any) -> str:
    """Return the ``SANDBOX_STATE_*`` string of an SDK ``Sandbox``, or ``""``."""
    status = getattr(sandbox, "status", None)
    state = getattr(status, "state", None)
    if state is None:
        return ""
    return str(getattr(state, "value", state))


def _is_not_found(exc: Exception) -> bool:
    """Whether an SDK error is a 404 / NOT_FOUND."""
    from databricks.sdk.errors import NotFound

    return isinstance(exc, NotFound)


class DatabricksSandboxLauncher(SandboxLauncher):
    """
    Managed-host launcher for Databricks Sandbox over the REST API.

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
    ) -> None:
        self._profile = profile
        self._prefix = sandbox_id_prefix or "omnigent"
        self._inactivity_timeout_s = inactivity_timeout_s
        self._bootstrap_command = bootstrap_command
        self._client: Any = None

    @property
    def capabilities(self) -> SandboxCapabilities:
        """Managed launch + resume + terminate; no CLI-bootstrap transport."""
        return SandboxCapabilities(
            cli_bootstrap=False,
            managed_launch=True,
            local_port_forward=False,
            resume_stopped=True,
            programmatic_terminate=True,
            file_copy=False,
            streaming_exec=False,
            foreground_exec=False,
            git_clone_options=True,
        )

    # ── SDK plumbing ────────────────────────────────────

    def _api(self) -> Any:
        """Return the SDK ``SandboxAPI`` (built lazily, cached)."""
        if self._client is None:
            try:
                from databricks.sdk import WorkspaceClient
                from databricks.sdk.config import Config
            except ImportError as exc:  # pragma: no cover - extra not installed
                raise click.ClickException(
                    "the 'databricks' sandbox provider needs databricks-sdk>=0.143 "
                    "(pip install 'omnigent[databricks]')"
                ) from exc
            kwargs: dict[str, Any] = {"http_timeout_seconds": _EXEC_HTTP_TIMEOUT_S}
            if self._profile:
                kwargs["profile"] = self._profile
            client = WorkspaceClient(config=Config(**kwargs))
            if not hasattr(client, "sandbox"):
                raise click.ClickException(
                    "the installed databricks-sdk has no Sandbox API; upgrade to "
                    "databricks-sdk>=0.143"
                )
            self._client = client
        return self._client.sandbox

    @staticmethod
    def _resource(sandbox_id: str) -> str:
        return f"sandboxes/{sandbox_id}"

    def _get(self, sandbox_id: str) -> Any:
        try:
            return self._api().get_sandbox(self._resource(sandbox_id))
        except Exception as exc:
            if _is_not_found(exc):
                raise SandboxGoneError(
                    f"Databricks Sandbox '{sandbox_id}' no longer exists"
                ) from exc
            raise click.ClickException(
                f"could not read Databricks Sandbox '{sandbox_id}': {exc}"
            ) from exc

    def _wait_for(self, sandbox_id: str, target: str) -> float:
        """Poll until the sandbox reaches *target*; return seconds waited."""
        start = time.monotonic()
        last = ""
        while time.monotonic() - start < _STATE_TIMEOUT_S:
            last = _state_of(self._get(sandbox_id))
            if last == target:
                return time.monotonic() - start
            time.sleep(_STATE_POLL_S)
        raise click.ClickException(
            f"Databricks Sandbox '{sandbox_id}' did not reach {target} within "
            f"{_STATE_TIMEOUT_S:.0f}s (last state {last or 'unknown'})"
        )

    # ── Lifecycle ───────────────────────────────────────

    def prepare(self) -> None:
        """Fail fast when credentials or the SDK's Sandbox API are missing."""
        try:
            next(iter(self._api().list_sandboxes(page_size=1)), None)
        except click.ClickException:
            raise
        except Exception as exc:
            raise click.ClickException(
                f"could not reach the Databricks Sandbox API "
                f"(profile={self._profile or '<default>'}): {exc}"
            ) from exc

    def provision(self, name: str) -> str:
        """Create a sandbox labelled *name* and wait for it to run."""
        from databricks.sdk.service.sandbox import ComputeSpec, Sandbox, SandboxSpec

        sandbox_id = f"{self._prefix}-{secrets.token_hex(5)}"
        spec = None
        if self._inactivity_timeout_s is not None:
            from google.protobuf.duration_pb2 import Duration

            spec = SandboxSpec(
                compute=ComputeSpec(
                    inactivity_timeout=Duration(seconds=self._inactivity_timeout_s)
                )
            )
        click.echo(f"▸ Creating Databricks Sandbox '{sandbox_id}' ({name})")
        try:
            self._api().create_sandbox(
                Sandbox(display_name=name, spec=spec), sandbox_id=sandbox_id
            )
        except Exception as exc:
            raise click.ClickException(f"could not create a Databricks Sandbox: {exc}") from exc
        try:
            waited = self._wait_for(sandbox_id, _RUNNING)
        except Exception:
            self.terminate(sandbox_id)
            raise
        click.echo(f"  → {sandbox_id} running after {waited:.1f}s")
        return sandbox_id

    def resume(self, sandbox_id: str) -> None:
        """Start a stopped (or stopping) sandbox in place and wait for RUNNING."""
        state = _state_of(self._get(sandbox_id))
        if state == _RUNNING:
            return
        if state == _STOPPING:
            self._wait_for(sandbox_id, _STOPPED)
            state = _STOPPED
        if state == _STOPPED:
            click.echo(f"▸ Starting Databricks Sandbox '{sandbox_id}'")
            try:
                self._api().start_sandbox(self._resource(sandbox_id))
            except Exception as exc:
                if _is_not_found(exc):
                    raise SandboxGoneError(
                        f"Databricks Sandbox '{sandbox_id}' no longer exists"
                    ) from exc
                # A concurrent start already won; fall through to the wait.
                if "already active" not in str(exc):
                    raise click.ClickException(
                        f"could not start Databricks Sandbox '{sandbox_id}': {exc}"
                    ) from exc
        self._wait_for(sandbox_id, _RUNNING)

    def is_running(self, sandbox_id: str) -> bool | None:
        """Map the control-plane state onto running / not running / unknown."""
        try:
            state = _state_of(self._get(sandbox_id))
        except SandboxGoneError:
            return False
        except click.ClickException:
            return None
        if state == _RUNNING:
            return True
        if state in (_STOPPED, _STOPPING, _PENDING):
            return False
        return None

    def terminate(self, sandbox_id: str) -> None:
        """Delete the sandbox; an already-absent sandbox counts as success."""
        try:
            self._api().delete_sandbox(self._resource(sandbox_id))
        except Exception as exc:
            if _is_not_found(exc):
                return
            raise click.ClickException(
                f"could not delete Databricks Sandbox '{sandbox_id}': {exc}"
            ) from exc

    # ── Transport ───────────────────────────────────────

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """Run *command* under ``bash -c`` via ``execute_command_sync``."""
        from google.protobuf.duration_pb2 import Duration

        try:
            response = self._api().execute_command_sync(
                self._resource(sandbox_id),
                "/bin/bash",
                args=["-c", _HOME_PRELUDE + command],
                execution_timeout=Duration(seconds=_EXEC_TIMEOUT_S),
            )
        except Exception as exc:
            if _is_not_found(exc):
                raise SandboxGoneError(
                    f"Databricks Sandbox '{sandbox_id}' no longer exists"
                ) from exc
            raise click.ClickException(
                f"command execution failed on Databricks Sandbox '{sandbox_id}': {exc}"
            ) from exc
        status = str(getattr(response.status, "value", response.status) or "")
        stdout = response.stdout or ""
        stderr = response.stderr or ""
        if response.truncated:
            stderr += "\n[omnigent: output truncated by the sandbox exec API]\n"
        if status.endswith("TIMED_OUT"):
            returncode = 124
            stderr += f"\n[omnigent: command timed out after {_EXEC_TIMEOUT_S}s]\n"
        elif response.exit_code is None:
            returncode = 1 if status.endswith("FAILED") else 0
        else:
            returncode = int(response.exit_code)
        if check and returncode != 0:
            detail = (stderr.strip() or stdout.strip())[-2000:]
            raise click.ClickException(
                f"Remote command failed on Databricks Sandbox '{sandbox_id}' "
                f"(exit {returncode}, {status}): {detail}"
            )
        return RemoteCommandResult(returncode=returncode, stdout=stdout, stderr=stderr)

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
        generic exec-model start.
        """
        self.run(
            sandbox_id,
            # `[o]` keeps the pattern from matching this very bash -c argv.
            "pkill -f '[o]mnigent host' >/dev/null 2>&1; sleep 1; true",
            check=False,
        )
        if self._bootstrap_command:
            if on_stage is not None:
                on_stage("starting")
            self.run(sandbox_id, self._bootstrap_command)
        return super().start_host(
            sandbox_id,
            token=token,
            host_id=host_id,
            host_name=host_name,
            server_url=server_url,
            repos=repos,
            host_config=host_config,
            on_stage=on_stage,
        )
