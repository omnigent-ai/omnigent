"""Microsandbox sandbox launcher (local libkrun microVMs).

Implements :class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher` for
`microsandbox <https://github.com/superradcompany/microsandbox>`_ - an
embedded microVM runtime that boots OCI images directly as hardware-isolated
VMs (libkrun; Apple Silicon macOS / KVM Linux). This module ships in the OSS
build; the microsandbox SDK itself is an optional dependency
(``pip install 'omnigent[microsandbox]'``) imported lazily, so the provider
can be listed and the module probed without it.

Platform traits that shape this launcher:

- **Embedded, no daemon.** The SDK bundles the runtime; sandboxes are spawned
  as host-side child processes and persist state under ``~/.microsandbox``.
  Sandboxes are created ``detached`` so they survive the launching CLI/server
  process; any later launcher instance reconnects by name.
- **Names are the id.** Microsandbox has no separate object id - the unique
  sandbox NAME is the canonical reference. ``provision`` appends a random
  suffix so repeated creates under the same label never collide.
- **Fast stop/restart lifecycle.** A stopped sandbox keeps its writable layer
  and restarts in place in well under a second, so ``can_resume`` is ``True``
  and idle sandboxes are drained (``idle_timeout``) instead of killed.
- **Guest-to-host networking.** The guest reaches the machine running the
  server at ``host.microsandbox.internal`` - the default network policy here
  is public egress plus a host allow-rule so a locally self-hosted server's
  ``server_url`` is reachable, and ``forward_local_port`` rides the same path
  via an in-guest relay.

Concurrency model: the SDK is async-only; omnigent calls launcher methods
synchronously (the server marshals them off its event loop via
``asyncio.to_thread``). Mirroring the boxlite launcher, every SDK call is
marshalled onto a single PROCESS-LIFETIME background loop thread
(:func:`_run`) - one daemon thread for all launcher instances.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import os
import platform
import secrets
import shlex
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

import click

from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    RemoteCommandResult,
    RemoteProcess,
    SandboxLauncher,
    host_image_wheel_install_command,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine, Generator

    import microsandbox as microsandbox_sdk


# Coroutine result marshalled back through the shared loop (see _run).
_T = TypeVar("_T")


# ── Constants ──────────────────────────────────────────

HOST_IMAGE_ENV_VAR: str = "OMNIGENT_MICROSANDBOX_HOST_IMAGE"
"""Environment variable overriding
:data:`~omnigent.onboarding.sandboxes.base.DEFAULT_HOST_IMAGE` for
microsandbox VMs, e.g. an org-internal copy of the host image
(``ghcr.io/<your-org>/omnigent-host:latest``)."""

SANDBOX_ENV_PASSTHROUGH_ENV_VAR: str = "OMNIGENT_MICROSANDBOX_SANDBOX_ENV"
"""Environment variable naming (comma-separated) the SERVER-process environment
variables whose values are injected into every sandbox this launcher creates -
typically the harness LLM credentials (``ANTHROPIC_API_KEY``,
``OPENAI_API_KEY``, gateway base URLs, ...) and ``GIT_TOKEN`` that the in-VM
host forwards to runners. Names, not values: read from the server's own
environment at provision time, so secrets never live in config files. The
server's managed-host config (``sandbox.microsandbox.env``) takes precedence
when set."""

DEFAULT_IDLE_TIMEOUT_S: int = 24 * 3600
"""Default ``idle_timeout`` requested at creation: a forgotten local microVM
drains itself (releasing CPU/RAM, keeping its writable layer) after a day of
inactivity, and :meth:`MicrosandboxSandboxLauncher.resume` restarts it in
place. Operators override via ``sandbox.microsandbox.idle_timeout_s`` (``0``
disables draining entirely)."""

NETWORK_MODES: tuple[str, ...] = ("host", "public-only", "all")
"""Recognized values for the launcher's ``network`` setting. ``host`` (the
default) is microsandbox's public-egress-only posture PLUS an allow-rule for
guest-to-host traffic, so hosts can dial back to a server on this machine via
``host.microsandbox.internal``; ``public-only`` drops the host rule (use it
when ``server_url`` is a genuinely public URL); ``all`` opens LAN/private
egress too."""

# Resources for the VM. Matches the Modal / Daytona / boxlite launchers:
# 2 vCPU / 4 GiB is enough for a host running one interactive session.
_SANDBOX_CPUS: int = 2
_SANDBOX_MEMORY_MIB: int = 4096

# Marshalling timeouts (seconds). The first provision from a given image makes
# microsandbox pull the OCI image, which for the ~GiB host image can take
# minutes; later boots reuse the cached image and finish in seconds.
_PROVISION_TIMEOUT_S: float = 900.0
_RUN_TIMEOUT_S: float = 600.0
_TERMINATE_TIMEOUT_S: float = 120.0
_CONNECT_TIMEOUT_S: float = 60.0

# How long forward_local_port waits for the in-guest relay to report its
# listener bound before giving up.
_RELAY_READY_TIMEOUT_S: float = 15.0

# In-guest TCP relay backing forward_local_port: listens on the guest's
# loopback and pipes every connection to the same port on the host machine
# (host.microsandbox.internal). Formatted with the port; needs guest python3.
_RELAY_SCRIPT = """\
import asyncio

