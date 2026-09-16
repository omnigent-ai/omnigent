from __future__ import annotations

import asyncio
import base64
import inspect
import json
import threading
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

import omnigent.host.setup_operations as operations
import omnigent.terminals.control_bridge as control_bridge
from omnigent.host.setup_operations import (
    SetupOperationAction,
    SetupOperationError,
    SetupOperationManager,
    SetupOperationState,
)


class _FakeTerminal:
    def __init__(self, plan: operations._CommandPlan) -> None:
        self.plan = plan
        self.socket_path = Path("/fixture/tmux.sock")
        self.tmux_target = "main"
        self.running = False
        self.closed = 0
        self._exit_code: int | None = None
        self._on_exit: Callable[[], None | Awaitable[None]] | None = None

    async def launch(self, *, cwd: Path | None = None) -> None:
        assert cwd is None
        self.running = True

    async def close(self) -> None:
        self.running = False
        self.closed += 1

    def last_exit_status(self) -> int | None:
        return self._exit_code

    def note_client_interaction(self) -> None:
        return

    def start_idle_watcher(
        self,
        on_idle: Callable[[], None | Awaitable[None]],
        *,
        on_exit: Callable[[], None | Awaitable[None]] | None = None,
    ) -> None:
        del on_idle
        self._on_exit = on_exit

    async def complete(self, exit_code: int) -> None:
        self._exit_code = exit_code
        self.running = False
        assert self._on_exit is not None
        result = self._on_exit()
        if inspect.isawaitable(result):
            await result


class _Harness:
    def __init__(
        self,
        *,
        verifier: Callable[
            [SetupOperationAction, Mapping[str, object]], bool | None
        ] = lambda _action, _parameters: True,
    ) -> None:
        self.terminals: list[_FakeTerminal] = []
        self.persisted: list[SetupOperationAction] = []
        self.manager = SetupOperationManager(
            terminal_factory=self._terminal_factory,
            executable_resolver=lambda name: f"/fixture/bin/{name}",
            verifier=verifier,
            post_success=self._persist,
            command_timeout_seconds=1,
            detached_expiration_seconds=1,
        )

    def _terminal_factory(self, plan: operations._CommandPlan, operation_id: str) -> _FakeTerminal:
        assert operation_id
        terminal = _FakeTerminal(plan)
        self.terminals.append(terminal)
        return terminal

    def _persist(self, action: SetupOperationAction, _parameters: Mapping[str, object]) -> None:
        self.persisted.append(action)


@pytest.mark.asyncio
async def test_current_screen_seed_drops_unused_rows_before_cursor_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = b"FIXTURE ONLY\nDevice code: TEST-1234\nInput: \n\n   \n\n"
    metadata = control_bridge._PaneMetadata(
        cursor_x=7,
        cursor_y=2,
        cursor_visible=True,
        alternate_on=False,
    )

    async def fake_metadata(
        _tmux: str,
        _socket_path: str,
        _tmux_target: str,
    ) -> control_bridge._PaneMetadata:
        return metadata

    class FakeCaptureProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, None]:
            return capture, None

    spawn_args: list[tuple[object, ...]] = []

    async def fake_spawn(*args: object, **_kwargs: object) -> FakeCaptureProcess:
        spawn_args.append(args)
        return FakeCaptureProcess()

    monkeypatch.setattr(control_bridge.shutil, "which", lambda _name: "/fixture/bin/tmux")
    monkeypatch.setattr(control_bridge, "_capture_pane_metadata", fake_metadata)
    monkeypatch.setattr(control_bridge.asyncio, "create_subprocess_exec", fake_spawn)

    seed = await control_bridge._run_tmux_capture(
        "/fixture/tmux.sock",
        "main",
        include_scrollback=False,
    )

    assert seed == (
        b"\x1b[H\x1b[2JFIXTURE ONLY\r\nDevice code: TEST-1234\r\nInput: \x1b[3;8H\x1b[?25h"
    )

    scrollback_seed = await control_bridge._run_tmux_capture(
        "/fixture/tmux.sock",
        "main",
        include_scrollback=True,
    )

    assert scrollback_seed == (
        b"\x1b[H\x1b[2JFIXTURE ONLY\r\nDevice code: TEST-1234\r\n"
        b"Input: \r\n\r\n   \r\n\x1b[3;8H\x1b[?25h"
    )
    base_args = (
        "/fixture/bin/tmux",
        "-S",
        "/fixture/tmux.sock",
        "capture-pane",
        "-e",
        "-p",
        "-J",
        "-t",
        "main",
    )
    assert spawn_args == [base_args, (*base_args, "-S", "-")]


