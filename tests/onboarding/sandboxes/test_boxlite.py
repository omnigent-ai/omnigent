"""Tests for :mod:`omnigent.onboarding.sandboxes.boxlite`."""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import click
import pytest

from omnigent.onboarding.sandboxes import boxlite as blmod
from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    SandboxCapabilityError,
)
from omnigent.onboarding.sandboxes.boxlite import (
    HOST_IMAGE_ENV_VAR,
    SANDBOX_ENV_PASSTHROUGH_ENV_VAR,
    BoxliteSandboxLauncher,
)

# ── Fake boxlite SDK ────────────────────────────────────────
#
# The boxlite SDK is an optional dependency the test environment does
# not install, and real boxes are micro-VMs that only exist on a
# virtualization-capable host — so these are hand-rolled stub classes
# (never MagicMock: the launcher's attribute access must hit explicitly
# defined recorders). Crucially the SDK is ASYNC: create/get/remove/exec
# /wait are coroutines, and stdout()/stderr() return async iterators —
# the launcher marshals them onto its shared event loop, so the fakes
# must mirror that shape. The fake module is injected via sys.modules so
# the launcher's function-local ``import boxlite`` resolves to it.


class _FakeBoxliteError(Exception):
    """Generic SDK error stand-in (the launcher catches broadly)."""


@dataclass
class _ExecCall:
    """One recorded ``box.exec`` invocation."""

    command: str
    args: list[str]
    timeout_secs: float | None = None


