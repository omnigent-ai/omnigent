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
    _TERMINAL_EXIT_OUTPUT_MAX_CHARS,
    CLAUDE_NATIVE_TERMINAL_ROLE,
    CODEX_NATIVE_TERMINAL_ROLE,
    SessionResourceRegistry,
    TerminalExitEvent,
    TerminalLifecycle,
    _sanitize_session_id,
    _session_workspace,
    _terminal_exit_diagnostics,
    _trim_terminal_exit_output,
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
        on_tick: object | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del on_idle, on_activity, on_tick, idle_threshold_s, poll_interval_s
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
        on_tick: object | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del idle_threshold_s, poll_interval_s, replace
        callbacks["on_idle"] = on_idle
        callbacks["on_activity"] = on_activity
        callbacks["on_exit"] = on_exit
        callbacks["on_tick"] = on_tick

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[attr-defined]
    # A status publisher is required for the native agent terminal's watcher to
    # wire its running/idle edges (and thus record the PTY status).
    registry.set_session_status_publisher(lambda _sid, _status, _reason=None: None)
    await registry.observe_required_terminal(
        session_id,
        instance.name,  # type: ignore[attr-defined]
        instance.session_key,  # type: ignore[attr-defined]
        instance,
        resource_role=CLAUDE_NATIVE_TERMINAL_ROLE,
    )
    return callbacks


class _FakeStatusPoller:
    """Controllable stand-in for the claude-native status-file poller.

    Lets a test flip :attr:`active` (file resolved and therefore owning the
    session's status, vs. the PTY watcher falling back) and fire status edges
    through the registry's callback, without touching a real
    ``sessions/<pid>.json``.
    """

    def __init__(self, on_status: object) -> None:
        self._on_status = on_status
        self.active = False
        self.blocked_on: str | None = None
        self.ticks = 0
        self.retired = False
        self.resyncs = 0

    def tick(self) -> None:
        self.ticks += 1

    def retire(self) -> None:
        self.retired = True
        self.active = False

    def resync(self) -> None:
        self.resyncs += 1

    def emit(self, status: str, blocked_on: str | None = None) -> None:
        """Simulate the file reporting a new status."""
        self._on_status(status, blocked_on)


