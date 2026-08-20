"""Tests for :mod:`omnigent.onboarding.sandboxes.e2b`."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import click
import pytest

from omnigent.onboarding.sandboxes.base import SandboxCapabilityError
from omnigent.onboarding.sandboxes.e2b import (
    _HOBBY_FALLBACK_LIFETIME_S,
    DEFAULT_E2B_TEMPLATE,
    SANDBOX_ENV_PASSTHROUGH_ENV_VAR,
    TEMPLATE_ENV_VAR,
    E2BSandboxLauncher,
    _is_missing_template_error,
    _lifetime_cap_from_error,
    resolve_max_lifetime_s,
)

# ── Fake e2b SDK ────────────────────────────────────────────
#
# The SDK is an optional dependency the test env may not install, and
# real Sandbox objects only exist server-side — so these are hand-rolled
# stubs injected via sys.modules, resolving the launcher's function-local
# `from e2b import ...` / `from e2b.exceptions import ...`.


class _SandboxException(Exception):
    pass


class _NotFoundException(_SandboxException):
    pass


class _TemplateException(_SandboxException):
    pass


class _AuthenticationException(Exception):
    # Mirrors the real class: extends Exception, NOT SandboxException.
    pass


class _CommandExitException(_SandboxException):
    """Mirrors the real class: a SandboxException that also carries the result."""

    def __init__(
        self, *, stdout: str = "", stderr: str = "", exit_code: int = 1, error: str | None = None
    ) -> None:
        super().__init__(f"Command exited with code {exit_code} and error:\n{stderr}")
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.error = error


@dataclass
class _FakeCommandResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    error: str | None = None


@dataclass
class _State:
    """Shared recorder for assertions."""

    create_kwargs: dict = field(default_factory=dict)
    create_calls: list[dict] = field(default_factory=list)
    run_calls: list[dict] = field(default_factory=list)
    written: list[tuple[str, bytes]] = field(default_factory=list)
    killed: list[str] = field(default_factory=list)
    set_timeouts: list[int] = field(default_factory=list)
    exec_result: _FakeCommandResult = field(default_factory=_FakeCommandResult)
    stream_result: _FakeCommandResult = field(default_factory=_FakeCommandResult)
    running: bool = True
    connect_missing: bool = False
    connect_calls: list[str] = field(default_factory=list)
    kill_missing: bool = False
    kill_raises: bool = False
    set_timeout_raises: bool = False
    create_raises: BaseException | None = None
    # When set, create() rejects (like E2B's 400) any timeout above this many
    # seconds, reporting the cap in hours — drives the clamp-and-retry test.
    reject_timeout_over: int | None = None
    handle_killed: bool = False
    # When set, a background command's wait() raises this — drives the
    # stream transport-error path.
    stream_wait_raises: BaseException | None = None


class _FakeCommandHandle:
    def __init__(self, state: _State) -> None:
        self._state = state

    def wait(self, on_stdout=None, on_stderr=None, on_pty=None) -> _FakeCommandResult:
        result = self._state.stream_result
        if on_stdout is not None and result.stdout:
            on_stdout(result.stdout)
        if on_stderr is not None and result.stderr:
            on_stderr(result.stderr)
        if self._state.stream_wait_raises is not None:
            raise self._state.stream_wait_raises
        if result.exit_code != 0:
            raise _CommandExitException(
                stdout=result.stdout, stderr=result.stderr, exit_code=result.exit_code
            )
        return result

    def kill(self) -> bool:
        if self._state.kill_raises:
            raise _SandboxException("already exited")
        self._state.handle_killed = True
        return True


class _FakeCommands:
    def __init__(self, state: _State) -> None:
        self._state = state

    def run(self, cmd: str, *, timeout=None, background: bool = False):
        self._state.run_calls.append({"cmd": cmd, "timeout": timeout, "background": background})
        if background:
            return _FakeCommandHandle(self._state)
        result = self._state.exec_result
        if result.exit_code != 0:
            raise _CommandExitException(
                stdout=result.stdout, stderr=result.stderr, exit_code=result.exit_code
            )
        return result


class _FakeFiles:
    def __init__(self, state: _State) -> None:
        self._state = state

    def write(self, path: str, data: bytes):
        self._state.written.append((path, data))
        return object()  # WriteInfo stand-in


class _FakeSandbox:
    _state: _State

    def __init__(self, sandbox_id: str = "sb-e2b-1") -> None:
        self._sandbox_id = sandbox_id
        self.commands = _FakeCommands(self._state)
        self.files = _FakeFiles(self._state)

    @property
    def sandbox_id(self) -> str:
        return self._sandbox_id

    @classmethod
    def create(cls, **kwargs) -> _FakeSandbox:
        cls._state.create_kwargs = kwargs
        cls._state.create_calls.append(kwargs)
        if cls._state.create_raises is not None:
            raise cls._state.create_raises
        cap = cls._state.reject_timeout_over
        if cap is not None and kwargs.get("timeout", 0) > cap:
            # Mirror E2B's 400 rejection of an over-cap lifetime.
            raise _SandboxException(f"400: Timeout cannot be greater than {cap // 3600} hours")
        return cls()

    @classmethod
    def connect(cls, sandbox_id: str, **kwargs) -> _FakeSandbox:
        cls._state.connect_calls.append(sandbox_id)
        if cls._state.connect_missing:
            raise _NotFoundException(sandbox_id)
        return cls(sandbox_id)

    @staticmethod
    def kill(sandbox_id: str, **kwargs) -> bool:
        if _FakeSandbox._state.kill_missing:
            raise _NotFoundException(sandbox_id)
        _FakeSandbox._state.killed.append(sandbox_id)
        return True

    def is_running(self, request_timeout=None) -> bool:
        return self._state.running

    def set_timeout(self, timeout: int, **kwargs) -> None:
        if self._state.set_timeout_raises:
            raise _SandboxException("rejected")
        self._state.set_timeouts.append(timeout)


@pytest.fixture()
def sdk(monkeypatch: pytest.MonkeyPatch) -> _State:
    state = _State()
    _FakeSandbox._state = state

    mod = types.ModuleType("e2b")
    mod.Sandbox = _FakeSandbox  # type: ignore[attr-defined]
    mod.CommandExitException = _CommandExitException  # type: ignore[attr-defined]
    mod.CommandResult = _FakeCommandResult  # type: ignore[attr-defined]
    exc = types.ModuleType("e2b.exceptions")
    exc.SandboxException = _SandboxException  # type: ignore[attr-defined]
    exc.NotFoundException = _NotFoundException  # type: ignore[attr-defined]
    exc.TemplateException = _TemplateException  # type: ignore[attr-defined]
    exc.AuthenticationException = _AuthenticationException  # type: ignore[attr-defined]
    mod.exceptions = exc  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "e2b", mod)
    monkeypatch.setitem(sys.modules, "e2b.exceptions", exc)
    monkeypatch.setenv("E2B_API_KEY", "e2b-test-key")
    monkeypatch.delenv(TEMPLATE_ENV_VAR, raising=False)
    monkeypatch.delenv(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, raising=False)
    return state


# ── prepare ─────────────────────────────────────────────────


def test_prepare_requires_api_key(sdk: _State, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("E2B_API_KEY")
    with pytest.raises(click.ClickException, match="E2B_API_KEY"):
        E2BSandboxLauncher().prepare()


def test_prepare_raises_install_hint_when_sdk_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # A None entry in sys.modules makes `import e2b` raise ImportError.
    monkeypatch.setitem(sys.modules, "e2b", None)
    monkeypatch.setenv("E2B_API_KEY", "k")
    with pytest.raises(click.ClickException, match=r"pip install 'omnigent\[e2b\]'"):
        E2BSandboxLauncher().prepare()


# ── provision ───────────────────────────────────────────────


def test_provision_uses_default_template_and_max_timeout(sdk: _State) -> None:
    assert E2BSandboxLauncher().provision("managed-x") == "sb-e2b-1"
    assert sdk.create_kwargs["template"] == DEFAULT_E2B_TEMPLATE
    assert sdk.create_kwargs["timeout"] == resolve_max_lifetime_s()
    assert sdk.create_kwargs["metadata"] == {"omnigent-name": "managed-x"}
    # No env configured → nothing injected.
    assert sdk.create_kwargs["envs"] is None


def test_provision_template_resolution_order(sdk: _State, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TEMPLATE_ENV_VAR, "env-template")
    E2BSandboxLauncher(template="explicit-template").provision("x")
    assert sdk.create_kwargs["template"] == "explicit-template"


def test_provision_template_from_env_when_no_explicit(
    sdk: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TEMPLATE_ENV_VAR, "env-template")
    E2BSandboxLauncher().provision("x")
    assert sdk.create_kwargs["template"] == "env-template"


def test_provision_env_passthrough_from_server_env(
    sdk: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-123")
    E2BSandboxLauncher(env=["ANTHROPIC_API_KEY"]).provision("x")
    assert sdk.create_kwargs["envs"] == {"ANTHROPIC_API_KEY": "sk-ant-123"}


def test_provision_env_passthrough_missing_var_fails_loud(sdk: _State) -> None:
    with pytest.raises(click.ClickException, match="NOT_SET_ANYWHERE"):
        E2BSandboxLauncher(env=["NOT_SET_ANYWHERE"]).provision("x")


def test_provision_incompatible_template_points_at_build(sdk: _State) -> None:
    # TemplateException is the SDK's incompatible/old-envd template signal.
    sdk.create_raises = _TemplateException("envd version too old")
    with pytest.raises(click.ClickException, match="e2b template build"):
        E2BSandboxLauncher().provision("x")


def test_provision_missing_template_points_at_build(sdk: _State) -> None:
    # A missing/unbuilt template is a PLAIN SandboxException ("404: template
    # '…' not found"), not TemplateException — the most common first-run
    # failure must still surface the build hint.
    sdk.create_raises = _SandboxException("404: template 'omnigent-host' not found")
    with pytest.raises(click.ClickException, match="e2b template build"):
        E2BSandboxLauncher().provision("x")


def test_provision_auth_error_is_friendly(sdk: _State) -> None:
    # AuthenticationException does NOT extend SandboxException; it must still
    # surface as a credential hint, not escape raw.
    sdk.create_raises = _AuthenticationException("401: invalid api key")
    with pytest.raises(click.ClickException, match="E2B_API_KEY"):
        E2BSandboxLauncher().provision("x")


def test_provision_sandbox_error_surfaces_reason(sdk: _State) -> None:
    sdk.create_raises = _SandboxException("quota exceeded")
    with pytest.raises(click.ClickException, match="quota exceeded"):
        E2BSandboxLauncher().provision("x")


def test_provision_clamps_lifetime_when_account_cap_rejects(sdk: _State) -> None:
    # E2B rejects (HTTP 400) — not clamps — a lifetime above the account cap
    # (e.g. Hobby's 1h vs the 24h default). provision must retry clamped to it.
    sdk.reject_timeout_over = 3600  # 1h cap
    assert E2BSandboxLauncher().provision("x") == "sb-e2b-1"
    timeouts = [call["timeout"] for call in sdk.create_calls]
    assert timeouts == [resolve_max_lifetime_s(), 3600]  # requested 24h, retried at 1h


def test_provision_env_override_skips_retry(sdk: _State, monkeypatch: pytest.MonkeyPatch) -> None:
    # With the lifetime pinned to the account cap, provision succeeds first try.
    monkeypatch.setenv("OMNIGENT_E2B_MAX_LIFETIME_S", "3600")
    sdk.reject_timeout_over = 3600
    E2BSandboxLauncher().provision("x")
    assert [call["timeout"] for call in sdk.create_calls] == [3600]


def test_provision_reraises_when_cap_not_below_request(sdk: _State) -> None:
    # A timeout rejection whose parsed cap (48h) is NOT below the 24h request
    # must surface as an error with NO second create attempt (no blind retry).
    sdk.create_raises = _SandboxException("400: Timeout cannot be greater than 48 hours")
    with pytest.raises(click.ClickException, match="E2B sandbox creation failed"):
        E2BSandboxLauncher().provision("x")
    assert len(sdk.create_calls) == 1


def test_provision_clamp_retry_also_failing_surfaces_error(sdk: _State) -> None:
    # First create rejects over-cap (→ clamp), and the clamped retry also
    # fails — provision must surface a ClickException after exactly 2 attempts.
    sdk.create_raises = _SandboxException("400: Timeout cannot be greater than 1 hours")
    with pytest.raises(click.ClickException, match="E2B sandbox creation failed"):
        E2BSandboxLauncher().provision("x")
    assert len(sdk.create_calls) == 2


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("400: Timeout cannot be greater than 1 hours", 3600),  # regex path
        ("400: Timeout cannot be greater than 24 hours", 24 * 3600),  # regex path
        ("Timeout cannot be greater than", _HOBBY_FALLBACK_LIFETIME_S),  # unparsed fallback
        ("404: template 'x' not found", None),  # unrelated → re-raise
    ],
)
def test_lifetime_cap_from_error_branches(message: str, expected: int | None) -> None:
    assert _lifetime_cap_from_error(message) == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("404: template 'omnigent-host' not found", True),
        ("Template 'x' Not Found", True),  # case-insensitive
        ("400: Timeout cannot be greater than 1 hours", False),
        ("429: rate limited", False),
    ],
)
def test_is_missing_template_error(message: str, expected: bool) -> None:
    assert _is_missing_template_error(message) is expected


def test_resolve_max_lifetime_rejects_non_numeric(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_E2B_MAX_LIFETIME_S", "soon")
    with pytest.raises(click.ClickException, match="must be a number of seconds"):
        resolve_max_lifetime_s()


# ── run ─────────────────────────────────────────────────────


def test_run_returns_separate_streams_and_exit_code(sdk: _State) -> None:
    sdk.exec_result = _FakeCommandResult(stdout="hi\n", stderr="warn\n", exit_code=0)
    result = E2BSandboxLauncher().run("sb-e2b-1", "echo hi")
    assert result.returncode == 0
    assert result.stdout == "hi\n"
    assert result.stderr == "warn\n"
    # Per-command timeout disabled so long jobs aren't killed at 60s.
    assert sdk.run_calls[0]["timeout"] == 0
    assert sdk.run_calls[0]["background"] is False


def test_run_handles_nonzero_exit_via_exception(sdk: _State) -> None:
    # E2B raises CommandExitException on non-zero exit; the launcher must
    # catch it, surface the captured output, and honor check.
    sdk.exec_result = _FakeCommandResult(stdout="boom\n", stderr="bad\n", exit_code=3)
    launcher = E2BSandboxLauncher()
    with pytest.raises(click.ClickException, match="exit 3"):
        launcher.run("sb-e2b-1", "false")
    unchecked = launcher.run("sb-e2b-1", "false", check=False)
    assert unchecked.returncode == 3
    assert unchecked.stdout == "boom\n"
    assert unchecked.stderr == "bad\n"


def test_run_wraps_command_in_login_bash(sdk: _State) -> None:
    E2BSandboxLauncher().run("sb-e2b-1", "echo hi")
    assert sdk.run_calls[0]["cmd"].startswith("bash -lc ")


# ── put ─────────────────────────────────────────────────────


def test_put_writes_bytes(sdk: _State, tmp_path: Path) -> None:
    local = tmp_path / "wheels.tgz"
    local.write_bytes(b"binary\x00data")
    E2BSandboxLauncher().put("sb-e2b-1", local, "/tmp/wheels.tgz")
    assert sdk.written == [("/tmp/wheels.tgz", b"binary\x00data")]


# ── attach ──────────────────────────────────────────────────


def test_attach_accepts_running_sandbox(sdk: _State) -> None:
    E2BSandboxLauncher().attach("sb-e2b-1")  # must not raise


def test_attach_rejects_stopped_sandbox(sdk: _State) -> None:
    sdk.running = False
    with pytest.raises(click.ClickException, match="not running"):
        E2BSandboxLauncher().attach("sb-e2b-1")


def test_resolve_missing_sandbox_is_friendly(sdk: _State) -> None:
    sdk.connect_missing = True
    with pytest.raises(click.ClickException, match="not found"):
        E2BSandboxLauncher().run("gone", "echo hi")


# ── keep_alive ──────────────────────────────────────────────


def test_keep_alive_extends_to_max(sdk: _State) -> None:
    E2BSandboxLauncher().keep_alive("sb-e2b-1")
    assert sdk.set_timeouts == [resolve_max_lifetime_s()]


def test_keep_alive_soft_fails(sdk: _State) -> None:
    sdk.set_timeout_raises = True
    E2BSandboxLauncher().keep_alive("sb-e2b-1")  # warns, must not raise
    assert sdk.set_timeouts == []


# ── terminate ───────────────────────────────────────────────


def test_terminate_kills_sandbox(sdk: _State) -> None:
    E2BSandboxLauncher().terminate("sb-e2b-1")
    assert sdk.killed == ["sb-e2b-1"]


def test_terminate_swallows_not_found(sdk: _State) -> None:
    sdk.kill_missing = True
    E2BSandboxLauncher().terminate("already-gone")  # must not raise
    assert sdk.killed == []


# ── streaming ───────────────────────────────────────────────


def test_stream_exec_combines_output_and_returns_stable_iterator(sdk: _State) -> None:
    sdk.stream_result = _FakeCommandResult(stdout="out\n", stderr="err\n", exit_code=0)
    process = E2BSandboxLauncher().stream_exec("sb-e2b-1", "do-thing")
    # The lines property must return the same iterator across accesses.
    first_access = process.lines
    assert process.lines is first_access
    assert list(process.lines) == ["out\n", "err\n"]
    assert process.wait() == 0


def test_stream_exec_close_kills_remote_handle(sdk: _State) -> None:
    process = E2BSandboxLauncher().stream_exec("sb-e2b-1", "do-thing")
    process.wait()
    process.close()
    assert sdk.handle_killed is True


def test_exec_foreground_echoes_and_returns_exit_code(
    sdk: _State, capsys: pytest.CaptureFixture[str]
) -> None:
    sdk.stream_result = _FakeCommandResult(stdout="line-1\n", exit_code=0)
    code = E2BSandboxLauncher().exec_foreground("sb-e2b-1", "omnigent host")
    assert code == 0
    assert "line-1" in capsys.readouterr().out
    # TERM is forced and the command is exec'd inside the login shell.
    foreground_cmd = sdk.run_calls[-1]["cmd"]
    assert "TERM=xterm-256color exec omnigent host" in foreground_cmd


def test_stream_exec_disables_per_command_timeout(sdk: _State) -> None:
    # The streaming path backs the long-lived foreground host; it must disable
    # the SDK's default 60s per-command cap (timeout=0), like run() does.
    E2BSandboxLauncher().stream_exec("sb-e2b-1", "omnigent host")
    assert sdk.run_calls[-1]["background"] is True
    assert sdk.run_calls[-1]["timeout"] == 0


def test_stream_exec_wait_surfaces_transport_error(sdk: _State) -> None:
    # A non-CommandExit failure from wait() (daemon outage) is a transport
    # error: wait() must re-raise it as a ClickException.
    sdk.stream_wait_raises = _SandboxException("daemon connection lost")
    process = E2BSandboxLauncher().stream_exec("sb-e2b-1", "do-thing")
    list(process.lines)  # drain to the sentinel
    with pytest.raises(click.ClickException, match="daemon connection lost"):
        process.wait()


def test_stream_exec_nonzero_exit_returns_code_without_raising(sdk: _State) -> None:
    # A non-zero stream exit is a normal outcome the caller inspects: wait()
    # RETURNS the code (contrast with run(), which raises when check=True).
    sdk.stream_result = _FakeCommandResult(stdout="x\n", exit_code=5)
    process = E2BSandboxLauncher().stream_exec("sb-e2b-1", "false")
    assert list(process.lines) == ["x\n"]
    assert process.wait() == 5


def test_stream_exec_close_swallows_kill_error(sdk: _State) -> None:
    # close() is best-effort and must never raise, even if kill() errors.
    sdk.kill_raises = True
    process = E2BSandboxLauncher().stream_exec("sb-e2b-1", "do-thing")
    process.wait()
    process.close()  # must not raise
    process.close()  # idempotent


def test_stream_exec_appends_newline_to_partial_final_chunk(sdk: _State) -> None:
    # A callback chunk with no trailing newline still yields a newline-
    # terminated combined stream (the RemoteProcess contract).
    sdk.stream_result = _FakeCommandResult(stdout="partial", exit_code=0)
    process = E2BSandboxLauncher().stream_exec("sb-e2b-1", "do-thing")
    assert "".join(process.lines) == "partial\n"
    assert process.wait() == 0


def test_exec_foreground_kills_on_keyboard_interrupt(
    sdk: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ctrl-C during the attach must kill the remote process (real close())
    # and re-raise.
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
        E2BSandboxLauncher, "stream_exec", lambda self, sid, cmd, *, pty=False: _Interrupting()
    )
    with pytest.raises(KeyboardInterrupt):
        E2BSandboxLauncher().exec_foreground("sb-e2b-1", "omnigent host")
    assert closed == [True]


def test_resolve_caches_handle_across_primitives(sdk: _State) -> None:
    # Two primitives on the same id connect once (cached handle).
    launcher = E2BSandboxLauncher()
    launcher.run("sb-e2b-1", "a")
    launcher.run("sb-e2b-1", "b")
    assert sdk.connect_calls == ["sb-e2b-1"]


def test_provision_caches_handle_so_run_skips_connect(sdk: _State) -> None:
    # provision caches the created sandbox; a follow-up run reuses it (no connect).
    launcher = E2BSandboxLauncher()
    sandbox_id = launcher.provision("x")
    launcher.run(sandbox_id, "echo hi")
    assert sdk.connect_calls == []


# ── wheel install + capability surface ──────────────────────


def test_wheel_install_command_overlays_wheels(sdk: _State) -> None:
    cmd = E2BSandboxLauncher().wheel_install_command("/tmp/oa-wheels.tgz")
    assert "tar xzf /tmp/oa-wheels.tgz" in cmd
    assert "--force-reinstall" in cmd
    assert "--no-deps" in cmd


def test_capability_surface() -> None:
    launcher = E2BSandboxLauncher()
    assert launcher.provider == "e2b"
    # CLI-bootstrap stays at the base default; no local port forward.
    assert launcher.supports_cli_bootstrap is True
    assert launcher.supports_local_port_forward is False
    with pytest.raises(SandboxCapabilityError, match="cannot forward a local port"):
        launcher.forward_local_port("sb-e2b-1", 8022)