async def _wait_for_state(
    manager: SetupOperationManager,
    operation_id: str,
    state: SetupOperationState,
) -> None:
    for _ in range(100):
        if (await manager.get(operation_id)).state == state:
            return
        await asyncio.sleep(0.001)
    raise AssertionError(f"operation did not reach {state}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "executable", "args"),
    [
        ("claude-login", "claude", ("auth", "login", "--claudeai")),
        ("codex-login", "codex", ("login",)),
        ("cursor-login", "cursor-agent", ("login",)),
        ("cursor-logout", "cursor-agent", ("logout",)),
        ("antigravity-login", "agy", ()),
        ("opencode-login", "opencode", ("auth", "login")),
        ("qwen-configure", "qwen", ()),
        ("goose-configure", "goose", ("configure",)),
        ("hermes-configure", "hermes", ("model",)),
        ("kiro-login", "kiro-cli", ("login",)),
        ("kimi-login", "kimi", ("login",)),
    ],
)
async def test_fixed_vendor_action_maps_to_host_owned_argv(
    action: str, executable: str, args: tuple[str, ...]
) -> None:
    if action == "antigravity-login":
        harness = _Harness(verifier=lambda _action, _parameters: bool(harness.terminals))
    else:
        harness = _Harness()
    snapshot = await harness.manager.start({"action": action, "parameters": {}})

    plan = harness.terminals[0].plan
    assert plan.executable == f"/fixture/bin/{executable}"
    assert plan.args == args
    assert snapshot.as_dict() == {
        "operation_id": snapshot.operation_id,
        "state": "running",
        "action": action,
        "exit_code": None,
        "error": None,
        **({"can_verify": True} if action == "antigravity-login" else {}),
    }

    await harness.terminals[0].complete(0)
    await _wait_for_state(harness.manager, snapshot.operation_id, SetupOperationState.SUCCEEDED)
    assert harness.persisted == [SetupOperationAction(action)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agents", "expected_agents"),
    [(["codex"], "codex"), (["claude", "codex", "pi"], "claude,codex,pi")],
)
async def test_databricks_parameters_are_normalized_and_closed_over(
    monkeypatch: pytest.MonkeyPatch,
    agents: list[str],
    expected_agents: str,
) -> None:
    monkeypatch.setattr(operations, "databricks_sdk_installed", lambda: True)
    harness = _Harness(verifier=lambda _action, _parameters: None)
    snapshot = await harness.manager.start(
        {
            "action": "databricks-configure",
            "parameters": {
                "workspace_url": "https://workspace.cloud.databricks.com/browse?o=123",
                "agents": agents,
            },
        }
    )

    plan = harness.terminals[0].plan
    assert plan.executable
    assert plan.args[-2:] == (
        "https://workspace.cloud.databricks.com",
        expected_agents,
    )
    assert "browse" not in repr(snapshot.as_dict())

    await harness.manager.cancel(snapshot.operation_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_body",
    [
        {"action": "claude-logout"},
        {"action": "codex-logout"},
        {"action": "codex-login", "parameters": {"argv": ["sh"]}},
        {
            "action": "databricks-configure",
            "parameters": {"workspace_url": "file:///etc/passwd", "agents": ["codex"]},
        },
        {
            "action": "databricks-configure",
            "parameters": {
                "workspace_url": "https://user:secret@example.com",
                "agents": ["codex"],
            },
        },
        {
            "action": "databricks-configure",
            "parameters": {"workspace_url": "https://example.com", "agents": ["shell"]},
        },
        {
            "action": "databricks-configure",
            "parameters": {"workspace_url": "https://example.com", "agents": ["opencode"]},
        },
        {
            "action": "databricks-configure",
            "parameters": {"workspace_url": "https://example.com", "agents": ["claude", "pi"]},
        },
        {
            "action": "databricks-configure",
            "parameters": {"workspace_url": "https://example.com", "agents": ["codex", "codex"]},
        },
        {
            "action": "databricks-configure",
            "parameters": {"workspace_url": "https://example.com", "agents": [[]]},
        },
        {
            "action": "databricks-configure",
            "parameters": {"workspace_url": "https://example.com", "agents": [{}]},
        },
    ],
)
async def test_request_rejects_arbitrary_execution_inputs(
    request_body: dict[str, object],
) -> None:
    harness = _Harness()
    with pytest.raises(SetupOperationError) as exc_info:
        await harness.manager.start(request_body)
    assert exc_info.value.code == "invalid_request"
    assert harness.terminals == []


