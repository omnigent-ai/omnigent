"""Tests for native session model, effort, compact, and plan-mode events."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent import (
    claude_native_bridge,
    cursor_native_bridge,
    kiro_native_bridge,
    qwen_native_bridge,
)
from omnigent.claude_native_bridge import (
    bridge_dir_for_bridge_id,
    bridge_dir_for_conversation_id,
)
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import TerminalRegistry
from tests.runner.conftest import (
    _drain_session_event_queue,
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)
from tests.runner.helpers import NullServerClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "effort_value",
    # ``EFFORT_VALUES`` is a superset of ``CLAUDE_EFFORTS``:
    # PATCH accepts {none, minimal, low, medium, high, xhigh, max}
    # but Claude Code's ``/effort`` slash only accepts the last five.
    # ``none`` and ``minimal`` must skip injection (typing ``/effort
    # none`` would land as a TUI error). ``None`` (clear) must skip
    # too — Claude has no slash form for "use spawn default".
    ["none", "minimal", None],
)
async def test_events_effort_change_on_native_session_skips_inject_for_unsupported_level(
    monkeypatch: pytest.MonkeyPatch,
    effort_value: str | None,
) -> None:
    """
    Unsupported / null effort values 204 without typing into tmux.

    Omnigent server is harness-agnostic — it always forwards the new
    persisted effort to ``/events``. The runner's native handler
    owns the level-validation, skipping injection when the value
    isn't in Claude's accepted set. Persistence already happened on
    the Omnigent side; the next spawn picks up the value via ``--effort``.

    Pins that the validation lives in the runner (where the
    harness-specific knowledge belongs), not in the Omnigent server.
    """
    from omnigent.spec.types import ExecutorSpec

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
    ) -> None:
        """Fail the test if the runner reaches inject for an unsupported level."""
        del bridge_dir, command, timeout_s
        raise AssertionError(
            f"inject_slash_command must not be called for effort={effort_value!r}; "
            f"the native handler should skip unsupported / null levels."
        )

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "1c88519bd9daa4e9bc2df649fe4685fc",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/1c88519bd9daa4e9bc2df649fe4685fc/events",
            json={"type": "effort_change", "effort": effort_value},
        )

    # 204 = the handler ran and decided to skip. 502 would mean it
    # fell through to the harness-forward path. The fake inject above
    # asserts loudly if injection was attempted — silence here proves
    # the skip took effect.
    assert resp.status_code == 204, (
        f"Native effort_change with unsupported / null level must "
        f"return 204 (no-op); got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_events_effort_change_on_native_session_returns_503_when_bridge_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Bridge-not-ready RuntimeError surfaces as 503 from /events.

    Sister to the happy-path test. Pins that the failure mode of the
    native effort dispatch (tmux pane gone / bridge dir not yet
    advertised) returns 503 with the same error code shape the
    legacy route returns. Omnigent server's PATCH swallows this 503 and
    still returns 200 with the persisted value — the next spawn
    will apply the new effort via ``--effort``.
    """
    from omnigent.spec.types import ExecutorSpec

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
    ) -> None:
        """Simulate the bridge-not-ready path."""
        del bridge_dir, command, timeout_s
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "876bea5691e426b42ecc3cc2c02cbf92",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/876bea5691e426b42ecc3cc2c02cbf92/events",
            json={"type": "effort_change", "effort": "high"},
        )

    assert resp.status_code == 503, (
        f"Native effort_change with inject failure must return 503; "
        f"got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    # ``claude_native_effort_failed`` is the same error code the
    # legacy route uses — keeps the failure shape stable for callers.
    assert body.get("error") == "claude_native_effort_failed", (
        f"503 body must carry the bridge-failure error code; got {body!r}"
    )


@pytest.mark.asyncio
async def test_events_effort_change_on_non_native_session_is_204_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Non-native sessions accept effort_change and 204 without side effects.

    In-process harnesses (default / claude-sdk / openai-agents / codex)
    re-read the persisted ``reasoning_effort`` from store on each
    turn, so they need no runtime notification when it changes. The
    Omnigent server still POSTs ``effort_change`` to ``/events`` for every
    PATCH (it's harness-agnostic), so the runner must accept the
    event and 204 — never reach the slash-command injector, never
    forward to the harness scaffold.
    """
    from omnigent.spec.types import ExecutorSpec

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
    ) -> None:
        """Fail the test if a non-native session reaches the injector."""
        del bridge_dir, command, timeout_s
        raise AssertionError(
            "inject_slash_command must never be called for non-native "
            "sessions — effort_change is a no-op for in-process harnesses."
        )

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    # Default harness (in-process LLM loop), NOT claude-native.
    default_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the default spec for any agent_id."""
        del agent_id
        return default_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "df1b63ea1861c5d7765dd4541f60d99a",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/df1b63ea1861c5d7765dd4541f60d99a/events",
            json={"type": "effort_change", "effort": "high"},
        )

    # 204 = the dispatch saw a non-native harness and returned the
    # no-op short-circuit before any forward / inject. Anything else
    # (200/202/4xx/5xx) would mean the event leaked into a code path
    # it shouldn't reach.
    assert resp.status_code == 204, (
        f"Non-native effort_change must return 204 no-op; got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_events_compact_on_native_session_types_slash_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    POST ``/events`` with ``{"type":"compact"}`` on a claude-native
    session injects ``/compact`` into tmux and returns 200.

    Explicit compaction on a claude-native session must run inside
    Claude Code (it owns its own context window in the terminal); the
    Omnigent server's own compaction would only summarise the transcript
    mirror. The runner's ``/events`` dispatch recognises the native
    harness and routes to ``_handle_claude_native_compact``, which
    types the slash command into the pane.

    The 200 (not 204) is load-bearing: the Omnigent server reads it to know
    the control was handled in the terminal and skips its own
    in-process compaction. A regression returning 204 here would make
    the Omnigent server fall through to ``_run_compact_locked``, which 400s
    on the LLM-less claude-native pseudo-agent — the original bug.
    """
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.spec.types import ExecutorSpec

    captured: list[Any] = []

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
    ) -> None:
        """Record the call (including auto_confirm) without touching tmux."""
        captured.append((bridge_dir, command, timeout_s, auto_confirm))

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "f70a14aaa23f51a5c4d915b9a29b0cd3",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        # Drain creation-time events (claude-native auto-create enqueues
        # session.terminal_pending) so the drain below isolates only
        # what /compact emits.
        _drain_session_event_queue(
            _session_event_queues_ref.get("f70a14aaa23f51a5c4d915b9a29b0cd3")
        )

        resp = await client.post(
            "/v1/sessions/f70a14aaa23f51a5c4d915b9a29b0cd3/events",
            json={"type": "compact"},
        )

        # Drain the event queue: /compact is a control signal and must
        # not enqueue session.status events.
        queue = _session_event_queues_ref.get("f70a14aaa23f51a5c4d915b9a29b0cd3")
        queued_events: list[dict[str, Any]] = []
        if queue is not None:
            while not queue.empty():
                item = queue.get_nowait()
                if isinstance(item, dict):
                    queued_events.append(item)

    # 200 = native dispatch routed to the compact handler and it
    # injected successfully. 204 would mean the handler returned the
    # in-process no-op (wrong harness branch) → Omnigent falls through to
    # _run_compact_locked and 400s. 404 = the dispatch fell through to
    # the generic harness-forward.
    assert resp.status_code == 200, (
        f"Native compact must return 200 from /events; got {resp.status_code}: {resp.text}"
    )
    # Exactly one inject call. 0 = native dispatch missed; 2+ = handler ran twice.
    assert len(captured) == 1, (
        f"Expected one inject_slash_command call from native compact, got {len(captured)}."
    )
    bridge_dir, command, timeout_s, auto_confirm = captured[0]
    assert bridge_dir == bridge_dir_for_conversation_id("f70a14aaa23f51a5c4d915b9a29b0cd3")
    # Body contract: the literal ``/compact`` is what Claude Code's TUI
    # accepts. A shape regression (``compact``, missing slash) would
    # land as plain prompt text instead of running compaction.
    assert command == "/compact", f"Expected '/compact' literal, got {command!r}."
    # 1.0s short timeout: missing tmux.json means the pane isn't
    # attached, so there's no live Claude to compact.
    assert timeout_s == 1.0
    # auto_confirm must be False — unlike /effort and /model, /compact
    # does not pop a confirmation dialog, so an extra Enter would land
    # on the prompt and submit a stray empty turn.
    assert auto_confirm is False, (
        f"compact must not auto-confirm; got auto_confirm={auto_confirm!r}."
    )
    # /compact is a control signal, not a state change.
    assert queued_events == [], f"compact must not publish session events; got {queued_events!r}."


@pytest.mark.asyncio
async def test_events_compact_on_native_session_returns_503_when_bridge_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Bridge-not-ready RuntimeError surfaces as 503 from /events.

    Sister to the happy-path test. When the tmux pane isn't attached
    there is no live Claude to compact, so the native compact handler
    returns 503 with the ``claude_native_compact_failed`` code. The AP
    server treats a non-200/204 runner response as an error rather
    than silently running its own (wrong) compaction.
    """
    from omnigent.spec.types import ExecutorSpec

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
    ) -> None:
        """Simulate the bridge-not-ready path."""
        del bridge_dir, command, timeout_s, auto_confirm
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "22126529b89836ff13480ca578a6dcf5",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/22126529b89836ff13480ca578a6dcf5/events",
            json={"type": "compact"},
        )

    assert resp.status_code == 503, (
        f"Native compact with inject failure must return 503; got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("error") == "claude_native_compact_failed", (
        f"503 body must carry the bridge-failure error code; got {body!r}"
    )


