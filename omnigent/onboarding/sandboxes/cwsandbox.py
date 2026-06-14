"""
CoreWeave Sandbox launcher.

Implements :class:`SandboxLauncher` for `CoreWeave Sandbox
<https://docs.coreweave.com/products/sandboxes>`_ on top of the official
``cwsandbox`` Python SDK. Same posture as the Modal and Daytona
launchers: the SDK is an optional dependency (``pip install
'omnigent[cwsandbox]'``) imported lazily, so the provider can be listed
and the module probed without it.

Supports both server-managed hosts (``host_type="managed"`` sessions)
and the CLI bootstrap flow. The one unsupported primitive is
``forward_local_port``: CW Sandbox has no local→sandbox path, so the
Databricks App OAuth flow doesn't apply — managed hosts authenticate
with a server-minted launch token instead.

Notes that shape this launcher:

- Egress defaults to none on CW Sandbox, so :meth:`provision` requests
  ``egress_mode="internet"`` — a managed host must dial the server out.
- Lifetime is a single hard cap (``max_lifetime_seconds``); there is no
  idle auto-stop. The managed token TTL is set above the cap.
- Credentials and base URL come from the SDK's own env vars
  (``CWSANDBOX_API_KEY`` / ``CWSANDBOX_BASE_URL``), 12-factor.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar

import click

from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    RemoteCommandResult,
    RemoteProcess,
    SandboxLauncher,
    host_image_wheel_install_command,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from cwsandbox import Sandbox
    from cwsandbox._types import Process

HOST_IMAGE_ENV_VAR = "OMNIGENT_CWSANDBOX_HOST_IMAGE"
SANDBOX_ENV_PASSTHROUGH_ENV_VAR = "OMNIGENT_CWSANDBOX_SANDBOX_ENV"
MAX_LIFETIME_ENV_VAR = "OMNIGENT_CWSANDBOX_MAX_LIFETIME_S"

_DEFAULT_MAX_LIFETIME_S = 24 * 60 * 60
_SANDBOX_RESOURCES = {"cpu": "2", "memory": "4Gi"}
# Slack the managed launch-token TTL is set above the sandbox lifetime, so a
# live sandbox can always re-authenticate its tunnel across reconnects.
_TOKEN_TTL_SLACK_S = 3600
# Where exec_foreground records the remote pid so Ctrl-C can kill it — the
# SDK's process.cancel() only cancels the local future, not the remote exec.
_FOREGROUND_PIDFILE = "/tmp/oa-foreground.pid"


def resolve_max_lifetime_s() -> int:
    """Resolve the sandbox lifetime cap in seconds (env override or default)."""
    raw = os.environ.get(MAX_LIFETIME_ENV_VAR)
    if raw is None:
        return _DEFAULT_MAX_LIFETIME_S
    try:
        return int(float(raw))
    except ValueError as exc:
        raise click.ClickException(
            f"{MAX_LIFETIME_ENV_VAR} must be a number of seconds"
        ) from exc


def managed_token_ttl_s() -> int:
    """Launch-token TTL, derived from (and always above) the sandbox lifetime."""
    return resolve_max_lifetime_s() + _TOKEN_TTL_SLACK_S


def _ensure_sdk() -> None:
    """Verify the cwsandbox SDK is importable, with an install hint when not."""
    try:
        import cwsandbox  # noqa: F401
    except ImportError as exc:
        raise click.ClickException(
            "The cwsandbox SDK is required for the 'cwsandbox' sandbox provider. "
            "Install it with `pip install 'omnigent[cwsandbox]'`, then set "
            "CWSANDBOX_API_KEY (and optionally CWSANDBOX_BASE_URL)."
        ) from exc


class _CWRemoteProcess(RemoteProcess):
    """:class:`RemoteProcess` over a cwsandbox ``Process`` (combined output)."""

    def __init__(self, process: Process) -> None:
        self._process = process
        self._lines: Iterator[str] = iter(process.stdout)

    @property
    def lines(self) -> Iterator[str]:
        return self._lines

    def wait(self) -> int:
        return self._process.wait()

    def close(self) -> None:
        # DEVIATION from the base contract: the SDK's process.cancel() only
        # cancels the local future, not the remote exec, so a still-running
        # process is left to die with the sandbox. Unreachable in practice —
        # the only stream_exec consumer is the App OAuth flow, which the
        # bootstrap gates off for cwsandbox (supports_local_port_forward=False).
        self._process.cancel()


class CWSandboxLauncher(SandboxLauncher):
    """:class:`SandboxLauncher` for CoreWeave Sandbox, over the ``cwsandbox`` SDK."""

    provider: ClassVar[str] = "cwsandbox"
    supports_local_port_forward: ClassVar[bool] = False

    def __init__(self, *, image: str | None = None, env: Sequence[str] | None = None) -> None:
        """
        :param image: Registry image to provision from
            (``sandbox.cwsandbox.image``); ``None`` resolves
            :data:`HOST_IMAGE_ENV_VAR` then the official host image.
        :param env: Server-process env var NAMES to inject into every
            sandbox (``sandbox.cwsandbox.env``); ``None`` resolves
            :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR`.
        """
        # Import the SDK now, on the constructing thread. The managed flow
        # builds the launcher on the event-loop (main) thread but calls
        # prepare()/provision() via asyncio.to_thread; the cwsandbox SDK
        # installs signal handlers at import time, which raises in a
        # worker thread ("signal only works in main thread"). Importing
        # here makes the later worker-thread imports cached no-ops.
        # Removable once the SDK fix ships (coreweave/cwsandbox-client#136,
        # PR #138 — skip signal handlers outside the main thread).
        _ensure_sdk()
        self._image_ref = image
        self._env_names = tuple(env) if env is not None else None
        self._sandboxes: dict[str, Sandbox] = {}

    def _resolve(self, sandbox_id: str) -> Sandbox:
        """Return the cached handle for *sandbox_id*, attaching on first use."""
        _ensure_sdk()
        from cwsandbox import Sandbox
        from cwsandbox.exceptions import SandboxNotFoundError

        handle = self._sandboxes.get(sandbox_id)
        if handle is None:
            try:
                handle = Sandbox.from_id(sandbox_id).result()
            except SandboxNotFoundError as exc:
                raise click.ClickException(
                    f"CW Sandbox '{sandbox_id}' not found — it may have been stopped "
                    "or reached its lifetime cap."
                ) from exc
            self._sandboxes[sandbox_id] = handle
        return handle

    def _resolve_sandbox_env(self) -> dict[str, str]:
        """Resolve env var names to inject, reading values from the server env."""
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
                    f"sandbox env passthrough names '{name}' but it is not set in the "
                    "server's environment — set it or remove it from sandbox.cwsandbox.env "
                    f"/ {SANDBOX_ENV_PASSTHROUGH_ENV_VAR}."
                )
            resolved[name] = value
        return resolved

    def prepare(self) -> None:
        """Preflight: the SDK must be installed and an API key available."""
        _ensure_sdk()
        if not os.environ.get("CWSANDBOX_API_KEY"):
            raise click.ClickException(
                "No CW Sandbox credentials found — set CWSANDBOX_API_KEY to a "
                "CoreWeave Sandbox API key."
            )

    def provision(self, name: str) -> str:
        """Create a sandbox from the host image and wait until it is running."""
        _ensure_sdk()
        from cwsandbox import NetworkOptions, Sandbox
        from cwsandbox.exceptions import CWSandboxError

        image = self._image_ref or os.environ.get(HOST_IMAGE_ENV_VAR) or DEFAULT_HOST_IMAGE
        max_lifetime = resolve_max_lifetime_s()
        env_vars = self._resolve_sandbox_env()
        click.echo(f"▸ Creating CW Sandbox '{name}' from {image}")
        try:
            sandbox = Sandbox.run(
                "sleep",
                "infinity",
                container_image=image,
                max_lifetime_seconds=max_lifetime,
                resources=dict(_SANDBOX_RESOURCES),
                # Egress defaults to none; a managed host must dial the server out.
                network=NetworkOptions(egress_mode="internet"),
                environment_variables=env_vars or None,
                tags=["omnigent", name],
            )
            sandbox.wait()
        except CWSandboxError as exc:
            raise click.ClickException(f"CW Sandbox creation failed: {exc}") from exc
        sandbox_id = sandbox.sandbox_id
        if not sandbox_id:
            raise click.ClickException("CW Sandbox creation returned no sandbox id")
        self._sandboxes[sandbox_id] = sandbox
        click.echo(f"  → created {sandbox_id}")
        return sandbox_id

    def attach(self, sandbox_id: str) -> None:
        """Validate access to an existing sandbox by id."""
        click.echo(f"▸ Reusing existing CW Sandbox '{sandbox_id}'")
        self._resolve(sandbox_id)

    def keep_alive(self, sandbox_id: str) -> None:
        """Nothing to configure: lifetime is fixed at provision; surface the cap."""
        click.echo(
            f"  → CW Sandbox lifetime is fixed at creation; '{sandbox_id}' is reaped at "
            "its max_lifetime_seconds (override OMNIGENT_CWSANDBOX_MAX_LIFETIME_S)."
        )

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """Run ``bash -lc <command>`` in the sandbox and capture its output."""
        handle = self._resolve(sandbox_id)
        try:
            result = handle.exec(["bash", "-lc", command]).result()
        except Exception as exc:
            # Catch broadly: besides CWSandboxError, .result() can raise a
            # plain concurrent.futures.TimeoutError; both must surface as the
            # launcher-contract ClickException, not leak to the caller raw.
            raise click.ClickException(
                f"Remote command failed to execute on sandbox '{sandbox_id}': {exc}"
            ) from exc
        for line in result.stdout.splitlines():
            if line.strip():
                click.echo(line)
        if check and result.returncode != 0:
            raise click.ClickException(
                f"Remote command failed on sandbox '{sandbox_id}' "
                f"(exit {result.returncode}): {command}"
            )
        return RemoteCommandResult(
            returncode=result.returncode, stdout=result.stdout, stderr=result.stderr
        )

    def put(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        """Copy a local file into the sandbox via write_file."""
        from cwsandbox.exceptions import CWSandboxError

        try:
            self._resolve(sandbox_id).write_file(remote_path, local_path.read_bytes()).result()
        except CWSandboxError as exc:
            raise click.ClickException(
                f"File upload to sandbox '{sandbox_id}' failed: {exc}"
            ) from exc

    def stream_exec(self, sandbox_id: str, command: str, *, pty: bool = False) -> RemoteProcess:
        """Spawn a command and stream its combined output line by line."""
        handle = self._resolve(sandbox_id)
        # exec returns separate stdout/stderr; merge in-shell for the
        # combined-output RemoteProcess contract.
        process = handle.exec(["bash", "-lc", f"{command} 2>&1"])
        return _CWRemoteProcess(process)

    def exec_foreground(self, sandbox_id: str, command: str) -> int:
        """Run *command*, echo its output locally until exit; Ctrl-C kills it."""
        handle = self._resolve(sandbox_id)
        # Record the remote pid (process.cancel() can't kill the remote exec):
        # `echo $$ … && exec` keeps the pid across the shell swap, so the
        # interrupt path can kill it with a second exec.
        remote = f"echo $$ > {_FOREGROUND_PIDFILE} && exec {command} 2>&1"
        process = handle.exec(["bash", "-lc", remote])
        try:
            for line in process.stdout:
                click.echo(line, nl=False)
            return process.wait()
        except KeyboardInterrupt:
            click.echo("\n  → detaching; stopping the remote process")
            handle.exec(["bash", "-lc", f"kill $(cat {_FOREGROUND_PIDFILE}) 2>/dev/null"]).wait()
            raise

    def wheel_install_command(self, remote_tgz_path: str) -> str:
        """Overlay shipped wheels onto the prebaked host image."""
        return host_image_wheel_install_command(remote_tgz_path)

    def terminate(self, sandbox_id: str) -> None:
        """Stop a sandbox; an already-gone sandbox is treated as success."""
        _ensure_sdk()
        from cwsandbox import Sandbox
        from cwsandbox.exceptions import CWSandboxError, SandboxNotFoundError

        try:
            handle = self._sandboxes.get(sandbox_id) or Sandbox.from_id(sandbox_id).result()
            handle.stop().result()
        except SandboxNotFoundError:
            pass  # Already gone — the desired end state holds.
        except CWSandboxError as exc:
            raise click.ClickException(f"Failed to stop CW Sandbox '{sandbox_id}': {exc}") from exc
        self._sandboxes.pop(sandbox_id, None)