@pytest.mark.asyncio
async def test_missing_prerequisite_is_explicit_and_does_not_allocate_terminal() -> None:
    harness = _Harness()
    harness.manager._resolve_executable = lambda name: (
        "/fixture/bin/tmux" if name == "tmux" else None
    )

    with pytest.raises(SetupOperationError) as exc_info:
        await harness.manager.start({"action": "codex-login"})

    assert exc_info.value.code == "unavailable"
    assert exc_info.value.message == "Codex CLI is not installed on this host."
    assert harness.terminals == []


def test_supported_actions_only_reports_available_fixed_workflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent._platform as platform

    monkeypatch.setattr(operations, "databricks_sdk_installed", lambda: True)
    monkeypatch.setattr(platform, "_cli_fallback_dirs", lambda: ())
    available = {"tmux", "codex", "databricks", "ucode"}
    manager = SetupOperationManager(
        terminal_factory=lambda _plan, _operation_id: _FakeTerminal(_plan),
        executable_resolver=lambda name: f"/fixture/bin/{name}" if name in available else None,
    )

    assert manager.supported_actions() == (
        SetupOperationAction.CODEX_LOGIN,
        SetupOperationAction.DATABRICKS_CONFIGURE,
    )

    manager._resolve_executable = lambda name: "/fixture/bin/codex" if name == "codex" else None
    assert manager.supported_actions() == ()


@pytest.mark.asyncio
async def test_databricks_extra_is_required_before_terminal_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent._platform as platform

    available = {"tmux", "databricks", "uvx"}
    harness = _Harness()
    harness.manager._resolve_executable = lambda name: (
        f"/fixture/bin/{name}" if name in available else None
    )
    monkeypatch.setattr(operations, "databricks_sdk_installed", lambda: False)
    monkeypatch.setattr(platform, "_cli_fallback_dirs", lambda: ())

    assert SetupOperationAction.DATABRICKS_CONFIGURE not in harness.manager.supported_actions()
    with pytest.raises(SetupOperationError) as exc_info:
        await harness.manager.start(
            {
                "action": "databricks-configure",
                "parameters": {
                    "workspace_url": "https://workspace.example.com",
                    "agents": ["codex"],
                },
            }
        )

    assert exc_info.value.code == "unavailable"
    assert "databricks extra" in exc_info.value.message
    assert harness.terminals == []