@pytest.mark.asyncio
async def test_events_compact_on_codex_native_injects_slash_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    POST ``/events`` with ``{"type":"compact"}`` on a codex-native
    session injects ``/compact`` into the codex tmux pane and returns 200.

    Codex owns its own context window in the terminal, so explicit
    compaction must run inside Codex — the same rationale as the
    claude-native path.  The pane coordinates come from the resource
    registry (not a ``tmux.json`` sidecar).  The 200 return is
    load-bearing: the Omnigent server reads it to skip its own
    AP-side compaction.
    """
    from omnigent.runner.app import _session_event_queues_ref
    from tests.runner.helpers import make_test_terminal_instance

    captured: list[tuple[str, list[str]]] = []

    def _fake_run_tmux(socket_path: str, *args: str) -> None:
        """Record tmux send-keys calls without touching tmux."""
        captured.append((socket_path, list(args)))

    monkeypatch.setattr(claude_native_bridge, "_run_tmux", _fake_run_tmux)

    codex_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the codex-native spec for any agent_id."""
        del agent_id, session_id
        return codex_native_spec

    conv_id = "9864122f95f2f013c9599f4014725784"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("codex", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("codex", "main")] = instance

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        _drain_session_event_queue(_session_event_queues_ref.get(conv_id))

        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "compact"},
        )

        # Drain the event queue: /compact is a control signal and must
        # not enqueue session.status events.
        queue = _session_event_queues_ref.get(conv_id)
        queued_events: list[dict[str, Any]] = []
        if queue is not None:
            while not queue.empty():
                item = queue.get_nowait()
                if isinstance(item, dict):
                    queued_events.append(item)

    # 200 = codex-native dispatch routed to the compact handler and it
    # injected successfully.
    assert resp.status_code == 200, (
        f"Codex-native compact must return 200 from /events; got {resp.status_code}: {resp.text}"
    )

    # Exactly 3 tmux send-keys calls: C-u, -l /compact, Enter.
    assert len(captured) == 3, (
        f"Expected 3 tmux send-keys calls (C-u, /compact, Enter), got {len(captured)}."
    )
    socket = str(instance.socket_path)
    # 1. Clear draft: C-u
    assert captured[0] == (socket, ["send-keys", "-t", "main", "C-u"]), (
        f"First call must clear draft with C-u; got {captured[0]!r}."
    )
    # 2. Type /compact literally
    assert captured[1] == (socket, ["send-keys", "-l", "-t", "main", "/compact"]), (
        f"Second call must type /compact literally; got {captured[1]!r}."
    )
    # 3. Submit with Enter
    assert captured[2] == (socket, ["send-keys", "-t", "main", "Enter"]), (
        f"Third call must submit with Enter; got {captured[2]!r}."
    )
    # /compact is a control signal, not a state change.
    assert queued_events == [], f"compact must not publish session events; got {queued_events!r}."