PORT = {port}
TARGET = "host.microsandbox.internal"


async def _pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()


async def _handle(reader, writer):
    try:
        target_reader, target_writer = await asyncio.open_connection(TARGET, PORT)
    except OSError:
        writer.close()
        return
    await asyncio.gather(
        _pipe(reader, target_writer), _pipe(target_reader, writer), return_exceptions=True
    )


async def _main():
    server = await asyncio.start_server(_handle, "127.0.0.1", PORT)
    print("relay-ready", flush=True)
    async with server:
        await server.serve_forever()


asyncio.run(_main())
"""


# ── Shared process-lifetime event loop ─────────────────

_loop_lock = threading.Lock()
_shared_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    """
    Return the shared microsandbox event loop, starting its daemon thread once.

    Recreates the loop (and thread) if a prior one was closed or its thread
    died - else a dead loop would brick every later microsandbox call for the
    process lifetime.
    """
    global _shared_loop, _loop_thread
    with _loop_lock:
        alive = (
            _shared_loop is not None
            and not _shared_loop.is_closed()
            and _loop_thread is not None
            and _loop_thread.is_alive()
        )
        if not alive:
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever, name="microsandbox-runtime", daemon=True
            )
            thread.start()
            _shared_loop = loop
            _loop_thread = thread
        assert _shared_loop is not None  # set just above when not alive
        return _shared_loop


# Grace added to the outer wait once a timeout fires: the in-loop wait_for
# should cancel the coroutine well within this, so the outer result() only
# trips if cancellation itself hangs (a double fault).
_CANCEL_GRACE_S: float = 30.0


def _run(coro: Coroutine[Any, Any, _T], *, timeout: float | None) -> _T:
    """
    Run *coro* on the shared loop and block for its result.

    The timeout is applied in-loop via ``asyncio.wait_for`` so it cancels the
    coroutine instead of orphaning it; the outer ``result`` is a grace
    backstop should cancellation hang. ``timeout=None`` waits indefinitely -
    used for streaming reads whose duration is caller-controlled (a foreground
    host process, an OAuth login waiting on the user).

    :raises asyncio.TimeoutError: when *coro* exceeds *timeout*.
    """
    if timeout is None:
        return asyncio.run_coroutine_threadsafe(coro, _get_loop()).result()

    async def _bounded() -> _T:
        return await asyncio.wait_for(coro, timeout)

    future = asyncio.run_coroutine_threadsafe(_bounded(), _get_loop())
    return future.result(timeout=timeout + _CANCEL_GRACE_S)


def _ensure_sdk() -> None:
    """
    Verify the microsandbox SDK is importable, with an install hint when not.

    Called at the top of every launcher entry point because the SDK is an
    optional dependency - the base ``omnigent`` install does not pull it in.

    :raises click.ClickException: When the ``microsandbox`` package is not
        installed.
    """
    try:
        import microsandbox  # noqa: F401  # presence probe only
    except ImportError as exc:
        raise click.ClickException(
            "The microsandbox SDK is required for the 'microsandbox' sandbox "
            "provider. Install it with `pip install 'omnigent[microsandbox]'`. "
            "It needs hardware virtualization: Apple Silicon on macOS, KVM on "
            "Linux."
        ) from exc


def _echo_lines(stream: str, *, err: bool = False) -> None:
    """
    Echo a captured remote output stream line-by-line, dropping
    pure-whitespace lines.

    :param stream: Captured stdout or stderr from a remote command.
    :param err: Whether to echo to the local stderr stream.
    """
    for line in stream.splitlines():
        if line.strip():
            click.echo(line, err=err)


class _MicrosandboxRemoteProcess(RemoteProcess):
    """
    :class:`RemoteProcess` over a microsandbox ``ExecHandle``.

    The handle is an async iterator of exec events; this wrapper pulls them
    through the shared loop and re-chunks the byte payloads into lines. The
    spawn site merges stderr in-shell (or via a TTY), so stdout- and
    stderr-tagged events alike carry the combined output in order.
    """

    def __init__(self, handle: microsandbox_sdk.ExecHandle) -> None:
        """
        Wrap a running streamed exec.

        :param handle: Handle returned by ``Sandbox.shell_stream``.
        """
        self._handle = handle
        self._exit_code: int | None = None
        self._closed = False
        # Materialize the line iterator once so repeated `lines` reads
        # resume the same stream (the RemoteProcess contract).
        self._lines: Iterator[str] = self._iter_lines()

    @property
    def lines(self) -> Iterator[str]:
        """
        The process's combined-output line iterator (same object on every
        access).

        :returns: Line iterator over the process's output.
        """
        return self._lines

    def _next_event(self) -> object | None:
        """Pull the next exec event off the shared loop, ``None`` at EOF."""

        # The SDK's ExecHandle methods bind to the running loop at CALL time
        # (pyo3), so the call itself must happen on the loop thread - hence
        # the async closure instead of passing the coroutine directly.
        async def _anext() -> object:
            return await self._handle.__anext__()

        try:
            return _run(_anext(), timeout=None)
        except StopAsyncIteration:
            return None

    def _iter_lines(self) -> Iterator[str]:
        """Yield output lines, recording the exit code as events arrive."""
        # Incremental decoder: a multi-byte UTF-8 sequence may be split
        # across event payloads, so per-event decode would mangle it.
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        buffer = ""
        while True:
            event = self._next_event()
            if event is None:
                break
            event_type = getattr(event, "event_type", None)
            if event_type in ("stdout", "stderr"):
                data = getattr(event, "data", None) or b""
                buffer += decoder.decode(data)
                while "\n" in buffer:
                    line, _, buffer = buffer.partition("\n")
                    yield line + "\n"
            elif event_type == "exited":
                code = getattr(event, "code", None)
                self._exit_code = 0 if code is None else int(code)
        buffer += decoder.decode(b"", final=True)
        if buffer:
            yield buffer

    def wait(self) -> int:
        """
        Block until the process exits.

        :returns: The process's exit code.
        """
        if self._exit_code is None and not self._closed:
            # Drain remaining events (recording the exit code) - the caller
            # has stopped consuming lines, so discarding them is fine. After
            # a close() the stream may never EOF, so skip straight to wait.
            for _ in self._lines:
                pass
        if self._exit_code is None:
            # Stream ended without an exited event (e.g. transport teardown);
            # ask the handle directly (on the loop thread - see _next_event).
            async def _wait() -> tuple[int, bool]:
                return await self._handle.wait()

            code, _success = _run(_wait(), timeout=_RUN_TIMEOUT_S)
            self._exit_code = int(code)
        return self._exit_code

    def close(self) -> None:
        """
        Terminate the process if it is still running and reap it. Idempotent:
        safe after :meth:`wait` and after a prior successful ``close``. A
        failed kill/reap leaves the handle open so a retry can still reap.
        """
        if self._closed:
            return
        if self._exit_code is None:
            # Kill may race a natural exit - suppress it and let wait()
            # deliver the (possibly natural) exit code as the reap.
            async def _kill_and_wait() -> int:
                with contextlib.suppress(Exception):
                    await self._handle.kill()
                code, _success = await self._handle.wait()
                return int(code)

            try:
                self._exit_code = _run(_kill_and_wait(), timeout=_TERMINATE_TIMEOUT_S)
            except Exception:
                return
        self._closed = True


class MicrosandboxSandboxLauncher(SandboxLauncher):
    """
    :class:`SandboxLauncher` for microsandbox microVMs on the local machine.

    All transport rides the microsandbox async SDK marshalled onto the shared
    loop: ``Sandbox.create`` / ``get`` / ``start`` / ``remove`` for lifecycle,
    ``sandbox.shell`` (+ ``shell_stream``) for commands, ``sandbox.fs`` for
    file shipping. Connected ``Sandbox`` objects are cached per name to avoid
    re-attaching on every primitive.
    """

    provider: ClassVar[str] = "microsandbox"
    # Public PyPI is reachable from the local wheel build; ambient uv config
    # applies.
    wheel_build_index_url: ClassVar[str | None] = None
    # forward_local_port bridges via an in-guest relay to
    # host.microsandbox.internal (see forward_local_port), so the in-sandbox
    # App OAuth flow works under the default "host" network mode.
    supports_local_port_forward: ClassVar[bool] = True
    # Stopped/drained sandboxes keep their writable layer and restart in
    # place, so the server's managed-host wake path may revive them.
    can_resume: ClassVar[bool] = True

    def __init__(
        self,
        *,
        image: str | None = None,
        cpus: int | None = None,
        memory_mib: int | None = None,
        env: Sequence[str] | None = None,
        idle_timeout_s: int | None = None,
        network: str | None = None,
        host_ports: Sequence[int] | None = None,
    ) -> None:
        """
        Initialize the launcher.

        :param image: Registry image reference with omnigent pre-installed,
            e.g. ``"docker.io/me/omnigent-host:latest"`` - the server's
            ``sandbox.microsandbox.image`` config. ``None`` resolves
            :data:`HOST_IMAGE_ENV_VAR` and falls back to
            :data:`~omnigent.onboarding.sandboxes.base.DEFAULT_HOST_IMAGE`.
        :param cpus: vCPUs per sandbox; ``None`` uses 2.
        :param memory_mib: Memory per sandbox in MiB; ``None`` uses 4096.
        :param env: Optional names of server-process environment variables to
            inject into every sandbox, e.g. ``["OPENAI_API_KEY", "GIT_TOKEN"]``
            - the server's ``sandbox.microsandbox.env`` config. ``None``
            resolves :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR` (comma-separated)
            and falls back to injecting nothing.
        :param idle_timeout_s: Seconds of inactivity after which the sandbox
            drains itself (restartable via :meth:`resume`); ``None`` uses
            :data:`DEFAULT_IDLE_TIMEOUT_S`, ``0`` disables draining.
        :param network: One of :data:`NETWORK_MODES`; ``None`` uses ``host``.
        :param host_ports: Under the ``host`` network mode, restrict
            guest-to-host traffic to these TCP ports (e.g. the omnigent
            server port plus an LLM-gateway port). ``None`` allows every
            host port - required for the CLI bootstrap, whose OAuth relay
            port isn't known at creation time; the server's managed path
            always passes an explicit list so untrusted agents cannot
            reach unrelated host-local services.
        :raises click.ClickException: When *network* is not a recognized mode.
        """
        if network is not None and network not in NETWORK_MODES:
            raise click.ClickException(
                f"unknown microsandbox network mode '{network}' - expected one "
                f"of: {', '.join(NETWORK_MODES)}"
            )
        self._image_ref = image
        self._cpus = cpus if cpus is not None else _SANDBOX_CPUS
        self._memory_mib = memory_mib if memory_mib is not None else _SANDBOX_MEMORY_MIB
        self._env_names = tuple(env) if env is not None else None
        self._idle_timeout_s = (
            idle_timeout_s if idle_timeout_s is not None else (DEFAULT_IDLE_TIMEOUT_S)
        )
        self._network_mode = network or "host"
        self._host_ports = tuple(host_ports) if host_ports is not None else None
        self._connections: dict[str, microsandbox_sdk.Sandbox] = {}

    # ── Config resolution ──────────────────────────────

    def _resolve_image(self) -> str:
        """Resolve the image ref: explicit config → env override → default."""
        return self._image_ref or os.environ.get(HOST_IMAGE_ENV_VAR) or DEFAULT_HOST_IMAGE

    def _resolve_sandbox_env(self) -> dict[str, str]:
        """
        Resolve the env vars to inject into created sandboxes.

        Explicit constructor names win; otherwise
        :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR` (comma-separated) applies; an
        empty resolution injects nothing. Values come from the server's own
        environment; a configured name that is unset there fails loud rather
        than launching without a credential the agent needs.

        :returns: Name → value mapping for ``Sandbox.create(env=...)``.
        :raises click.ClickException: When a configured name is not set in the
            server process environment.
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
                    f"sandbox env passthrough names '{name}' but it is not set in "
                    "the server's environment - set it (or remove it from "
                    f"sandbox.microsandbox.env / {SANDBOX_ENV_PASSTHROUGH_ENV_VAR})."
                )
            resolved[name] = value
        return resolved

    def _build_network(self) -> microsandbox_sdk.Network:
        """
        Build the sandbox network config for the configured mode.

        ``host`` keeps microsandbox's public-egress-only posture and adds
        guest-to-host allow-rules (``host.microsandbox.internal``), so hosts
        can dial back to a server on this machine and
        :meth:`forward_local_port` can relay. Loopback / private-LAN /
        metadata destinations stay blocked, and when ``host_ports`` is set
        the host rules are scoped to just those TCP ports.
        """
        import microsandbox as msb

        if self._network_mode == "all":
            return msb.Network.allow_all()
        if self._network_mode == "public-only":
            return msb.Network.public_only()
        host = msb.Destination.group(msb.DestGroup.HOST)
        host_rules: tuple[msb.Rule, ...]
        if self._host_ports is None:
            host_rules = (msb.Rule.allow(destination=host),)
        else:
            host_rules = tuple(
                msb.Rule.allow(protocol=msb.Protocol.TCP, port=port, destination=host)
                for port in self._host_ports
            )
        return msb.Network(
            policy=msb.NetworkPolicy(
                default_egress=msb.Action.DENY,
                rules=(
                    *msb.Rule.allow_dns(),
                    msb.Rule.allow(destination=msb.Destination.group(msb.DestGroup.PUBLIC)),
                    *host_rules,
                ),
            )
        )

    # ── Connections ────────────────────────────────────

    async def _aconnect(self, sandbox_id: str) -> microsandbox_sdk.Sandbox:
        """
        Return a connected ``Sandbox`` for *sandbox_id*, reattaching on first
        use (sandboxes are detached and outlive the process that created them).

        :raises click.ClickException: When the sandbox does not exist or is
            not running.
        """
        cached = self._connections.get(sandbox_id)
        if cached is not None:
            return cached
        import microsandbox as msb

        try:
            handle = await msb.Sandbox.get(sandbox_id)
        except msb.SandboxNotFoundError as exc:
            raise click.ClickException(
                f"microsandbox sandbox '{sandbox_id}' not found - it may have "
                "been removed. Managed sessions provision a replacement on the "
                "next message."
            ) from exc
        if handle.status != msb.SandboxStatus.RUNNING:
            raise click.ClickException(
                f"microsandbox sandbox '{sandbox_id}' is {handle.status} - "
                "resume it first (`omnigent sandbox connect` restarts stopped "
                "sandboxes; managed sessions wake them automatically)."
            )
        sandbox = await handle.connect(timeout=_CONNECT_TIMEOUT_S)
        self._connections[sandbox_id] = sandbox
        return sandbox

    def _forget(self, sandbox_id: str) -> None:
        """Drop the cached connection for *sandbox_id*, if any."""
        self._connections.pop(sandbox_id, None)

    # ── SandboxLauncher primitives ─────────────────────

    def prepare(self) -> None:
        """
        Local preflight: the microsandbox SDK must be installed and the
        machine must support hardware virtualization.

        :raises click.ClickException: When the SDK is missing, macOS is not
            Apple Silicon, or Linux has no ``/dev/kvm``.
        """
        _ensure_sdk()
        system = platform.system()
        if system == "Darwin" and platform.machine() != "arm64":
            raise click.ClickException(
                "microsandbox on macOS requires Apple Silicon - Intel Macs are not supported."
            )
        if system == "Linux" and not os.path.exists("/dev/kvm"):
            raise click.ClickException(
                "microsandbox on Linux requires KVM, but /dev/kvm was not "
                "found. Enable KVM and add the user to the 'kvm' group."
            )

    def provision(self, name: str) -> str:
        """
        Create a new detached microsandbox VM from the host image.

        The sandbox name doubles as its id, so a random suffix keeps repeated
        creates under the same label collision-free. The VM is created
        detached (it survives this process) with the configured idle-drain
        timeout; the managed-session machinery owns its teardown.

        :param name: Human-readable label, e.g. ``"managed-a1b2c3d4"``.
        :returns: The sandbox name/id, e.g. ``"managed-a1b2c3d4-9f01ab"``.
        :raises click.ClickException: If creation fails (image pull failure,
            no virtualization, ...).
        """
        _ensure_sdk()
        resolved_ref = self._resolve_image()
        env = self._resolve_sandbox_env()
        sandbox_id = f"{name}-{secrets.token_hex(3)}"
        click.echo(f"▸ Creating microsandbox VM '{sandbox_id}' from {resolved_ref}")

        async def _do() -> None:
            import microsandbox as msb

            create = msb.Sandbox.create
            kwargs: dict[str, Any] = {
                "image": resolved_ref,
                "cpus": self._cpus,
                "memory": self._memory_mib,
                "env": env,
                "network": self._build_network(),
                "detached": True,
                "labels": {"omnigent-name": name},
            }
            if self._idle_timeout_s > 0:
                kwargs["idle_timeout"] = self._idle_timeout_s
            sandbox = await create(sandbox_id, **kwargs)
            self._connections[sandbox_id] = sandbox

        try:
            _run(_do(), timeout=_PROVISION_TIMEOUT_S)
        except click.ClickException:
            self._best_effort_remove(sandbox_id)
            raise
        except (TimeoutError, asyncio.CancelledError):
            # Cancellation is not atomic: the native create may register the
            # VM a moment AFTER the coroutine was cancelled, so keep probing
            # for it briefly - else it leaks untracked with no lifetime cap.
            self._best_effort_remove(sandbox_id, retry_not_found=True)
            raise click.ClickException(
                f"microsandbox VM creation timed out after {_PROVISION_TIMEOUT_S:.0f}s"
            ) from None
        except Exception as exc:
            self._best_effort_remove(sandbox_id)
            # Surface the provider's reason (image pull failure, no
            # virtualization, ...) so the managed-launch 502 carries it
            # verbatim.
            raise click.ClickException(f"microsandbox VM creation failed: {exc}") from exc
        click.echo(f"  → created {sandbox_id}")
        return sandbox_id

    def _best_effort_remove(self, sandbox_id: str, *, retry_not_found: bool = False) -> None:
        """
        Kill and remove a sandbox by name, swallowing every error. Used to
        clean up a provision that failed or was cancelled - the sandbox may
        exist even though ``create()`` never returned.

        :param retry_not_found: Keep probing briefly when the sandbox is not
            found yet - a CANCELLED create may register it a moment after the
            cancellation. Only the timeout/cancel path pays this wait.
        """
        attempts = 3 if retry_not_found else 1

        async def _do() -> None:
            import microsandbox as msb

            for attempt in range(attempts):
                try:
                    handle = await msb.Sandbox.get(sandbox_id)
                except msb.SandboxNotFoundError:
                    if attempt == attempts - 1:
                        return
                    await asyncio.sleep(2.0)
                    continue
                with contextlib.suppress(Exception):
                    await handle.kill()
                await handle.remove()
                return

        self._forget(sandbox_id)
        with contextlib.suppress(Exception):
            _run(_do(), timeout=_TERMINATE_TIMEOUT_S)

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """
        Run a shell command in the sandbox and capture its output.

        :param sandbox_id: Target sandbox name.
        :param command: Shell command to execute remotely (runs under the
            image's ``/bin/sh``).
        :param check: When ``True``, raise on non-zero exit.
        :returns: Exit code plus captured stdout/stderr.
        :raises click.ClickException: If the sandbox is gone, the command
            times out, or *check* is ``True`` and the command exits non-zero.
        """
        _ensure_sdk()

        async def _do() -> tuple[int, str, str]:
            sandbox = await self._aconnect(sandbox_id)
            # The SDK-side timeout kills the GUEST process; the _run wait_for
            # only cancels the coroutine.
            output = await sandbox.shell(command, timeout=_RUN_TIMEOUT_S)
            return output.exit_code, output.stdout_text, output.stderr_text

        try:
            # Bound above the guest timeout so the guest kill fires first.
            exit_code, stdout, stderr = _run(_do(), timeout=_RUN_TIMEOUT_S + _CANCEL_GRACE_S)
        except click.ClickException:
            raise
        except Exception as exc:
            raise click.ClickException(
                f"Remote command failed to execute on microsandbox '{sandbox_id}': {exc}"
            ) from exc
        _echo_lines(stdout)
        _echo_lines(stderr, err=True)
        if check and exit_code != 0:
            # Surface a stderr tail (e.g. git-clone "fatal: ...") - a bare
            # exit code is otherwise opaque.
            stderr_tail = stderr.strip()[-800:]
            detail = f" - {stderr_tail}" if stderr_tail else ""
            raise click.ClickException(
                f"Remote command failed on microsandbox '{sandbox_id}' "
                f"(exit {exit_code}): {command}{detail}"
            )
        return RemoteCommandResult(returncode=exit_code, stdout=stdout, stderr=stderr)

    def attach(self, sandbox_id: str) -> None:
        """
        Validate access to an existing sandbox, restarting it in place if it
        was stopped or drained.

        :param sandbox_id: The sandbox to attach to.
        :raises click.ClickException: When the sandbox does not exist or
            cannot be restarted.
        """
        _ensure_sdk()
        click.echo(f"▸ Reusing existing microsandbox VM '{sandbox_id}'")

        async def _do() -> None:
            import microsandbox as msb

            try:
                handle = await msb.Sandbox.get(sandbox_id)
            except msb.SandboxNotFoundError as exc:
                raise click.ClickException(
                    f"microsandbox sandbox '{sandbox_id}' not found. Create a "
                    "fresh one with `omnigent sandbox create --provider "
                    "microsandbox`."
                ) from exc
            if handle.status == msb.SandboxStatus.RUNNING:
                return
            click.echo(f"  → restarting {handle.status} sandbox in place")
            sandbox = await msb.Sandbox.start(sandbox_id, detached=True)
            self._connections[sandbox_id] = sandbox

        try:
            _run(_do(), timeout=_PROVISION_TIMEOUT_S)
        except click.ClickException:
            raise
        except Exception as exc:
            raise click.ClickException(
                f"Could not attach to microsandbox '{sandbox_id}': {exc}"
            ) from exc

    def keep_alive(self, sandbox_id: str) -> None:
        """
        Refresh the sandbox's idle timer (soft-fail) and surface the
        idle-drain behavior. Draining is recoverable - :meth:`resume`
        restarts the sandbox with its writable layer intact - so there is no
        autostop to disable, only a timer to reset.

        :param sandbox_id: The sandbox to touch.
        """
        _ensure_sdk()

        async def _do() -> None:
            sandbox = await self._aconnect(sandbox_id)
            await sandbox.touch()

        try:
            _run(_do(), timeout=_CONNECT_TIMEOUT_S)
        except Exception as exc:
            click.echo(f"  → could not refresh idle timer for '{sandbox_id}': {exc}", err=True)
            return
        if self._idle_timeout_s > 0:
            click.echo(
                f"  → idle timer refreshed; '{sandbox_id}' drains after "
                f"{self._idle_timeout_s}s of inactivity and restarts in place "
                "on the next use."
            )

    def put(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        """
        Copy a local file into the sandbox via the guest filesystem API.

        :param sandbox_id: Target sandbox.
        :param local_path: Local file to read.
        :param remote_path: Absolute destination path in the guest, e.g.
            ``"/tmp/oa-wheels.tgz"``.
        :raises click.ClickException: If the transfer fails.
        """
        _ensure_sdk()

        async def _do() -> None:
            sandbox = await self._aconnect(sandbox_id)
            await sandbox.fs.copy_from_host(str(local_path), remote_path)

        try:
            _run(_do(), timeout=_RUN_TIMEOUT_S)
        except click.ClickException:
            raise
        except Exception as exc:
            raise click.ClickException(
                f"Could not copy '{local_path}' into microsandbox '{sandbox_id}': {exc}"
            ) from exc

    def stream_exec(self, sandbox_id: str, command: str, *, pty: bool = False) -> RemoteProcess:
        """
        Spawn a command in the sandbox and stream its output line by line.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely.
        :param pty: When ``True``, allocate a guest TTY (output arrives
            pre-merged).
        :returns: Handle over the streaming process.
        :raises click.ClickException: If the spawn fails.
        """
        _ensure_sdk()
        # Without a TTY, stdout/stderr arrive as separately-tagged events and
        # the RemoteProcess contract wants combined output - merge in-shell.
        # A TTY already interleaves both.
        remote = command if pty else f"{command} 2>&1"

        async def _do() -> microsandbox_sdk.ExecHandle:
            sandbox = await self._aconnect(sandbox_id)
            return await sandbox.shell_stream(remote, tty=pty)

        try:
            handle = _run(_do(), timeout=_CONNECT_TIMEOUT_S)
        except click.ClickException:
            raise
        except Exception as exc:
            raise click.ClickException(
                f"Could not start streamed command on microsandbox '{sandbox_id}': {exc}"
            ) from exc
        return _MicrosandboxRemoteProcess(handle)

    def exec_foreground(self, sandbox_id: str, command: str) -> int:
        """
        Run *command* in the sandbox, echoing its output to the local terminal
        until it exits; Ctrl-C kills the remote process and re-raises.

        The SDK's exec handle CAN kill the guest process (unlike Modal's), so
        no pidfile dance is needed. ``TERM`` is forced to ``xterm-256color``
        because native harnesses spawn tmux, which refuses to start under a
        dumb/unset TERM.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely, e.g.
            ``"omnigent host --server https://..."``.
        :returns: The remote command's exit code.
        :raises KeyboardInterrupt: Re-raised after killing the remote process
            when the user detaches with Ctrl-C.
        """
        _ensure_sdk()

        async def _spawn() -> microsandbox_sdk.ExecHandle:
            sandbox = await self._aconnect(sandbox_id)
            return await sandbox.shell_stream(command, tty=True, env={"TERM": "xterm-256color"})

        try:
            handle = _run(_spawn(), timeout=_CONNECT_TIMEOUT_S)
        except click.ClickException:
            raise
        except Exception as exc:
            raise click.ClickException(
                f"Could not start foreground command on microsandbox '{sandbox_id}': {exc}"
            ) from exc
        process = _MicrosandboxRemoteProcess(handle)
        try:
            for line in process.lines:
                click.echo(line, nl=False)
            return process.wait()
        except KeyboardInterrupt:
            click.echo("\n  → detaching; stopping the remote process")
            process.close()
            raise

    def terminate(self, sandbox_id: str) -> None:
        """
        Kill and remove a sandbox, releasing its compute and writable layer.

        Idempotent from the caller's perspective: a sandbox that no longer
        exists is treated as success - the desired end state holds.

        :param sandbox_id: The sandbox to terminate.
        :raises click.ClickException: If a sandbox that exists cannot be
            removed.
        """
        _ensure_sdk()

        async def _do() -> None:
            import microsandbox as msb

            try:
                handle = await msb.Sandbox.get(sandbox_id)
            except msb.SandboxNotFoundError:
                return  # already gone - idempotent success
            # kill() on an already-stopped sandbox raises; stopping first is
            # not required for removal, only being non-running is.
            if handle.status == msb.SandboxStatus.RUNNING:
                with contextlib.suppress(Exception):
                    await handle.kill()
            try:
                await handle.remove()
            except msb.SandboxNotFoundError:
                # A concurrent terminate won the race - desired end state
                # holds either way.
                return

        self._forget(sandbox_id)
        try:
            _run(_do(), timeout=_TERMINATE_TIMEOUT_S)
        except Exception as exc:
            raise click.ClickException(
                f"Could not remove microsandbox '{sandbox_id}': {exc}"
            ) from exc

    def resume(self, sandbox_id: str) -> None:
        """
        Restart a stopped/drained sandbox in place, with its writable layer
        (workspace, installed tools) intact.

        :param sandbox_id: The stopped sandbox to resume.
        :raises click.ClickException: If the sandbox does not exist or cannot
            be restarted.
        """
        _ensure_sdk()

        async def _do() -> None:
            import microsandbox as msb

            try:
                handle = await msb.Sandbox.get(sandbox_id)
            except msb.SandboxNotFoundError as exc:
                raise click.ClickException(
                    f"microsandbox sandbox '{sandbox_id}' not found - it may have been removed."
                ) from exc
            if handle.status == msb.SandboxStatus.RUNNING:
                return  # already running - resume is a no-op
            sandbox = await msb.Sandbox.start(sandbox_id, detached=True)
            self._connections[sandbox_id] = sandbox

        self._forget(sandbox_id)
        try:
            _run(_do(), timeout=_PROVISION_TIMEOUT_S)
        except click.ClickException:
            raise
        except Exception as exc:
            raise click.ClickException(
                f"Could not resume microsandbox '{sandbox_id}': {exc}"
            ) from exc

    def is_running(self, sandbox_id: str) -> bool | None:
        """
        Return whether microsandbox reports this sandbox as running.

        :param sandbox_id: The sandbox to inspect.
        :returns: ``True`` when running, ``False`` when stopped / drained /
            missing, or ``None`` when the runtime cannot be queried.
        """
        _ensure_sdk()

        async def _do() -> bool:
            import microsandbox as msb

            try:
                handle = await msb.Sandbox.get(sandbox_id)
            except msb.SandboxNotFoundError:
                return False
            return handle.status == msb.SandboxStatus.RUNNING

        try:
            return _run(_do(), timeout=_CONNECT_TIMEOUT_S)
        except Exception:
            return None

    def forward_local_port(
        self, sandbox_id: str, port: int
    ) -> contextlib.AbstractContextManager[None]:
        """
        Forward ``localhost:<port>`` on the local machine into the sandbox.

        Implemented with an in-guest relay: a small python3 process inside
        the guest listens on ``127.0.0.1:<port>`` and pipes each connection
        to ``host.microsandbox.internal:<port>`` - the local machine, where
        the real listener is already bound. Requires the guest image to
        provide ``python3`` (the official host image does) and a network mode
        that allows guest-to-host traffic (``host`` / ``all``).

        :param sandbox_id: Target sandbox.
        :param port: Local + guest loopback port to bridge, e.g. ``8022``.
        :returns: Context manager holding the relay open.
        :raises click.ClickException: When the network mode blocks host
            traffic, python3 is missing in the guest, or the relay fails to
            come up.
        """
        if self._network_mode == "public-only":
            raise click.ClickException(
                "forward_local_port needs guest-to-host networking, but the "
                "microsandbox network mode is 'public-only' - use 'host' (the "
                "default) or 'all'."
            )
        return self._relay_forward(sandbox_id, port)

    @contextlib.contextmanager
    def _relay_forward(self, sandbox_id: str, port: int) -> Generator[None, None, None]:
        """Run the in-guest relay for :meth:`forward_local_port`."""
        # Per-forward random paths: two concurrent forwards for the same
        # port must not share script/log/readiness state.
        run_tag = f"oa-relay-{port}-{secrets.token_hex(4)}"
        script_path = f"/tmp/{run_tag}.py"
        log_path = f"/tmp/{run_tag}.log"
        self.run(sandbox_id, "command -v python3 >/dev/null")

        async def _ship() -> None:
            sandbox = await self._aconnect(sandbox_id)
            await sandbox.fs.write(script_path, _RELAY_SCRIPT.format(port=port).encode())

        try:
            _run(_ship(), timeout=_RUN_TIMEOUT_S)
        except click.ClickException:
            raise
        except Exception as exc:
            raise click.ClickException(
                f"Could not ship the port-forward relay into microsandbox '{sandbox_id}': {exc}"
            ) from exc
        started = self.run(
            sandbox_id,
            f"nohup python3 {shlex.quote(script_path)} > {shlex.quote(log_path)} "
            "2>&1 < /dev/null & echo $!",
        )
        # Cleanup covers everything after the spawn attempt: kill by recorded
        # pid when we have one, else by script path (the relay may be running
        # even when the pid echo came back garbled). Teardown is best-effort
        # and must never mask the context body's exception.
        pid = started.stdout.strip().splitlines()[-1] if started.stdout.strip() else ""
        try:
            if not pid.isdigit():
                raise click.ClickException(
                    f"could not start the port-forward relay in microsandbox '{sandbox_id}'"
                )
            deadline = time.monotonic() + _RELAY_READY_TIMEOUT_S
            while True:
                probe = self.run(
                    sandbox_id, f"grep -q relay-ready {shlex.quote(log_path)}", check=False
                )
                if probe.returncode == 0:
                    break
                if time.monotonic() > deadline:
                    tail = self.run(
                        sandbox_id, f"cat {shlex.quote(log_path)}", check=False
                    ).stdout[-400:]
                    raise click.ClickException(
                        f"port-forward relay did not come up in microsandbox "
                        f"'{sandbox_id}'" + (f" - {tail.strip()}" if tail.strip() else "")
                    )
                time.sleep(0.2)
            yield
        finally:
            kill_cmd = (
                f"kill {pid} 2>/dev/null; "
                if pid.isdigit()
                else f"pkill -f {shlex.quote(script_path)} 2>/dev/null; "
            )
            try:
                self.run(
                    sandbox_id,
                    f"{kill_cmd}rm -f {shlex.quote(script_path)} {shlex.quote(log_path)}",
                    check=False,
                )
            except click.ClickException as exc:
                click.echo(f"  → port-forward relay cleanup failed: {exc.message}", err=True)

    def wheel_install_command(self, remote_tgz_path: str) -> str:
        """
        Remote command that overlays the shipped wheels onto the prebaked
        host image - see
        :func:`~omnigent.onboarding.sandboxes.base.host_image_wheel_install_command`
        for the flag rationale.

        :param remote_tgz_path: Sandbox path of the shipped tarball, e.g.
            ``"/tmp/oa-wheels.tgz"``.
        :returns: Shell command string for :meth:`run`.
        """
        return host_image_wheel_install_command(remote_tgz_path)