@pytest.mark.asyncio
async def test_antigravity_login_resolves_supported_fallback_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnigent._platform as platform

    binary = tmp_path / "agy"
    binary.write_text("fixture executable", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(platform, "_cli_fallback_dirs", lambda: (tmp_path,))
    terminals: list[_FakeTerminal] = []

    def terminal_factory(plan: operations._CommandPlan, _operation_id: str) -> _FakeTerminal:
        terminal = _FakeTerminal(plan)
        terminals.append(terminal)
        return terminal

    manager = SetupOperationManager(
        terminal_factory=terminal_factory,
        executable_resolver=lambda name: "/fixture/bin/tmux" if name == "tmux" else None,
        verifier=lambda _action, _parameters: False,
    )

    assert SetupOperationAction.ANTIGRAVITY_LOGIN in manager.supported_actions()
    snapshot = await manager.start({"action": "antigravity-login"})
    assert terminals[0].plan.executable == str(binary)
    await manager.cancel(snapshot.operation_id)


def test_antigravity_verifier_uses_bounded_existing_cli_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.onboarding.harness_install as harness_install

    observed: list[tuple[str, float | None]] = []

    def logged_in(key: str, timeout: float | None = None) -> bool:
        observed.append((key, timeout))
        return True

    monkeypatch.setattr(harness_install, "harness_cli_logged_in", logged_in)
    assert operations._verify_action(SetupOperationAction.ANTIGRAVITY_LOGIN, {})
    assert observed == [
        (harness_install.GEMINI_FAMILY, harness_install.READINESS_CLI_PROBE_TIMEOUT_S)
    ]


@pytest.mark.asyncio
async def test_antigravity_already_connected_finishes_without_terminal() -> None:
    harness = _Harness(verifier=lambda _action, _parameters: True)

    snapshot = await harness.manager.start({"action": "antigravity-login"})

    assert snapshot.state == SetupOperationState.SUCCEEDED
    assert snapshot.as_dict()["already_connected"] is True
    assert "can_verify" not in snapshot.as_dict()
    assert harness.terminals == []
    assert harness.persisted == [SetupOperationAction.ANTIGRAVITY_LOGIN]
    assert (await harness.manager.get(snapshot.operation_id)).state == (
        SetupOperationState.SUCCEEDED
    )


@pytest.mark.asyncio
async def test_antigravity_explicit_verification_keeps_unverified_process_running() -> None:
    connected = False
    probes = 0

    def verifier(_action: SetupOperationAction, _parameters: Mapping[str, object]) -> bool:
        nonlocal probes
        probes += 1
        return connected

    harness = _Harness(verifier=verifier)
    started = await harness.manager.start({"action": "antigravity-login"})
    terminal = harness.terminals[0]
    assert started.state == SetupOperationState.RUNNING
    assert started.as_dict()["can_verify"] is True
    assert "already_connected" not in started.as_dict()

    with pytest.raises(SetupOperationError) as exc_info:
        await harness.manager.verify(started.operation_id)
    assert exc_info.value.code == "conflict"
    assert (await harness.manager.get(started.operation_id)).state == SetupOperationState.RUNNING
    assert terminal.closed == 0
    assert harness.persisted == []

    connected = True
    verified = await harness.manager.verify(started.operation_id)
    assert verified.state == SetupOperationState.SUCCEEDED
    assert "can_verify" not in verified.as_dict()
    assert terminal.closed == 1
    assert harness.persisted == [SetupOperationAction.ANTIGRAVITY_LOGIN]
    assert probes == 3
    assert not harness.manager.has_active_operation()


@pytest.mark.asyncio
async def test_antigravity_verify_close_failure_is_retryable_without_false_success() -> None:
    connected = False
    harness = _Harness(verifier=lambda _action, _parameters: connected)
    started = await harness.manager.start({"action": "antigravity-login"})
    terminal = harness.terminals[0]
    original_close = terminal.close
    closes = 0

    async def flaky_close() -> None:
        nonlocal closes
        closes += 1
        if closes == 1:
            raise RuntimeError("fixture-sensitive-terminal-output")
        await original_close()

    terminal.close = flaky_close  # type: ignore[method-assign]
    connected = True
    with pytest.raises(SetupOperationError) as exc_info:
        await harness.manager.verify(started.operation_id)
    assert exc_info.value.code == "verification_failed"
    assert "fixture-sensitive" not in exc_info.value.message
    retryable = await harness.manager.get(started.operation_id)
    assert retryable.state == SetupOperationState.RUNNING
    assert retryable.can_verify
    assert harness.manager.has_active_operation()
    assert not harness.manager._operations[started.operation_id].finishing
    assert harness.persisted == []

    verified = await harness.manager.verify(started.operation_id)
    assert verified.state == SetupOperationState.SUCCEEDED
    assert terminal.closed == 1
    assert harness.persisted == [SetupOperationAction.ANTIGRAVITY_LOGIN]
    assert not harness.manager.has_active_operation()


@pytest.mark.asyncio
async def test_antigravity_verify_rejects_concurrent_actions_during_terminal_close() -> None:
    connected = False
    harness = _Harness(verifier=lambda _action, _parameters: connected)
    started = await harness.manager.start({"action": "antigravity-login"})
    terminal = harness.terminals[0]
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    original_close = terminal.close

    async def delayed_close() -> None:
        close_started.set()
        await release_close.wait()
        await original_close()

    terminal.close = delayed_close  # type: ignore[method-assign]
    connected = True
    verification = asyncio.create_task(harness.manager.verify(started.operation_id))
    await asyncio.wait_for(close_started.wait(), timeout=1)
    for request in (
        harness.manager.verify(started.operation_id),
        harness.manager.cancel(started.operation_id),
        harness.manager.attach(started.operation_id, "fixture", lambda _event: _done()),
    ):
        with pytest.raises(SetupOperationError) as exc_info:
            await request
        assert exc_info.value.code == "conflict"
    assert harness.persisted == []
    release_close.set()
    assert (await verification).state == SetupOperationState.SUCCEEDED
    assert harness.persisted == [SetupOperationAction.ANTIGRAVITY_LOGIN]


@pytest.mark.asyncio
async def test_antigravity_verify_sanitizes_status_probe_exception() -> None:
    probes = 0

    def verifier(_action: SetupOperationAction, _parameters: Mapping[str, object]) -> bool:
        nonlocal probes
        probes += 1
        if probes == 1:
            return False
        raise RuntimeError("fixture-sensitive-auth-status")

    harness = _Harness(verifier=verifier)
    started = await harness.manager.start({"action": "antigravity-login"})
    with pytest.raises(SetupOperationError) as exc_info:
        await harness.manager.verify(started.operation_id)
    assert exc_info.value.code == "verification_failed"
    assert "fixture-sensitive" not in exc_info.value.message
    assert (await harness.manager.get(started.operation_id)).state == SetupOperationState.RUNNING
    assert harness.terminals[0].closed == 0
    await harness.manager.cancel(started.operation_id)


@pytest.mark.asyncio
async def test_antigravity_cancel_prevents_later_verification_probe() -> None:
    probes = 0

    def verifier(_action: SetupOperationAction, _parameters: Mapping[str, object]) -> bool:
        nonlocal probes
        probes += 1
        return False

    harness = _Harness(verifier=verifier)
    started = await harness.manager.start({"action": "antigravity-login"})
    cancelled = await harness.manager.cancel(started.operation_id)
    verified = await harness.manager.verify(started.operation_id)

    assert cancelled.state == verified.state == SetupOperationState.CANCELLED
    assert probes == 1
    assert harness.terminals[0].closed == 1
    assert harness.persisted == []


@pytest.mark.asyncio
async def test_antigravity_preflight_serializes_concurrent_starts() -> None:
    entered = threading.Event()
    release = threading.Event()

    def verifier(_action: SetupOperationAction, _parameters: Mapping[str, object]) -> bool:
        entered.set()
        release.wait(timeout=1)
        return False

    harness = _Harness(verifier=verifier)
    first = asyncio.create_task(harness.manager.start({"action": "antigravity-login"}))
    assert await asyncio.to_thread(entered.wait, 1)
    second = asyncio.create_task(harness.manager.start({"action": "codex-login"}))
    await asyncio.sleep(0)
    assert harness.terminals == []
    release.set()
    started = await first
    with pytest.raises(SetupOperationError) as exc_info:
        await second
    assert exc_info.value.code == "conflict"
    assert len(harness.terminals) == 1
    await harness.manager.cancel(started.operation_id)


@pytest.mark.asyncio
async def test_cancelled_preflight_keeps_guard_until_status_probe_exits() -> None:
    entered = threading.Event()
    release = threading.Event()

    def verifier(_action: SetupOperationAction, _parameters: Mapping[str, object]) -> bool:
        entered.set()
        release.wait(timeout=1)
        return False

    harness = _Harness(verifier=verifier)
    first = asyncio.create_task(harness.manager.start({"action": "antigravity-login"}))
    assert await asyncio.to_thread(entered.wait, 1)
    first.cancel()
    second = asyncio.create_task(harness.manager.start({"action": "codex-login"}))
    await asyncio.sleep(0)
    assert harness.terminals == []

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    started = await second
    assert started.state == SetupOperationState.RUNNING
    assert len(harness.terminals) == 1
    await harness.manager.cancel(started.operation_id)


@pytest.mark.asyncio
async def test_antigravity_exit_and_explicit_verify_persist_once() -> None:
    monitor_entered = threading.Event()
    release_monitor = threading.Event()
    probes = 0

    def verifier(_action: SetupOperationAction, _parameters: Mapping[str, object]) -> bool:
        nonlocal probes
        probes += 1
        if probes == 1:
            return False
        if probes == 2:
            monitor_entered.set()
            release_monitor.wait(timeout=1)
        return True

    harness = _Harness(verifier=verifier)
    started = await harness.manager.start({"action": "antigravity-login"})
    await harness.terminals[0].complete(0)
    assert await asyncio.to_thread(monitor_entered.wait, 1)

    verification = asyncio.create_task(harness.manager.verify(started.operation_id))
    for _ in range(100):
        if (
            await harness.manager.get(started.operation_id)
        ).state == SetupOperationState.SUCCEEDED:
            break
        await asyncio.sleep(0.001)
    release_monitor.set()
    assert (await verification).state == SetupOperationState.SUCCEEDED
    assert harness.persisted == [SetupOperationAction.ANTIGRAVITY_LOGIN]
    assert harness.terminals[0].closed == 1


@pytest.mark.asyncio
async def test_other_guided_login_cannot_verify_while_running() -> None:
    harness = _Harness()
    started = await harness.manager.start({"action": "codex-login"})
    with pytest.raises(SetupOperationError) as exc_info:
        await harness.manager.verify(started.operation_id)
    assert exc_info.value.code == "conflict"
    assert "can_verify" not in started.as_dict()
    await harness.manager.cancel(started.operation_id)


@pytest.mark.asyncio
async def test_guided_cursor_logout_rechecks_cli_after_cached_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.onboarding.harness_install as harness_install

    logged_in = True
    status_calls = 0

    def fake_status(*_args: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal status_calls
        status_calls += 1
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"isAuthenticated": logged_in}),
        )

    monkeypatch.setattr(harness_install, "_LOGIN_PROBE_CACHE", {})
    monkeypatch.setattr(harness_install.shutil, "which", lambda _name: "/fixture/bin/cursor-agent")
    monkeypatch.setattr(harness_install.subprocess, "run", fake_status)

    assert harness_install.harness_cli_logged_in(harness_install.CURSOR_KEY)
    logged_in = False
    harness = _Harness(verifier=operations._verify_action)
    snapshot = await harness.manager.start({"action": "cursor-logout"})
    await harness.terminals[0].complete(0)
    await _wait_for_state(harness.manager, snapshot.operation_id, SetupOperationState.SUCCEEDED)
    assert status_calls == 2


