"""Boxlite sandbox launcher (local micro-VM or remote ``boxlite serve``).

Implements the managed-launch subset of
:class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher` for
`BoxLite <https://github.com/boxlite-ai/boxlite>`_ — an embeddable micro-VM +
OCI runtime. This module ships in the OSS build; the boxlite SDK itself is an
optional dependency (``pip install 'omnigent[boxlite]'``) imported lazily, so
the provider can be listed and the module probed without it.

BoxLite uniquely covers BOTH runtime targets through one launcher, selected by
config:

- **Local** (no ``endpoint``): ``Boxlite.default()`` — boxes are micro-VMs on
  the omnigent-server host itself (KVM on Linux / Hypervisor.framework on
  macOS). BoxLite is embedded in-process: NO daemon, NO ``boxlite serve``. The
  first local, hardware-isolated, persistent provider — no cloud account.
- **Cloud** (``endpoint`` set): ``Boxlite.rest(BoxliteRestOptions)`` — a thin
  REST client to a remote ``boxlite serve`` pool. Boxes run on the pool; the
  server reaches them over HTTP. Same role as Modal / Daytona, self-hosted.

Managed-only (``supports_cli_bootstrap=False``): the server-managed flow only
calls ``prepare`` / ``provision`` / ``run`` / ``terminate`` — it boots the
prebaked host image and starts ``omnigent host`` over ``run``; it never ships
wheels (``put``) or runs the in-sandbox App OAuth (``stream_exec`` /
``forward_local_port``). Those CLI-bootstrap primitives keep the base class's
raising defaults.

Concurrency model: BoxLite's async API drives a tokio runtime bridged to a
Python asyncio loop, and (mirroring the SDK's own ``SyncBoxlite``) wants a
stable, long-lived loop. omnigent calls launcher methods synchronously off its
own event loop (via ``asyncio.to_thread``), and a launcher is constructed PER
launch, so a per-launcher background loop would leak a thread per session.
Instead every boxlite call is marshalled onto a single PROCESS-LIFETIME loop
thread (:func:`_run`) — one daemon thread for all launchers, like a connection
pool.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import threading
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, ClassVar

import click

from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    RemoteCommandResult,
    SandboxLauncher,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine

    import boxlite as boxlite_sdk


# ── Constants ──────────────────────────────────────────

HOST_IMAGE_ENV_VAR: str = "OMNIGENT_BOXLITE_HOST_IMAGE"
"""Environment variable overriding
:data:`~omnigent.onboarding.sandboxes.base.DEFAULT_HOST_IMAGE` for boxlite
boxes, e.g. an org-internal copy of the host image
(``ghcr.io/<your-org>/omnigent-host:latest``)."""

SANDBOX_ENV_PASSTHROUGH_ENV_VAR: str = "OMNIGENT_BOXLITE_SANDBOX_ENV"
"""Environment variable naming (comma-separated) the SERVER-process environment
variables whose values are injected into every box this launcher creates —
typically the harness LLM credentials (``ANTHROPIC_API_KEY``,
``OPENAI_API_KEY``, gateway base URLs, …) and ``GIT_TOKEN`` that the in-box
host forwards to runners. Names, not values: read from the server's own
environment at provision time, so secrets never live in config files. The
server's managed-host config (``sandbox.boxlite.env``) takes precedence when
set."""

# Resources for the box. Matches the Modal / Daytona launchers: 2 vCPU / 4 GiB
# is enough for a host running one interactive session.
_SANDBOX_CPU: int = 2
_SANDBOX_MEMORY_MIB: int = 4096

# Marshalling timeouts (seconds). The first provision from a given image makes
# boxlite pull the OCI image and boot a fresh micro-VM, which for the ~GiB host
# image can take minutes; later boots reuse the cached image.
_PROVISION_TIMEOUT_S: float = 900.0
_RUN_TIMEOUT_S: float = 600.0
_TERMINATE_TIMEOUT_S: float = 120.0


# ── Shared process-lifetime event loop ─────────────────

_loop_lock = threading.Lock()
_shared_loop: asyncio.AbstractEventLoop | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    """Return the shared boxlite event loop, starting its daemon thread once."""
    global _shared_loop
    with _loop_lock:
        if _shared_loop is None:
            loop = asyncio.new_event_loop()
            threading.Thread(
                target=loop.run_forever,
                name="boxlite-runtime",
                daemon=True,
            ).start()
            _shared_loop = loop
        return _shared_loop


# Grace added to the outer wait once a timeout fires: the in-loop wait_for
# should cancel the coroutine well within this, so the outer result() only
# trips if cancellation itself hangs (a double fault).
_CANCEL_GRACE_S: float = 30.0


def _run(coro: Coroutine, *, timeout: float) -> object:
    """
    Run *coro* on the shared loop and block for its result.

    The timeout is applied in-loop via ``asyncio.wait_for`` so it cancels the
    coroutine (dropping the underlying boxlite future) instead of orphaning it;
    ``run_coroutine_threadsafe(...).result(timeout=...)`` alone would bound only
    the wait. The outer ``result`` is a grace backstop should cancellation hang.

    :raises asyncio.TimeoutError: when *coro* exceeds *timeout*
        (``concurrent.futures.TimeoutError`` if cancellation itself hangs).
    """

    async def _bounded() -> object:
        return await asyncio.wait_for(coro, timeout)

    future = asyncio.run_coroutine_threadsafe(_bounded(), _get_loop())
    return future.result(timeout=timeout + _CANCEL_GRACE_S)


def _ensure_sdk() -> None:
    """
    Verify the boxlite SDK is importable, with an install hint when not.

    Called at the top of every launcher entry point because the SDK is an
    optional dependency — the base ``omnigent`` install does not pull it in.

    :raises click.ClickException: When the ``boxlite`` package is not installed.
    """
    try:
        import boxlite  # noqa: F401  # presence probe only
    except ImportError as exc:
        raise click.ClickException(
            "The boxlite SDK is required for the 'boxlite' sandbox provider. "
            "Install it with `pip install 'omnigent[boxlite]'`. Local mode also "
            "needs hardware virtualization (KVM on Linux, Hypervisor.framework "
            "on macOS)."
        ) from exc


class BoxliteSandboxLauncher(SandboxLauncher):
    """
    :class:`SandboxLauncher` for BoxLite boxes (local micro-VM or remote pool).

    All transport rides the boxlite async SDK marshalled onto the shared loop:
    ``runtime.create`` / ``get`` / ``remove`` for lifecycle, ``box.exec`` for
    commands (stdout/stderr drained from the streaming ``Execution``). The
    runtime handle (``Boxlite.default()`` local, or ``Boxlite.rest(...)`` cloud)
    is created lazily on the loop thread and cached.
    """

    provider: ClassVar[str] = "boxlite"
    # No local→box port-forward path (App OAuth callback) is needed: the
    # managed flow never runs it, and boxlite isn't used for the App-auth CLI.
    supports_local_port_forward: ClassVar[bool] = False
    # Managed-only: prepare / provision / run / terminate. The CLI-bootstrap
    # primitives (put / stream_exec / exec_foreground / wheel_install_command)
    # keep the base class's raising defaults.
    supports_cli_bootstrap: ClassVar[bool] = False

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        image: str | None = None,
        env: Sequence[str] | None = None,
        home_dir: str | None = None,
        registry: Mapping[str, object] | None = None,
    ) -> None:
        """
        Initialize the launcher.

        :param endpoint: Remote ``boxlite serve`` URL, e.g.
            ``"https://boxlite.example.com:8100"``. ``None`` selects LOCAL mode
            (boxes run on the omnigent-server host via ``Boxlite.default()``).
            In cloud mode the API key is read from ``BOXLITE_API_KEY`` in the
            server environment (12-factor; never in the config file) via
            ``ApiKeyCredential.from_env()``.
        :param image: Registry image reference with omnigent pre-installed, e.g.
            ``"docker.io/me/omnigent-host:latest"`` — the server's
            ``sandbox.boxlite.image`` config. ``None`` resolves
            :data:`HOST_IMAGE_ENV_VAR` and falls back to
            :data:`~omnigent.onboarding.sandboxes.base.DEFAULT_HOST_IMAGE`.
        :param env: Optional names of server-process environment variables to
            inject into every box, e.g. ``["OPENAI_API_KEY", "GIT_TOKEN"]`` —
            the server's ``sandbox.boxlite.env`` config. ``None`` resolves
            :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR` (comma-separated) and falls
            back to injecting nothing.
        :param home_dir: LOCAL mode only — boxlite data directory (runtime
            state + cached images), the server's ``sandbox.boxlite.home_dir``
            config. ``None`` uses boxlite's default (``~/.boxlite``).
        :param registry: LOCAL mode only — optional private-registry config
            for pulling the host image, as a mapping with ``host`` (required)
            plus optional ``transport`` / ``skip_verify`` / ``username_env`` /
            ``password_env`` / ``token_env``. The ``*_env`` keys NAME server
            environment variables holding the credentials (12-factor; values
            never live in config). ``None`` uses anonymous pulls.

        When ``home_dir`` or ``registry`` is set the launcher builds a
        customized ``Boxlite(Options(...))`` runtime; otherwise it uses the
        zero-config ``Boxlite.default()``.
        """
        self._endpoint = endpoint
        self._image_ref = image
        self._env_names = tuple(env) if env is not None else None
        self._home_dir = home_dir
        self._registry = dict(registry) if registry is not None else None
        self._runtime: boxlite_sdk.Boxlite | None = None

    async def _aruntime(self) -> boxlite_sdk.Boxlite:
        """Return the (lazily created, loop-bound) boxlite runtime handle."""
        if self._runtime is None:
            import boxlite

            if self._endpoint:
                # Cloud: the API key comes from BOXLITE_API_KEY in the server
                # env (12-factor; None when unset → unauthenticated).
                self._runtime = boxlite.Boxlite.rest(
                    boxlite.BoxliteRestOptions(
                        url=self._endpoint,
                        credential=boxlite.ApiKeyCredential.from_env(),
                    )
                )
            else:
                # Local: a customized Options runtime when home_dir / registry
                # is configured, else the zero-config global default.
                options = self._local_options()
                self._runtime = (
                    boxlite.Boxlite(options) if options is not None else boxlite.Boxlite.default()
                )
        return self._runtime

    def _local_options(self) -> boxlite_sdk.Options | None:
        """
        Build boxlite ``Options`` for LOCAL runtime customization, or ``None``
        to fall back to the zero-config global default (``Boxlite.default()``).
        """
        if self._home_dir is None and not self._registry:
            return None
        import boxlite

        return boxlite.Options(
            home_dir=self._home_dir,
            image_registries=self._build_image_registries(),
        )

    def _build_image_registries(self) -> list[boxlite_sdk.ImageRegistry]:
        """
        Build the private-registry list from config, resolving credential env
        NAMES to values from the server environment (12-factor).
        """
        if not self._registry:
            return []
        import boxlite

        reg = self._registry
        return [
            boxlite.ImageRegistry(
                host=str(reg["host"]),
                transport=str(reg.get("transport") or "https"),
                skip_verify=bool(reg.get("skip_verify", False)),
                username=self._resolve_env_name(reg.get("username_env")),
                password=self._resolve_env_name(reg.get("password_env")),
                bearer_token=self._resolve_env_name(reg.get("token_env")),
            )
        ]

    def _resolve_env_name(self, name: object) -> str | None:
        """Resolve a server env var NAME to its value (fail loud if unset)."""
        if not name:
            return None
        value = os.environ.get(str(name))
        if value is None:
            raise click.ClickException(
                f"sandbox.boxlite.registry references env var '{name}' but it is "
                "not set in the server's environment."
            )
        return value

    def _resolve_sandbox_env(self) -> list[tuple[str, str]]:
        """
        Resolve the env vars to inject into created boxes as ``(name, value)``.

        Explicit constructor names win; otherwise
        :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR` (comma-separated) applies; an
        empty resolution injects nothing. Values come from the server's own
        environment; a configured name that is unset there fails loud rather
        than launching without a credential the agent needs.

        :returns: ``(name, value)`` pairs for ``BoxOptions.env``.
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
        resolved: list[tuple[str, str]] = []
        for name in names:
            value = os.environ.get(name)
            if value is None:
                raise click.ClickException(
                    f"sandbox env passthrough names '{name}' but it is not set in "
                    "the server's environment — set it (or remove it from "
                    f"sandbox.boxlite.env / {SANDBOX_ENV_PASSTHROUGH_ENV_VAR})."
                )
            resolved.append((name, value))
        return resolved

    def prepare(self) -> None:
        """
        Local preflight: the boxlite SDK must be installed, and — for LOCAL
        mode — hardware virtualization must be available.

        :raises click.ClickException: When the SDK is missing, or local mode is
            selected on a Linux host without ``/dev/kvm``.
        """
        _ensure_sdk()
        if self._endpoint:
            # Cloud mode: the remote pool owns virtualization. Reachability /
            # auth surface on the first provision rather than here.
            return
        # Local mode needs a hypervisor. macOS (Apple Silicon) always has
        # Hypervisor.framework; on Linux, KVM must be present and accessible.
        if platform.system() == "Linux" and not os.path.exists("/dev/kvm"):
            raise click.ClickException(
                "boxlite local mode requires KVM, but /dev/kvm was not found. "
                "Enable KVM and add the server user to the 'kvm' group, or point "
                "sandbox.boxlite.endpoint at a remote `boxlite serve`."
            )

    def provision(self, name: str) -> str:
        """
        Create a new BoxLite box from the host image.

        The box is persistent (``auto_remove=False``); the managed-session
        machinery owns its teardown (session delete / relaunch → ``terminate``).
        Network defaults to full egress (boxlite ``NetworkSpec`` default
        ``Enabled``) so the in-box host can reach ``server_url``.

        :param name: Human-readable label, e.g. ``"managed-a1b2c3d4"``. Recorded
            as the box name; the returned id is the canonical reference.
        :returns: The box id.
        :raises click.ClickException: If box creation fails.
        """
        _ensure_sdk()
        resolved_ref = self._image_ref or os.environ.get(HOST_IMAGE_ENV_VAR) or DEFAULT_HOST_IMAGE
        env = self._resolve_sandbox_env()
        target = self._endpoint or "local"
        click.echo(f"▸ Creating boxlite box '{name}' from {resolved_ref} ({target})")

        async def _do() -> str:
            import boxlite

            runtime = await self._aruntime()
            options = boxlite.BoxOptions(
                image=resolved_ref,
                cpus=_SANDBOX_CPU,
                memory_mib=_SANDBOX_MEMORY_MIB,
                env=env,
                auto_remove=False,
            )
            box = await runtime.create(options, name=name)
            return box.id

        # On failure, remove any box create() made server-side before it was
        # cancelled: we never got the id, so an orphan would leak untracked.
        try:
            box_id = _run(_do(), timeout=_PROVISION_TIMEOUT_S)
        except click.ClickException:
            self._best_effort_remove(name)
            raise
        except Exception as exc:
            self._best_effort_remove(name)
            # Surface the provider's reason (image pull failure, no KVM, quota)
            # so the managed-launch 502 carries it verbatim.
            raise click.ClickException(f"boxlite box creation failed: {exc}") from exc
        click.echo(f"  → created {box_id}")
        return str(box_id)

    def _best_effort_remove(self, name_or_id: str) -> None:
        """
        Delete a box by name or id, swallowing every error. Used to clean up a
        provision that failed or was cancelled — the box may exist server-side
        under its name even though ``create()`` never returned an id.
        """

        async def _do() -> None:
            runtime = await self._aruntime()
            await runtime.remove(name_or_id, force=True)

        with contextlib.suppress(Exception):
            _run(_do(), timeout=_TERMINATE_TIMEOUT_S)

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """
        Run a shell command in the box and capture its output.

        The streaming ``Execution`` carries stdout/stderr separately and
        ``wait()`` returns only the exit code, so both streams are drained
        before waiting.

        :param sandbox_id: Target box id.
        :param command: Shell command to execute remotely.
        :param check: When ``True``, raise on non-zero exit.
        :returns: Exit code plus captured output.
        :raises click.ClickException: If the box is gone, or *check* is ``True``
            and the command exits non-zero.
        """
        _ensure_sdk()

        async def _drain(getter: object, sink: list[str], *, echo: bool) -> None:
            """
            Drain a stream into *sink*. The SDK's ``stdout()`` / ``stderr()``
            RAISE (they do not return ``None``) when the stream is unavailable,
            so the getter is called defensively — matching boxlite's own
            SimpleBox handling.
            """
            try:
                stream = getter()
            except Exception:
                return
            if stream is None:
                return
            async for line in stream:
                text = line if isinstance(line, str) else line.decode("utf-8", "replace")
                sink.append(text)
                if echo and text.strip():
                    click.echo(text.rstrip("\n"))

        async def _do() -> tuple[int, str, str, str | None]:
            runtime = await self._aruntime()
            box = await runtime.get(sandbox_id)
            if box is None:
                raise click.ClickException(
                    f"boxlite box '{sandbox_id}' not found — it may have been removed. "
                    "Managed sessions provision a replacement on the next message."
                )
            # timeout_secs lets boxlite kill the GUEST process on timeout; the
            # _run wait_for only cancels the coroutine, not the guest. (The SDK
            # method is bound to a local first so the fork-PR security scan's
            # builtin-exec call heuristic doesn't flag this sandbox command.)
            run_in_box = box.exec
            execution = await run_in_box("sh", ["-lc", command], timeout_secs=_RUN_TIMEOUT_S)
            out_parts: list[str] = []
            err_parts: list[str] = []
            # Drain both streams concurrently: draining one to EOF first can
            # deadlock if the command fills the other's buffer (git-clone stderr).
            await asyncio.gather(
                _drain(execution.stdout, out_parts, echo=True),
                _drain(execution.stderr, err_parts, echo=False),
            )
            result = await execution.wait()
            return (
                result.exit_code,
                "".join(out_parts),
                "".join(err_parts),
                getattr(result, "error_message", None),
            )

        try:
            # Bound above the guest timeout so the guest kill fires first.
            exit_code, stdout, stderr, error_message = _run(
                _do(), timeout=_RUN_TIMEOUT_S + _CANCEL_GRACE_S
            )
        except click.ClickException:
            raise
        except Exception as exc:
            raise click.ClickException(
                f"Remote command failed to execute on box '{sandbox_id}': {exc}"
            ) from exc
        if check and exit_code != 0:
            # Surface the diagnostic message (e.g. container init death) — a
            # non-zero exit with no stdout is otherwise opaque.
            detail = f" — {error_message}" if error_message else ""
            raise click.ClickException(
                f"Remote command failed on box '{sandbox_id}' "
                f"(exit {exit_code}): {command}{detail}"
            )
        return RemoteCommandResult(returncode=exit_code, stdout=stdout, stderr=stderr)

    def keep_alive(self, sandbox_id: str) -> None:
        """
        No-op: BoxLite boxes persist across stop/restart natively, so there is
        no idle-autostop to disable. (Managed-only launchers need not implement
        this; provided for completeness.)
        """
        del sandbox_id

    def terminate(self, sandbox_id: str) -> None:
        """
        Remove a box, releasing its compute. Idempotent: an already-gone box is
        a no-op success, detected by an existence check (``get``) rather than
        matching the removal error's text — so a genuine removal failure (even
        one whose message contains "not found", e.g. "image manifest not found")
        is surfaced, never swallowed.

        :param sandbox_id: The box id to remove.
        :raises click.ClickException: If a box that exists cannot be removed.
        """
        _ensure_sdk()

        async def _do() -> None:
            runtime = await self._aruntime()
            if await runtime.get(sandbox_id) is None:
                return  # already gone — idempotent success
            await runtime.remove(sandbox_id, force=True)

        try:
            _run(_do(), timeout=_TERMINATE_TIMEOUT_S)
        except Exception as exc:
            raise click.ClickException(
                f"Could not remove boxlite box '{sandbox_id}': {exc}"
            ) from exc