@pytest.mark.asyncio
async def test_events_compact_on_codex_native_returns_204_when_no_terminal() -> None:
    """
    Codex-native compact returns 204 when no live terminal is registered.

    Without a running codex terminal the ``/compact`` slash command
    has nowhere to go.  204 tells the Omnigent server to fall back to
    its own AP-side compaction (or skip it).
    """
    codex_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the codex-native spec for any agent_id."""
        del agent_id, session_id
        return codex_native_spec

    conv_id = "4be2f8fe2204fade6a89dafade0a0fd2"
    # Empty registry — no codex terminal registered.
    terminal_registry = TerminalRegistry()

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "compact"},
        )

    assert resp.status_code == 204, (
        f"Codex-native compact with no terminal must return 204; "
        f"got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_events_compact_on_codex_native_returns_503_on_tmux_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Codex-native compact returns 503 when the tmux send-keys call fails.

    The 503 tells the Omnigent server the control was NOT handled, so it
    can surface an error rather than silently running its own (wrong)
    compaction.
    """
    from tests.runner.helpers import make_test_terminal_instance

    def _failing_run_tmux(socket_path: str, *args: str) -> None:
        """Simulate a tmux pane that is no longer alive."""
        del socket_path, args
        raise RuntimeError("no server running on /tmp/dead.sock")

    monkeypatch.setattr(claude_native_bridge, "_run_tmux", _failing_run_tmux)

    codex_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the codex-native spec for any agent_id."""
        del agent_id, session_id
        return codex_native_spec

    conv_id = "e5d09a0ff8458b2d6abb7f0c7deda0d3"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("codex", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("codex", "main")] = instance

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "compact"},
        )

    assert resp.status_code == 503, (
        f"Codex-native compact with tmux failure must return 503; "
        f"got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("error") == "codex_native_compact_failed", (
        f"503 body must carry the codex bridge-failure error code; got {body!r}"
    )


@pytest.mark.asyncio
async def test_events_compact_on_cursor_native_pastes_summarize_and_raises_spinner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    POST ``/events`` with ``{"type":"compact"}`` on a cursor-native
    session submits ``/summarize`` via bracketed paste, returns 200, and
    raises the "Compacting…" spinner — but does NOT complete it.

    cursor-agent manages its own context window in the TUI, so explicit
    compaction must run there (its built-in ``/summarize`` command) rather
    than as AP-side compaction — the same rationale as the claude-native
    path.  The 200 (not 204) is load-bearing: the Omnigent server reads it to
    skip its own ``_run_compact_locked`` (which 400s on the LLM-less native
    pseudo-agent).

    Two properties are pinned here:

    1. **The command must go through the bracketed-paste path**
       (``inject_user_message``), NOT a ``send-keys``-typed slash command.
       Typing the literal ``/summarize`` opens cursor-agent's slash-command
       autocomplete dropdown, and the single submit Enter then confirms the
       highlighted completion instead of submitting the command — so the
       command was never sent (the original bug, seen as ``/summarize`` left
       sitting in the input box).
    2. **The handler raises the spinner but must NOT complete it.** It publishes
       ``response.compaction.in_progress`` (→ "Compacting conversation…") only.
       cursor-agent runs the summarization asynchronously in the pane after the
       submit, so completing here would flash "Conversation compacted" while
       the TUI is still summarizing.  The ``completed`` edge is emitted later by
       the cursor forwarder when it observes the summary blob (covered by
       ``tests/test_cursor_native_forwarder.py``).
    """
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.spec.types import ExecutorSpec

    monkeypatch.setattr(cursor_native_bridge, "_BRIDGE_ROOT", tmp_path / "cursor-bridge")

    captured: list[tuple[Any, str, float]] = []

    def _fake_inject(bridge_dir: Any, *, content: str, timeout_s: float) -> None:
        """Record the bracketed-paste call without touching tmux."""
        captured.append((bridge_dir, content, timeout_s))

    monkeypatch.setattr(cursor_native_bridge, "inject_user_message", _fake_inject)

    cursor_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "cursor-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the cursor-native spec for any agent_id."""
        del agent_id, session_id
        return cursor_native_spec

    conv_id = "764ebbade28dd774a5d673378c034933"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        # Drain creation-time events so the drain below isolates only what
        # /compact emits.
        _drain_session_event_queue(_session_event_queues_ref.get(conv_id))

        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "compact"},
        )

        queued_events = _drain_session_event_queue(_session_event_queues_ref.get(conv_id))

    # 200 = cursor-native dispatch routed to the compact handler and the paste
    # succeeded. 204 would mean the dispatch fell through to the in-process
    # no-op branch (the original gap) → Omnigent runs its own compaction and 400s.
    assert resp.status_code == 200, (
        f"Cursor-native compact must return 200 from /events; got {resp.status_code}: {resp.text}"
    )
    # Exactly one paste call. 0 = dispatch missed the cursor branch.
    assert len(captured) == 1, (
        f"Expected one inject_user_message call from cursor compact, got {len(captured)}."
    )
    bridge_dir, content, timeout_s = captured[0]
    assert bridge_dir == cursor_native_bridge.bridge_dir_for_session_id(conv_id)
    # The literal ``/summarize`` is cursor-agent's compaction command. It is
    # delivered as *paste content*, not a typed slash command, so the
    # autocomplete dropdown never opens and the submit Enter sends the command.
    assert content == "/summarize", f"Expected '/summarize' paste content, got {content!r}."
    # 1.0s short timeout: a missing tmux target means the pane isn't attached,
    # so there is no live cursor TUI to compact.
    assert timeout_s == 1.0

    # The handler raises the spinner (in_progress) but must NOT complete it —
    # completion is the forwarder's job once the summary blob actually lands.
    # A regression re-adding ``completed`` here would flash the permanent
    # "Conversation compacted" marker while the TUI is still summarizing.
    compaction_types = [
        e.get("type")
        for e in queued_events
        if str(e.get("type", "")).startswith("response.compaction")
    ]
    assert compaction_types == ["response.compaction.in_progress"], (
        f"Handler must publish only in_progress (forwarder completes it); "
        f"got {compaction_types!r}."
    )
    in_progress = next(
        e for e in queued_events if e.get("type") == "response.compaction.in_progress"
    )
    assert in_progress.get("task_id") == conv_id, (
        f"in_progress must carry the session id as task_id; got {in_progress!r}."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inject_exc", "label"),
    [
        # Pane not attached: _wait_for_tmux_info / _run_tmux raise RuntimeError.
        (RuntimeError("tmux target is not advertised"), "runtime"),
        # Filesystem fault writing the paste tempfile into bridge_dir (disk
        # full, perms, dir removed). cursor's inject_user_message has this
        # surface; the claude-native analog does not. A narrow
        # ``except (RuntimeError, ValueError)`` would let this escape AFTER
        # in_progress fired, stranding the spinner with no failed edge.
        (OSError("No space left on device"), "oserror"),
    ],
)
async def test_events_compact_on_cursor_native_503_dismisses_spinner_on_inject_failure(
    inject_exc: Exception,
    label: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An injection failure surfaces as 503 AND dismisses the spinner.

    The handler publishes ``response.compaction.in_progress`` before injecting,
    so every failure path must publish ``response.compaction.failed`` to
    dismiss the "Compacting…" spinner — otherwise it is stranded forever — and
    must NOT publish ``completed`` (the history was never compacted). Covers
    both the tmux ``RuntimeError`` and the tempfile ``OSError`` surfaces; the
    latter is unique to cursor's bracketed-paste path.
    """
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.spec.types import ExecutorSpec

    monkeypatch.setattr(cursor_native_bridge, "_BRIDGE_ROOT", tmp_path / "cursor-bridge")

    def _fake_inject(bridge_dir: Any, *, content: str, timeout_s: float) -> None:
        """Simulate an injection failure (tmux down, or tempfile write fault)."""
        del bridge_dir, content, timeout_s
        raise inject_exc

    monkeypatch.setattr(cursor_native_bridge, "inject_user_message", _fake_inject)

    cursor_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "cursor-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the cursor-native spec for any agent_id."""
        del agent_id, session_id
        return cursor_native_spec

    conv_id = uuid.uuid4().hex
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        _drain_session_event_queue(_session_event_queues_ref.get(conv_id))

        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "compact"},
        )

        queued_events = _drain_session_event_queue(_session_event_queues_ref.get(conv_id))

    assert resp.status_code == 503, (
        f"Cursor-native compact with no live pane must return 503; "
        f"got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("error") == "cursor_native_compact_failed", (
        f"503 body must carry the cursor bridge-failure error code; got {body!r}"
    )

    compaction_types = [
        e.get("type")
        for e in queued_events
        if str(e.get("type", "")).startswith("response.compaction")
    ]
    # in_progress raised the spinner; failed must dismiss it. completed must
    # never fire — the history was not compacted.
    assert compaction_types == [
        "response.compaction.in_progress",
        "response.compaction.failed",
    ], f"Expected in_progress then failed (no completed); got {compaction_types!r}."