def test_setup_terminal_does_not_load_user_shell_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_dir = tmp_path / "terminal"
    private_dir.mkdir()
    monkeypatch.setattr(
        operations.tempfile,
        "mkdtemp",
        lambda **_kwargs: str(private_dir),
    )

    terminal = operations._create_terminal(
        operations._CommandPlan("/fixture/bin/codex", ("login",), "Codex CLI"),
        "operation-id",
    )

    assert terminal.env == {"SHELL": "/bin/sh"}
    assert terminal.env_unset == ["BASH_ENV", "ENV"]


@pytest.mark.parametrize(
    ("agents", "surface"),
    [
        (("claude",), "anthropic"),
        (("codex",), "openai"),
        (("pi",), "pi"),
        (("claude", "codex", "pi"), None),
    ],
)
def test_databricks_persistence_uses_selected_surface(
    monkeypatch: pytest.MonkeyPatch,
    agents: tuple[str, ...],
    surface: str | None,
) -> None:
    import omnigent.onboarding.setup as setup
    import omnigent.onboarding.setup_operations as persistence

    saved: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        setup,
        "_existing_profile_hosts",
        lambda: {"fixture-profile": "https://workspace.example.com"},
    )
    monkeypatch.setattr(setup, "_host_matches", lambda left, right: left == right)
    monkeypatch.setattr(
        persistence,
        "record_databricks_provider",
        lambda profile, *, surface=None: saved.append((profile, surface)),
    )

    operations._persist_verified_action(
        SetupOperationAction.DATABRICKS_CONFIGURE,
        {"workspace_url": "https://workspace.example.com", "agents": agents},
    )

    assert saved == [("fixture-profile", surface)]


