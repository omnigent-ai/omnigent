"""Tests for :mod:`omnigent.onboarding.sandboxes.microsandbox`."""

from __future__ import annotations

import asyncio
import socket
import sys
import time
import types
from dataclasses import dataclass, field
from pathlib import Path

import click
import pytest

from omnigent.onboarding.sandboxes import microsandbox as msmod
from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    host_image_wheel_install_command,
)
from omnigent.onboarding.sandboxes.microsandbox import (
    HOST_IMAGE_ENV_VAR,
    SANDBOX_ENV_PASSTHROUGH_ENV_VAR,
    MicrosandboxSandboxLauncher,
)

# Fakes mirror the optional async SDK without requiring a virtualization host.
# Explicit recorders catch invalid access, and sys.modules resolves local imports.
# Streaming fakes preserve the async iterator and shared-loop behavior.


class _FakeSandboxNotFoundError(Exception):
    """Stands in for ``microsandbox.SandboxNotFoundError``."""


class _FakeSandboxStatus:
    """String constants mirroring ``microsandbox.SandboxStatus``."""

    RUNNING = "running"
    STOPPED = "stopped"
    CRASHED = "crashed"
    DRAINING = "draining"
    PAUSED = "paused"


@dataclass
class _ShellCall:
    """One recorded ``sandbox.shell`` / ``shell_stream`` invocation."""

    script: str
    timeout: float | None = None
    tty: bool = False
    env: dict[str, str] | None = None


@dataclass
class _FakeExecOutput:
    """Stand-in for ``ExecOutput``."""

    exit_code: int
    stdout_text: str = ""
    stderr_text: str = ""


class _FakeExecEvent:
    """One exec event (``event_type`` + optional ``data`` / ``code``)."""

    def __init__(
        self, event_type: str, data: bytes | None = None, code: int | None = None
    ) -> None:
        self.event_type = event_type
        self.data = data
        self.code = code


class _FakeExecHandle:
    """Async-iterator stand-in for ``ExecHandle``."""

    def __init__(self, events: list[_FakeExecEvent]) -> None:
        self._events = list(events)
        self.killed = False
        self.waits = 0
        self.wait_raises: Exception | None = None
        self.wait_result: tuple[int, bool] = (0, True)

    def __aiter__(self) -> _FakeExecHandle:
        return self

    async def __anext__(self) -> _FakeExecEvent:
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)

    async def wait(self) -> tuple[int, bool]:
        self.waits += 1
        if self.wait_raises is not None:
            raise self.wait_raises
        return self.wait_result

    async def kill(self) -> None:
        self.killed = True


class _FakeExecSink:
    """Fake stdin pipe: echoes each write back as a prefixed stdout event."""

    def __init__(self, handle: _FakeBridgeHandle) -> None:
        self._handle = handle
        self.closed = False

    async def write(self, data: bytes) -> None:
        self._handle.stdin_chunks.append(data)
        await self._handle.events.put(_FakeExecEvent("stdout", data=b"guest:" + data))

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        # Stdin EOF ends the fake bridge, like the real one after SHUT_WR.
        await self._handle.events.put(_FakeExecEvent("exited", code=0))
        await self._handle.events.put(None)


class _FakeBridgeHandle:
    """Stand-in for a piped-stdin ``exec_stream`` handle (the guest bridge)."""

    def __init__(self) -> None:
        # None is the end-of-stream sentinel.
        self.events: asyncio.Queue[_FakeExecEvent | None] = asyncio.Queue()
        self.stdin_chunks: list[bytes] = []
        self.killed = False
        self._sink = _FakeExecSink(self)

    def take_stdin(self) -> _FakeExecSink:
        return self._sink

    def __aiter__(self) -> _FakeBridgeHandle:
        return self

    async def __anext__(self) -> _FakeExecEvent:
        event = await self.events.get()
        if event is None:
            raise StopAsyncIteration
        return event

    async def kill(self) -> None:
        self.killed = True
        await self.events.put(None)


class _FakeStdin:
    """Stand-in for ``microsandbox.Stdin`` (pipe mode only)."""

    @staticmethod
    def pipe() -> str:
        return "pipe"


class _FakeFs:
    """Recording stand-in for ``sandbox.fs``."""

    def __init__(self) -> None:
        self.copied: list[tuple[str, str]] = []
        self.written: list[tuple[str, bytes]] = []

    async def copy_from_host(self, host_path: str, guest_path: str) -> None:
        self.copied.append((host_path, guest_path))

    async def write(self, path: str, data: bytes) -> None:
        self.written.append((path, data))


