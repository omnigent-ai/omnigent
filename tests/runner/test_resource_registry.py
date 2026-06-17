"""Tests for runner-side SessionResourceRegistry (Phase 2)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.entities import DEFAULT_ENVIRONMENT_ID
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.inner.os_env import EditEntry, OpResult, OSEnvironment
from omnigent.inner.terminal import TerminalInstance
from omnigent.runner.resource_registry import (
    CLAUDE_NATIVE_TERMINAL_ROLE,
    CODEX_NATIVE_TERMINAL_ROLE,
    SessionResourceRegistry,
    TerminalExitEvent,
    TerminalLifecycle,
)
from omnigent.terminals import TerminalRegistry
from tests.runner.helpers import make_test_terminal_instance


@dataclass
class _FakeOSEnvironment(OSEnvironment):
    """Minimal concrete OSEnvironment for registry tests."""

    _closed: bool = False

    async def read(
        self,
        path: str,
        offset: int = 1,
        limit: int | None = None,
    ) -> OpResult:
        del path, offset, limit
        return {}

    async def write(self, path: str, content: str) -> OpResult:
        del path, content
        return {}

    async def edit(
        self,
        path: str,
        *,
        old_text: str | None = None,
        new_text: str | None = None,
        edits: Sequence[EditEntry] | None = None,
    ) -> OpResult:
        del path, old_text, new_text, edits
        return {}

    async def shell(
        self,
        command: str,
        timeout: int | None = None,
    ) -> OpResult:
        del command, timeout
        return {}

    def close(self) -> None:
        self._closed = True


def _agent_spec_with_sandbox_none(cwd: Path) -> SimpleNamespace:
    """
    Return an agent-like object with an explicit sandbox-free OS env.

    :param cwd: Working directory for the OS environment.
    :returns: Object exposing an ``os_env`` attribute.
    """
    cwd.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(cwd),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )


def _seed_terminal(
    registry: TerminalRegistry,
    conversation_id: str,
    name: str,
    session_key: str,
    tmp_path: Path,
    *,
    os_env: OSEnvironment | None = None,
) -> None:
    """Seed a running terminal in the registry."""
    slot = registry._by_conversation.setdefault(conversation_id, {})
    slot[(name, session_key)] = TerminalInstance(
        name=name,
        session_key=session_key,
        socket_path=tmp_path / f"{name}-{session_key}.sock",
        private_dir=tmp_path / f"{name}-{session_key}",
        os_env=os_env,
        running=True,
    )


def test_list_resources_includes_default_env() -> None:
    """Registry always includes the logical default environment."""
    reg = SessionResourceRegistry()
    page = reg.list_resources("conv_1")

    ids = [r.id for r in page.data]
    assert DEFAULT_ENVIRONMENT_ID in ids
    default = page.data[0]
    assert default.type == "environment"
    assert default.metadata["role"] == "primary"


def test_list_resources_includes_terminals(tmp_path: Path) -> None:
    """Registry includes running terminals from the TerminalRegistry."""
    tr = TerminalRegistry()
    _seed_terminal(tr, "conv_1", "bash", "s1", tmp_path)
    reg = SessionResourceRegistry(terminal_registry=tr)

    page = reg.list_resources("conv_1")
    ids = [r.id for r in page.data]
    assert "terminal_bash_s1" in ids


def test_list_resources_filters_by_type(tmp_path: Path) -> None:
    """Registry filters by resource_type when specified."""
    tr = TerminalRegistry()
    _seed_terminal(tr, "conv_1", "bash", "s1", tmp_path)
    reg = SessionResourceRegistry(terminal_registry=tr)

    env_page = reg.list_resources("conv_1", resource_type="environment")
    assert all(r.type == "environment" for r in env_page.data)

    term_page = reg.list_resources("conv_1", resource_type="terminal")
    assert all(r.type == "terminal" for r in term_page.data)


@pytest.mark.asyncio
async def test_terminal_resource_role_is_private_and_cleared_on_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Terminal role markers stay private and follow close lifecycle.

    The Codex ensure route relies on this internal marker to distinguish a
    runner-owned Codex TUI from a generic ``codex/main`` terminal. If the
    marker leaks into public resource metadata, or if close leaves the marker
    behind, stale generic terminals can be misclassified on a later ensure.

    :param tmp_path: Temporary directory for fake terminal paths.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("codex", "main", tmp_path)

    async def _fake_launch(
        conversation_id: str,
        terminal_name: str,
        session_key: str,
        spec: TerminalEnvSpec,
        **kwargs: object,
    ) -> TerminalInstance:
        """
        Register a fake terminal instead of starting tmux.

        :param conversation_id: Owning session id, e.g. ``"conv_codex"``.
        :param terminal_name: Terminal name, e.g. ``"codex"``.
        :param session_key: Terminal session key, e.g. ``"main"``.
        :param spec: Terminal spec passed by the caller.
        :param kwargs: Additional launch kwargs.
        :returns: The fake terminal instance.
        """
        del spec, kwargs
        terminal_registry._by_conversation.setdefault(conversation_id, {})[
            (terminal_name, session_key)
        ] = instance
        return instance

    async def _fake_close(
        conversation_id: str,
        terminal_name: str,
        session_key: str,
    ) -> bool:
        """
        Remove the fake terminal from the registry.

        :param conversation_id: Owning session id, e.g. ``"conv_codex"``.
        :param terminal_name: Terminal name, e.g. ``"codex"``.
        :param session_key: Terminal session key, e.g. ``"main"``.
        :returns: ``True`` when the fake terminal existed.
        """
        slot = terminal_registry._by_conversation.get(conversation_id, {})
        return slot.pop((terminal_name, session_key), None) is not None

    monkeypatch.setattr(terminal_registry, "launch", _fake_launch)
    monkeypatch.setattr(terminal_registry, "close", _fake_close)

    view = await registry.launch_auxiliary_terminal(
        "conv_codex",
        "codex",
        "main",
        TerminalEnvSpec(command="codex", args=["--remote", "ws://127.0.0.1:1234"]),
        resource_role=CODEX_NATIVE_TERMINAL_ROLE,
    )

    assert registry.terminal_resource_role("conv_codex", view.id) == CODEX_NATIVE_TERMINAL_ROLE
    assert "command" not in view.metadata
    assert "args" not in view.metadata

    closed = await registry.close_terminal("conv_codex", view.id)

    assert closed is True
    assert registry.terminal_resource_role("conv_codex", view.id) is None


@pytest.mark.asyncio
async def test_terminal_resource_role_moves_on_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Private terminal role markers follow terminal transfer.

    Native Codex can rotate ownership between Omnigent sessions. If the role stays
    on the old session id, a warm reattach to the new session would look like
    a generic terminal and be replaced incorrectly.

    :param tmp_path: Temporary directory for fake terminal paths.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("codex", "main", tmp_path)

    async def _fake_launch(
        conversation_id: str,
        terminal_name: str,
        session_key: str,
        spec: TerminalEnvSpec,
        **kwargs: object,
    ) -> TerminalInstance:
        """
        Register a fake terminal instead of starting tmux.

        :param conversation_id: Owning session id, e.g. ``"conv_old"``.
        :param terminal_name: Terminal name, e.g. ``"codex"``.
        :param session_key: Terminal session key, e.g. ``"main"``.
        :param spec: Terminal spec passed by the caller.
        :param kwargs: Additional launch kwargs.
        :returns: The fake terminal instance.
        """
        del spec, kwargs
        terminal_registry._by_conversation.setdefault(conversation_id, {})[
            (terminal_name, session_key)
        ] = instance
        return instance

    async def _no_status_link(_link: str) -> None:
        """
        Avoid tmux calls while transfer updates the conversation link.

        :param _link: New conversation link.
        :returns: None.
        """

    monkeypatch.setattr(terminal_registry, "launch", _fake_launch)
    monkeypatch.setattr(instance, "set_conversation_link", _no_status_link)

    view = await registry.launch_auxiliary_terminal(
        "conv_old",
        "codex",
        "main",
        TerminalEnvSpec(command="codex", args=["--remote", "ws://127.0.0.1:1234"]),
        resource_role=CODEX_NATIVE_TERMINAL_ROLE,
    )

    moved = await registry.transfer_terminal("conv_old", "conv_new", view.id)

    assert moved is not None
    assert registry.terminal_resource_role("conv_old", view.id) is None
    assert registry.terminal_resource_role("conv_new", view.id) == CODEX_NATIVE_TERMINAL_ROLE


@pytest.mark.asyncio
async def test_terminal_lifecycle_cannot_change_after_observe(tmp_path: Path) -> None:
    """A terminal cannot silently switch between auxiliary and required lifecycle."""
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("worker", "main", tmp_path)

    await registry.observe_auxiliary_terminal("conv_lifecycle", "worker", "main", instance)

    with pytest.raises(RuntimeError, match="already observed as auxiliary"):
        await registry.observe_required_terminal("conv_lifecycle", "worker", "main", instance)


@pytest.mark.asyncio
async def test_auxiliary_terminal_exit_publishes_resource_exit_only(tmp_path: Path) -> None:
    """Auxiliary terminal exit is reported with auxiliary lifecycle metadata."""
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("sidecar", "s1", tmp_path)
    instance.command = "worker-cli"
    instance.args = ["--verbose"]
    instance.launch_cwd = str(tmp_path)
    instance._remember_pane_snapshot("\x1b[31mstartup failed\x1b[0m\nretry login")
    terminal_registry._by_conversation.setdefault("conv_exit", {})[("sidecar", "s1")] = instance
    exits: list[TerminalExitEvent] = []
    exit_published = asyncio.Event()
    callbacks: dict[str, object] = {}

    def _publish_exit(event: TerminalExitEvent) -> None:
        exits.append(event)
        exit_published.set()

    def _capture_watcher(
        on_idle: object | None = None,
        *,
        on_activity: object | None = None,
        on_exit: object | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del on_idle, on_activity, idle_threshold_s, poll_interval_s
        callbacks["on_exit"] = on_exit
        callbacks["replace"] = replace

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[method-assign]
    registry.set_terminal_exit_publisher(_publish_exit)

    await registry.observe_auxiliary_terminal("conv_exit", "sidecar", "s1", instance)
    on_exit = callbacks["on_exit"]
    assert callable(on_exit)
    on_exit()
    await asyncio.wait_for(exit_published.wait(), timeout=1.0)

    assert [event.lifecycle for event in exits] == [TerminalLifecycle.AUXILIARY]
    assert exits[0].terminal_id == "terminal_sidecar_s1"
    assert exits[0].command == "worker-cli"
    assert exits[0].args_count == 1
    assert exits[0].cwd == str(tmp_path)
    assert exits[0].last_output == "startup failed\nretry login"
    assert terminal_registry.get("conv_exit", "sidecar", "s1") is None


async def _observe_native_agent_terminal_and_capture(
    registry: SessionResourceRegistry,
    terminal_registry: TerminalRegistry,
    instance: object,
    session_id: str,
) -> dict[str, object]:
    """Observe *instance* as the native agent terminal, capturing its watcher.

    Returns the captured ``on_idle`` / ``on_activity`` / ``on_exit`` callbacks
    so a test can drive the PTY-status edges directly.
    """
    callbacks: dict[str, object] = {}

    def _capture_watcher(
        on_idle: object | None = None,
        *,
        on_activity: object | None = None,
        on_exit: object | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del idle_threshold_s, poll_interval_s, replace
        callbacks["on_idle"] = on_idle
        callbacks["on_activity"] = on_activity
        callbacks["on_exit"] = on_exit

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[attr-defined]
    # A status publisher is required for the native agent terminal's watcher to
    # wire its running/idle edges (and thus record the PTY status).
    registry.set_session_status_publisher(lambda _sid, _status: None)
    await registry.observe_required_terminal(
        session_id,
        instance.name,  # type: ignore[attr-defined]
        instance.session_key,  # type: ignore[attr-defined]
        instance,
        resource_role=CLAUDE_NATIVE_TERMINAL_ROLE,
    )
    return callbacks


@pytest.mark.asyncio
async def test_required_terminal_exit_while_idle_is_clean_shutdown(tmp_path: Path) -> None:
    """A required terminal that exits after going idle is not a failure.

    The native agent terminal is long-lived: it goes ``idle`` when its turn
    finishes. A pane exit observed while idle means the work was already
    delivered and the process simply shut down, so the exit event must carry
    ``session_was_idle=True`` — the runner uses that to avoid flipping the chat
    to ``failed`` (the spurious-"failed"-session bug).

    :param tmp_path: Temporary directory for fake terminal paths.
    """
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    terminal_registry._by_conversation.setdefault("conv_idle", {})[("claude", "main")] = instance
    exits: list[TerminalExitEvent] = []
    exit_published = asyncio.Event()

    def _publish_exit(event: TerminalExitEvent) -> None:
        exits.append(event)
        exit_published.set()

    registry.set_terminal_exit_publisher(_publish_exit)
    callbacks = await _observe_native_agent_terminal_and_capture(
        registry, terminal_registry, instance, "conv_idle"
    )

    # The agent worked, then its turn completed (pane quiesced → idle).
    on_activity = callbacks["on_activity"]
    on_idle = callbacks["on_idle"]
    assert callable(on_activity) and callable(on_idle)
    on_activity()
    on_idle()
    # Then the pane disappeared (e.g. Claude Code exited cleanly).
    on_exit = callbacks["on_exit"]
    assert callable(on_exit)
    on_exit()
    await asyncio.wait_for(exit_published.wait(), timeout=1.0)

    assert len(exits) == 1
    assert exits[0].lifecycle == TerminalLifecycle.REQUIRED
    assert exits[0].session_was_idle is True


@pytest.mark.asyncio
async def test_required_terminal_exit_while_running_is_failure(tmp_path: Path) -> None:
    """A required terminal that vanishes mid-turn is still a failure.

    When the last PTY-status edge was ``running``, the pane disappeared while
    a turn was in flight — a genuine crash — so the exit event reports
    ``session_was_idle=False`` and the runner keeps failing the session.

    :param tmp_path: Temporary directory for fake terminal paths.
    """
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    terminal_registry._by_conversation.setdefault("conv_run", {})[("claude", "main")] = instance
    exits: list[TerminalExitEvent] = []
    exit_published = asyncio.Event()

    def _publish_exit(event: TerminalExitEvent) -> None:
        exits.append(event)
        exit_published.set()

    registry.set_terminal_exit_publisher(_publish_exit)
    callbacks = await _observe_native_agent_terminal_and_capture(
        registry, terminal_registry, instance, "conv_run"
    )

    on_activity = callbacks["on_activity"]
    assert callable(on_activity)
    on_activity()
    on_exit = callbacks["on_exit"]
    assert callable(on_exit)
    on_exit()
    await asyncio.wait_for(exit_published.wait(), timeout=1.0)

    assert len(exits) == 1
    assert exits[0].session_was_idle is False


@pytest.mark.asyncio
async def test_required_terminal_exit_without_observed_status_is_failure(tmp_path: Path) -> None:
    """A required terminal that never reported a PTY status fails on exit.

    A boot failure (the process dies before producing any pane activity) leaves
    no recorded status, so the exit defaults to ``session_was_idle=False`` and
    the session still fails — only a positively-observed ``idle`` suppresses the
    failure.

    :param tmp_path: Temporary directory for fake terminal paths.
    """
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("worker", "main", tmp_path)
    terminal_registry._by_conversation.setdefault("conv_boot", {})[("worker", "main")] = instance
    exits: list[TerminalExitEvent] = []
    exit_published = asyncio.Event()
    callbacks: dict[str, object] = {}

    def _publish_exit(event: TerminalExitEvent) -> None:
        exits.append(event)
        exit_published.set()

    def _capture_watcher(
        on_idle: object | None = None,
        *,
        on_activity: object | None = None,
        on_exit: object | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del on_idle, on_activity, idle_threshold_s, poll_interval_s, replace
        callbacks["on_exit"] = on_exit

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[method-assign]
    registry.set_terminal_exit_publisher(_publish_exit)

    await registry.observe_required_terminal("conv_boot", "worker", "main", instance)
    on_exit = callbacks["on_exit"]
    assert callable(on_exit)
    on_exit()
    await asyncio.wait_for(exit_published.wait(), timeout=1.0)

    assert len(exits) == 1
    assert exits[0].session_was_idle is False


@pytest.mark.asyncio
async def test_required_terminal_exit_after_new_turn_is_failure(tmp_path: Path) -> None:
    """A crash right after a new turn starts (before the watcher's first
    ``running`` edge) is a failure, not a stale clean shutdown.

    ``note_session_turn_started`` resets the prior turn's ``idle`` memo so the
    turn-boundary window can't silently swallow a real crash.

    :param tmp_path: Temporary directory for fake terminal paths.
    """
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    terminal_registry._by_conversation.setdefault("conv_turn", {})[("claude", "main")] = instance
    exits: list[TerminalExitEvent] = []
    exit_published = asyncio.Event()

    def _publish_exit(event: TerminalExitEvent) -> None:
        exits.append(event)
        exit_published.set()

    registry.set_terminal_exit_publisher(_publish_exit)
    callbacks = await _observe_native_agent_terminal_and_capture(
        registry, terminal_registry, instance, "conv_turn"
    )

    # The previous turn finished (idle), then a new message starts a turn and
    # the pane crashes before the watcher emits its next ``running`` edge.
    on_idle = callbacks["on_idle"]
    assert callable(on_idle)
    on_idle()
    registry.note_session_turn_started("conv_turn")
    on_exit = callbacks["on_exit"]
    assert callable(on_exit)
    on_exit()
    await asyncio.wait_for(exit_published.wait(), timeout=1.0)

    assert len(exits) == 1
    assert exits[0].session_was_idle is False


@pytest.mark.asyncio
async def test_cleanup_session_clears_status_memo(tmp_path: Path) -> None:
    """``cleanup_session`` drops the session's PTY-status memo.

    :param tmp_path: Temporary directory (unused beyond registry construction).
    """
    del tmp_path
    registry = SessionResourceRegistry()
    registry.note_session_turn_started("conv_cleanup")
    assert "conv_cleanup" in registry._last_session_status

    await registry.cleanup_session("conv_cleanup")

    assert "conv_cleanup" not in registry._last_session_status


@pytest.mark.asyncio
async def test_transfer_terminal_moves_status_memo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transferring a terminal moves its PTY-status memo to the new owner.

    Fakes the launch (no real tmux/process) and the conversation-link update,
    mirroring ``test_terminal_resource_role_moves_on_transfer``.

    :param tmp_path: Temporary directory for fake terminal paths.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("codex", "main", tmp_path)

    async def _fake_launch(
        conversation_id: str,
        terminal_name: str,
        session_key: str,
        spec: TerminalEnvSpec,
        **kwargs: object,
    ) -> TerminalInstance:
        del spec, kwargs
        terminal_registry._by_conversation.setdefault(conversation_id, {})[
            (terminal_name, session_key)
        ] = instance
        return instance

    async def _no_status_link(_link: str) -> None:
        """Avoid tmux calls while transfer updates the conversation link."""

    monkeypatch.setattr(terminal_registry, "launch", _fake_launch)
    monkeypatch.setattr(instance, "set_conversation_link", _no_status_link)

    view = await registry.launch_auxiliary_terminal(
        "conv_src",
        "codex",
        "main",
        TerminalEnvSpec(command="codex", args=["--remote", "ws://127.0.0.1:1234"]),
        resource_role=CODEX_NATIVE_TERMINAL_ROLE,
    )
    registry.note_session_turn_started("conv_src")

    moved = await registry.transfer_terminal("conv_src", "conv_dst", view.id)

    assert moved is not None
    assert "conv_src" not in registry._last_session_status
    assert registry._last_session_status.get("conv_dst") == "running"


def test_get_resource_finds_default() -> None:
    """get_resource finds the default environment."""
    reg = SessionResourceRegistry()
    resource = reg.get_resource("conv_1", DEFAULT_ENVIRONMENT_ID)
    assert resource is not None
    assert resource.type == "environment"


def test_get_resource_returns_none_for_unknown() -> None:
    """get_resource returns None for unknown ids."""
    reg = SessionResourceRegistry()
    assert reg.get_resource("conv_1", "nonexistent") is None


def test_resolve_environment_creates_primary_lazily(
    tmp_path: Path,
) -> None:
    """resolve_environment lazily creates the primary OSEnvironment."""
    os.environ["OMNIGENT_RUNNER_OS_ENV_ROOT"] = str(tmp_path)
    try:
        reg = SessionResourceRegistry()
        assert not reg.has_primary_env("conv_1")

        agent_spec = _agent_spec_with_sandbox_none(tmp_path / "conv_1" / "workspace")
        env = reg.resolve_environment("conv_1", DEFAULT_ENVIRONMENT_ID, agent_spec)
        assert env is not None
        assert reg.has_primary_env("conv_1")

        env2 = reg.resolve_environment("conv_1", DEFAULT_ENVIRONMENT_ID, agent_spec)
        assert env2 is env
    finally:
        os.environ.pop("OMNIGENT_RUNNER_OS_ENV_ROOT", None)


def test_resolve_environment_default_pins_none_sandbox_when_no_agent_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default env (no agent_spec) must pin ``sandbox.type="none"``.

    Regression test for the resource-endpoint default: the default
    env must work on hosts without a usable sandbox backend. The
    pre-fix code left the default ``OSEnvSpec`` with
    ``sandbox=None``, so it routed through the Linux platform
    default (which would raise when no backend was available). We
    stub ``shutil.which`` to report ``bwrap`` IS present so that, if
    the default env wrongly routed through the platform default, it
    would resolve to an active ``linux_bwrap`` policy — the assertion
    below proves it pins ``none`` instead.

    :param tmp_path: Per-test workspace dir.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setattr(
        "omnigent.inner.sandbox.shutil.which",
        lambda name: "/usr/bin/bwrap",
    )
    monkeypatch.setenv("OMNIGENT_RUNNER_OS_ENV_ROOT", str(tmp_path))

    reg = SessionResourceRegistry()

    env = reg.resolve_environment("conv_no_spec", DEFAULT_ENVIRONMENT_ID)

    # backend_type="none" + active=False prove sandbox=none was
    # pinned, not picked by the platform-default fallback.
    assert env.sandbox.backend_type == "none"
    assert env.sandbox.active is False


def test_resolve_environment_uses_agent_spec_os_env(
    tmp_path: Path,
) -> None:
    """resolve_environment uses agent_spec.os_env when available."""
    os.environ["OMNIGENT_RUNNER_OS_ENV_ROOT"] = str(tmp_path)
    try:
        reg = SessionResourceRegistry()

        class _FakeSpec:
            os_env = OSEnvSpec(
                type="caller_process",
                cwd=str(tmp_path / "custom-cwd"),
                sandbox=OSEnvSandboxSpec(type="none"),
            )

        (tmp_path / "custom-cwd").mkdir()
        env = reg.resolve_environment(
            "conv_spec",
            DEFAULT_ENVIRONMENT_ID,
            _FakeSpec(),
        )
        assert env is not None
        assert str(env.cwd).endswith("custom-cwd")
    finally:
        os.environ.pop("OMNIGENT_RUNNER_OS_ENV_ROOT", None)


def test_resolve_environment_raises_for_unknown_env_id() -> None:
    """resolve_environment raises ValueError for unknown ids."""
    reg = SessionResourceRegistry()
    with pytest.raises(ValueError, match="not found"):
        reg.resolve_environment("conv_1", "env_nonexistent_foo")


def test_resolve_terminal_environment(tmp_path: Path) -> None:
    """resolve_environment resolves terminal environment ids."""
    tr = TerminalRegistry()
    terminal_env = _FakeOSEnvironment(
        spec=OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
        cwd=tmp_path,
    )
    _seed_terminal(
        tr,
        "conv_1",
        "bash",
        "s1",
        tmp_path,
        os_env=terminal_env,
    )
    reg = SessionResourceRegistry(terminal_registry=tr)

    env = reg.resolve_environment("conv_1", "env_terminal_bash_s1")
    assert env is terminal_env


@pytest.mark.asyncio
async def test_cleanup_session_closes_primary_env(
    tmp_path: Path,
) -> None:
    """cleanup_session closes the primary env and cleans terminals."""
    os.environ["OMNIGENT_RUNNER_OS_ENV_ROOT"] = str(tmp_path)
    try:
        reg = SessionResourceRegistry()
        reg.resolve_environment(
            "conv_1",
            DEFAULT_ENVIRONMENT_ID,
            _agent_spec_with_sandbox_none(tmp_path / "conv_1" / "workspace"),
        )
        assert reg.has_primary_env("conv_1")

        await reg.cleanup_session("conv_1")
        assert not reg.has_primary_env("conv_1")
    finally:
        os.environ.pop("OMNIGENT_RUNNER_OS_ENV_ROOT", None)


# ── Phase 4: cleanup endpoint tests ─────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_endpoint_returns_confirmation(
    tmp_path: Path,
) -> None:
    """DELETE /v1/sessions/{id}/resources returns cleanup confirmation."""
    import httpx

    from omnigent.runner import create_runner_app

    os.environ["OMNIGENT_RUNNER_OS_ENV_ROOT"] = str(tmp_path)
    try:
        reg = SessionResourceRegistry()
        reg.resolve_environment(
            "conv_cleanup",
            DEFAULT_ENVIRONMENT_ID,
            _agent_spec_with_sandbox_none(tmp_path / "conv_cleanup" / "workspace"),
        )
        from tests.runner.helpers import NullServerClient

        app = create_runner_app(
            resource_registry=reg,
            server_client=NullServerClient(),  # type: ignore[arg-type]
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://runner",
        ) as client:
            resp = await client.delete(
                "/v1/sessions/conv_cleanup/resources",
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["session_id"] == "conv_cleanup"
        assert body["cleaned"] is True
        assert not reg.has_primary_env("conv_cleanup")
    finally:
        os.environ.pop("OMNIGENT_RUNNER_OS_ENV_ROOT", None)


@pytest.mark.asyncio
async def test_cleanup_idempotent_for_unknown_session() -> None:
    """DELETE /v1/sessions/{id}/resources is safe for unknown sessions."""
    import httpx

    from omnigent.runner import create_runner_app
    from tests.runner.helpers import NullServerClient

    reg = SessionResourceRegistry()
    app = create_runner_app(
        resource_registry=reg,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://runner",
    ) as client:
        resp = await client.delete(
            "/v1/sessions/conv_unknown/resources",
        )
    assert resp.status_code == 200
    assert resp.json()["cleaned"] is True


# ── os_env gate: list_resources ──────────────────────────────────────────────


def test_list_resources_suppresses_default_env_when_spec_has_no_os_env() -> None:
    """list_resources omits the default environment when agent_spec.os_env is None.

    Agents without an os_env block have no primary filesystem environment,
    so the resource listing must not advertise one.
    """

    reg = SessionResourceRegistry()
    spec = SimpleNamespace(os_env=None)

    page = reg.list_resources("conv_no_env", agent_spec=spec)

    ids = [r.id for r in page.data]
    # Default environment must be absent — the spec has no os_env so there
    # is no primary filesystem environment to expose.  If this assertion
    # fails, the gate was not applied and the UI would show a "Working
    # folder" panel that can never return any files.
    assert DEFAULT_ENVIRONMENT_ID not in ids, (
        f"Default environment should be suppressed when os_env is None, but found ids: {ids}"
    )


def test_list_resources_includes_default_env_when_spec_has_os_env(
    tmp_path: Path,
) -> None:
    """list_resources keeps the default environment when agent_spec.os_env is set."""
    reg = SessionResourceRegistry()
    spec = _agent_spec_with_sandbox_none(tmp_path / "workspace")

    page = reg.list_resources("conv_with_env", agent_spec=spec)

    ids = [r.id for r in page.data]
    # Default environment must be present — the spec has an os_env configured
    # so a primary filesystem environment exists and must be advertised.
    assert DEFAULT_ENVIRONMENT_ID in ids, (
        f"Default environment should be present when os_env is set, but found ids: {ids}"
    )


def test_list_resources_includes_default_env_when_no_spec() -> None:
    """list_resources preserves legacy behaviour when agent_spec is None.

    Callers that do not pass an agent_spec (dev/standalone mode) must still
    see the default environment so the filesystem API is usable.
    """
    reg = SessionResourceRegistry()

    page = reg.list_resources("conv_legacy", agent_spec=None)

    ids = [r.id for r in page.data]
    # agent_spec=None is the legacy path; default env must always be present
    # so pre-existing callers that do not supply a spec are unaffected.
    assert DEFAULT_ENVIRONMENT_ID in ids, (
        f"Default environment should be present when agent_spec=None, but found ids: {ids}"
    )


# ── os_env gate: resolve_environment ────────────────────────────────────────


def test_resolve_environment_raises_when_spec_has_no_os_env() -> None:
    """resolve_environment raises ValueError when agent_spec.os_env is None.

    The registry must not silently fall back to a synthetic default
    environment when the spec explicitly has no os_env configured —
    that would create an environment the agent cannot use.
    """
    reg = SessionResourceRegistry()
    spec = SimpleNamespace(os_env=None)

    with pytest.raises(ValueError, match="no os_env"):
        reg.resolve_environment("conv_no_env", DEFAULT_ENVIRONMENT_ID, spec)


# ── Runner workspace overrides agent spec cwd ────────────────────────────


def test_compute_default_env_root_runner_workspace_overrides_relative_cwd(
    tmp_path: Path,
) -> None:
    """
    When runner_workspace is set and the agent spec has a relative
    cwd (``"."``), the runner workspace wins.

    This is the common case for CLI-launched sessions: the user's
    terminal cwd flows through ``OMNIGENT_RUNNER_WORKSPACE`` and
    the agent's relative cwd resolves against it.
    """
    workspace = tmp_path / "user-project"
    workspace.mkdir()
    reg = SessionResourceRegistry(
        runner_workspace=workspace,
        per_session_workspace=False,
    )
    spec = SimpleNamespace(
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=".",
            sandbox=OSEnvSandboxSpec(type="none"),
        )
    )

    root = reg.compute_default_env_root("conv_rel", spec)

    assert root == str(workspace.resolve())


def test_compute_default_env_root_runner_workspace_overrides_absolute_cwd(
    tmp_path: Path,
) -> None:
    """
    When runner_workspace is set and the agent spec has an absolute
    cwd, the runner workspace STILL wins.

    This is the new contract under
    designs/SESSION_WORKSPACE_SELECTION.md: an absolute cwd in the
    spec is a session-create-time *boundary*, not a runtime
    override. Host-launched sessions pick a workspace inside the
    boundary and that pick — not the boundary itself — drives the
    runtime cwd. Without this rule, a user picking
    ``~/universe/src/foo`` for an agent declaring ``cwd: ~/universe``
    would be silently relocated up to ``~/universe``.
    """
    workspace = tmp_path / "picked-subdir"
    workspace.mkdir()
    spec_cwd = tmp_path / "agent-spec-cwd"
    spec_cwd.mkdir()
    reg = SessionResourceRegistry(
        runner_workspace=workspace,
        per_session_workspace=False,
    )
    spec = SimpleNamespace(
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(spec_cwd),
            sandbox=OSEnvSandboxSpec(type="none"),
        )
    )

    root = reg.compute_default_env_root("conv_abs", spec)

    # Workspace wins, NOT spec_cwd.
    assert root == str(workspace.resolve())
    assert root != str(spec_cwd.resolve())


def test_compute_default_env_root_no_runner_workspace_uses_absolute_spec_cwd(
    tmp_path: Path,
) -> None:
    """
    When runner_workspace is NOT set, an absolute spec cwd is used.

    This pins the fallback path so unit tests / pure local runs
    that construct a spec directly without the env var keep
    working as before.
    """
    spec_cwd = tmp_path / "agent-spec-cwd"
    spec_cwd.mkdir()
    reg = SessionResourceRegistry(runner_workspace=None)
    spec = SimpleNamespace(
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(spec_cwd),
            sandbox=OSEnvSandboxSpec(type="none"),
        )
    )

    root = reg.compute_default_env_root("conv_no_workspace", spec)

    assert root == str(spec_cwd.resolve())


def test_compute_default_env_root_no_os_env_returns_none(tmp_path: Path) -> None:
    """
    When the agent spec has no os_env, return None regardless of
    whether runner_workspace is set.

    Headless agents (no ``os_env``) intentionally don't expose the
    filesystem; the runner_workspace override must not bypass that
    gate. Without this check, host-launched headless agents would
    suddenly grow filesystem access.
    """
    reg = SessionResourceRegistry(
        runner_workspace=tmp_path,
        per_session_workspace=False,
    )
    spec = SimpleNamespace(os_env=None)

    assert reg.compute_default_env_root("conv_headless", spec) is None


def test_resolve_environment_runner_workspace_overrides_absolute_spec_cwd(
    tmp_path: Path,
) -> None:
    """
    Materializing the primary OS environment uses runner_workspace
    over an absolute spec cwd.

    Pairs with the compute_default_env_root tests above to cover
    the eager creation path. _create_primary_env and
    compute_default_env_root must agree on cwd, otherwise the
    filesystem-list endpoint and the agent's actual cwd would
    drift apart.
    """
    workspace = tmp_path / "picked-subdir"
    workspace.mkdir()
    spec_cwd = tmp_path / "agent-spec-cwd"
    spec_cwd.mkdir()
    reg = SessionResourceRegistry(
        runner_workspace=workspace,
        per_session_workspace=False,
    )
    spec = SimpleNamespace(
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(spec_cwd),
            sandbox=OSEnvSandboxSpec(type="none"),
        )
    )

    env = reg.resolve_environment("conv_abs_eager", DEFAULT_ENVIRONMENT_ID, spec)

    assert env is not None
    # Compare via realpath because tmp_path on macOS goes through
    # /var → /private/var symlinks.
    assert os.path.realpath(env.cwd) == os.path.realpath(workspace)


# ── O6: bridge-dir cleanup on conversation teardown ─────────────────────────


@pytest.mark.asyncio
async def test_cleanup_session_removes_native_bridge_dirs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cleanup_session reaps the conversation's native bridge dirs (O6).

    Bridge dirs are keyed by conversation id, so the conversation-level
    reap is the right hook.  A bridge cleanup error must not propagate
    out of cleanup_session.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    import omnigent.claude_native_bridge as claude_bridge
    import omnigent.codex_native_bridge as codex_bridge

    cleaned: list[tuple[str, str]] = []

    def _codex_cleanup(bridge_id: str) -> bool:
        cleaned.append(("codex", bridge_id))
        return True

    def _claude_cleanup(bridge_id: str) -> bool:
        cleaned.append(("claude", bridge_id))
        raise RuntimeError("boom")  # must be swallowed by cleanup_session

    monkeypatch.setattr(codex_bridge, "cleanup_bridge_dir", _codex_cleanup)
    monkeypatch.setattr(claude_bridge, "cleanup_bridge_dir", _claude_cleanup)

    registry = SessionResourceRegistry(terminal_registry=None)

    await registry.cleanup_session("conv_bridge")

    assert ("codex", "conv_bridge") in cleaned
    assert ("claude", "conv_bridge") in cleaned