@pytest.mark.asyncio
async def test_nonzero_exit_and_failed_verification_never_persist() -> None:
    harness = _Harness(verifier=lambda _action, _parameters: False)
    failed_exit = await harness.manager.start({"action": "codex-login"})
    await harness.terminals[-1].complete(7)
    await _wait_for_state(harness.manager, failed_exit.operation_id, SetupOperationState.FAILED)
    assert (await harness.manager.get(failed_exit.operation_id)).exit_code == 7

    failed_verify = await harness.manager.start({"action": "claude-login"})
    await harness.terminals[-1].complete(0)
    await _wait_for_state(harness.manager, failed_verify.operation_id, SetupOperationState.FAILED)
    assert (await harness.manager.get(failed_verify.operation_id)).error == (
        "The command completed, but setup could not be verified."
    )
    assert harness.persisted == []


@pytest.mark.asyncio
async def test_verifier_exception_finishes_operation_and_releases_guard() -> None:
    def broken_verifier(_action: SetupOperationAction, _parameters: Mapping[str, object]) -> bool:
        raise RuntimeError("credential details must not escape")

    harness = _Harness(verifier=broken_verifier)
    snapshot = await harness.manager.start({"action": "codex-login"})
    await harness.terminals[0].complete(0)
    await _wait_for_state(harness.manager, snapshot.operation_id, SetupOperationState.FAILED)

    failed = await harness.manager.get(snapshot.operation_id)
    assert failed.error == "The setup verification failed."
    assert harness.terminals[0].closed == 1
    assert not harness.manager.has_active_operation()


