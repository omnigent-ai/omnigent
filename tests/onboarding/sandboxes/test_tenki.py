"""Tests for :mod:`omnigent.onboarding.sandboxes.tenki`."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import click
import pytest

from omnigent.onboarding.sandboxes.base import SandboxCapabilityError
from omnigent.onboarding.sandboxes.tenki import (
    IMAGE_ENV_VAR,
    PROJECT_ENV_VAR,
    SANDBOX_ENV_PASSTHROUGH_ENV_VAR,
    WORKSPACE_ENV_VAR,
    TenkiSandboxLauncher,
)

# ── Fake tenki_sandbox SDK ──────────────────────────────────
#
# The SDK is an optional dependency the test env may not install, and
# real Sandbox objects only exist server-side — so these are hand-rolled
# stubs injected via sys.modules, resolving the launcher's function-local
# `from tenki_sandbox import ...` / `from tenki_sandbox.errors import ...`.


class _SandboxError(Exception):
    pass


class _MissingAuthTokenError(_SandboxError):
    pass


class _SessionNotFoundError(_SandboxError):
    pass


class _SessionTerminatedError(_SandboxError):
    pass


class _InvalidStateError(_SandboxError):
    pass


@dataclass
class _FakeCommandResult:
    exit_code: int = 0
    stdout: bytes = b""
    stderr: bytes = b""

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode(errors="replace")

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode(errors="replace")


@dataclass
class _State:
    """Shared recorder for assertions."""

    client_kwargs: list[dict] = field(default_factory=list)
    create_kwargs: dict = field(default_factory=dict)
    create_calls: list[dict] = field(default_factory=list)
    get_calls: list[str] = field(default_factory=list)
    exec_calls: list[dict] = field(default_factory=list)
    start_calls: list[dict] = field(default_factory=list)
    uploaded: list[tuple[str, str]] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    extends: list[object] = field(default_factory=list)
    # Behavior toggles.
    exec_result: _FakeCommandResult = field(default_factory=_FakeCommandResult)
    exec_raises: BaseException | None = None
    start_raises: BaseException | None = None
    create_raises: BaseException | None = None
    client_raises: BaseException | None = None
    close_raises: BaseException | None = None
    upload_raises: bool = False
    extend_raises: bool = False
    get_missing: bool = False
    get_state: str = "RUNNING"
    # Streaming process behavior.
    stream_stdout: bytes = b""
    stream_stderr: bytes = b""
    stream_exit_code: int = 0
    stream_wait_raises: BaseException | None = None
    stream_kill_raises: bool = False
    stream_killed: bool = False
    stream_wait_calls: list[object] = field(default_factory=list)


class _FakeStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def __iter__(self):
        yield from self._chunks


class _FakeProcess:
    def __init__(self, state: _State) -> None:
        self._state = state
        self.stdout = _FakeStream([state.stream_stdout] if state.stream_stdout else [])
        self.stderr = _FakeStream([state.stream_stderr] if state.stream_stderr else [])

    def wait(self, timeout: float | None = None) -> _FakeCommandResult:
        self._state.stream_wait_calls.append(timeout)
        if self._state.stream_wait_raises is not None:
            raise self._state.stream_wait_raises
        return _FakeCommandResult(exit_code=self._state.stream_exit_code)

    def kill(self) -> None:
        if self._state.stream_kill_raises:
            raise _SandboxError("already exited")
        self._state.stream_killed = True


class _FakeFs:
    def __init__(self, state: _State) -> None:
        self._state = state

    def upload(self, local_path: str, remote_path: str) -> None:
        if self._state.upload_raises:
            raise _SandboxError("upload failed")
        self._state.uploaded.append((local_path, remote_path))


class _FakeSandbox:
    def __init__(
        self, state: _State, *, sandbox_id: str = "sb-tenki-1", state_str: str = "RUNNING"
    ) -> None:
        self._state = state
        self._id = sandbox_id
        self._state_str = state_str
        self.fs = _FakeFs(state)

    @property
    def id(self) -> str:
        return self._id

    @property
    def state(self) -> str:
        return self._state_str

    def exec(self, *argv: str, timeout: float | None = None, **kwargs) -> _FakeCommandResult:
        self._state.exec_calls.append({"argv": argv, "timeout": timeout})
        if self._state.exec_raises is not None:
            raise self._state.exec_raises
        return self._state.exec_result

    def start(self, *argv: str, **kwargs) -> _FakeProcess:
        self._state.start_calls.append({"argv": argv})
        if self._state.start_raises is not None:
            raise self._state.start_raises
        return _FakeProcess(self._state)

    def close_if_open(self) -> None:
        if self._state.close_raises is not None:
            raise self._state.close_raises
        self._state.closed.append(self._id)

    def extend(self, additional: object) -> None:
        if self._state.extend_raises:
            raise _SandboxError("no lifetime to extend")
        self._state.extends.append(additional)


class _FakeClient:
    _state: _State

    def __init__(self, *, base_url: str | None = None, **kwargs) -> None:
        state = _FakeClient._state
        state.client_kwargs.append({"base_url": base_url, **kwargs})
        if state.client_raises is not None:
            raise state.client_raises
        self._state = state

    def create(self, **kwargs) -> _FakeSandbox:
        self._state.create_kwargs = kwargs
        self._state.create_calls.append(kwargs)
        if self._state.create_raises is not None:
            raise self._state.create_raises
        return _FakeSandbox(self._state, sandbox_id="sb-tenki-1")

    def get(self, sandbox_id: str) -> _FakeSandbox:
        self._state.get_calls.append(sandbox_id)
        if self._state.get_missing:
            raise _SessionNotFoundError(sandbox_id)
        return _FakeSandbox(self._state, sandbox_id=sandbox_id, state_str=self._state.get_state)


@pytest.fixture()
def sdk(monkeypatch: pytest.MonkeyPatch) -> _State:
    state = _State()
    _FakeClient._state = state

    mod = types.ModuleType("tenki_sandbox")
    mod.Client = _FakeClient  # type: ignore[attr-defined]
    mod.Sandbox = _FakeSandbox  # type: ignore[attr-defined]
    mod.CommandResult = _FakeCommandResult  # type: ignore[attr-defined]
    err = types.ModuleType("tenki_sandbox.errors")
    err.SandboxError = _SandboxError  # type: ignore[attr-defined]
    err.MissingAuthTokenError = _MissingAuthTokenError  # type: ignore[attr-defined]
    err.SessionNotFoundError = _SessionNotFoundError  # type: ignore[attr-defined]
    err.SessionTerminatedError = _SessionTerminatedError  # type: ignore[attr-defined]
    err.InvalidStateError = _InvalidStateError  # type: ignore[attr-defined]
    mod.errors = err  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "tenki_sandbox", mod)
    monkeypatch.setitem(sys.modules, "tenki_sandbox.errors", err)
    monkeypatch.setenv("TENKI_API_KEY", "tk_test")
    monkeypatch.delenv("TENKI_AUTH_TOKEN", raising=False)
    monkeypatch.delenv(IMAGE_ENV_VAR, raising=False)
    monkeypatch.delenv(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, raising=False)
    monkeypatch.delenv(PROJECT_ENV_VAR, raising=False)
    monkeypatch.delenv(WORKSPACE_ENV_VAR, raising=False)
    return state


def _launcher(**kwargs) -> TenkiSandboxLauncher:
    """Launcher with a configured image (needed by everything but the
    fail-fast test)."""
    kwargs.setdefault("image", "ws/omnigent-host:latest")
    return TenkiSandboxLauncher(**kwargs)


# ── prepare ─────────────────────────────────────────────────


def test_prepare_requires_credentials(sdk: _State, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TENKI_API_KEY")
    with pytest.raises(click.ClickException, match="TENKI_API_KEY"):
        _launcher().prepare()


def test_prepare_accepts_auth_token(sdk: _State, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TENKI_API_KEY")
    monkeypatch.setenv("TENKI_AUTH_TOKEN", "ory_st_x")
    _launcher().prepare()  # must not raise


def test_prepare_raises_install_hint_when_sdk_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # A None entry in sys.modules makes `import tenki_sandbox` raise ImportError.
    monkeypatch.setitem(sys.modules, "tenki_sandbox", None)
    monkeypatch.setenv("TENKI_API_KEY", "k")
    with pytest.raises(click.ClickException, match=r"pip install 'omnigent\[tenki\]'"):
        _launcher().prepare()


# ── provision ───────────────────────────────────────────────


def test_provision_creates_with_networking_and_resources(sdk: _State) -> None:
    launcher = TenkiSandboxLauncher(image="ws/host:latest", vcpus=4, memory_mb=8192, disk_gb=40)
    assert launcher.provision("managed-x") == "sb-tenki-1"
    kw = sdk.create_kwargs
    assert kw["image"] == "ws/host:latest"
    assert kw["name"] == "managed-x"
    assert kw["cpu_cores"] == 4
    assert kw["memory_mb"] == 8192
    assert kw["disk_size_gb"] == 40
    # The one Tenki-specific requirement: outbound on, inbound off.
    assert kw["allow_outbound"] is True
    assert kw["allow_inbound"] is False
    assert kw["wait"] is True
    # No env configured → nothing injected.
    assert kw["env"] is None


def test_provision_image_resolution_order(sdk: _State, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(IMAGE_ENV_VAR, "env/image:latest")
    TenkiSandboxLauncher(image="ctor/image:latest").provision("x")
    assert sdk.create_kwargs["image"] == "ctor/image:latest"


def test_provision_image_from_env_when_no_explicit(
    sdk: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(IMAGE_ENV_VAR, "env/image:latest")
    TenkiSandboxLauncher().provision("x")
    assert sdk.create_kwargs["image"] == "env/image:latest"


def test_provision_without_image_fails_fast_with_build_hint(sdk: _State) -> None:
    with pytest.raises(click.ClickException, match=r"deploy/tenki/README\.md"):
        TenkiSandboxLauncher().provision("x")
    # Fail-fast before any create call.
    assert sdk.create_calls == []


def test_provision_env_passthrough_from_server_env(
    sdk: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-123")
    _launcher(env=["ANTHROPIC_API_KEY"]).provision("x")
    assert sdk.create_kwargs["env"] == {"ANTHROPIC_API_KEY": "sk-ant-123"}


def test_provision_env_passthrough_missing_var_fails_loud(sdk: _State) -> None:
    with pytest.raises(click.ClickException, match="NOT_SET_ANYWHERE"):
        _launcher(env=["NOT_SET_ANYWHERE"]).provision("x")


def test_provision_sandbox_error_surfaces_reason(sdk: _State) -> None:
    sdk.create_raises = _SandboxError("quota exceeded")
    with pytest.raises(click.ClickException, match="quota exceeded"):
        _launcher().provision("x")


def test_provision_passes_base_url_to_client(sdk: _State) -> None:
    _launcher(base_url="https://api.tenki.cloud").provision("x")
    assert sdk.client_kwargs[0]["base_url"] == "https://api.tenki.cloud"


def test_provision_passes_project_and_workspace(sdk: _State) -> None:
    # The service requires an explicit project unless the key is project-scoped.
    _launcher(project="proj-1", workspace="ws-1").provision("x")
    assert sdk.create_kwargs["project_id"] == "proj-1"
    assert sdk.create_kwargs["workspace_id"] == "ws-1"


def test_provision_project_workspace_from_env(
    sdk: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PROJECT_ENV_VAR, "env-proj")
    monkeypatch.setenv(WORKSPACE_ENV_VAR, "env-ws")
    _launcher().provision("x")
    assert sdk.create_kwargs["project_id"] == "env-proj"
    assert sdk.create_kwargs["workspace_id"] == "env-ws"


def test_provision_without_project_passes_none(sdk: _State) -> None:
    # A project-scoped key needs no explicit project; unset resolves to None.
    _launcher().provision("x")
    assert sdk.create_kwargs["project_id"] is None
    assert sdk.create_kwargs["workspace_id"] is None


def test_client_missing_auth_is_friendly(sdk: _State) -> None:
    sdk.client_raises = _MissingAuthTokenError()
    with pytest.raises(click.ClickException, match="TENKI_API_KEY"):
        _launcher().provision("x")


# ── run ─────────────────────────────────────────────────────


def test_run_returns_streams_and_exit_code(sdk: _State) -> None:
    sdk.exec_result = _FakeCommandResult(stdout=b"hi\n", stderr=b"warn\n", exit_code=0)
    result = _launcher().run("sb-tenki-1", "echo hi")
    assert result.returncode == 0
    assert result.stdout == "hi\n"
    assert result.stderr == "warn\n"
    # No per-command timeout so long jobs (clone/install) aren't killed.
    assert sdk.exec_calls[0]["timeout"] is None


def test_run_wraps_command_in_login_bash(sdk: _State) -> None:
    _launcher().run("sb-tenki-1", "echo hi")
    assert sdk.exec_calls[0]["argv"] == ("bash", "-lc", "echo hi")


def test_run_handles_nonzero_exit(sdk: _State) -> None:
    # Tenki's exec returns the result (does not raise) on non-zero; the
    # launcher applies `check` itself.
    sdk.exec_result = _FakeCommandResult(stdout=b"boom\n", stderr=b"bad\n", exit_code=3)
    launcher = _launcher()
    with pytest.raises(click.ClickException, match="exit 3"):
        launcher.run("sb-tenki-1", "false")
    unchecked = launcher.run("sb-tenki-1", "false", check=False)
    assert unchecked.returncode == 3
    assert unchecked.stdout == "boom\n"


def test_run_transport_error_surfaces(sdk: _State) -> None:
    sdk.exec_raises = _SandboxError("daemon connection lost")
    with pytest.raises(click.ClickException, match="daemon connection lost"):
        _launcher().run("sb-tenki-1", "echo hi")


def test_resolve_missing_sandbox_is_friendly(sdk: _State) -> None:
    sdk.get_missing = True
    with pytest.raises(click.ClickException, match="not found"):
        _launcher().run("gone", "echo hi")


def test_run_missing_sdk_raises_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    # A direct primitive call (no prepare() first) with the SDK absent must
    # surface the omnigent[tenki] hint, not a raw ImportError.
    monkeypatch.setitem(sys.modules, "tenki_sandbox", None)
    with pytest.raises(click.ClickException, match=r"pip install 'omnigent\[tenki\]'"):
        TenkiSandboxLauncher(image="ws/host:latest").run("sb", "echo hi")


def test_terminate_missing_sdk_raises_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "tenki_sandbox", None)
    with pytest.raises(click.ClickException, match=r"pip install 'omnigent\[tenki\]'"):
        TenkiSandboxLauncher(image="ws/host:latest").terminate("sb")


# ── put ─────────────────────────────────────────────────────


def test_put_uploads_file(sdk: _State, tmp_path: Path) -> None:
    local = tmp_path / "wheels.tgz"
    local.write_bytes(b"binary\x00data")
    _launcher().put("sb-tenki-1", local, "/tmp/wheels.tgz")
    assert sdk.uploaded == [(str(local), "/tmp/wheels.tgz")]


def test_put_failure_surfaces(sdk: _State, tmp_path: Path) -> None:
    sdk.upload_raises = True
    local = tmp_path / "wheels.tgz"
    local.write_bytes(b"x")
    with pytest.raises(click.ClickException, match="File upload"):
        _launcher().put("sb-tenki-1", local, "/tmp/wheels.tgz")


# ── attach ──────────────────────────────────────────────────


def test_attach_accepts_running_sandbox(sdk: _State) -> None:
    _launcher().attach("sb-tenki-1")  # must not raise


def test_attach_rejects_non_running_sandbox(sdk: _State) -> None:
    sdk.get_state = "PAUSED"
    with pytest.raises(click.ClickException, match="not running"):
        _launcher().attach("sb-tenki-1")


def test_attach_missing_sandbox_is_friendly(sdk: _State) -> None:
    sdk.get_missing = True
    with pytest.raises(click.ClickException, match="not found"):
        _launcher().attach("gone")


# ── keep_alive ──────────────────────────────────────────────


def test_keep_alive_extends(sdk: _State) -> None:
    _launcher().keep_alive("sb-tenki-1")
    assert sdk.extends == [24 * 60 * 60]


def test_keep_alive_soft_fails(sdk: _State) -> None:
    sdk.extend_raises = True
    _launcher().keep_alive("sb-tenki-1")  # warns, must not raise
    assert sdk.extends == []


# ── terminate ───────────────────────────────────────────────


def test_terminate_closes_sandbox(sdk: _State) -> None:
    _launcher().terminate("sb-tenki-1")
    assert sdk.closed == ["sb-tenki-1"]


def test_terminate_swallows_not_found(sdk: _State) -> None:
    sdk.get_missing = True
    _launcher().terminate("already-gone")  # must not raise
    assert sdk.closed == []


def test_terminate_swallows_already_terminated(sdk: _State) -> None:
    sdk.close_raises = _SessionTerminatedError("already terminated")
    _launcher().terminate("sb-tenki-1")  # must not raise
    assert sdk.closed == []


def test_terminate_swallows_invalid_state(sdk: _State) -> None:
    sdk.close_raises = _InvalidStateError("terminating")
    _launcher().terminate("sb-tenki-1")  # must not raise


# ── streaming ───────────────────────────────────────────────


def test_stream_exec_combines_output_and_returns_stable_iterator(sdk: _State) -> None:
    sdk.stream_stdout = b"out\n"
    sdk.stream_stderr = b"err\n"
    process = _launcher().stream_exec("sb-tenki-1", "do-thing")
    # The lines property must return the same iterator across accesses.
    first_access = process.lines
    assert process.lines is first_access
    # Two reader threads merge into one queue → order is not guaranteed,
    # so compare as a multiset.
    assert sorted(process.lines) == ["err\n", "out\n"]
    assert process.wait() == 0


def test_stream_exec_close_kills_remote_process(sdk: _State) -> None:
    process = _launcher().stream_exec("sb-tenki-1", "do-thing")
    process.wait()
    process.close()
    assert sdk.stream_killed is True


def test_stream_exec_close_swallows_kill_error(sdk: _State) -> None:
    sdk.stream_kill_raises = True
    process = _launcher().stream_exec("sb-tenki-1", "do-thing")
    process.wait()
    process.close()  # must not raise
    process.close()  # idempotent


def test_stream_exec_close_reaps_process_and_readers(sdk: _State) -> None:
    # close() before draining must kill the remote process, bounded-reap it via
    # the SDK's own wait(), and join both stream-reader threads — so a cancelled
    # foreground attach doesn't return before teardown.
    from omnigent.onboarding.sandboxes.tenki import _CLOSE_REAP_TIMEOUT_S

    process = _launcher().stream_exec("sb-tenki-1", "long-running")
    process.close()
    assert sdk.stream_killed is True
    # A bounded SDK wait() was issued for reaping (not the wrapper's unbounded one).
    assert _CLOSE_REAP_TIMEOUT_S in sdk.stream_wait_calls
    # Both reader threads were shut down.
    assert all(not reader.is_alive() for reader in process._readers)


def test_stream_exec_appends_newline_to_partial_final_chunk(sdk: _State) -> None:
    sdk.stream_stdout = b"partial"
    process = _launcher().stream_exec("sb-tenki-1", "do-thing")
    assert "".join(process.lines) == "partial\n"
    assert process.wait() == 0


def test_stream_exec_nonzero_exit_returns_code_without_raising(sdk: _State) -> None:
    sdk.stream_stdout = b"x\n"
    sdk.stream_exit_code = 5
    process = _launcher().stream_exec("sb-tenki-1", "false")
    assert list(process.lines) == ["x\n"]
    assert process.wait() == 5


def test_stream_exec_wait_surfaces_transport_error(sdk: _State) -> None:
    sdk.stream_wait_raises = _SandboxError("daemon connection lost")
    process = _launcher().stream_exec("sb-tenki-1", "do-thing")
    list(process.lines)  # drain to the sentinel
    with pytest.raises(click.ClickException, match="daemon connection lost"):
        process.wait()


def test_exec_foreground_echoes_and_returns_exit_code(
    sdk: _State, capsys: pytest.CaptureFixture[str]
) -> None:
    sdk.stream_stdout = b"line-1\n"
    code = _launcher().exec_foreground("sb-tenki-1", "omnigent host")
    assert code == 0
    assert "line-1" in capsys.readouterr().out
    # TERM is forced and the command is exec'd inside the login shell.
    argv = sdk.start_calls[-1]["argv"]
    assert argv[:2] == ("bash", "-lc")
    assert "TERM=xterm-256color exec omnigent host" in argv[2]


def test_exec_foreground_kills_on_keyboard_interrupt(
    sdk: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []

    class _Interrupting:
        @property
        def lines(self):
            raise KeyboardInterrupt

        def wait(self) -> int:
            return 0

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(
        TenkiSandboxLauncher, "stream_exec", lambda self, sid, cmd, *, pty=False: _Interrupting()
    )
    with pytest.raises(KeyboardInterrupt):
        _launcher().exec_foreground("sb-tenki-1", "omnigent host")
    assert closed == [True]


# ── handle caching ──────────────────────────────────────────


def test_resolve_caches_handle_across_primitives(sdk: _State) -> None:
    launcher = _launcher()
    launcher.run("sb-tenki-1", "a")
    launcher.run("sb-tenki-1", "b")
    assert sdk.get_calls == ["sb-tenki-1"]


def test_provision_caches_handle_so_run_skips_get(sdk: _State) -> None:
    launcher = _launcher()
    sandbox_id = launcher.provision("x")
    launcher.run(sandbox_id, "echo hi")
    assert sdk.get_calls == []


# ── wheel install + capability surface ──────────────────────


def test_wheel_install_command_overlays_wheels(sdk: _State) -> None:
    cmd = _launcher().wheel_install_command("/tmp/oa-wheels.tgz")
    assert "tar xzf /tmp/oa-wheels.tgz" in cmd
    assert "--force-reinstall" in cmd
    assert "--no-deps" in cmd


def test_capability_surface() -> None:
    launcher = TenkiSandboxLauncher()
    assert launcher.provider == "tenki"
    # CLI-bootstrap stays at the base default; no resume, no local port forward.
    assert launcher.supports_cli_bootstrap is True
    assert launcher.supports_local_port_forward is False
    assert launcher.can_resume is False
    with pytest.raises(SandboxCapabilityError, match="cannot forward a local port"):
        launcher.forward_local_port("sb-tenki-1", 8022)