class _FakeSandbox:
    """Recording stand-in for a connected ``Sandbox``."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.status = _FakeSandboxStatus.RUNNING
        self.fs = _FakeFs()
        self.shell_calls: list[_ShellCall] = []
        # (exit_code, stdout, stderr) handed back by successive shell calls;
        # an empty queue yields a success no-output.
        self.shell_queue: list[tuple[int, str, str]] = []
        self.shell_raises: Exception | None = None
        # Events handed to the next shell_stream call.
        self.stream_events: list[_FakeExecEvent] = []
        self.stream_handles: list[_FakeExecHandle] = []
        # (cmd, args, stdin) per exec_stream call, and the handles vended.
        self.exec_calls: list[tuple[str, list[str], object]] = []
        self.bridge_handles: list[_FakeBridgeHandle] = []
        self.touches = 0
        self.killed = False

    async def shell(
        self,
        script: str,
        *,
        timeout: float | None = None,
        tty: bool = False,
        env: dict[str, str] | None = None,
        **kwargs: object,
    ) -> _FakeExecOutput:
        self.shell_calls.append(_ShellCall(script=script, timeout=timeout, tty=tty, env=env))
        if self.shell_raises is not None:
            raise self.shell_raises
        item = self.shell_queue.pop(0) if self.shell_queue else (0, "", "")
        return _FakeExecOutput(*item)

    async def shell_stream(
        self,
        script: str,
        *,
        tty: bool = False,
        env: dict[str, str] | None = None,
        **kwargs: object,
    ) -> _FakeExecHandle:
        self.shell_calls.append(_ShellCall(script=script, tty=tty, env=env))
        handle = _FakeExecHandle(self.stream_events)
        self.stream_handles.append(handle)
        return handle

    async def exec_stream(
        self,
        cmd: str,
        args: list[str] | None = None,
        *,
        stdin: object = None,
        **kwargs: object,
    ) -> _FakeBridgeHandle:
        self.exec_calls.append((cmd, list(args or []), stdin))
        handle = _FakeBridgeHandle()
        self.bridge_handles.append(handle)
        return handle

    async def touch(self) -> None:
        self.touches += 1

    async def ping(self) -> None:
        # Mirror the real SDK: a ping over a drained/stopped sandbox's dead
        # transport fails rather than answering.
        if self.status != _FakeSandboxStatus.RUNNING:
            raise RuntimeError("transport closed")

    async def kill(self) -> None:
        self.killed = True
        self.status = _FakeSandboxStatus.STOPPED


class _FakeHandle:
    """Stand-in for ``SandboxHandle`` (status + connect/kill/remove)."""

    def __init__(self, state: _FakeMicrosandboxState, name: str) -> None:
        self._state = state
        self._name = name

    @property
    def status(self) -> str:
        return self._state.sandboxes[self._name].status

    async def connect(self, timeout: float | None = None) -> _FakeSandbox:
        return self._state.sandboxes[self._name]

    async def kill(self) -> None:
        sandbox = self._state.sandboxes[self._name]
        if sandbox.status != _FakeSandboxStatus.RUNNING:
            raise RuntimeError("sandbox is not running")
        await sandbox.kill()

    async def remove(self) -> None:
        self._state.remove_attempts.append(self._name)
        if self._state.remove_raises is not None:
            raise self._state.remove_raises
        if self._state.sandboxes[self._name].status == _FakeSandboxStatus.RUNNING:
            raise RuntimeError("sandbox is still running")
        self._state.sandboxes.pop(self._name)
        self._state.removed.append(self._name)


@dataclass
class _CreateCall:
    """One recorded ``Sandbox.create`` invocation."""

    name: str
    kwargs: dict[str, object]


class _FakeNetwork:
    """Recording stand-in for ``Network`` (kind + policy rules)."""

    def __init__(self, policy: object = None, **kwargs: object) -> None:
        self.kind = "custom"
        self.policy = policy
        self.profiles: tuple[object, ...] = ()

    @staticmethod
    def allow_all() -> _FakeNetwork:
        network = _FakeNetwork()
        network.kind = "all"
        return network

    @staticmethod
    def from_profiles(*profiles: object) -> _FakeNetwork:
        network = _FakeNetwork()
        network.kind = "profiles"
        network.profiles = profiles
        return network


@dataclass
class _FakeNetworkPolicy:
    """Stand-in for ``NetworkPolicy``."""

    default_egress: object = None
    rules: tuple = ()


class _FakeAction:
    DENY = "deny"
    ALLOW = "allow"


@dataclass(frozen=True)
class _FakeRule:
    """Stand-in for ``Rule`` (records what it allows)."""

    destination: object = None
    protocol: object = None
    port: object = None

    @staticmethod
    def allow(
        *,
        destination: object = None,
        protocol: object = None,
        port: object = None,
        **kwargs: object,
    ) -> _FakeRule:
        return _FakeRule(destination=destination, protocol=protocol, port=port)

    @staticmethod
    def allow_dns() -> tuple[_FakeRule, _FakeRule]:
        return (_FakeRule(destination="dns-udp"), _FakeRule(destination="dns-tcp"))


class _FakeDestGroup:
    HOST = "host"
    PUBLIC = "public"


class _FakeNetworkProfile:
    PUBLIC = "public"


class _FakeProtocol:
    TCP = "tcp"


class _FakeDestination:
    @staticmethod
    def group(group: str) -> str:
        return f"group:{group}"


@dataclass
class _FakeMicrosandboxState:
    """Shared recorder the fake module writes into."""

    create_calls: list[_CreateCall] = field(default_factory=list)
    sandboxes: dict[str, _FakeSandbox] = field(default_factory=dict)
    start_calls: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    remove_attempts: list[str] = field(default_factory=list)
    create_raises: Exception | None = None
    remove_raises: Exception | None = None
    create_hangs: bool = False  # if True, create() sleeps (to exercise cancellation)
    cancelled: bool = False  # set when a hung create coroutine is cancelled


def _install_fake_microsandbox(monkeypatch: pytest.MonkeyPatch) -> _FakeMicrosandboxState:
    """Inject a fake ``microsandbox`` module and return its recorder state."""
    state = _FakeMicrosandboxState()

    class _Sandbox:
        """Fake ``microsandbox.Sandbox`` statics (async API)."""

        @staticmethod
        async def create(name: str, **kwargs: object) -> _FakeSandbox:
            if state.create_hangs:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    state.cancelled = True
                    raise
            if state.create_raises is not None:
                raise state.create_raises
            state.create_calls.append(_CreateCall(name=name, kwargs=dict(kwargs)))
            sandbox = _FakeSandbox(name)
            state.sandboxes[name] = sandbox
            return sandbox

        @staticmethod
        async def get(name: str) -> _FakeHandle:
            if name not in state.sandboxes:
                raise _FakeSandboxNotFoundError(f"sandbox '{name}' not found")
            return _FakeHandle(state, name)

        @staticmethod
        async def start(name: str, *, detached: bool = False) -> _FakeSandbox:
            state.start_calls.append(name)
            sandbox = state.sandboxes[name]
            sandbox.status = _FakeSandboxStatus.RUNNING
            return sandbox

    fake = types.ModuleType("microsandbox")
    fake.Sandbox = _Sandbox  # type: ignore[attr-defined]
    fake.SandboxNotFoundError = _FakeSandboxNotFoundError  # type: ignore[attr-defined]
    fake.SandboxStatus = _FakeSandboxStatus  # type: ignore[attr-defined]
    fake.Network = _FakeNetwork  # type: ignore[attr-defined]
    fake.NetworkPolicy = _FakeNetworkPolicy  # type: ignore[attr-defined]
    fake.Action = _FakeAction  # type: ignore[attr-defined]
    fake.Rule = _FakeRule  # type: ignore[attr-defined]
    fake.Destination = _FakeDestination  # type: ignore[attr-defined]
    fake.DestGroup = _FakeDestGroup  # type: ignore[attr-defined]
    fake.NetworkProfile = _FakeNetworkProfile  # type: ignore[attr-defined]
    fake.Protocol = _FakeProtocol  # type: ignore[attr-defined]
    fake.Stdin = _FakeStdin  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "microsandbox", fake)
    return state


@pytest.fixture()
def fake_microsandbox(monkeypatch: pytest.MonkeyPatch) -> _FakeMicrosandboxState:
    """Install the fake SDK and clear ambient provider config."""
    monkeypatch.delenv(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, raising=False)
    monkeypatch.delenv(HOST_IMAGE_ENV_VAR, raising=False)
    return _install_fake_microsandbox(monkeypatch)


def _provisioned(
    state: _FakeMicrosandboxState, launcher: MicrosandboxSandboxLauncher, label: str = "a"
) -> str:
    """Provision a sandbox and return its (suffixed) id."""
    sandbox_id = launcher.provision(label)
    assert sandbox_id in state.sandboxes
    return sandbox_id


# ── prepare ─────────────────────────────────────────────────


def test_prepare_requires_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the microsandbox SDK installed, preflight fails with an install hint."""
    monkeypatch.setitem(sys.modules, "microsandbox", None)
    with pytest.raises(click.ClickException, match="microsandbox SDK"):
        MicrosandboxSandboxLauncher().prepare()