@pytest.mark.asyncio
async def test_timeout_closes_terminal_and_releases_guard() -> None:
    harness = _Harness()
    harness.manager._command_timeout_seconds = 0.01
    snapshot = await harness.manager.start({"action": "codex-login"})

    await _wait_for_state(harness.manager, snapshot.operation_id, SetupOperationState.FAILED)
    failed = await harness.manager.get(snapshot.operation_id)
    assert failed.error == "Setup operation timed out."
    assert harness.terminals[0].closed == 1
    assert not harness.manager.has_active_operation()


@pytest.mark.asyncio
async def test_cancel_during_verification_never_runs_persistence_late() -> None:
    verifier_started = threading.Event()
    release_verifier = threading.Event()

    def verifier(_action: SetupOperationAction, _parameters: Mapping[str, object]) -> bool:
        verifier_started.set()
        release_verifier.wait(timeout=1)
        return True

    harness = _Harness(verifier=verifier)
    snapshot = await harness.manager.start({"action": "codex-login"})
    await harness.terminals[0].complete(0)
    assert await asyncio.to_thread(verifier_started.wait, 1)

    cancel_task = asyncio.create_task(harness.manager.cancel(snapshot.operation_id))
    await asyncio.sleep(0)
    assert harness.manager.has_active_operation()
    release_verifier.set()
    cancelled = await cancel_task

    assert cancelled.state == SetupOperationState.CANCELLED
    assert harness.persisted == []


@pytest.mark.asyncio
async def test_cancel_closes_only_owned_terminal_and_keeps_guard_until_cleanup() -> None:
    release_close = asyncio.Event()
    harness = _Harness()
    snapshot = await harness.manager.start({"action": "codex-login"})
    terminal = harness.terminals[0]
    original_close = terminal.close

    async def delayed_close() -> None:
        await release_close.wait()
        await original_close()

    terminal.close = delayed_close  # type: ignore[method-assign]
    cancel_task = asyncio.create_task(harness.manager.cancel(snapshot.operation_id))
    await asyncio.sleep(0)
    assert harness.manager.has_active_operation()
    release_close.set()
    cancelled = await cancel_task

    assert cancelled.state == SetupOperationState.CANCELLED
    assert terminal.closed == 1
    assert not harness.manager.has_active_operation()


@pytest.mark.asyncio
async def test_conflicting_start_does_not_allocate_another_terminal() -> None:
    harness = _Harness()
    first = await harness.manager.start({"action": "codex-login"})

    with pytest.raises(SetupOperationError) as exc_info:
        await harness.manager.start({"action": "claude-login"})

    assert exc_info.value.code == "conflict"
    assert len(harness.terminals) == 1
    await harness.manager.cancel(first.operation_id)


@pytest.mark.asyncio
async def test_attach_uses_live_only_bridge_and_detach_does_not_stop_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness()
    harness.manager._detached_expiration_seconds = 0.05
    snapshot = await harness.manager.start({"action": "codex-login"})
    terminal = harness.terminals[0]
    seen: list[dict[str, object]] = []
    output_events: list[dict[str, object]] = []

    async def fake_bridge(websocket: object, **kwargs: object) -> None:
        assert kwargs["seed_scrollback"] is False
        await websocket.send_bytes(b"pre-attach prompt")  # type: ignore[attr-defined]
        while True:
            message = await websocket.receive()  # type: ignore[attr-defined]
            seen.append(message)
            if message["type"] == "websocket.disconnect":
                return

    monkeypatch.setattr(operations, "bridge_tmux_control_to_websocket", fake_bridge)

    async def send(event: dict[str, object]) -> None:
        output_events.append(event)

    await harness.manager.attach(snapshot.operation_id, "attachment-1", send)
    await harness.manager.handle_terminal(
        snapshot.operation_id,
        "attachment-1",
        {"type": "resize", "cols": 100, "rows": 32},
    )
    await harness.manager.handle_terminal(
        snapshot.operation_id,
        "attachment-1",
        {
            "type": "input",
            "encoding": "base64",
            "data": base64.b64encode(b"secret-not-replayed").decode("ascii"),
        },
    )
    for _ in range(100):
        if len(seen) == 2:
            break
        await asyncio.sleep(0.001)
    await harness.manager.detach(snapshot.operation_id, "attachment-1")

    assert terminal.closed == 0
    assert base64.b64decode(str(output_events[0]["data"])) == b"pre-attach prompt"
    assert json.loads(str(seen[0]["text"])) == {"type": "resize", "cols": 100, "rows": 32}
    assert seen[1]["bytes"] == b"secret-not-replayed"
    assert seen[2]["type"] == "websocket.disconnect"

    await _wait_for_state(harness.manager, snapshot.operation_id, SetupOperationState.EXPIRED)
    assert terminal.closed == 1