@pytest.mark.asyncio
async def test_events_compact_on_pi_native_enqueues_compact_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    POST ``/events`` with ``{"type":"compact"}`` on a pi-native session
    queues a ``compact`` payload to the Pi extension inbox and returns 200.

    Pi owns its context window inside the resident Pi TUI process, so explicit
    compaction must run there (the Omnigent server's AP-side compaction would
    only summarise the transcript mirror and desync the two, and 400s on the
    LLM-less pi-native pseudo-agent). The runner's ``compact`` dispatch routes
    to ``_handle_pi_native_compact``, which drops a ``compact`` payload into the
    bridge inbox; the resident extension consumes it and calls Pi's
    ``ExtensionContext.compact()``.

    Regression guard: the dispatch originally enumerated only claude/codex/
    cursor-native, so pi-native fell through to the 204 no-op.

    Pins:
    1. 200 returned (not 204) so the Omnigent server skips its own AP-side
       compaction.
    2. A ``compact_*`` payload is written to the session's bridge inbox.
    3. /compact is a control signal and publishes no ``session.status`` events.
    """
    import omnigent.pi_native_bridge as pi_native_bridge
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.spec.types import ExecutorSpec

    conv_id = "03f435963d78fe4ea313325f729eefc5"
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-bridge")

    pi_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "pi-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the pi-native spec for any agent_id."""
        del agent_id, session_id
        return pi_native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        # Seeds _session_spec_cache so the dispatch detects "pi-native".
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        # Drain creation-time events (pi-native auto-create enqueues
        # session.terminal_pending and, with no real Pi terminal in the test,
        # a session.status failure) so the drain below isolates only what
        # /compact emits.
        _drain_session_event_queue(_session_event_queues_ref.get(conv_id))

        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "compact"},
        )

        # /compact is a control signal; it must not enqueue session.status events.
        queue = _session_event_queues_ref.get(conv_id)
        queued_events: list[dict[str, Any]] = []
        if queue is not None:
            while not queue.empty():
                item = queue.get_nowait()
                if isinstance(item, dict):
                    queued_events.append(item)

    # 1) 200 means pi-native owns its context, so the control was handled in the
    # terminal and the server must skip its own compaction. 204 would mean the
    # dispatch fell through to the no-op (the original bug).
    assert resp.status_code == 200, (
        f"pi-native compact must return 200; got {resp.status_code}: {resp.text}"
    )

    # 2) The compact request reached the bridge inbox (the extension's
    # compaction channel). If empty, the dispatch fell through to the no-op.
    inbox = pi_native_bridge.bridge_dir_for_session_id(conv_id) / "inbox"
    queued = sorted(p.name for p in inbox.glob("*.json")) if inbox.exists() else []
    assert any("compact_" in name for name in queued), (
        f"pi-native compact must enqueue a compact payload to the bridge inbox; "
        f"inbox contained {queued!r}."
    )

    # 3) No session.status events; /compact is a control signal, not a state change.
    assert queued_events == [], (
        f"pi-native compact must not publish session events; got {queued_events!r}."
    )


@pytest.mark.asyncio
async def test_events_compact_on_pi_native_returns_503_when_inbox_unwritable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Pi-native compact returns 503 when the bridge inbox cannot be written.

    Sister to the happy-path test. If the inbox enqueue raises OSError (e.g. a
    filesystem fault), the handler surfaces 503 with the
    ``pi_native_compact_failed`` code rather than silently swallowing the
    request; the Omnigent server then treats it as not-handled.
    """
    import omnigent.pi_native_bridge as pi_native_bridge
    from omnigent.spec.types import ExecutorSpec

    conv_id = "9c52b3dbe1d543718c1678a256017326"
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-bridge")

    def _boom(*_args: Any, **_kwargs: Any) -> str:
        """Simulate an unwritable inbox."""
        raise OSError("inbox is read-only")

    monkeypatch.setattr(pi_native_bridge, "enqueue_compact", _boom)

    pi_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "pi-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the pi-native spec for any agent_id."""
        del agent_id, session_id
        return pi_native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "compact"},
        )

    assert resp.status_code == 503, (
        f"pi-native compact with unwritable inbox must return 503; "
        f"got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("error") == "pi_native_compact_failed", (
        f"503 body must carry the pi bridge-failure error code; got {body!r}"
    )


@pytest.mark.asyncio
async def test_events_compact_on_qwen_native_submits_compress_and_raises_spinner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    POST ``/events`` ``{"type":"compact"}`` on a qwen-native session submits
    ``/compress`` via the input file, returns 200, and raises the spinner only.

    qwen owns its context window inside the TUI, so compaction runs there
    (``/compress``), not as AP-side compaction; same rationale as cursor-native.
    Unlike cursor, injection is file-based: a ``submit`` line routes through
    qwen's ``RemoteInputWatcher`` then ``submitQuery``, which processes the slash
    command (no autocomplete-dropdown trap). The 200 is load-bearing (server
    skips its own ``_run_compact_locked``). The handler publishes only
    ``in_progress``; the ``completed`` edge is the compaction mirror's job once
    the ``chat_compression`` record lands (covered in test_qwen_native_forwarder).
    """
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.spec.types import ExecutorSpec

    captured: list[tuple[Any, str]] = []

    def _fake_submit(bridge_dir: Any, *, content: str) -> None:
        """Record the input-file submit without touching disk."""
        captured.append((bridge_dir, content))

    monkeypatch.setattr(qwen_native_bridge, "submit_user_message", _fake_submit)

    qwen_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "qwen-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return qwen_native_spec

    conv_id = "233c1fedaaeb18c91267904e79b7d10c"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        _drain_session_event_queue(_session_event_queues_ref.get(conv_id))

        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "compact"},
        )

        queued_events = _drain_session_event_queue(_session_event_queues_ref.get(conv_id))

    assert resp.status_code == 200, (
        f"qwen-native compact must return 200 from /events; got {resp.status_code}: {resp.text}"
    )
    assert len(captured) == 1, (
        f"Expected one submit_user_message call from qwen compact, got {len(captured)}."
    )
    bridge_dir, content = captured[0]
    assert bridge_dir == qwen_native_bridge.bridge_dir_for_session_id(conv_id)
    assert content == "/compress", f"Expected '/compress' submit content, got {content!r}."

    compaction_types = [
        e.get("type")
        for e in queued_events
        if str(e.get("type", "")).startswith("response.compaction")
    ]
    assert compaction_types == ["response.compaction.in_progress"], (
        f"Handler must publish only in_progress (mirror completes it); got {compaction_types!r}."
    )
    in_progress = next(
        e for e in queued_events if e.get("type") == "response.compaction.in_progress"
    )
    assert in_progress.get("task_id") == conv_id


@pytest.mark.asyncio
async def test_events_compact_on_qwen_native_503_dismisses_spinner_on_submit_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A submit failure surfaces as 503 AND dismisses the spinner (in_progress->failed)."""
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.spec.types import ExecutorSpec

    def _fake_submit(bridge_dir: Any, *, content: str) -> None:
        del bridge_dir, content
        raise RuntimeError("input file unwritable")

    monkeypatch.setattr(qwen_native_bridge, "submit_user_message", _fake_submit)

    qwen_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "qwen-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return qwen_native_spec

    conv_id = "7c94e9a0306b300d81233cccef543a84"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        _drain_session_event_queue(_session_event_queues_ref.get(conv_id))

        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "compact"},
        )

        queued_events = _drain_session_event_queue(_session_event_queues_ref.get(conv_id))

    assert resp.status_code == 503, f"got {resp.status_code}: {resp.text}"
    assert resp.json().get("error") == "qwen_native_compact_failed"
    compaction_types = [
        e.get("type")
        for e in queued_events
        if str(e.get("type", "")).startswith("response.compaction")
    ]
    assert compaction_types == [
        "response.compaction.in_progress",
        "response.compaction.failed",
    ], f"Expected in_progress then failed (no completed); got {compaction_types!r}."