def test_prepare_rejects_intel_macos(
    fake_microsandbox: _FakeMicrosandboxState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """macOS requires Apple Silicon - Intel Macs have no libkrun support."""
    monkeypatch.setattr(
        "omnigent.onboarding.sandboxes.microsandbox.platform.system", lambda: "Darwin"
    )
    monkeypatch.setattr(
        "omnigent.onboarding.sandboxes.microsandbox.platform.machine", lambda: "x86_64"
    )
    with pytest.raises(click.ClickException, match="Apple Silicon"):
        MicrosandboxSandboxLauncher().prepare()


def test_prepare_passes_on_apple_silicon(
    fake_microsandbox: _FakeMicrosandboxState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Apple Silicon macOS passes preflight."""
    monkeypatch.setattr(
        "omnigent.onboarding.sandboxes.microsandbox.platform.system", lambda: "Darwin"
    )
    monkeypatch.setattr(
        "omnigent.onboarding.sandboxes.microsandbox.platform.machine", lambda: "arm64"
    )
    MicrosandboxSandboxLauncher().prepare()


def test_prepare_linux_requires_kvm(
    fake_microsandbox: _FakeMicrosandboxState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux without /dev/kvm fails loud (no hypervisor)."""
    monkeypatch.setattr(
        "omnigent.onboarding.sandboxes.microsandbox.platform.system", lambda: "Linux"
    )
    monkeypatch.setattr("os.path.exists", lambda path: False)
    with pytest.raises(click.ClickException, match="KVM"):
        MicrosandboxSandboxLauncher().prepare()


# ── construction ────────────────────────────────────────────


def test_unknown_network_mode_rejected_at_construction() -> None:
    """A bad network mode is an operator error caught before any launch."""
    with pytest.raises(click.ClickException, match="network mode"):
        MicrosandboxSandboxLauncher(network="lan")


# ── provision ───────────────────────────────────────────────


def test_provision_defaults_official_image_detached_sized(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """
    A bare provision uses the official host image, creates DETACHED (the VM
    must outlive the launching process), sizes like Modal/Daytona/boxlite,
    injects no env, defaults the 24h idle drain, and labels the VM with the
    requested name.
    """
    sandbox_id = MicrosandboxSandboxLauncher().provision("managed-abc")

    assert sandbox_id.startswith("managed-abc-")
    [create] = fake_microsandbox.create_calls
    assert create.name == sandbox_id
    assert create.kwargs["image"] == DEFAULT_HOST_IMAGE
    assert create.kwargs["cpus"] == 2
    assert create.kwargs["memory"] == 4096
    assert create.kwargs["env"] == {}
    assert create.kwargs["detached"] is True
    assert create.kwargs["idle_timeout"] == msmod.DEFAULT_IDLE_TIMEOUT_S
    assert create.kwargs["labels"] == {"omnigent-name": "managed-abc"}


def test_provision_ids_are_collision_free(fake_microsandbox: _FakeMicrosandboxState) -> None:
    """Sandbox names are the id, so repeated creates under one label must not collide."""
    launcher = MicrosandboxSandboxLauncher()
    first = launcher.provision("omnigent-host")
    second = launcher.provision("omnigent-host")
    assert first != second


def test_provision_zero_idle_timeout_disables_draining(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """``idle_timeout_s=0`` omits the idle_timeout kwarg entirely."""
    MicrosandboxSandboxLauncher(idle_timeout_s=0).provision("a")
    [create] = fake_microsandbox.create_calls
    assert "idle_timeout" not in create.kwargs


def test_provision_image_resolution_order(
    fake_microsandbox: _FakeMicrosandboxState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit constructor image > env override > official default."""
    monkeypatch.setenv(HOST_IMAGE_ENV_VAR, "docker.io/env/override:1")

    MicrosandboxSandboxLauncher(image="docker.io/explicit/img:2").provision("a")
    MicrosandboxSandboxLauncher().provision("b")

    first, second = fake_microsandbox.create_calls
    assert first.kwargs["image"] == "docker.io/explicit/img:2"
    assert second.kwargs["image"] == "docker.io/env/override:1"


def test_provision_env_passthrough_resolves_from_server_env(
    fake_microsandbox: _FakeMicrosandboxState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Constructor env NAMES resolve to a name → value mapping from the server
    process environment - the config carries names only.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    monkeypatch.setenv("GIT_TOKEN", "ghp-test-456")

    MicrosandboxSandboxLauncher(env=["OPENAI_API_KEY", "GIT_TOKEN"]).provision("a")

    [create] = fake_microsandbox.create_calls
    assert create.kwargs["env"] == {
        "OPENAI_API_KEY": "sk-test-123",
        "GIT_TOKEN": "ghp-test-456",
    }


def test_provision_env_passthrough_env_var_fallback(
    fake_microsandbox: _FakeMicrosandboxState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without constructor names, the comma-separated env-var list applies."""
    monkeypatch.setenv(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, "OPENAI_API_KEY , GIT_TOKEN")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    monkeypatch.setenv("GIT_TOKEN", "ghp-test-456")

    MicrosandboxSandboxLauncher().provision("a")

    [create] = fake_microsandbox.create_calls
    assert create.kwargs["env"] == {
        "OPENAI_API_KEY": "sk-test-123",
        "GIT_TOKEN": "ghp-test-456",
    }


def test_provision_env_passthrough_missing_var_fails_loud(
    fake_microsandbox: _FakeMicrosandboxState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured name unset in the server environment is an operator error."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(click.ClickException, match="OPENAI_API_KEY"):
        MicrosandboxSandboxLauncher(env=["OPENAI_API_KEY"]).provision("a")
    assert fake_microsandbox.create_calls == []


def test_provision_wraps_sdk_errors_with_provider_reason(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """SDK/VMM failures surface as launcher-contract ClickExceptions."""
    fake_microsandbox.create_raises = RuntimeError("image pull failed")

    with pytest.raises(click.ClickException, match="image pull failed"):
        MicrosandboxSandboxLauncher().provision("a")


def test_provision_timeout_cancels_and_cleans_up(
    fake_microsandbox: _FakeMicrosandboxState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A provision that hangs must be CANCELLED on timeout (not orphaned on the
    shared loop), and the possibly-created VM removed by name - else it leaks
    an untracked persistent VM invisible to managed teardown.
    """
    monkeypatch.setattr(msmod, "_PROVISION_TIMEOUT_S", 0.05)
    monkeypatch.setattr(msmod, "_CANCEL_GRACE_S", 0.1, raising=False)
    fake_microsandbox.create_hangs = True

    with pytest.raises(click.ClickException):
        MicrosandboxSandboxLauncher().provision("managed-x")

    assert fake_microsandbox.cancelled is True


# ── network modes ───────────────────────────────────────────


def test_provision_default_network_allows_public_and_host(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """
    The default "host" mode keeps deny-by-default egress with DNS + public +
    host allow rules - the host rule is what lets a VM dial back to a server
    on this machine via host.microsandbox.internal.
    """
    MicrosandboxSandboxLauncher().provision("a")

    [create] = fake_microsandbox.create_calls
    network = create.kwargs["network"]
    assert network.kind == "custom"
    assert network.policy.default_egress == _FakeAction.DENY
    destinations = {rule.destination for rule in network.policy.rules}
    assert "group:public" in destinations
    assert "group:host" in destinations


def test_provision_host_ports_scope_the_host_rules(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """
    With host_ports set (the managed path), guest-to-host access is limited
    to exactly those TCP ports - untrusted agents must not reach unrelated
    host-local services.
    """
    MicrosandboxSandboxLauncher(host_ports=[8799, 8317]).provision("a")

    [create] = fake_microsandbox.create_calls
    host_rules = [
        rule for rule in create.kwargs["network"].policy.rules if rule.destination == "group:host"
    ]
    assert {rule.port for rule in host_rules} == {8799, 8317}
    assert all(rule.protocol == _FakeProtocol.TCP for rule in host_rules)


def test_provision_network_mode_public_only(fake_microsandbox: _FakeMicrosandboxState) -> None:
    """``public-only`` maps to the SDK's stock public-egress network."""
    MicrosandboxSandboxLauncher(network="public-only").provision("a")
    [create] = fake_microsandbox.create_calls
    assert create.kwargs["network"].profiles == (_FakeNetworkProfile.PUBLIC,)


def test_provision_network_mode_all(fake_microsandbox: _FakeMicrosandboxState) -> None:
    """``all`` maps to the SDK's allow-all network."""
    MicrosandboxSandboxLauncher(network="all").provision("a")
    [create] = fake_microsandbox.create_calls
    assert create.kwargs["network"].kind == "all"


# ── run ─────────────────────────────────────────────────────


def test_run_passes_command_with_guest_timeout(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """
    ``run`` hands the command to ``sandbox.shell`` with an SDK-side timeout
    (the guest kill; the Python-side wait alone would leave the guest
    process running) and returns the captured output - the shape
    ``SandboxLauncher.start_host`` relies on for ``printf %s "$HOME"``.
    """
    launcher = MicrosandboxSandboxLauncher()
    sandbox_id = _provisioned(fake_microsandbox, launcher)
    sandbox = fake_microsandbox.sandboxes[sandbox_id]
    sandbox.shell_queue.append((0, "/root", ""))

    result = launcher.run(sandbox_id, 'printf %s "$HOME"')

    assert result.returncode == 0
    assert result.stdout == "/root"
    [call] = sandbox.shell_calls
    assert call.script == 'printf %s "$HOME"'
    assert call.timeout is not None
    assert call.timeout > 0


def test_run_check_raises_with_stderr_detail(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """
    ``check=True`` (the managed default) raises and surfaces the captured
    stderr (e.g. a git-clone "fatal: ...") - a bare exit code is opaque.
    ``check=False`` returns the result instead.
    """
    launcher = MicrosandboxSandboxLauncher()
    sandbox_id = _provisioned(fake_microsandbox, launcher)
    sandbox = fake_microsandbox.sandboxes[sandbox_id]
    sandbox.shell_queue.append((128, "", "fatal: repository not found"))
    sandbox.shell_queue.append((1, "boom", ""))

    with pytest.raises(click.ClickException, match="fatal: repository not found"):
        launcher.run(sandbox_id, "git clone ...")
    result = launcher.run(sandbox_id, "false", check=False)
    assert result.returncode == 1
    assert result.stdout == "boom"


def test_run_unknown_sandbox_fails_with_hint(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """A vanished sandbox surfaces as a clear error naming the id."""
    with pytest.raises(click.ClickException, match="ms-gone"):
        MicrosandboxSandboxLauncher().run("ms-gone", "true")


def test_run_stopped_sandbox_names_resume_path(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """A drained/stopped sandbox is not silently used - the error names resume."""
    launcher = MicrosandboxSandboxLauncher()
    sandbox_id = _provisioned(fake_microsandbox, launcher)
    fake_microsandbox.sandboxes[sandbox_id].status = _FakeSandboxStatus.STOPPED
    # A fresh launcher has no cached connection, so it must consult status.
    with pytest.raises(click.ClickException, match="stopped"):
        MicrosandboxSandboxLauncher().run(sandbox_id, "true")


def test_run_revalidates_stale_cached_connection(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """
    A launcher holding a cached connection across an external drain must not
    surface an opaque transport error - the cache is revalidated (ping) and
    the fresh path reports the actionable stopped-status message.
    """
    launcher = MicrosandboxSandboxLauncher()
    sandbox_id = _provisioned(fake_microsandbox, launcher)  # caches the connection
    fake_microsandbox.sandboxes[sandbox_id].status = _FakeSandboxStatus.STOPPED

    with pytest.raises(click.ClickException, match="stopped"):
        launcher.run(sandbox_id, "true")


def test_run_wraps_exec_errors_with_provider_reason(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """``run`` wraps SDK exec failures as launcher-contract ClickExceptions."""
    launcher = MicrosandboxSandboxLauncher()
    sandbox_id = _provisioned(fake_microsandbox, launcher)
    fake_microsandbox.sandboxes[sandbox_id].shell_raises = RuntimeError("vmm gone")

    with pytest.raises(click.ClickException, match="vmm gone"):
        launcher.run(sandbox_id, "true")


# ── put ─────────────────────────────────────────────────────


def test_put_copies_via_guest_fs(fake_microsandbox: _FakeMicrosandboxState) -> None:
    """``put`` rides the guest filesystem API's host transfer."""
    launcher = MicrosandboxSandboxLauncher()
    sandbox_id = _provisioned(fake_microsandbox, launcher)

    launcher.put(sandbox_id, Path("/tmp/oa-wheels.tgz"), "/tmp/oa-wheels.tgz")

    assert fake_microsandbox.sandboxes[sandbox_id].fs.copied == [
        ("/tmp/oa-wheels.tgz", "/tmp/oa-wheels.tgz")
    ]


# ── stream_exec / exec_foreground ───────────────────────────


def test_stream_exec_merges_streams_and_yields_lines(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """
    Without a TTY the launcher merges stderr in-shell (``2>&1``) and the
    RemoteProcess re-chunks event payloads into newline-terminated lines,
    with wait() returning the exit code from the exited event.
    """
    launcher = MicrosandboxSandboxLauncher()
    sandbox_id = _provisioned(fake_microsandbox, launcher)
    sandbox = fake_microsandbox.sandboxes[sandbox_id]
    sandbox.stream_events = [
        _FakeExecEvent("started"),
        _FakeExecEvent("stdout", data=b"one\ntw"),
        _FakeExecEvent("stdout", data=b"o\n"),
        _FakeExecEvent("exited", code=4),
    ]

    process = launcher.stream_exec(sandbox_id, "echo hi")

    assert list(process.lines) == ["one\n", "two\n"]
    assert process.wait() == 4
    call = sandbox.shell_calls[-1]
    assert call.script == "echo hi 2>&1"
    assert call.tty is False


def test_stream_exec_reassembles_split_utf8(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """A multi-byte UTF-8 sequence split across event payloads must survive."""
    launcher = MicrosandboxSandboxLauncher()
    sandbox_id = _provisioned(fake_microsandbox, launcher)
    sandbox = fake_microsandbox.sandboxes[sandbox_id]
    euro = "€".encode()
    sandbox.stream_events = [
        _FakeExecEvent("stdout", data=b"cost " + euro[:1]),
        _FakeExecEvent("stdout", data=euro[1:] + b"42\n"),
        _FakeExecEvent("exited", code=0),
    ]

    process = launcher.stream_exec(sandbox_id, "echo cost")

    assert list(process.lines) == ["cost €42\n"]


def test_stream_exec_pty_skips_merge(fake_microsandbox: _FakeMicrosandboxState) -> None:
    """A PTY already interleaves both streams - no in-shell merge."""
    launcher = MicrosandboxSandboxLauncher()
    sandbox_id = _provisioned(fake_microsandbox, launcher)

    launcher.stream_exec(sandbox_id, "echo hi", pty=True)

    call = fake_microsandbox.sandboxes[sandbox_id].shell_calls[-1]
    assert call.script == "echo hi"
    assert call.tty is True


def test_stream_exec_close_kills_and_reaps(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """
    ``close`` on a still-running process kills it AND reaps it (the base
    contract), caching the exit code so a later wait() returns instantly.
    """
    launcher = MicrosandboxSandboxLauncher()
    sandbox_id = _provisioned(fake_microsandbox, launcher)
    sandbox = fake_microsandbox.sandboxes[sandbox_id]
    sandbox.stream_events = [_FakeExecEvent("stdout", data=b"partial")]

    process = launcher.stream_exec(sandbox_id, "sleep 300")
    process.close()

    handle = sandbox.stream_handles[-1]
    assert handle.killed is True
    assert handle.waits == 1  # reaped
    assert process.wait() == 0  # cached; no second SDK wait
    assert handle.waits == 1
    process.close()  # idempotent
    assert handle.waits == 1


def test_stream_exec_failed_close_can_be_retried(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """A close whose reap fails leaves the handle open so a retry can reap."""
    launcher = MicrosandboxSandboxLauncher()
    sandbox_id = _provisioned(fake_microsandbox, launcher)
    sandbox = fake_microsandbox.sandboxes[sandbox_id]
    sandbox.stream_events = [_FakeExecEvent("stdout", data=b"partial")]

    process = launcher.stream_exec(sandbox_id, "sleep 300")
    handle = sandbox.stream_handles[-1]
    handle.wait_raises = RuntimeError("transport flake")
    process.close()  # swallowed; must NOT latch closed
    handle.wait_raises = None
    handle.wait_result = (137, False)
    process.close()  # retry succeeds and reaps

    assert handle.waits == 2
    assert process.wait() == 137


def test_exec_foreground_uses_tty_and_term(
    fake_microsandbox: _FakeMicrosandboxState, capsys: pytest.CaptureFixture[str]
) -> None:
    """
    Foreground execs allocate a TTY with TERM forced (tmux refuses a dumb
    TERM), echo output, and return the remote exit code.
    """
    launcher = MicrosandboxSandboxLauncher()
    sandbox_id = _provisioned(fake_microsandbox, launcher)
    sandbox = fake_microsandbox.sandboxes[sandbox_id]
    sandbox.stream_events = [
        _FakeExecEvent("stdout", data=b"host up\n"),
        _FakeExecEvent("exited", code=0),
    ]

    rc = launcher.exec_foreground(sandbox_id, "omnigent host --server https://s")

    assert rc == 0
    call = sandbox.shell_calls[-1]
    assert call.tty is True
    assert call.env == {"TERM": "xterm-256color"}
    assert "host up" in capsys.readouterr().out


# ── terminate / resume / is_running / keep_alive ────────────


def test_terminate_kills_running_and_removes(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """Terminate kills a running VM, removes its state, and is idempotent."""
    launcher = MicrosandboxSandboxLauncher()
    sandbox_id = _provisioned(fake_microsandbox, launcher)

    launcher.terminate(sandbox_id)
    assert fake_microsandbox.removed == [sandbox_id]

    # Already gone → no-op success.
    launcher.terminate(sandbox_id)
    assert fake_microsandbox.removed == [sandbox_id]


def test_terminate_tolerates_concurrent_removal(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """A concurrent terminate winning the remove race is success, not an error."""
    launcher = MicrosandboxSandboxLauncher()
    sandbox_id = _provisioned(fake_microsandbox, launcher)
    fake_microsandbox.remove_raises = _FakeSandboxNotFoundError("already removed")

    launcher.terminate(sandbox_id)  # must not raise


def test_terminate_wraps_removal_errors(fake_microsandbox: _FakeMicrosandboxState) -> None:
    """A removal failure surfaces the provider's reason."""
    launcher = MicrosandboxSandboxLauncher()
    sandbox_id = _provisioned(fake_microsandbox, launcher)
    fake_microsandbox.remove_raises = RuntimeError("device busy")

    with pytest.raises(click.ClickException, match="device busy"):
        launcher.terminate(sandbox_id)


def test_resume_restarts_stopped_sandbox(fake_microsandbox: _FakeMicrosandboxState) -> None:
    """Resume restarts a stopped/drained VM in place (writable layer intact)."""
    launcher = MicrosandboxSandboxLauncher()
    sandbox_id = _provisioned(fake_microsandbox, launcher)
    fake_microsandbox.sandboxes[sandbox_id].status = _FakeSandboxStatus.STOPPED

    launcher.resume(sandbox_id)

    assert fake_microsandbox.start_calls == [sandbox_id]
    assert fake_microsandbox.sandboxes[sandbox_id].status == _FakeSandboxStatus.RUNNING


def test_resume_running_sandbox_is_noop(fake_microsandbox: _FakeMicrosandboxState) -> None:
    """Resuming an already-running VM does not restart it."""
    launcher = MicrosandboxSandboxLauncher()
    sandbox_id = _provisioned(fake_microsandbox, launcher)

    launcher.resume(sandbox_id)

    assert fake_microsandbox.start_calls == []


def test_resume_missing_sandbox_fails(fake_microsandbox: _FakeMicrosandboxState) -> None:
    """Resume of a removed sandbox is an error (the wake path relaunches instead)."""
    with pytest.raises(click.ClickException, match="ms-gone"):
        MicrosandboxSandboxLauncher().resume("ms-gone")


def test_is_running_reports_status(fake_microsandbox: _FakeMicrosandboxState) -> None:
    """is_running maps running/stopped/missing to True/False/False."""
    launcher = MicrosandboxSandboxLauncher()
    sandbox_id = _provisioned(fake_microsandbox, launcher)

    assert launcher.is_running(sandbox_id) is True
    fake_microsandbox.sandboxes[sandbox_id].status = _FakeSandboxStatus.STOPPED
    assert launcher.is_running(sandbox_id) is False
    assert launcher.is_running("ms-gone") is False


def test_is_running_without_sdk_reports_unqueryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing SDK means the runtime cannot be queried - None, not a raise."""
    monkeypatch.setitem(sys.modules, "microsandbox", None)
    assert MicrosandboxSandboxLauncher().is_running("ms-1") is None


def test_keep_alive_touches_idle_timer_softly(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """keep_alive refreshes the idle timer; failures warn instead of raising."""
    launcher = MicrosandboxSandboxLauncher()
    sandbox_id = _provisioned(fake_microsandbox, launcher)

    launcher.keep_alive(sandbox_id)
    assert fake_microsandbox.sandboxes[sandbox_id].touches == 1

    # Soft-fail: a vanished sandbox must not raise out of keep_alive.
    MicrosandboxSandboxLauncher().keep_alive("ms-gone")


# ── attach ──────────────────────────────────────────────────


def test_attach_restarts_stopped_sandbox_in_place(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """Attach revives a drained VM instead of failing (writable layer intact)."""
    launcher = MicrosandboxSandboxLauncher()
    sandbox_id = _provisioned(fake_microsandbox, launcher)
    fake_microsandbox.sandboxes[sandbox_id].status = _FakeSandboxStatus.STOPPED

    launcher.attach(sandbox_id)

    assert fake_microsandbox.start_calls == [sandbox_id]


def test_attach_missing_sandbox_fails_with_create_hint(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """Attach to a removed sandbox names the create command."""
    with pytest.raises(click.ClickException, match="omnigent sandbox create"):
        MicrosandboxSandboxLauncher().attach("ms-gone")


# ── forward_local_port ──────────────────────────────────────


def _free_port() -> int:
    """Grab an ephemeral local port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_forward_local_port_bridges_host_connection_into_guest(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """
    The forward binds the LOCAL port (``ssh -L`` semantics) and pipes each
    accepted connection into a per-connection guest bridge exec dialing the
    guest's loopback - bytes sent from the host come back through the fake
    guest's echo.
    """
    launcher = MicrosandboxSandboxLauncher()
    sandbox_id = _provisioned(fake_microsandbox, launcher)
    sandbox = fake_microsandbox.sandboxes[sandbox_id]
    sandbox.shell_queue.append((0, "", ""))  # command -v python3
    port = _free_port()

    with launcher.forward_local_port(sandbox_id, port):
        with socket.create_connection(("127.0.0.1", port), timeout=10) as conn:
            conn.settimeout(10)
            conn.sendall(b"ping")
            conn.shutdown(socket.SHUT_WR)
            received = b""
            while chunk := conn.recv(65536):
                received += chunk
    assert received == b"guest:ping"

    [(cmd, args, stdin)] = sandbox.exec_calls
    assert cmd == "python3"
    assert args[0] == "-c"
    assert "127.0.0.1" in args[1]  # the bridge dials the guest's loopback
    assert args[-1] == str(port)
    assert stdin == "pipe"
    [handle] = sandbox.bridge_handles
    assert handle.stdin_chunks == [b"ping"]


def test_forward_local_port_requires_guest_python3(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """A guest image without python3 fails with the actionable message."""
    launcher = MicrosandboxSandboxLauncher()
    sandbox_id = _provisioned(fake_microsandbox, launcher)
    sandbox = fake_microsandbox.sandboxes[sandbox_id]
    sandbox.shell_queue.append((1, "", ""))  # command -v python3 → missing

    with pytest.raises(click.ClickException, match="python3"):
        with launcher.forward_local_port(sandbox_id, 8022):
            pass


def test_forward_local_port_rejects_busy_local_port(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """An already-bound local port fails loud, naming the port."""
    launcher = MicrosandboxSandboxLauncher()
    sandbox_id = _provisioned(fake_microsandbox, launcher)
    sandbox = fake_microsandbox.sandboxes[sandbox_id]
    sandbox.shell_queue.append((0, "", ""))  # command -v python3

    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        with pytest.raises(click.ClickException, match=f"localhost:{port}"):
            with launcher.forward_local_port(sandbox_id, port):
                pass


def test_forward_local_port_teardown_kills_live_bridges(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """
    Closing the forward cancels connections still in flight and kills their
    guest bridges - no orphaned execs, no lingering local listener.
    """
    launcher = MicrosandboxSandboxLauncher()
    sandbox_id = _provisioned(fake_microsandbox, launcher)
    sandbox = fake_microsandbox.sandboxes[sandbox_id]
    sandbox.shell_queue.append((0, "", ""))  # command -v python3
    port = _free_port()

    with launcher.forward_local_port(sandbox_id, port):
        conn = socket.create_connection(("127.0.0.1", port), timeout=10)
        conn.sendall(b"still-open")
        # Wait for the bytes to land in a guest bridge before tearing down.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any(
            handle.stdin_chunks for handle in sandbox.bridge_handles
        ):
            time.sleep(0.01)

    [handle] = sandbox.bridge_handles
    assert handle.stdin_chunks == [b"still-open"]
    assert handle.killed is True
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1)
    conn.close()


def test_forward_local_port_works_under_public_only(
    fake_microsandbox: _FakeMicrosandboxState,
) -> None:
    """
    The bridge rides the SDK exec channel, not guest egress, so even the
    most restrictive network mode supports the forward.
    """
    launcher = MicrosandboxSandboxLauncher(network="public-only")
    sandbox_id = _provisioned(fake_microsandbox, launcher)
    sandbox = fake_microsandbox.sandboxes[sandbox_id]
    sandbox.shell_queue.append((0, "", ""))  # command -v python3
    port = _free_port()

    with launcher.forward_local_port(sandbox_id, port):
        with socket.create_connection(("127.0.0.1", port), timeout=10) as conn:
            conn.settimeout(10)
            conn.sendall(b"hi")
            conn.shutdown(socket.SHUT_WR)
            assert conn.recv(65536) == b"guest:hi"


# ── misc ────────────────────────────────────────────────────


def test_capability_surface() -> None:
    """Full CLI-bootstrap + port-forward + resume surface is advertised."""
    launcher = MicrosandboxSandboxLauncher()
    assert launcher.supports_cli_bootstrap is True
    assert launcher.supports_local_port_forward is True
    assert launcher.can_resume is True


def test_wheel_install_command_matches_host_image_overlay() -> None:
    """VMs boot from the prebaked host image → the shared overlay command applies."""
    launcher = MicrosandboxSandboxLauncher()
    assert launcher.wheel_install_command(
        "/tmp/oa-wheels.tgz"
    ) == host_image_wheel_install_command("/tmp/oa-wheels.tgz")


def test_get_loop_recreates_dead_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A closed/dead shared loop is replaced, not reused - a dead loop must not
    permanently brick every later microsandbox call for the process lifetime.
    """
    dead = asyncio.new_event_loop()
    dead.close()
    monkeypatch.setattr(msmod, "_shared_loop", dead, raising=False)
    monkeypatch.setattr(msmod, "_loop_thread", None, raising=False)

    loop = msmod._get_loop()

    assert loop is not dead
    assert not loop.is_closed()
    loop.call_soon_threadsafe(loop.stop)  # let the recreated daemon thread exit