@pytest.mark.asyncio
async def test_only_one_attachment_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness()
    snapshot = await harness.manager.start({"action": "codex-login"})

    async def fake_bridge(websocket: object, **_kwargs: object) -> None:
        await websocket.receive()  # type: ignore[attr-defined]

    monkeypatch.setattr(operations, "bridge_tmux_control_to_websocket", fake_bridge)
    await harness.manager.attach(snapshot.operation_id, "first", lambda _event: _done())
    with pytest.raises(SetupOperationError) as exc_info:
        await harness.manager.attach(snapshot.operation_id, "second", lambda _event: _done())
    assert exc_info.value.code == "conflict"

    await harness.manager.detach(snapshot.operation_id, "first")
    await harness.manager.cancel(snapshot.operation_id)


@pytest.mark.asyncio
async def test_input_queue_overrun_disconnects_attachment() -> None:
    async def send(_event: dict[str, object]) -> None:
        return

    attachment = operations._TerminalAttachment(send)
    payload = {
        "type": "input",
        "encoding": "base64",
        "data": base64.b64encode(b"x").decode("ascii"),
    }
    for _ in range(operations._MAX_PENDING_INPUT_EVENTS):
        await attachment.feed(payload)

    with pytest.raises(SetupOperationError) as exc_info:
        await attachment.feed(payload)
    assert exc_info.value.code == "conflict"
    assert await attachment.receive() == {"type": "websocket.disconnect", "code": 1009}

    with pytest.raises(SetupOperationError):
        await attachment.feed(payload)


@pytest.mark.asyncio
async def test_shutdown_does_not_close_owned_terminal_twice_during_finish() -> None:
    release_close = asyncio.Event()
    harness = _Harness()
    snapshot = await harness.manager.start({"action": "codex-login"})
    terminal = harness.terminals[0]
    original_close = terminal.close

    async def delayed_close() -> None:
        await release_close.wait()
        await original_close()

    terminal.close = delayed_close  # type: ignore[method-assign]
    await terminal.complete(0)
    await asyncio.sleep(0)
    shutdown_task = asyncio.create_task(harness.manager.shutdown())
    await asyncio.sleep(0)
    release_close.set()
    await shutdown_task

    assert terminal.closed == 1
    assert (await harness.manager.get(snapshot.operation_id)).state in {
        SetupOperationState.SUCCEEDED,
        SetupOperationState.CANCELLED,
    }
    assert not harness.manager.has_active_operation()


@pytest.mark.asyncio
async def test_completed_operation_retention_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(operations, "_COMPLETED_OPERATION_RETENTION", 2)
    harness = _Harness()
    completed_ids: list[str] = []
    for _ in range(3):
        snapshot = await harness.manager.start({"action": "codex-login"})
        completed_ids.append(snapshot.operation_id)
        await harness.terminals[-1].complete(0)
        await _wait_for_state(
            harness.manager, snapshot.operation_id, SetupOperationState.SUCCEEDED
        )

    assert len(harness.manager._operations) == 2
    with pytest.raises(SetupOperationError) as exc_info:
        await harness.manager.get(completed_ids[0])
    assert exc_info.value.code == "not_found"


async def _done() -> None:
    return


@pytest.mark.asyncio
async def test_terminal_callback_encodes_output_without_storing_it() -> None:
    events: list[dict[str, object]] = []

    async def send(event: dict[str, object]) -> None:
        events.append(event)

    attachment = operations._TerminalAttachment(send)
    await attachment.send_bytes(b"auth-output")
    await attachment.send_text('{"type":"clipboard-write"}')
    await attachment.close(4405, "terminal detached")

    assert events == [
        {
            "type": "output",
            "encoding": "base64",
            "data": base64.b64encode(b"auth-output").decode("ascii"),
        },
        {"type": "control", "data": '{"type":"clipboard-write"}'},
        {"type": "close", "code": 4405, "reason": "terminal detached"},
    ]