class _FakeOpenCodeCompactClient:
    """OpenCode client stub recording ``summarize`` calls for compact tests.

    Stands in for :class:`omnigent.opencode_native_client.OpenCodeClient` so
    the opencode-native compact handler's model-resolution + ``/summarize``
    call is observable without a live ``opencode serve``.
    """

    def __init__(
        self,
        *,
        session: Any,
        messages: list[dict[str, Any]],
        summarize_error: BaseException | None = None,
    ) -> None:
        """
        Initialize with the session/messages the handler will resolve from.

        :param session: The :class:`OpenCodeSession` ``get_session`` returns
            (or ``None``).
        :param messages: The list ``list_messages`` returns.
        :param summarize_error: When set, ``summarize`` raises it instead of
            recording the call (drives the 503 path).
        :returns: None.
        """
        self._session = session
        self._messages = messages
        self._summarize_error = summarize_error
        self.summarize_calls: list[tuple[str, str, str]] = []
        self.closed = False

    async def get_session(self, session_id: str) -> Any:
        """Return the scripted session."""
        del session_id
        return self._session

    async def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Return the scripted messages."""
        del session_id
        return self._messages

    async def summarize(self, session_id: str, *, provider_id: str, model_id: str) -> bool:
        """Record the compaction call (or raise the scripted error)."""
        if self._summarize_error is not None:
            raise self._summarize_error
        self.summarize_calls.append((session_id, provider_id, model_id))
        return True

    async def aclose(self) -> None:
        """Mark the client closed (the handler always closes in ``finally``)."""
        self.closed = True


class _FakeOpenCodeCompactServer:
    """``OpenCodeNativeServer`` stub whose ``client()`` returns a fixed stub."""

    def __init__(self, client: _FakeOpenCodeCompactClient) -> None:
        """Wrap *client* so :meth:`client` returns it."""
        self._client = client

    def client(self, *, directory: str | None = None) -> _FakeOpenCodeCompactClient:
        """Return the fixed compact client."""
        del directory
        return self._client


async def _drive_opencode_native_compact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    conv_id: str,
    session_payload: dict[str, Any] | None,
    messages: list[dict[str, Any]],
    model_override: str | None,
    summarize_error: BaseException | None = None,
) -> tuple[httpx.Response, _FakeOpenCodeCompactClient]:
    """
    Build an opencode-native runner app and POST a ``compact`` control event.

    Registers a pre-built opencode terminal so session creation skips the real
    ``_auto_create_opencode_terminal`` launch, injects a live fake server into
    ``_AUTO_OPENCODE_SERVERS``, and stubs ``read_bridge_state`` so the handler
    resolves an ``opencode_session_id`` (+ optional ``model_override``).

    :param conv_id: Conversation id to create and compact.
    :param session_payload: Payload for the :class:`OpenCodeSession`
        ``get_session`` returns, or ``None`` for no session.
    :param messages: Messages ``list_messages`` returns.
    :param model_override: Bridge-state ``model_override`` (qualified
        ``provider/model``), or ``None``.
    :param summarize_error: When set, ``summarize`` raises it (503 path).
    :returns: ``(response, fake_client)`` for the compact POST.
    """
    from omnigent import opencode_native_bridge
    from omnigent.opencode_native_bridge import OpenCodeNativeBridgeState
    from omnigent.opencode_native_client import OpenCodeSession
    from omnigent.runner.app import _AUTO_OPENCODE_SERVERS, _session_event_queues_ref
    from omnigent.spec.types import ExecutorSpec
    from tests.runner.helpers import make_test_terminal_instance

    opencode_native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the opencode-native spec for any agent_id."""
        del agent_id, session_id
        return opencode_native_spec

    # A pre-registered opencode terminal makes the create path's auto-launch a
    # no-op (the per-session ensure-lock sees a live terminal and skips it).
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("opencode", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("opencode", "main")] = instance

    session = (
        OpenCodeSession.from_payload(session_payload) if session_payload is not None else None
    )
    client = _FakeOpenCodeCompactClient(
        session=session, messages=messages, summarize_error=summarize_error
    )
    server = _FakeOpenCodeCompactServer(client)

    state = OpenCodeNativeBridgeState(
        session_id=conv_id,
        server_base_url="http://127.0.0.1:1",
        opencode_session_id="ses_x",
        model_override=model_override,
    )
    monkeypatch.setattr(opencode_native_bridge, "read_bridge_state", lambda _dir: state)

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )

    try:
        async with _runner_client(app) as http_client:
            create_resp = await http_client.post(
                "/v1/sessions",
                json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
            )
            assert create_resp.status_code == 201, create_resp.text
            _drain_session_event_queue(_session_event_queues_ref.get(conv_id))
            # Inject AFTER create so the create flow's cleanup cannot evict it.
            _AUTO_OPENCODE_SERVERS[conv_id] = server
            resp = await http_client.post(
                f"/v1/sessions/{conv_id}/events",
                json={"type": "compact"},
            )
        return resp, client
    finally:
        _AUTO_OPENCODE_SERVERS.pop(conv_id, None)
        _session_event_queues_ref.pop(conv_id, None)


def test_resolve_opencode_compact_model_prefers_latest_assistant_message() -> None:
    """
    The latest assistant message's live model wins over session/override.

    On a MESSAGE the model keys are ``providerID`` + ``modelID``. The chain
    must iterate in reverse and ignore user-role messages, picking the live
    model even when a session ``model`` and a ``model_override`` also resolve.
    """
    from omnigent.opencode_native_client import OpenCodeSession
    from omnigent.runner.app import _resolve_opencode_compact_model

    session = OpenCodeSession.from_payload(
        {"id": "ses_x", "model": {"providerID": "stale", "id": "stale-model"}}
    )
    messages = [
        {"info": {"role": "user"}, "parts": []},
        {"info": {"role": "assistant", "providerID": "openai", "modelID": "gpt-old"}, "parts": []},
        {"info": {"role": "user"}, "parts": []},
        {
            "info": {
                "role": "assistant",
                "providerID": "anthropic",
                "modelID": "claude-sonnet-4-5",
            },
            "parts": [],
        },
    ]

    provider_id, model_id = _resolve_opencode_compact_model(
        session, messages, "override-prov/override-model"
    )

    assert (provider_id, model_id) == ("anthropic", "claude-sonnet-4-5")


def test_resolve_opencode_compact_model_falls_back_to_session_model() -> None:
    """
    With no usable assistant message, the session ``model`` field resolves.

    On the SESSION object the keys are ``providerID`` + ``id`` (NOT
    ``modelID``). An assistant message missing ``modelID`` must be skipped so
    the session field is used.
    """
    from omnigent.opencode_native_client import OpenCodeSession
    from omnigent.runner.app import _resolve_opencode_compact_model

    session = OpenCodeSession.from_payload(
        {"id": "ses_x", "model": {"providerID": "anthropic", "id": "claude-opus-4"}}
    )
    # Assistant message without a modelID is not usable → fall through.
    messages = [{"info": {"role": "assistant", "providerID": "anthropic"}, "parts": []}]

    provider_id, model_id = _resolve_opencode_compact_model(session, messages, None)

    assert (provider_id, model_id) == ("anthropic", "claude-opus-4")


def test_resolve_opencode_compact_model_falls_back_to_model_override() -> None:
    """
    With no message/session model, ``model_override`` splits on the first ``/``.

    A model id may itself contain ``/`` (e.g. an OpenRouter slug), so only the
    FIRST separator delimits provider from model.
    """
    from omnigent.opencode_native_client import OpenCodeSession
    from omnigent.runner.app import _resolve_opencode_compact_model

    session = OpenCodeSession.from_payload({"id": "ses_x"})

    provider_id, model_id = _resolve_opencode_compact_model(
        session, [], "openrouter/anthropic/claude-3.5"
    )

    assert (provider_id, model_id) == ("openrouter", "anthropic/claude-3.5")