async def _observe_native_with_fake_poller(
    tmp_path: Path,
    session_id: str,
) -> tuple[dict[str, object], list[str], list[_FakeStatusPoller], SessionResourceRegistry]:
    """Observe a claude-native terminal with an injected fake poller.

    :returns: ``(callbacks, statuses, pollers, registry)`` — the wired watcher
        callbacks, the list the status publisher appends to, the
        single-element list holding the injected poller (so the test can
        drive ``active`` / ``running_level`` / ``emit``), and the registry
        itself (so the test can post external status edges).
    """
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(session_id, {})[("claude", "main")] = instance
    statuses: list[str] = []
    pollers: list[_FakeStatusPoller] = []
    registry.set_session_status_publisher(
        lambda _sid, status, _reason=None: statuses.append(status)
    )

    def _fake_build(*, session_id: str, instance: object, on_status: object) -> _FakeStatusPoller:
        del session_id, instance
        poller = _FakeStatusPoller(on_status)
        pollers.append(poller)
        return poller

    registry._build_claude_native_status_poller = _fake_build  # type: ignore[method-assign]

    callbacks: dict[str, object] = {}

    def _capture_watcher(
        on_idle: object | None = None,
        *,
        on_activity: object | None = None,
        on_exit: object | None = None,
        on_tick: object | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del idle_threshold_s, poll_interval_s, replace
        callbacks["on_idle"] = on_idle
        callbacks["on_activity"] = on_activity
        callbacks["on_exit"] = on_exit
        callbacks["on_tick"] = on_tick

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[attr-defined]
    await registry.observe_required_terminal(
        session_id, "claude", "main", instance, resource_role=CLAUDE_NATIVE_TERMINAL_ROLE
    )
    return callbacks, statuses, pollers, registry


@pytest.mark.asyncio
async def test_claude_native_wires_status_poller_tick(tmp_path: Path) -> None:
    """The claude-native watcher is wired with an ``on_tick`` that drives
    the status-file poller."""
    callbacks, _statuses, pollers, _registry = await _observe_native_with_fake_poller(
        tmp_path, "conv_tick"
    )
    assert len(pollers) == 1
    on_tick = callbacks["on_tick"]
    assert callable(on_tick)
    on_tick()
    on_tick()
    assert pollers[0].ticks == 2


@pytest.mark.asyncio
async def test_pane_publishes_no_status_while_the_file_owns_it(tmp_path: Path) -> None:
    """An active file poller is the only status source; the pane publishes none.

    Claude redraws its prompt after a turn and blinks a cursor, so the pane
    keeps changing once the file has already said ``idle``. Letting both
    publish is what made that redraw contradict the file and needed a
    freshness window to arbitrate — so while the file is readable it decides,
    and the pane's edges are dropped.
    """
    callbacks, statuses, pollers, _registry = await _observe_native_with_fake_poller(
        tmp_path, "conv_file_owns"
    )
    poller = pollers[0]
    poller.active = True

    poller.emit("running")
    callbacks["on_activity"]()  # pane redraws mid-turn — no second edge
    await asyncio.sleep(0)
    assert statuses == ["running"]

    poller.emit("idle")
    callbacks["on_activity"]()  # post-turn prompt redraw is not a new turn
    callbacks["on_idle"]()  # nor does a quiet pane re-assert idle
    await asyncio.sleep(0)
    assert statuses == ["running", "idle"]


@pytest.mark.asyncio
async def test_parked_pane_stays_running_then_recovers_on_pane_death(tmp_path: Path) -> None:
    """A dialog keeps the session running; a dead pane still ends it.

    While Claude is parked on a prompt the pane is quiet but the turn is not
    over, and only the file knows that — so the quiet pane must not publish
    ``idle``. But a killed Claude leaves that ``waiting`` record behind, so
    pane death retires the poller and the PTY side owns the outcome. Without
    that, the session would spin forever.
    """
    callbacks, statuses, pollers, _registry = await _observe_native_with_fake_poller(
        tmp_path, "conv_parked"
    )
    poller = pollers[0]
    poller.active = True

    poller.emit("running", "permission prompt")
    callbacks["on_idle"]()  # pane quiet under the dialog — turn is NOT over
    await asyncio.sleep(0)
    assert statuses == ["running"]

    # Claude is killed at the prompt. Its file survives holding ``waiting``.
    callbacks["on_exit"]()
    assert poller.retired is True
    assert poller.active is False

    # The pane now owns status again, so the session can settle.
    callbacks["on_idle"]()
    await asyncio.sleep(0)
    assert statuses == ["running", "idle"]


@pytest.mark.asyncio
async def test_hook_status_resyncs_watcher_dedup(tmp_path: Path) -> None:
    """A forwarder's hook-derived edge rebases the shared dedup baseline.

    ``Stop`` → ``idle`` is posted to the server by the claude-native forwarder,
    bypassing this watcher. Adopting it as the baseline is what makes the pair
    idempotent: the file's own ``idle`` lands on the same edge and is collapsed,
    so the two agree regardless of which arrives first.
    """
    callbacks, statuses, pollers, registry = await _observe_native_with_fake_poller(
        tmp_path, "conv_resync"
    )
    poller = pollers[0]
    poller.active = True

    poller.emit("running")
    await asyncio.sleep(0)
    assert statuses == ["running"]

    # The forwarder posts Stop → idle straight to the server.
    registry.note_external_session_status("conv_resync", "idle")

    # The file catches up moments later with the same edge — deduped away, so
    # the user sees one idle rather than a flicker.
    poller.emit("idle")
    await asyncio.sleep(0)
    assert statuses == ["running"]

    # Next turn: the file reports work again and must publish.
    poller.emit("running")
    await asyncio.sleep(0)
    assert statuses == ["running", "running"]
    del callbacks


@pytest.mark.asyncio
async def test_reconnect_resync_republishes_a_running_session(tmp_path: Path) -> None:
    """A server restart mid-turn must not strand the session on a stale status.

    The tunnel reconnecting means the *listener* restarted and lost its status
    cache. This runner did not, so its dedup baseline still asserts ``running``
    was delivered — and Claude's file is written only when its value *changes*,
    so nothing re-asserts on its own. Without the resync the session would show
    no spinner and no stop button for the rest of the turn.
    """
    _callbacks, statuses, pollers, registry = await _observe_native_with_fake_poller(
        tmp_path, "conv_restart"
    )
    poller = pollers[0]
    poller.active = True

    poller.emit("running")
    await asyncio.sleep(0)
    assert statuses == ["running"]

    # Mid-turn, the file's value is unchanged, so a re-read publishes nothing:
    # this is exactly what leaves the restarted server on a stale status.
    poller.emit("running")
    await asyncio.sleep(0)
    assert statuses == ["running"]

    registry.resync_session_statuses()
    assert poller.resyncs == 1

    # The same value now republishes, so the fresh server learns the truth.
    poller.emit("running")
    await asyncio.sleep(0)
    assert statuses == ["running", "running"]


@pytest.mark.asyncio
async def test_reconnect_resync_keeps_the_exit_classification_memo(tmp_path: Path) -> None:
    """The resync clears published edges, not the exit memo.

    ``_last_session_status`` decides whether a terminal exit reads as a clean
    shutdown or a mid-turn crash. It tracks what the PANE last did, not what the
    server has heard, so a reconnect must leave it alone — clearing it would make
    a crash right after a reconnect look like a tidy exit.
    """
    _callbacks, _statuses, pollers, registry = await _observe_native_with_fake_poller(
        tmp_path, "conv_memo"
    )
    pollers[0].active = True
    pollers[0].emit("running")
    await asyncio.sleep(0)

    registry.resync_session_statuses()

    assert registry._take_session_status_memo("conv_memo") == "running"


@pytest.mark.asyncio
async def test_pty_edges_drive_status_when_poller_inactive(tmp_path: Path) -> None:
    """With no file (poller inactive), the PTY pane edges remain the status
    source — the fallback path for old Claude versions."""
    callbacks, statuses, pollers, _registry = await _observe_native_with_fake_poller(
        tmp_path, "conv_fallback"
    )
    # Poller stays inactive (file never resolved).
    assert pollers[0].active is False

    callbacks["on_activity"]()  # → running
    callbacks["on_idle"]()  # → idle
    # Status edges publish via loop.call_soon_threadsafe; let them drain.
    await asyncio.sleep(0)
    assert statuses == ["running", "idle"]


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


def test_trim_terminal_exit_output_drops_whole_leading_lines() -> None:
    # Over the char budget: the first surviving line must be a WHOLE line, never
    # a mid-word fragment (the "rity reasons" cut). The final line — the one that
    # matters — stays intact.
    filler = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
    text = filler + "\n--dangerously-skip-permissions cannot be run for security reasons"
    trimmed = _trim_terminal_exit_output(text)
    assert trimmed is not None
    assert len(trimmed) <= _TERMINAL_EXIT_OUTPUT_MAX_CHARS + 60  # + the omitted-lines marker
    assert trimmed.startswith("... omitted ")
    # The last line survived whole (not clipped mid-word).
    assert trimmed.endswith("for security reasons")
    # The first content line after the marker is a complete line.
    first_content = trimmed.splitlines()[1]
    assert first_content.startswith("line ")


def test_trim_terminal_exit_output_hard_clips_single_overlong_line() -> None:
    # A single line longer than the budget has no line boundary to snap to, so
    # it's clipped from the tail as a last resort.
    line = "y" * (_TERMINAL_EXIT_OUTPUT_MAX_CHARS + 500)
    trimmed = _trim_terminal_exit_output(line)
    assert trimmed is not None
    assert len(trimmed) == _TERMINAL_EXIT_OUTPUT_MAX_CHARS


def test_terminal_exit_diagnostics_reads_exit_status(tmp_path: Path) -> None:
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    instance.command = "claude"
    instance.args = ["--dangerously-skip-permissions"]
    instance._remember_pane_snapshot("boom")
    # Simulate tmux having reported a dead pane with a captured status.
    instance._remember_exit_status("1 42")
    command, args_count, _cwd, last_output, exit_status = _terminal_exit_diagnostics(instance)
    assert command == "claude"
    assert args_count == 1
    assert last_output == "boom"
    assert exit_status == 42


def test_remember_exit_status_ignores_live_pane() -> None:
    instance = make_test_terminal_instance("claude", "main", tmp_path=Path("/tmp"))
    # Live pane: pane_dead=0, empty status → nothing recorded.
    instance._remember_exit_status("0 ")
    assert instance.last_exit_status() is None


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
        on_tick: object | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del on_idle, on_activity, on_tick, idle_threshold_s, poll_interval_s, replace
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


@pytest.mark.asyncio
async def test_blocked_reason_survives_pane_redraws(tmp_path: Path) -> None:
    """The parked reason survives the pane redrawing underneath the dialog.

    Claude reports ``waitingFor`` once, when the dialog opens, and the pane
    keeps redrawing while it is up. Because the file owns status outright the
    pane publishes nothing, so there is no bare ``running`` to erase the reason
    — it stands until the file itself drops it.
    """
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    terminal_registry._by_conversation.setdefault("conv_reason", {})[("claude", "main")] = instance
    edges: list[tuple[str, str | None]] = []
    pollers: list[_FakeStatusPoller] = []
    registry.set_session_status_publisher(
        lambda _sid, status, reason=None: edges.append((status, reason))
    )

    def _fake_build(*, session_id: str, instance: object, on_status: object) -> _FakeStatusPoller:
        del session_id, instance
        poller = _FakeStatusPoller(on_status)
        pollers.append(poller)
        return poller

    registry._build_claude_native_status_poller = _fake_build  # type: ignore[method-assign]

    callbacks: dict[str, object] = {}

    def _capture_watcher(
        on_idle: object | None = None,
        *,
        on_activity: object | None = None,
        on_exit: object | None = None,
        on_tick: object | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del idle_threshold_s, poll_interval_s, replace
        callbacks["on_idle"] = on_idle
        callbacks["on_activity"] = on_activity
        callbacks["on_exit"] = on_exit
        callbacks["on_tick"] = on_tick

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[attr-defined]
    await registry.observe_required_terminal(
        "conv_reason", "claude", "main", instance, resource_role=CLAUDE_NATIVE_TERMINAL_ROLE
    )

    poller = pollers[0]
    poller.active = True
    poller.blocked_on = "permission prompt"

    poller.emit("running", "permission prompt")
    callbacks["on_activity"]()  # pane redraw under the dialog
    callbacks["on_idle"]()  # and the quiet spells between redraws
    await asyncio.sleep(0)
    assert edges == [("running", "permission prompt")]

    # Dialog answered: the file drops the reason on its own edge.
    poller.blocked_on = None
    poller.emit("running", None)
    await asyncio.sleep(0)
    assert edges == [("running", "permission prompt"), ("running", None)]


@pytest.mark.parametrize(
    "raw,expected,why",
    [
        ("conv_abc123", "conv_abc123", "an ordinary id is untouched"),
        ("a" * 32, "a" * 32, "a uuid4().hex id is untouched"),
        ("a/b", "a_b", "a POSIX separator cannot survive"),
        ("a\\b", "a_b", "a Windows separator cannot survive either"),
        ("../..", ".._..", "separators go, leaving no traversal component"),
        ("..", "__", "a bare parent reference never survives"),
        (".", "_", "a bare self reference never survives"),
        ("", "_", "an empty id still yields a usable component"),
        ("a\x00b", "a_b", "a NUL cannot reach os.path.join"),
        ("a b", "a_b", "whitespace is normalized rather than quoted downstream"),
    ],
)
def test_sanitize_session_id_yields_one_safe_component(raw: str, expected: str, why: str) -> None:
    """``_sanitize_session_id`` must return a single, non-traversing path component.

    The id reaches the filesystem as a directory name under the runner
    workspace, so anything that could act as a separator or a parent reference
    has to be neutralized here. Uses an allowlist: the previous denylist
    stopped ``/`` and ``..`` but let a backslash through.
    """
    got = _sanitize_session_id(raw)

    assert got == expected, why
    # The invariants that actually matter, restated independently of the
    # table above so a wrong `expected` cannot make this vacuous.
    assert got, "must never be empty — it becomes a path component"
    assert "/" not in got and "\\" not in got, "must be a single component"
    assert set(got) != {"."}, "must not be '.' or '..'"


def test_sanitize_session_id_keeps_traversal_out_of_the_workspace_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A hostile id cannot walk the session workspace out of the runner root.

    The unit test above pins the component; this pins the property callers
    actually depend on — that the joined path stays under the root.
    """
    monkeypatch.setenv("OMNIGENT_RUNNER_OS_ENV_ROOT", str(tmp_path))

    resolved = Path(_session_workspace("../../../../etc")).resolve()

    assert resolved.is_relative_to(tmp_path.resolve()), f"escaped the root: {resolved}"


# ── native bridge-dir reaping: live-session regression ──────────────────────


@pytest.mark.asyncio
async def test_cleanup_session_preserves_live_native_bridge_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cleanup_session must NOT delete a live session's native bridge dir.

    Bridge-dir deletion is deliberately kept OUT of cleanup_session because
    the in-place agent-switch reset (reset_session_state) reuses
    cleanup_session while the session — and its bridge — lives on. Wiring a
    reap in here would rmtree a live session's ``bridge.json`` +
    ``permission_hook.json`` and break approval routing until cold launch.
    This guard fails if any such deletion is ever wired back in.

    :param tmp_path: Pytest temp dir.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    import omnigent.claude_native_bridge as claude_bridge

    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", tmp_path / "claude-native")
    monkeypatch.setattr(claude_bridge, "_TRUSTED_PARENT", tmp_path)

    # A live session's bridge dir: current-process owner.pid + the token and
    # permission-hook files that must survive an agent-switch reset.
    bridge_dir = claude_bridge.prepare_bridge_dir("conv_live", workspace=tmp_path)
    permission_hook = bridge_dir / "permission_hook.json"
    permission_hook.write_text("{}", encoding="utf-8")
    assert (bridge_dir / "bridge.json").exists()
    assert (bridge_dir / "owner.pid").exists()

    registry = SessionResourceRegistry()
    await registry.cleanup_session("conv_live")

    assert bridge_dir.exists()
    assert (bridge_dir / "bridge.json").exists()
    assert permission_hook.exists()