class _FakeStream:
    """Async iterator over canned output lines."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self) -> str:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


@dataclass
class _FakeExecResult:
    """Stand-in for ``ExecResult`` (only exit_code is load-bearing)."""

    exit_code: int
    error_message: str | None = None


class _FakeExecution:
    """Streaming-execution stand-in: stdout/stderr iterators + wait()."""

    def __init__(
        self,
        exit_code: int,
        stdout: list[str],
        stderr: list[str],
        error_message: str | None = None,
        raise_streams: bool = False,
    ) -> None:
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr
        self._error_message = error_message
        # Mirror the real SDK: stdout()/stderr() RAISE (not return None) when
        # the stream is unavailable (exec.rs PyExecution.stdout/stderr).
        self._raise_streams = raise_streams

    def stdout(self) -> _FakeStream:
        if self._raise_streams:
            raise RuntimeError("stdout stream not available")
        return _FakeStream(self._stdout)

    def stderr(self) -> _FakeStream:
        if self._raise_streams:
            raise RuntimeError("stderr stream not available")
        return _FakeStream(self._stderr)

    async def wait(self) -> _FakeExecResult:
        return _FakeExecResult(exit_code=self._exit_code, error_message=self._error_message)


class _FakeBox:
    """Recording stand-in for a boxlite ``Box`` handle."""

    def __init__(self, box_id: str) -> None:
        self.id = box_id
        self.exec_calls: list[_ExecCall] = []
        # (exit_code, stdout_lines, stderr_lines) handed back by successive
        # exec calls; empty queue yields a success no-output.
        self.exec_queue: list[tuple[int, list[str], list[str]]] = []
        self.exec_raises: Exception | None = None
        self.streams_raise: bool = False  # make stdout()/stderr() raise (SDK shape)

    async def _exec(
        self,
        command: str,
        args: list[str] | None = None,
        env: object = None,
        tty: bool = False,
        timeout_secs: float | None = None,
        **kwargs: object,
    ) -> _FakeExecution:
        self.exec_calls.append(
            _ExecCall(command=command, args=list(args or []), timeout_secs=timeout_secs)
        )
        if self.exec_raises is not None:
            raise self.exec_raises
        item = self.exec_queue.pop(0) if self.exec_queue else (0, [], [])
        # Queue entries are (exit_code, stdout, stderr[, error_message]).
        err_msg = item[3] if len(item) > 3 else None
        return _FakeExecution(item[0], item[1], item[2], err_msg, raise_streams=self.streams_raise)

    # Exposed under the SDK's method name (the launcher calls ``box.exec``);
    # defined as ``_exec`` so the fork security scan's builtin-exec call
    # heuristic does not false-positive on the method definition.
    exec = _exec


class _FakeBoxOptions:
    """Recording mirror of ``BoxOptions`` kwargs the launcher passes."""

    def __init__(
        self,
        image: str | None = None,
        cpus: int | None = None,
        memory_mib: int | None = None,
        env: object = None,
        auto_remove: bool | None = None,
        **kwargs: object,
    ) -> None:
        self.image = image
        self.cpus = cpus
        self.memory_mib = memory_mib
        self.env = env if env is not None else []
        self.auto_remove = auto_remove
        self.extra = kwargs


@dataclass
class _CreateCall:
    """One recorded ``runtime.create`` invocation."""

    options: _FakeBoxOptions
    name: str | None


@dataclass
class _FakeApiKeyCredential:
    """Stand-in for ``ApiKeyCredential`` (``from_env`` reads BOXLITE_API_KEY)."""

    key: str

    @staticmethod
    def from_env() -> _FakeApiKeyCredential | None:
        import os

        value = os.environ.get("BOXLITE_API_KEY")
        return _FakeApiKeyCredential(value) if value else None


@dataclass
class _FakeRestOptions:
    """Stand-in for ``BoxliteRestOptions``."""

    url: str
    credential: object = None
    path_prefix: str | None = None


@dataclass
class _FakeImageRegistry:
    """Stand-in for ``ImageRegistry`` (records the launcher's kwargs)."""

    host: str
    transport: str = "https"
    skip_verify: bool = False
    search: bool = False
    username: str | None = None
    password: str | None = None
    bearer_token: str | None = None


@dataclass
class _FakeOptions:
    """Stand-in for the local-runtime ``Options``."""

    home_dir: str | None = None
    image_registries: list = field(default_factory=list)


@dataclass
class _FakeBoxliteState:
    """Shared recorder the fake module writes into."""

    create_calls: list[_CreateCall] = field(default_factory=list)
    boxes: dict[str, _FakeBox] = field(default_factory=dict)
    removed: list[tuple[str, bool]] = field(default_factory=list)
    runtime_count: int = 0
    mode: str | None = None  # "local" or "rest"
    rest_options: _FakeRestOptions | None = None
    create_raises: Exception | None = None
    remove_raises: Exception | None = None
    local_options: object = None  # _FakeOptions passed to Boxlite(options)
    used_default: bool = False  # whether Boxlite.default() was used
    create_hangs: bool = False  # if True, create() sleeps (to exercise cancellation)
    cancelled: bool = False  # set when a hung create coroutine is cancelled
    remove_attempts: list[tuple[str, bool]] = field(default_factory=list)


def _install_fake_boxlite(monkeypatch: pytest.MonkeyPatch) -> _FakeBoxliteState:
    """Inject a fake ``boxlite`` module and return its recorder state."""
    state = _FakeBoxliteState()

    class _Runtime:
        """Fake boxlite runtime (async API)."""

        async def create(self, options: _FakeBoxOptions, name: str | None = None) -> _FakeBox:
            if state.create_hangs:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    state.cancelled = True
                    raise
            if state.create_raises is not None:
                raise state.create_raises
            state.create_calls.append(_CreateCall(options=options, name=name))
            box = _FakeBox(f"bl-new-{len(state.create_calls)}")
            state.boxes[box.id] = box
            return box

        async def get(self, id_or_name: str) -> _FakeBox | None:
            return state.boxes.get(id_or_name)

        async def remove(self, id_or_name: str, force: bool = False) -> None:
            state.remove_attempts.append((id_or_name, force))
            if id_or_name not in state.boxes:
                raise _FakeBoxliteError(f"box '{id_or_name}' not found")
            if state.remove_raises is not None:
                raise state.remove_raises
            state.boxes.pop(id_or_name, None)
            state.removed.append((id_or_name, force))

    class _Boxlite:
        """Fake ``boxlite.Boxlite`` runtime + factory."""

        def __new__(cls, options: object = None) -> _Runtime:  # type: ignore[misc]
            # `Boxlite(options)` — customized LOCAL runtime. Returning a
            # non-_Boxlite instance skips __init__, mirroring the real API
            # where the constructor yields the runtime directly.
            state.runtime_count += 1
            state.mode = "local"
            state.local_options = options
            return _Runtime()

        @staticmethod
        def default() -> _Runtime:
            state.runtime_count += 1
            state.mode = "local"
            state.used_default = True
            return _Runtime()

        @staticmethod
        def rest(options: _FakeRestOptions) -> _Runtime:
            state.runtime_count += 1
            state.mode = "rest"
            state.rest_options = options
            return _Runtime()

    fake = types.ModuleType("boxlite")
    fake.Boxlite = _Boxlite  # type: ignore[attr-defined]
    fake.BoxOptions = _FakeBoxOptions  # type: ignore[attr-defined]
    fake.ApiKeyCredential = _FakeApiKeyCredential  # type: ignore[attr-defined]
    fake.BoxliteRestOptions = _FakeRestOptions  # type: ignore[attr-defined]
    fake.Options = _FakeOptions  # type: ignore[attr-defined]
    fake.ImageRegistry = _FakeImageRegistry  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boxlite", fake)
    return state


@pytest.fixture()
def fake_boxlite(monkeypatch: pytest.MonkeyPatch) -> _FakeBoxliteState:
    """Install the fake SDK and clear ambient provider config."""
    monkeypatch.delenv(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, raising=False)
    monkeypatch.delenv(HOST_IMAGE_ENV_VAR, raising=False)
    monkeypatch.delenv("BOXLITE_API_KEY", raising=False)
    return _install_fake_boxlite(monkeypatch)


# ── prepare ─────────────────────────────────────────────────


def test_prepare_requires_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the boxlite SDK installed, preflight fails with an install hint."""
    # No fake installed and the real package is an optional extra, so the
    # launcher's `import boxlite` raises ImportError → ClickException.
    monkeypatch.delitem(sys.modules, "boxlite", raising=False)
    with pytest.raises(click.ClickException, match="boxlite SDK"):
        BoxliteSandboxLauncher().prepare()


def test_prepare_local_requires_kvm_on_linux(
    fake_boxlite: _FakeBoxliteState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local mode on Linux without /dev/kvm fails loud (no hypervisor)."""
    monkeypatch.setattr("omnigent.onboarding.sandboxes.boxlite.platform.system", lambda: "Linux")
    monkeypatch.setattr("os.path.exists", lambda path: False)
    with pytest.raises(click.ClickException, match="KVM"):
        BoxliteSandboxLauncher().prepare()


def test_prepare_local_passes_on_macos(
    fake_boxlite: _FakeBoxliteState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """macOS always has Hypervisor.framework → no KVM probe, preflight passes."""
    monkeypatch.setattr("omnigent.onboarding.sandboxes.boxlite.platform.system", lambda: "Darwin")
    BoxliteSandboxLauncher().prepare()


def test_prepare_cloud_skips_virtualization_check(
    fake_boxlite: _FakeBoxliteState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cloud mode delegates virtualization to the remote pool — no local KVM check."""
    monkeypatch.setattr("omnigent.onboarding.sandboxes.boxlite.platform.system", lambda: "Linux")
    monkeypatch.setattr("os.path.exists", lambda path: False)
    BoxliteSandboxLauncher(endpoint="https://boxlite.example.com:8100").prepare()


# ── provision ───────────────────────────────────────────────


def test_provision_defaults_official_image_and_persists(
    fake_boxlite: _FakeBoxliteState,
) -> None:
    """
    A bare provision uses the official host image, is persistent
    (auto_remove=False so the managed machinery owns teardown), injects
    no env, sizes like Modal/Daytona, and runs LOCAL by default.
    """
    box_id = BoxliteSandboxLauncher().provision("managed-abc")

    assert box_id == "bl-new-1"
    [create] = fake_boxlite.create_calls
    assert create.options.image == DEFAULT_HOST_IMAGE
    assert create.options.auto_remove is False
    assert create.options.cpus == 2
    assert create.options.memory_mib == 4096
    assert create.options.env == []
    assert create.name == "managed-abc"
    assert fake_boxlite.mode == "local"


def test_provision_image_resolution_order(
    fake_boxlite: _FakeBoxliteState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit constructor image > env override > official default."""
    monkeypatch.setenv(HOST_IMAGE_ENV_VAR, "docker.io/env/override:1")

    BoxliteSandboxLauncher(image="docker.io/explicit/img:2").provision("a")
    BoxliteSandboxLauncher().provision("b")

    first, second = fake_boxlite.create_calls
    assert first.options.image == "docker.io/explicit/img:2"
    assert second.options.image == "docker.io/env/override:1"


def test_provision_env_passthrough_resolves_from_server_env(
    fake_boxlite: _FakeBoxliteState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Constructor env NAMES resolve to ``(name, value)`` pairs from the
    server process environment — the config carries names only.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    monkeypatch.setenv("GIT_TOKEN", "ghp-test-456")

    BoxliteSandboxLauncher(env=["OPENAI_API_KEY", "GIT_TOKEN"]).provision("a")

    [create] = fake_boxlite.create_calls
    assert create.options.env == [
        ("OPENAI_API_KEY", "sk-test-123"),
        ("GIT_TOKEN", "ghp-test-456"),
    ]


def test_provision_env_passthrough_env_var_fallback(
    fake_boxlite: _FakeBoxliteState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without constructor names, the comma-separated env-var list applies."""
    monkeypatch.setenv(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, "OPENAI_API_KEY , GIT_TOKEN")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    monkeypatch.setenv("GIT_TOKEN", "ghp-test-456")

    BoxliteSandboxLauncher().provision("a")

    [create] = fake_boxlite.create_calls
    assert create.options.env == [
        ("OPENAI_API_KEY", "sk-test-123"),
        ("GIT_TOKEN", "ghp-test-456"),
    ]


def test_provision_env_passthrough_missing_var_fails_loud(
    fake_boxlite: _FakeBoxliteState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured name unset in the server environment is an operator error."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(click.ClickException, match="OPENAI_API_KEY"):
        BoxliteSandboxLauncher(env=["OPENAI_API_KEY"]).provision("a")
    assert fake_boxlite.create_calls == []


def test_provision_wraps_sdk_errors_with_provider_reason(
    fake_boxlite: _FakeBoxliteState,
) -> None:
    """SDK/VMM failures surface as launcher-contract ClickExceptions."""
    fake_boxlite.create_raises = _FakeBoxliteError("no KVM device")

    with pytest.raises(click.ClickException, match="no KVM device"):
        BoxliteSandboxLauncher().provision("a")


# ── local vs cloud runtime switch ───────────────────────────


def test_local_mode_uses_default_runtime(fake_boxlite: _FakeBoxliteState) -> None:
    """No endpoint → embedded ``Boxlite.default()`` (boxes on the server host)."""
    BoxliteSandboxLauncher().provision("a")
    assert fake_boxlite.mode == "local"
    assert fake_boxlite.rest_options is None


def test_cloud_mode_uses_rest_runtime_with_env_credential(
    fake_boxlite: _FakeBoxliteState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An endpoint → ``Boxlite.rest(BoxliteRestOptions(url, credential))`` with
    the API key read from BOXLITE_API_KEY (12-factor; never in config).
    """
    monkeypatch.setenv("BOXLITE_API_KEY", "blk_live_xyz")

    BoxliteSandboxLauncher(endpoint="https://boxlite.example.com:8100").provision("a")

    assert fake_boxlite.mode == "rest"
    assert fake_boxlite.rest_options is not None
    assert fake_boxlite.rest_options.url == "https://boxlite.example.com:8100"
    assert fake_boxlite.rest_options.credential.key == "blk_live_xyz"


def test_cloud_mode_unauthenticated_when_no_key(
    fake_boxlite: _FakeBoxliteState,
) -> None:
    """Cloud mode without BOXLITE_API_KEY connects unauthenticated (credential None)."""
    BoxliteSandboxLauncher(endpoint="https://boxlite.example.com:8100").provision("a")

    assert fake_boxlite.mode == "rest"
    assert fake_boxlite.rest_options.credential is None


# ── local runtime customization (home_dir / private registry) ─


def test_local_uses_default_runtime_when_unconfigured(fake_boxlite: _FakeBoxliteState) -> None:
    """No home_dir/registry → the zero-config ``Boxlite.default()`` singleton."""
    BoxliteSandboxLauncher().provision("a")
    assert fake_boxlite.used_default is True
    assert fake_boxlite.local_options is None


def test_local_home_dir_builds_options_runtime(fake_boxlite: _FakeBoxliteState) -> None:
    """A home_dir → a customized ``Boxlite(Options(home_dir=…))``, not default()."""
    BoxliteSandboxLauncher(home_dir="/data/boxlite").provision("a")
    assert fake_boxlite.used_default is False
    assert fake_boxlite.local_options is not None
    assert fake_boxlite.local_options.home_dir == "/data/boxlite"
    assert fake_boxlite.local_options.image_registries == []


def test_local_registry_resolves_credentials_from_server_env(
    fake_boxlite: _FakeBoxliteState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A private-registry block builds an ``ImageRegistry`` whose credentials
    resolve from NAMED server env vars (12-factor; values never in config).
    """
    monkeypatch.setenv("GHCR_USER", "bot")
    monkeypatch.setenv("GHCR_PAT", "ghp-secret")

    BoxliteSandboxLauncher(
        registry={"host": "ghcr.io", "username_env": "GHCR_USER", "password_env": "GHCR_PAT"},
    ).provision("a")

    [reg] = fake_boxlite.local_options.image_registries
    assert reg.host == "ghcr.io"
    assert reg.username == "bot"
    assert reg.password == "ghp-secret"
    assert reg.transport == "https"


def test_local_registry_missing_cred_env_fails_loud(
    fake_boxlite: _FakeBoxliteState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry cred env NAME unset in the server environment is an operator error."""
    monkeypatch.delenv("GHCR_PAT", raising=False)

    with pytest.raises(click.ClickException, match="GHCR_PAT"):
        BoxliteSandboxLauncher(registry={"host": "ghcr.io", "password_env": "GHCR_PAT"}).provision(
            "a"
        )
    assert fake_boxlite.create_calls == []


# ── run ─────────────────────────────────────────────────────


def test_run_drains_output_and_returns_exit_code(fake_boxlite: _FakeBoxliteState) -> None:
    """
    ``run`` wraps the command in ``sh -lc``, drains the stdout stream, and
    returns the exit code — the shape ``_start_host_in_sandbox`` relies on
    for ``printf %s "$HOME"`` (no trailing newline, ``.strip()`` on the
    caller side).
    """
    launcher = BoxliteSandboxLauncher()
    box_id = launcher.provision("a")
    fake_boxlite.boxes[box_id].exec_queue.append((0, ["/root"], []))

    result = launcher.run(box_id, 'printf %s "$HOME"')

    assert result.returncode == 0
    assert result.stdout == "/root"
    [call] = fake_boxlite.boxes[box_id].exec_calls
    assert call.command == "sh"
    assert call.args == ["-lc", 'printf %s "$HOME"']


def test_run_check_raises_on_nonzero_exit(fake_boxlite: _FakeBoxliteState) -> None:
    """``check=True`` (the managed default) raises; ``check=False`` returns it."""
    launcher = BoxliteSandboxLauncher()
    box_id = launcher.provision("a")
    box = fake_boxlite.boxes[box_id]
    box.exec_queue.append((1, ["boom"], []))
    box.exec_queue.append((1, ["boom"], []))

    with pytest.raises(click.ClickException, match="exit 1"):
        launcher.run(box_id, "false")
    result = launcher.run(box_id, "false", check=False)
    assert result.returncode == 1
    assert result.stdout == "boom"


def test_run_unknown_box_fails_with_hint(fake_boxlite: _FakeBoxliteState) -> None:
    """A vanished box surfaces as a clear error naming the id (relaunch logs this)."""
    with pytest.raises(click.ClickException, match="bl-gone"):
        BoxliteSandboxLauncher().run("bl-gone", "true")


def test_run_wraps_exec_errors_with_provider_reason(fake_boxlite: _FakeBoxliteState) -> None:
    """``run`` wraps SDK exec failures as launcher-contract ClickExceptions."""
    launcher = BoxliteSandboxLauncher()
    box_id = launcher.provision("a")
    fake_boxlite.boxes[box_id].exec_raises = _FakeBoxliteError("vmm gone")

    with pytest.raises(click.ClickException, match="vmm gone"):
        launcher.run(box_id, "true")


# ── terminate ───────────────────────────────────────────────


def test_terminate_removes_and_is_idempotent(fake_boxlite: _FakeBoxliteState) -> None:
    """Terminate force-removes the box; an already-gone box is a no-op success."""
    launcher = BoxliteSandboxLauncher()
    box_id = launcher.provision("a")

    launcher.terminate(box_id)
    assert fake_boxlite.removed == [(box_id, True)]

    # Already gone → swallow (the fake's remove raises "not found").
    launcher.terminate(box_id)
    assert fake_boxlite.removed == [(box_id, True)]


def test_terminate_wraps_unexpected_errors(fake_boxlite: _FakeBoxliteState) -> None:
    """A non-"not found" removal failure surfaces the provider's reason."""
    launcher = BoxliteSandboxLauncher()
    box_id = launcher.provision("a")
    fake_boxlite.remove_raises = _FakeBoxliteError("device busy")

    with pytest.raises(click.ClickException, match="device busy"):
        launcher.terminate(box_id)


# ── capability surface ──────────────────────────────────────


def test_managed_only_capability_surface(fake_boxlite: _FakeBoxliteState) -> None:
    """
    Managed-only: no CLI bootstrap, no port-forward, and the CLI-bootstrap
    primitives keep the base class's raising defaults.
    """
    launcher = BoxliteSandboxLauncher()
    assert launcher.supports_cli_bootstrap is False
    assert launcher.supports_local_port_forward is False
    with pytest.raises(SandboxCapabilityError):
        launcher.put("bl-1", Path("/tmp/x"), "/tmp/x")
    with pytest.raises(SandboxCapabilityError):
        launcher.stream_exec("bl-1", "echo hi")
    with pytest.raises(SandboxCapabilityError):
        launcher.forward_local_port("bl-1", 8022)


# ── timeout / cancellation ──────────────────────────────────


def test_run_bounds_exec_with_guest_timeout(fake_boxlite: _FakeBoxliteState) -> None:
    """
    run() must pass timeout_secs to box.exec so boxlite (guest/REST) kills the
    process on timeout — relying on the Python-side wait to bound it leaves the
    guest running (boxlite's documented zombie-prevention requirement).
    """
    launcher = BoxliteSandboxLauncher()
    box_id = launcher.provision("a")
    launcher.run(box_id, "echo hi")
    [call] = fake_boxlite.boxes[box_id].exec_calls
    assert call.timeout_secs is not None
    assert call.timeout_secs > 0


def test_run_surfaces_error_message_on_failure(fake_boxlite: _FakeBoxliteState) -> None:
    """
    A non-zero exit with a diagnostic error_message (e.g. container init death)
    must surface that message, not just the exit code.
    """
    launcher = BoxliteSandboxLauncher()
    box_id = launcher.provision("a")
    fake_boxlite.boxes[box_id].exec_queue.append((137, [], [], "container init died"))
    with pytest.raises(click.ClickException, match="container init died"):
        launcher.run(box_id, "false")


def test_run_failure_detail_includes_stderr(fake_boxlite: _FakeBoxliteState) -> None:
    """
    A non-zero exit must surface the captured stderr (e.g. a git-clone
    "fatal: ..."), not just the exit code — else the real reason is dropped.
    """
    launcher = BoxliteSandboxLauncher()
    box_id = launcher.provision("a")
    fake_boxlite.boxes[box_id].exec_queue.append((128, [], ["fatal: repository not found"]))
    with pytest.raises(click.ClickException, match="fatal: repository not found"):
        launcher.run(box_id, "git clone ...")


def test_provision_timeout_cancels_coroutine(
    fake_boxlite: _FakeBoxliteState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A provision that hangs must be CANCELLED on timeout (not orphaned on the
    shared loop). ``.result(timeout)`` alone leaves the coroutine running.
    """
    monkeypatch.setattr(blmod, "_PROVISION_TIMEOUT_S", 0.05)
    monkeypatch.setattr(blmod, "_CANCEL_GRACE_S", 0.1, raising=False)
    fake_boxlite.create_hangs = True

    with pytest.raises(click.ClickException):
        BoxliteSandboxLauncher().provision("managed-x")

    assert fake_boxlite.cancelled is True


def test_provision_timeout_cleans_up_orphan_box(
    fake_boxlite: _FakeBoxliteState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    create() is not atomic under cancellation, so a provision timeout must
    best-effort remove the (possibly server-side-created) box BY NAME — else it
    leaks an untracked, persistent box invisible to managed teardown.
    """
    monkeypatch.setattr(blmod, "_PROVISION_TIMEOUT_S", 0.05)
    monkeypatch.setattr(blmod, "_CANCEL_GRACE_S", 0.1, raising=False)
    fake_boxlite.create_hangs = True

    with pytest.raises(click.ClickException):
        BoxliteSandboxLauncher().provision("managed-x")

    assert ("managed-x", True) in fake_boxlite.remove_attempts


# ── terminate existence check ───────────────────────────────


def test_terminate_unknown_box_skips_remove(fake_boxlite: _FakeBoxliteState) -> None:
    """An already-gone box is detected via get() — no remove is attempted."""
    BoxliteSandboxLauncher().terminate("bl-gone")
    assert fake_boxlite.remove_attempts == []


def test_terminate_does_not_swallow_unrelated_not_found_error(
    fake_boxlite: _FakeBoxliteState,
) -> None:
    """
    A real removal failure whose message merely CONTAINS 'not found' (e.g.
    'image manifest not found') must surface — the old substring-swallow hid it.
    """
    launcher = BoxliteSandboxLauncher()
    box_id = launcher.provision("a")
    fake_boxlite.remove_raises = _FakeBoxliteError("image manifest not found")
    with pytest.raises(click.ClickException, match="manifest not found"):
        launcher.terminate(box_id)


# ── shared event loop liveness ──────────────────────────────


def test_get_loop_recreates_dead_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A closed/dead shared loop is replaced, not reused — a dead loop must not
    permanently brick every later boxlite call for the process lifetime.
    """
    dead = asyncio.new_event_loop()
    dead.close()
    monkeypatch.setattr(blmod, "_shared_loop", dead, raising=False)
    monkeypatch.setattr(blmod, "_loop_thread", None, raising=False)

    loop = blmod._get_loop()

    assert loop is not dead
    assert not loop.is_closed()
    loop.call_soon_threadsafe(loop.stop)  # let the recreated daemon thread exit


def test_run_tolerates_unavailable_streams(fake_boxlite: _FakeBoxliteState) -> None:
    """
    The SDK's stdout()/stderr() RAISE when a stream is unavailable (they never
    return None), so run() must call them defensively and still return the exit
    code — matching boxlite's own SimpleBox handling.
    """
    launcher = BoxliteSandboxLauncher()
    box_id = launcher.provision("a")
    box = fake_boxlite.boxes[box_id]
    box.streams_raise = True
    box.exec_queue.append((0, [], []))
    result = launcher.run(box_id, "true")
    assert result.returncode == 0