def test_resolve_opencode_compact_model_returns_none_when_unresolvable() -> None:
    """
    Nothing resolvable → ``(None, None)`` so the handler 204s to AP-side.

    Covers the live Omnigent flow: the session is created without a model and
    has no assistant turn yet, and no override is set.
    """
    from omnigent.opencode_native_client import OpenCodeSession
    from omnigent.runner.app import _resolve_opencode_compact_model

    session = OpenCodeSession.from_payload({"id": "ses_x"})

    assert _resolve_opencode_compact_model(session, [], None) == (None, None)
    # A bare token without ``/`` is not a qualified override.
    assert _resolve_opencode_compact_model(None, [], "not-qualified") == (None, None)


@pytest.mark.asyncio
async def test_events_compact_on_opencode_native_summarizes_from_assistant_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    opencode-native compact resolves the live model and calls ``/summarize``.

    The model comes from the latest assistant message (``providerID`` +
    ``modelID``) because Omnigent creates the session without a model. A 200
    return is load-bearing: the Omnigent server reads it to skip its AP-side
    compaction (the native ``/summarize`` path was previously dead, always
    204ing because ``session.raw["model"]`` is empty).
    """
    resp, client = await _drive_opencode_native_compact(
        monkeypatch,
        tmp_path,
        conv_id="f67241520c2101c4de5f81b976467bad",
        session_payload={"id": "ses_x"},
        messages=[
            {
                "info": {
                    "role": "assistant",
                    "providerID": "anthropic",
                    "modelID": "claude-sonnet-4-5",
                },
                "parts": [],
            }
        ],
        model_override=None,
    )

    assert resp.status_code == 200, (
        f"opencode-native compact must 200 once /summarize is accepted; "
        f"got {resp.status_code}: {resp.text}"
    )
    assert client.summarize_calls == [("ses_x", "anthropic", "claude-sonnet-4-5")], (
        f"summarize must run with the assistant message's model; got {client.summarize_calls!r}."
    )
    assert client.closed, "the handler must close the client in its finally block."


@pytest.mark.asyncio
async def test_events_compact_on_opencode_native_summarizes_from_session_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    With no assistant message, the session ``model`` field drives ``/summarize``.

    Covers create-with-model / TUI ``switchModel`` sessions: the SESSION keys
    are ``providerID`` + ``id``.
    """
    resp, client = await _drive_opencode_native_compact(
        monkeypatch,
        tmp_path,
        conv_id="7b333fd1a0e1c32b3961f98930f41bf3",
        session_payload={
            "id": "ses_x",
            "model": {"providerID": "anthropic", "id": "claude-opus-4"},
        },
        messages=[],
        model_override=None,
    )

    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    assert client.summarize_calls == [("ses_x", "anthropic", "claude-opus-4")], (
        f"summarize must run with the session model; got {client.summarize_calls!r}."
    )


@pytest.mark.asyncio
async def test_events_compact_on_opencode_native_summarizes_from_model_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    With no message/session model, bridge-state ``model_override`` resolves it.

    The override is a qualified ``provider/model`` string split on the first
    ``/``.
    """
    resp, client = await _drive_opencode_native_compact(
        monkeypatch,
        tmp_path,
        conv_id="e45977bad8d13b2fdc00eb2bbdae2bd7",
        session_payload={"id": "ses_x"},
        messages=[],
        model_override="openai/gpt-5",
    )

    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    assert client.summarize_calls == [("ses_x", "openai", "gpt-5")], (
        f"summarize must run with the override model; got {client.summarize_calls!r}."
    )


@pytest.mark.asyncio
async def test_events_compact_on_opencode_native_204_when_model_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    No resolvable model → 204 and ``/summarize`` is never called.

    The 204 tells the Omnigent server to run its own AP-side compaction.
    """
    resp, client = await _drive_opencode_native_compact(
        monkeypatch,
        tmp_path,
        conv_id="90c89e7e5e9131aa4bb062fd427927ae",
        session_payload={"id": "ses_x"},
        messages=[],
        model_override=None,
    )

    assert resp.status_code == 204, (
        f"opencode-native compact must 204 when no model resolves; "
        f"got {resp.status_code}: {resp.text}"
    )
    assert client.summarize_calls == [], (
        f"summarize must NOT run when no model resolves; got {client.summarize_calls!r}."
    )


@pytest.mark.asyncio
async def test_events_compact_on_opencode_native_503_when_summarize_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A failing ``/summarize`` surfaces 503 with the opencode error code.

    The Omnigent server must see the failure (rather than a silent fallback)
    so it does not run a duplicate compaction.
    """
    from omnigent.opencode_native_client import OpenCodeClientError

    resp, client = await _drive_opencode_native_compact(
        monkeypatch,
        tmp_path,
        conv_id="309c268a432cb4dbda4e8c15585578ee",
        session_payload={"id": "ses_x"},
        messages=[
            {
                "info": {
                    "role": "assistant",
                    "providerID": "anthropic",
                    "modelID": "claude-sonnet-4-5",
                },
                "parts": [],
            }
        ],
        model_override=None,
        summarize_error=OpenCodeClientError("summarize failed: 500"),
    )

    assert resp.status_code == 503, (
        f"opencode-native compact must 503 on a failed /summarize; "
        f"got {resp.status_code}: {resp.text}"
    )
    assert resp.json().get("error") == "opencode_native_compact_failed"
    assert client.closed, "the handler must close the client even on failure."


@pytest.mark.asyncio
async def test_events_compact_on_non_native_session_is_204_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Non-native sessions accept compact and 204 without side effects.

    For in-process harnesses, explicit compaction is an AP-side
    operation (``_run_compact_locked`` → ``compact_conversation_now``).
    The Omnigent server forwards ``compact`` to ``/events`` for every harness
    (it stays harness-agnostic), so the runner must accept the event
    and 204 — never reach the slash-command injector. The 204 tells the
    Omnigent server to run its own compaction.
    """
    from omnigent.spec.types import ExecutorSpec

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
    ) -> None:
        """Fail the test if a non-native session reaches the injector."""
        del bridge_dir, command, timeout_s, auto_confirm
        raise AssertionError(
            "inject_slash_command must never be called for non-native "
            "sessions — compact is an AP-side operation for in-process harnesses."
        )

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    # Default harness (in-process LLM loop), NOT claude-native.
    default_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the default spec for any agent_id."""
        del agent_id
        return default_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "49f1d65e8b591ac277b7fca4e50f228c",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/49f1d65e8b591ac277b7fca4e50f228c/events",
            json={"type": "compact"},
        )

    # 204 = the dispatch saw a non-native harness and returned the
    # no-op short-circuit before any inject. The fake injector above
    # asserts loudly if reached, so silence here proves the no-op.
    assert resp.status_code == 204, (
        f"Non-native compact must return 204 no-op; got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_payload,inject_attr",
    # ``/fork`` creates a new conversation that reuses the
    # same Claude process (same bridge_dir), so the new session has
    # bridge_id != conv_id, stored on the ``omnigent.claude_native
    # .bridge_id`` label. The runner-side native dispatch MUST
    # resolve bridge_id via ``_claude_native_bridge_id_for_session``
    # so the slash command lands in the right pane. Using
    # ``bridge_dir_for_conversation_id(conv_id)`` would target a
    # stale / non-existent dir and silently 503.
    #
    # This is the bug that forced a revert. Pinned
    # here for both effort_change and model_change.
    [
        ({"type": "effort_change", "effort": "high"}, "inject_slash_command"),
        ({"type": "model_change", "model": "claude-opus-4-7"}, "inject_slash_command"),
    ],
    ids=["effort_change", "model_change"],
)
async def test_events_native_dispatch_resolves_bridge_id_via_label_lookup(
    monkeypatch: pytest.MonkeyPatch,
    event_payload: dict[str, Any],
    inject_attr: str,
) -> None:
    """
    Native effort / model dispatch must call
    ``_claude_native_bridge_id_for_session`` to resolve the
    bridge_id, not pass conv_id straight to
    ``bridge_dir_for_conversation_id``.

    Regression test for the bug that forced a revert. The
    handlers used ``bridge_dir_for_conversation_id(conv_id)``
    directly, which is broken for ``/fork`` sessions (bridge_id !=
    conv_id, stored on label
    ``omnigent.claude_native.bridge_id``).

    Strategy: monkeypatch ``_claude_native_bridge_id_for_session``
    to return a sentinel bridge_id distinct from conv_id. Then
    assert that the dispatch resolves the bridge_dir from the
    sentinel — proving the handler went through the label-lookup
    path rather than calling ``bridge_dir_for_conversation_id``
    directly. If the handler regresses to the conv_id-only path,
    the assertion fails.
    """
    from omnigent.runner import app as runner_app_module
    from omnigent.spec.types import ExecutorSpec

    captured_bridge_dir: list[Any] = []

    def _fake_inject(bridge_dir: Any, **kwargs: Any) -> None:
        """Record the bridge_dir the dispatch resolved."""
        del kwargs
        captured_bridge_dir.append(bridge_dir)

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    sentinel_bridge_id = "bridge_from_fork_label_xyz"

    async def _fake_bridge_id_lookup(*, server_client: Any, session_id: str) -> str:
        """Pretend the session's bridge_id label is the sentinel."""
        del server_client, session_id
        return sentinel_bridge_id

    monkeypatch.setattr(
        runner_app_module,
        "_claude_native_bridge_id_for_session",
        _fake_bridge_id_lookup,
    )

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    conv_id = "94e59ac02c7c81b1f65ba05e6481d759"
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json=event_payload,
        )

    # The dispatch ran the native handler (inject was called via the
    # fake, which doesn't raise) and returned 204.
    assert resp.status_code == 204, (
        f"Native dispatch for {event_payload['type']!r} must return "
        f"204; got {resp.status_code}: {resp.text}"
    )
    # Exactly one inject call, with the bridge_dir derived from the
    # sentinel bridge_id — NOT from the conv_id.
    assert len(captured_bridge_dir) == 1, (
        f"Expected one inject call, got {len(captured_bridge_dir)}"
    )
    expected = bridge_dir_for_bridge_id(sentinel_bridge_id)
    assert captured_bridge_dir[0] == expected, (
        f"Native dispatch used the wrong bridge_dir. Expected the "
        f"bridge_id-label path ({expected!r}); got "
        f"{captured_bridge_dir[0]!r}. If this matches the conv_id-"
        f"hashed path, the handler regressed to ``bridge_dir_for_"
        f"conversation_id(conv_id)`` and would silently 503 against "
        f"the stale dir on real /fork sessions — the same bug that "
        f"previously forced a revert."
    )


@pytest.mark.asyncio
async def test_events_model_change_on_native_session_types_slash_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    POST ``/events`` with ``{"type":"model_change","model":"claude-opus-4-7"}``
    on a claude-native session injects ``/model claude-opus-4-7`` into tmux.

    Mirrors the effort_change happy-path test. Pins that the new
    runner dispatch routes model_change to the native handler and
    assembles the right slash command.
    """
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.spec.types import ExecutorSpec

    captured: list[Any] = []

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
    ) -> None:
        """Record the call and return without touching tmux."""
        captured.append((bridge_dir, command, timeout_s))

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "57c7c1acc5eeec3978c5e62043da51a4",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        # Drain creation-time events (claude-native auto-create enqueues
        # session.terminal_pending) so the drain below isolates only
        # what model_change emits.
        _drain_session_event_queue(
            _session_event_queues_ref.get("57c7c1acc5eeec3978c5e62043da51a4")
        )

        resp = await client.post(
            "/v1/sessions/57c7c1acc5eeec3978c5e62043da51a4/events",
            json={"type": "model_change", "model": "claude-opus-4-7"},
        )

        # Drain the event queue before delete clears it. model_change
        # is a control signal, not a state change — no events should
        # land on the SSE queue.
        queue = _session_event_queues_ref.get("57c7c1acc5eeec3978c5e62043da51a4")
        queued_events: list[dict[str, Any]] = []
        if queue is not None:
            while not queue.empty():
                item = queue.get_nowait()
                if isinstance(item, dict):
                    queued_events.append(item)

    assert resp.status_code == 204, (
        f"Native model_change must return 204 from /events; got {resp.status_code}: {resp.text}"
    )
    assert len(captured) == 1, (
        f"Expected one inject_slash_command call from native model_change, got {len(captured)}."
    )
    _bridge_dir, command, timeout_s = captured[0]
    assert command == "/model claude-opus-4-7", (
        f"Expected '/model claude-opus-4-7' literal, got {command!r}."
    )
    assert timeout_s == 1.0
    assert queued_events == [], (
        f"model_change must not publish session events; got {queued_events!r}."
    )


@pytest.mark.asyncio
async def test_events_model_change_on_kiro_session_types_slash_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    POST ``/events`` ``{"type":"model_change","model":"claude-haiku-4.5"}`` on a
    kiro-native session drives ``inject_model_command`` (which types
    ``/model claude-haiku-4.5`` into the live kiro TUI).

    Pins that the runner dispatch routes model_change to the kiro handler.
    Mirrors ``test_events_model_change_on_native_session_types_slash_command``.
    """
    from omnigent.runner.app import _session_event_queues_ref
    from omnigent.spec.types import ExecutorSpec

    captured: list[Any] = []

    def _fake_inject(bridge_dir: Any, *, model: str, timeout_s: float) -> None:
        """Record the call and return without touching tmux."""
        captured.append((bridge_dir, model, timeout_s))

    monkeypatch.setattr(kiro_native_bridge, "inject_model_command", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "kiro-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the kiro-native spec for any agent_id."""
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "b07013b8f257ae8e087e343a0d7008a3",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        # Drain kiro auto-create events so nothing below trips on them.
        _drain_session_event_queue(
            _session_event_queues_ref.get("b07013b8f257ae8e087e343a0d7008a3")
        )

        resp = await client.post(
            "/v1/sessions/b07013b8f257ae8e087e343a0d7008a3/events",
            json={"type": "model_change", "model": "claude-haiku-4.5"},
        )

    assert resp.status_code == 204, (
        f"Kiro model_change must return 204 from /events; got {resp.status_code}: {resp.text}"
    )
    assert len(captured) == 1, (
        f"Expected one inject_model_command call from kiro model_change, got {len(captured)}."
    )
    _bridge_dir, model, timeout_s = captured[0]
    assert model == "claude-haiku-4.5"
    assert timeout_s == 1.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_value",
    # Claude Code has no slash form for "use spawn default", so
    # ``None`` (clear) must skip injection. Empty / whitespace-only
    # strings must also skip — typing ``/model `` with nothing after
    # would land as a TUI error.
    [None, "", "   "],
)
async def test_events_model_change_on_native_session_skips_inject_for_empty_or_null(
    monkeypatch: pytest.MonkeyPatch,
    model_value: str | None,
) -> None:
    """
    Null / empty / whitespace-only model values 204 without typing.

    Pins that the empty-value validation lives in the runner native
    handler, not in the Omnigent server.
    """
    from omnigent.spec.types import ExecutorSpec

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
    ) -> None:
        """Fail the test if the runner reaches inject for an empty value."""
        del bridge_dir, command, timeout_s
        raise AssertionError(
            f"inject_slash_command must not be called for model={model_value!r}; "
            f"the native handler should skip empty / null values."
        )

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "5aaed5c8eac5f60a5030e6e830602018",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/5aaed5c8eac5f60a5030e6e830602018/events",
            json={"type": "model_change", "model": model_value},
        )

    assert resp.status_code == 204, (
        f"Native model_change with empty / null value must return "
        f"204 (no-op); got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_events_model_change_on_native_session_returns_503_when_bridge_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Bridge-not-ready RuntimeError surfaces as 503 from /events.

    Sister to the happy-path test. Pins that the failure mode of the
    native model dispatch (tmux pane gone / bridge dir not yet
    advertised) returns 503 with the same error code shape the
    legacy ``/claude-native-model`` route used. Omnigent server's PATCH
    swallows this 503 and still returns 200 with the persisted
    value — the next spawn applies the new model via ``--model``.
    """
    from omnigent.spec.types import ExecutorSpec

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
    ) -> None:
        """Simulate the bridge-not-ready path."""
        del bridge_dir, command, timeout_s
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the native spec for any agent_id."""
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "75db03d3ecf58400bd53ce0326f49c4d",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/75db03d3ecf58400bd53ce0326f49c4d/events",
            json={"type": "model_change", "model": "claude-opus-4-7"},
        )

    assert resp.status_code == 503, (
        f"Native model_change with inject failure must return 503; "
        f"got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("error") == "claude_native_model_failed", (
        f"503 body must carry the bridge-failure error code; got {body!r}"
    )


@pytest.mark.asyncio
async def test_events_model_change_on_non_native_session_is_204_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Non-native sessions accept model_change and 204 without side effects.

    In-process harnesses re-read the persisted ``model_override`` on
    each turn (or via the per-event override). Omnigent server is harness-
    agnostic and POSTs model_change for every PATCH, so the runner
    must accept the event with a 204 — never reach the slash-command
    injector.
    """
    from omnigent.spec.types import ExecutorSpec

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
    ) -> None:
        """Fail the test if a non-native session reaches the injector."""
        del bridge_dir, command, timeout_s
        raise AssertionError(
            "inject_slash_command must never be called for non-native "
            "sessions — model_change is a no-op for in-process harnesses."
        )

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    # Default harness (in-process LLM loop), NOT claude-native.
    default_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the default spec for any agent_id."""
        del agent_id, session_id
        return default_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "3bd7b988ac7f00912b39823dbb0c6956",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/3bd7b988ac7f00912b39823dbb0c6956/events",
            json={"type": "model_change", "model": "claude-opus-4-7"},
        )

    assert resp.status_code == 204, (
        f"Non-native model_change must return 204 no-op; got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_events_model_change_on_cursor_native_session_types_slash_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    POST ``/events`` with ``model_change`` on a cursor-native session
    drives cursor-agent's ``/model`` picker via ``inject_model_command``.

    Cursor analog of the claude-native happy-path test: the runner
    dispatch must route cursor-native model_change to its TUI handler
    (not the claude slash injector and not a 204 no-op) and pass the
    model id straight through.
    """
    from omnigent.spec.types import ExecutorSpec

    captured: list[tuple[Any, str, float]] = []

    def _fake_inject(bridge_dir: Any, *, model: str, timeout_s: float) -> None:
        """Record the call and return without touching tmux."""
        captured.append((bridge_dir, model, timeout_s))

    monkeypatch.setattr(cursor_native_bridge, "inject_model_command", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "cursor-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the cursor-native spec for any agent_id."""
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "c42dbcb16fd3a87ee8f5d1fe4cabfdf8",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/c42dbcb16fd3a87ee8f5d1fe4cabfdf8/events",
            json={"type": "model_change", "model": "gpt-5.2"},
        )

    assert resp.status_code == 204, (
        f"cursor-native model_change must return 204 from /events; "
        f"got {resp.status_code}: {resp.text}"
    )
    assert len(captured) == 1, f"Expected one inject_model_command call, got {len(captured)}."
    _bridge_dir, model, timeout_s = captured[0]
    assert model == "gpt-5.2", f"Expected the model id passed through, got {model!r}."
    assert timeout_s == 1.0


@pytest.mark.asyncio
@pytest.mark.parametrize("model_value", [None, "", "   "])
async def test_events_model_change_on_cursor_native_session_skips_inject_for_empty(
    monkeypatch: pytest.MonkeyPatch,
    model_value: str | None,
) -> None:
    """
    Null / empty / whitespace-only model values 204 without driving the picker.

    cursor-agent has no slash form for "use the spawn default", so a
    clear only takes effect on the next spawn — mirrors the claude-native
    skip test.
    """
    from omnigent.spec.types import ExecutorSpec

    def _fake_inject(bridge_dir: Any, *, model: str, timeout_s: float) -> None:
        """Fail the test if the runner reaches inject for an empty value."""
        del bridge_dir, timeout_s
        raise AssertionError(f"inject_model_command must not be called for model={model_value!r}.")

    monkeypatch.setattr(cursor_native_bridge, "inject_model_command", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "cursor-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the cursor-native spec for any agent_id."""
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "1f86fc567a3d953efdbbc6409ca286e3",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/1f86fc567a3d953efdbbc6409ca286e3/events",
            json={"type": "model_change", "model": model_value},
        )

    assert resp.status_code == 204, (
        f"cursor-native model_change with empty / null value must return "
        f"204 (no-op); got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_events_model_change_on_cursor_native_session_returns_503_when_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Bridge-not-ready RuntimeError surfaces as 503 from /events.

    Cursor analog of the claude-native 503 test: a missing tmux target
    (pane not attached yet) returns 503 with the cursor-specific error
    code; Omnigent server swallows it and the next spawn applies ``--model``.
    """
    from omnigent.spec.types import ExecutorSpec

    def _fake_inject(bridge_dir: Any, *, model: str, timeout_s: float) -> None:
        """Simulate the bridge-not-ready path."""
        del bridge_dir, model, timeout_s
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr(cursor_native_bridge, "inject_model_command", _fake_inject)

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "cursor-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the cursor-native spec for any agent_id."""
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "ccf1b995c37a1ae8339d408fb227dff5",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/ccf1b995c37a1ae8339d408fb227dff5/events",
            json={"type": "model_change", "model": "gpt-5.2"},
        )

    assert resp.status_code == 503, (
        f"cursor-native model_change with inject failure must return 503; "
        f"got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("error") == "cursor_native_model_failed", (
        f"503 body must carry the cursor bridge-failure error code; got {body!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("effort_value", ["high", "medium", "low", "xhigh", None, ""])
async def test_events_effort_change_on_cursor_native_session_is_disabled_noop(
    effort_value: str | None,
) -> None:
    """
    cursor-native effort switching is intentionally dropped (for now): a model
    switch resets cursor's per-model effort to that model's default, so a web
    effort would silently diverge from the TUI. The dispatch must 204 for ANY
    effort value (cursor-native is excluded from the effort_change gate, and the
    effort injector no longer exists).
    """
    from omnigent.spec.types import ExecutorSpec

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "cursor-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the cursor-native spec for any agent_id."""
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "6d57cf20b7c9680bd7bfe465b4b666d5",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/6d57cf20b7c9680bd7bfe465b4b666d5/events",
            json={"type": "effort_change", "effort": effort_value},
        )

    assert resp.status_code == 204, (
        f"cursor-native effort_change must 204 (disabled); got {resp.status_code}: {resp.text}"
    )
