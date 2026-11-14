"""Tests for native session model, effort, compact, and plan-mode events."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.entities.session_resources import SessionResourceView
from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.claude_native.bridge import (
    bridge_dir_for_bridge_id,
    bridge_dir_for_conversation_id,
)
from omnigent.harnesses.codex_native import bridge as codex_native_bridge
from omnigent.harnesses.cursor_native import bridge as cursor_native_bridge
from omnigent.harnesses.kiro_native import bridge as kiro_native_bridge
from omnigent.harnesses.qwen_native import bridge as qwen_native_bridge
from omnigent.runner import create_runner_app, native_controls
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import TerminalRegistry
from tests._helpers.providers import isolate_provider_resolution
from tests.runner.conftest import (
    _build_app_for_spec,
    _drain_session_event_queue,
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient
from tests.runner.native_helpers import _harness_spec


@pytest.fixture(autouse=True)
def _isolate_anthropic_default_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear ambient ``ANTHROPIC_DEFAULT_*_MODEL`` gateway pins (#4279).

    claude-native model resolution reads these from ``os.environ``; a developer
    whose shell pins them (anyone driving Claude through a gateway) otherwise
    gets a different model-change verdict and these tests fail spuriously.
    """
    for var in (
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "effort_value",
    # ``EFFORT_VALUES`` is a superset of ``CLAUDE_EFFORTS``: PATCH accepts the
    # full effort vocabulary, but Claude Code's ``/effort`` slash only accepts
    # low/medium/high/xhigh/max. ``none`` and ``minimal`` must skip injection
    # (typing ``/effort none`` would land as a TUI error). ``None`` (clear) must
    # skip too — Claude has no slash form for "use spawn default".
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

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        """Fail the test if the runner reaches inject for an unsupported level."""
        del bridge_dir, command, timeout_s
        raise AssertionError(
            f"inject_slash_command must not be called for effort={effort_value!r}; "
            f"the native handler should skip unsupported / null levels."
        )

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    native_spec = _harness_spec("claude-native")

    app, _ = await _build_app_for_spec(native_spec)

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
@pytest.mark.parametrize("error_type", [RuntimeError, OSError])
async def test_events_effort_change_on_native_session_returns_503_when_bridge_not_ready(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    """
    A bridge-not-ready RuntimeError or a tmux OSError surfaces as 503 from /events.

    Sister to the happy-path test. Pins that the failure mode of the
    native effort dispatch (tmux pane gone / bridge dir not yet
    advertised) returns 503 with the same error code shape the
    legacy route returns. Omnigent server's PATCH swallows this 503 and
    still returns 200 with the persisted value — the next spawn
    will apply the new effort via ``--effort``.
    """

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
        ready_timeout_s: float | None = None,
    ) -> None:
        """Simulate the bridge-not-ready path."""
        del bridge_dir, command, timeout_s
        raise error_type("tmux target is not advertised")

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    native_spec = _harness_spec("claude-native")

    app, _ = await _build_app_for_spec(native_spec)

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

    In-process harnesses (default / claude-sdk / openai-agents / codex / pi)
    get the new effort on their next turn, from the ``reasoning`` block the
    runner threads onto the forwarded body — so the event needs no injection
    and no immediate forward. The Omnigent server POSTs ``effort_change`` to
    ``/events`` for every PATCH (it's harness-agnostic), so the runner must
    accept the event and 204 — never reach the slash-command injector, never
    forward to the harness scaffold.
    """

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
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

    app, _ = await _build_app_for_spec(default_spec)

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
async def test_events_permission_mode_change_on_native_session_switches_and_echoes_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``permission_mode_change`` cycles the pane and returns the settled mode.

    Claude Code's ``--permission-mode`` is launch-only, so the runner drives
    the TUI's shift+tab cycle via the bridge. The 200 body echoes the mode the
    pane actually landed on — the Omnigent server persists that value, so a
    regression returning 204 (or dropping the body) would leave the web UI
    showing a mode the session isn't in.
    """

    calls: list[str] = []

    def _fake_set_mode(bridge_dir: Any, *, mode: str, timeout_s: float) -> str:
        """Record the requested mode; report it as reached."""
        del bridge_dir, timeout_s
        calls.append(mode)
        return mode

    monkeypatch.setattr(claude_native_bridge, "set_permission_mode", _fake_set_mode)

    native_spec = _harness_spec("claude-native")

    app, _ = await _build_app_for_spec(native_spec)

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "2f77519bd9daa4e9bc2df649fe468500",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/2f77519bd9daa4e9bc2df649fe468500/events",
            json={"type": "permission_mode_change", "permission_mode": "auto"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"permission_mode": "auto"}
    assert calls == ["auto"], f"Expected one switch to auto, got {calls!r}"


@pytest.mark.asyncio
async def test_events_permission_mode_change_returns_503_when_mode_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A failed switch surfaces 503 so the label is never persisted.

    ``auto`` is only in the shift+tab cycle for accounts that have the mode.
    The Omnigent server treats a non-2xx as "the pane did not move" and skips
    persisting the label, so this must not report success.
    """

    def _fake_set_mode(bridge_dir: Any, *, mode: str, timeout_s: float) -> str:
        """Fail the way an unreachable mode does."""
        del bridge_dir, mode, timeout_s
        raise RuntimeError("The mode is not available in this session's cycle.")

    monkeypatch.setattr(claude_native_bridge, "set_permission_mode", _fake_set_mode)

    native_spec = _harness_spec("claude-native")

    app, _ = await _build_app_for_spec(native_spec)

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "3f88519bd9daa4e9bc2df649fe468511",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/3f88519bd9daa4e9bc2df649fe468511/events",
            json={"type": "permission_mode_change", "permission_mode": "auto"},
        )

    assert resp.status_code == 503, resp.text
    body = resp.json()
    # The error CODE names the failure category; the runner deliberately
    # redacts exception text from client-facing details (the cause is logged
    # server-side instead), so the detail is the fixed safe string.
    assert body.get("error") == "claude_native_permission_mode_failed", body


@pytest.mark.asyncio
async def test_events_permission_mode_change_on_non_native_session_is_204_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Only claude-native sessions act on ``permission_mode_change``.

    No other harness has Claude's shift+tab cycle, so the dispatch must
    short-circuit before reaching the bridge.
    """

    def _fake_set_mode(bridge_dir: Any, *, mode: str, timeout_s: float) -> str:
        """Fail the test if a non-native session reaches the bridge."""
        del bridge_dir, mode, timeout_s
        raise AssertionError(
            "set_permission_mode must never be called for non-claude-native sessions."
        )

    monkeypatch.setattr(claude_native_bridge, "set_permission_mode", _fake_set_mode)

    default_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={}),
    )

    app, _ = await _build_app_for_spec(default_spec)

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "4f99519bd9daa4e9bc2df649fe468522",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            "/v1/sessions/4f99519bd9daa4e9bc2df649fe468522/events",
            json={"type": "permission_mode_change", "permission_mode": "auto"},
        )

    assert resp.status_code == 204, (
        f"Non-native permission_mode_change must 204 no-op; got {resp.status_code}: {resp.text}"
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
    the control was handled in the terminal. A regression returning 204 here
    would make the server return a 400 "not available for this session type"
    error instead of the native compact succeeding.
    """
    from omnigent.runner.app import _session_event_queues_ref

    captured: list[Any] = []

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
        ready_timeout_s: float | None = None,
    ) -> None:
        """Record the call (including auto_confirm) without touching tmux."""
        captured.append((bridge_dir, command, timeout_s, auto_confirm))

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    native_spec = _harness_spec("claude-native")

    app, _ = await _build_app_for_spec(native_spec)

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

    # 200 = native dispatch routed to the compact handler and injected
    # successfully. 204 would mean the wrong harness branch fired (→ server
    # returns 400). 404 = dispatch fell through to the generic forward.
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
async def test_events_clear_on_claude_native_types_clear_into_the_tui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    POST ``/events`` with ``{"type":"clear"}`` on a claude-native session types
    Claude Code's own ``/clear`` into the pane and returns 200.

    Claude Code's /clear hook rotates the terminal onto a fresh Omnigent
    session, so the web /clear must run inside Claude Code.
    """
    captured: list[str] = []

    def _fake_inject(bridge_dir: Any, *, command: str, **_kwargs: Any) -> None:
        """Record the typed command without touching tmux."""
        captured.append(command)

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)
    app, _ = await _build_app_for_spec(_harness_spec("claude-native"))

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "0d1c2b3a4f5e6d7c8b9a0f1e2d3c4b5a",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        resp = await client.post(
            "/v1/sessions/0d1c2b3a4f5e6d7c8b9a0f1e2d3c4b5a/events",
            json={"type": "clear"},
        )

    assert resp.status_code == 200, resp.text
    assert captured == ["/clear"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RuntimeError, OSError])
async def test_events_compact_on_native_session_returns_503_when_bridge_not_ready(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    """
    A bridge-not-ready RuntimeError or a tmux OSError surfaces as 503 from /events.

    Sister to the happy-path test. When the tmux pane isn't attached
    there is no live Claude to compact, so the native compact handler
    returns 503 with the ``claude_native_compact_failed`` code. The AP
    server treats a non-200/204 runner response as an error rather
    than silently running its own (wrong) compaction.
    """

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
        ready_timeout_s: float | None = None,
    ) -> None:
        """Simulate the bridge-not-ready path."""
        del bridge_dir, command, timeout_s, auto_confirm
        raise error_type("tmux target is not advertised")

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)

    native_spec = _harness_spec("claude-native")

    app, _ = await _build_app_for_spec(native_spec)

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
async def test_events_compact_on_codex_native_types_settles_then_submits(
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

    The settle between typing and Enter is load-bearing too: typing
    ``/compact`` opens Codex's slash-command popup, which draws
    asynchronously, and an Enter sent back-to-back is swallowed by the
    still-opening popup — the command is left un-submitted in the TUI
    composer and the user sees no compaction feedback at all.
    """
    import time as real_time
    from typing import Any as _Any

    from omnigent.runner.app import _session_event_queues_ref
    from tests.runner.helpers import make_test_terminal_instance

    events: list[tuple[str, object]] = []

    def _fake_run_tmux(socket_path: str, *args: str) -> None:
        """Record tmux send-keys calls without touching tmux."""
        events.append(("tmux", (socket_path, list(args))))

    class _RecordingTime:
        """Delegate to the real ``time`` module but record ``sleep`` calls."""

        def __getattr__(self, name: str) -> _Any:
            return getattr(real_time, name)

        @staticmethod
        def sleep(seconds: float) -> None:
            events.append(("sleep", seconds))

    monkeypatch.setattr(claude_native_bridge, "_run_tmux", _fake_run_tmux)
    monkeypatch.setattr(native_controls, "time", _RecordingTime())

    codex_native_spec = _harness_spec("codex-native")

    conv_id = "9864122f95f2f013c9599f4014725784"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("codex", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("codex", "main")] = instance

    app, _ = await _build_app_for_spec(codex_native_spec, terminal_registry=terminal_registry)

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
    socket = str(instance.socket_path)
    tmux_calls = [payload for kind, payload in events if kind == "tmux"]
    assert tmux_calls == [
        (socket, ["send-keys", "-t", "main", "C-u"]),
        (socket, ["send-keys", "-l", "-t", "main", "/compact"]),
        (socket, ["send-keys", "-t", "main", "Enter"]),
    ], f"Expected C-u, literal /compact, Enter; got {tmux_calls!r}."

    # A settle pause must separate typing the command from the submit Enter,
    # or the asynchronously-rendered slash-command popup swallows the Enter
    # and the command never submits.
    typed = ("tmux", (socket, ["send-keys", "-l", "-t", "main", "/compact"]))
    entered = ("tmux", (socket, ["send-keys", "-t", "main", "Enter"]))
    settles = [
        payload
        for kind, payload in events[events.index(typed) + 1 : events.index(entered)]
        if kind == "sleep"
    ]
    assert settles and all(isinstance(s, float | int) and s > 0 for s in settles), (
        "Typing /compact and pressing Enter must be separated by a settle "
        f"pause for the slash-command popup to render; got events={events!r}."
    )
    # /compact is a control signal, not a state change.
    assert queued_events == [], f"compact must not publish session events; got {queued_events!r}."


@pytest.mark.asyncio
async def test_events_clear_on_codex_native_types_clear_into_the_tui(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    POST ``/events`` with ``{"type":"clear"}`` on a codex-native session types
    Codex's own ``/clear`` into its tmux pane and returns 200.

    Codex's /clear starts a fresh thread in the same terminal; the forwarder
    then rotates the Omnigent session, so the web /clear must run inside Codex.
    """
    from tests.runner.helpers import make_test_terminal_instance

    tmux_calls: list[tuple[str, list[str]]] = []

    def _fake_run_tmux(socket_path: str, *args: str) -> None:
        """Record tmux send-keys calls without touching tmux."""
        tmux_calls.append((socket_path, list(args)))

    monkeypatch.setattr(claude_native_bridge, "_run_tmux", _fake_run_tmux)
    monkeypatch.setattr(native_controls, "_CODEX_POPUP_RENDER_S", 0)

    conv_id = "5b6a0c3d0f1e4a7b8c9d0e1f2a3b4c5d"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("codex", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("codex", "main")] = instance
    app, _ = await _build_app_for_spec(
        _harness_spec("codex-native"), terminal_registry=terminal_registry
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        resp = await client.post(f"/v1/sessions/{conv_id}/events", json={"type": "clear"})

    assert resp.status_code == 200, resp.text
    socket = str(instance.socket_path)
    assert tmux_calls == [
        (socket, ["send-keys", "-t", "main", "C-u"]),
        (socket, ["send-keys", "-l", "-t", "main", "/clear"]),
        (socket, ["send-keys", "-t", "main", "Enter"]),
    ]


@pytest.mark.asyncio
async def test_events_compact_on_codex_native_returns_503_when_no_terminal() -> None:
    """
    Codex-native compact returns 503 when no live terminal is registered.

    Without a running codex terminal the ``/compact`` slash command has
    nowhere to go. 503 tells the caller to reconnect first.
    """
    codex_native_spec = _harness_spec("codex-native")

    conv_id = "4be2f8fe2204fade6a89dafade0a0fd2"
    # Empty registry — no codex terminal registered.
    terminal_registry = TerminalRegistry()

    app, _ = await _build_app_for_spec(codex_native_spec, terminal_registry=terminal_registry)

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
        f"Codex-native compact with no terminal must return 503; "
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

    codex_native_spec = _harness_spec("codex-native")

    conv_id = "e5d09a0ff8458b2d6abb7f0c7deda0d3"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("codex", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("codex", "main")] = instance

    app, _ = await _build_app_for_spec(codex_native_spec, terminal_registry=terminal_registry)

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
@pytest.mark.parametrize("active_turn_id", ["turn_running", None], ids=["busy", "idle"])
async def test_events_codex_native_approval_mode_change_types_only_when_idle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    active_turn_id: str | None,
) -> None:
    """
    An approval-mode switch while Codex runs a turn is refused, not keyed in.

    Codex disables /permissions during a turn, and the Escape that opens it
    interrupts the turn and drops the message it carries. The 409 detail tells
    the person why the mode did not change. Once the session is idle the same
    request drives the /permissions popup and reports the confirmed mode.
    """
    from tests.runner.helpers import make_test_terminal_instance

    popup = (
        "  Update Model Permissions\n\n"
        "› 1. Ask for approval  Read and edit workspace files and run commands\n"
        "  2. Full Access       Use with caution: Codex can edit files outside\n\n"
        "  enter select · esc back\n"
    )
    tmux_calls: list[tuple[str, ...]] = []

    def _recording_run_tmux(socket_path: str, *args: str) -> None:
        del socket_path
        tmux_calls.append(args)

    def _fake_capture_pane(socket_path: str, target: str, **kwargs: Any) -> str:
        del socket_path, target, kwargs
        # The popup stays up until its Full Access row is picked; Codex then
        # prints the confirmation line.
        if any(call[-1] == "2" for call in tmux_calls):
            return "• Permissions updated to Full Access\n"
        return popup

    async def _no_op_auto_create(*args: Any, **kwargs: Any) -> None:
        # Session creation would otherwise rewrite the bridge state below.
        del args, kwargs

    monkeypatch.setattr(claude_native_bridge, "_run_tmux", _recording_run_tmux)
    monkeypatch.setattr(claude_native_bridge, "_capture_pane", _fake_capture_pane)
    monkeypatch.setattr(native_controls, "_CODEX_POPUP_RENDER_S", 0)
    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_codex_terminal", _no_op_auto_create
    )
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    conv_id = "3c1f6be0b1f04e45a8d9e2f7c6b5a4d3"
    codex_native_bridge.write_bridge_state(
        codex_native_bridge.bridge_dir_for_bridge_id(conv_id),
        codex_native_bridge.CodexNativeBridgeState(
            session_id=conv_id,
            socket_path="ws://127.0.0.1:43210",
            thread_id="thread_codex",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id=active_turn_id,
        ),
    )
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("codex", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("codex", "main")] = instance

    app, _ = await _build_app_for_spec(
        _harness_spec("codex-native"), terminal_registry=terminal_registry
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "codex_approval_mode_change", "approval_mode": "full-access"},
        )

    if active_turn_id is None:
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"approval_mode": "full-access"}
        target = instance.tmux_target
        assert tmux_calls == [
            ("send-keys", "-t", target, "Escape"),
            ("send-keys", "-t", target, "C-u"),
            ("send-keys", "-l", "-t", target, "/permissions"),
            ("send-keys", "-t", target, "Enter"),
            ("send-keys", "-l", "-t", target, "2"),
            ("send-keys", "-l", "-t", target, "1"),
        ]
        return
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "codex_native_approval_mode_busy"
    assert body["detail"] == (
        "Codex is working on a turn. Change the approval mode after it finishes, "
        "or stop the turn first."
    )
    assert tmux_calls == []


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
    path. The 200 (not 204) is load-bearing: the Omnigent server reads it to
    know the control was handled in the terminal.

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

    monkeypatch.setattr(cursor_native_bridge, "_BRIDGE_ROOT", tmp_path / "cursor-bridge")

    captured: list[tuple[Any, str, float]] = []

    def _fake_inject(bridge_dir: Any, *, content: str, timeout_s: float) -> None:
        """Record the bracketed-paste call without touching tmux."""
        captured.append((bridge_dir, content, timeout_s))

    monkeypatch.setattr(cursor_native_bridge, "inject_user_message", _fake_inject)

    cursor_native_spec = _harness_spec("cursor-native")

    conv_id = "764ebbade28dd774a5d673378c034933"
    app, _ = await _build_app_for_spec(cursor_native_spec)

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

    monkeypatch.setattr(cursor_native_bridge, "_BRIDGE_ROOT", tmp_path / "cursor-bridge")

    def _fake_inject(bridge_dir: Any, *, content: str, timeout_s: float) -> None:
        """Simulate an injection failure (tmux down, or tempfile write fault)."""
        del bridge_dir, content, timeout_s
        raise inject_exc

    monkeypatch.setattr(cursor_native_bridge, "inject_user_message", _fake_inject)

    cursor_native_spec = _harness_spec("cursor-native")

    conv_id = uuid.uuid4().hex
    app, _ = await _build_app_for_spec(cursor_native_spec)

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
    import omnigent.harnesses.pi_native.bridge as pi_native_bridge
    from omnigent.runner.app import _session_event_queues_ref

    conv_id = "03f435963d78fe4ea313325f729eefc5"
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-bridge")

    pi_native_spec = _harness_spec("pi-native")

    app, _ = await _build_app_for_spec(pi_native_spec)

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
    import omnigent.harnesses.pi_native.bridge as pi_native_bridge

    conv_id = "9c52b3dbe1d543718c1678a256017326"
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-bridge")

    def _boom(*_args: Any, **_kwargs: Any) -> str:
        """Simulate an unwritable inbox."""
        raise OSError("inbox is read-only")

    monkeypatch.setattr(pi_native_bridge, "enqueue_compact", _boom)

    pi_native_spec = _harness_spec("pi-native")

    app, _ = await _build_app_for_spec(pi_native_spec)

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
    reads it to know the control was handled). The handler publishes only
    ``in_progress``; the ``completed`` edge is the compaction mirror's job once
    the ``chat_compression`` record lands (covered in test_qwen_native_forwarder).
    """
    from omnigent.runner.app import _session_event_queues_ref

    captured: list[tuple[Any, str]] = []

    def _fake_submit(bridge_dir: Any, *, content: str) -> None:
        """Record the input-file submit without touching disk."""
        captured.append((bridge_dir, content))

    monkeypatch.setattr(qwen_native_bridge, "submit_user_message", _fake_submit)

    qwen_native_spec = _harness_spec("qwen-native")

    conv_id = "233c1fedaaeb18c91267904e79b7d10c"
    app, _ = await _build_app_for_spec(qwen_native_spec)

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

    def _fake_submit(bridge_dir: Any, *, content: str) -> None:
        del bridge_dir, content
        raise RuntimeError("input file unwritable")

    monkeypatch.setattr(qwen_native_bridge, "submit_user_message", _fake_submit)

    qwen_native_spec = _harness_spec("qwen-native")

    conv_id = "7c94e9a0306b300d81233cccef543a84"
    app, _ = await _build_app_for_spec(qwen_native_spec)

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
    """OpenCode client stub recording ``compact`` calls."""

    def __init__(self, *, compact_error: BaseException | None = None) -> None:
        self._compact_error = compact_error
        self.compact_calls: list[str] = []
        self.closed = False

    async def compact(self, session_id: str) -> dict[str, Any]:
        if self._compact_error is not None:
            raise self._compact_error
        self.compact_calls.append(session_id)
        return {"id": "msg_compaction", "type": "compaction"}

    async def aclose(self) -> None:
        self.closed = True


class _FakeOpenCodeCompactServer:
    """``OpenCodeNativeServer`` stub whose ``client()`` returns a fixed stub."""

    def __init__(self, client: _FakeOpenCodeCompactClient) -> None:
        self._client = client

    def client(self, *, directory: str | None = None) -> _FakeOpenCodeCompactClient:
        del directory
        return self._client


async def _drive_opencode_native_compact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    conv_id: str,
    compact_error: BaseException | None = None,
    with_server: bool = True,
) -> tuple[httpx.Response, _FakeOpenCodeCompactClient]:
    """Create an opencode-native session and POST a ``compact`` control event."""
    from omnigent.harnesses.opencode_native import bridge as opencode_native_bridge
    from omnigent.harnesses.opencode_native.bridge import OpenCodeNativeBridgeState
    from omnigent.runner.app import _AUTO_OPENCODE_SERVERS, _session_event_queues_ref
    from tests.runner.helpers import make_test_terminal_instance

    spec = _harness_spec("opencode-native")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("opencode", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("opencode", "main")] = instance
    client = _FakeOpenCodeCompactClient(compact_error=compact_error)
    state = OpenCodeNativeBridgeState(
        session_id=conv_id,
        server_base_url="http://127.0.0.1:1",
        opencode_session_id="ses_x",
    )
    monkeypatch.setattr(opencode_native_bridge, "read_bridge_state", lambda _dir: state)
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
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
            if with_server:
                _AUTO_OPENCODE_SERVERS[conv_id] = _FakeOpenCodeCompactServer(client)
            resp = await http_client.post(
                f"/v1/sessions/{conv_id}/events", json={"type": "compact"}
            )
        return resp, client
    finally:
        _AUTO_OPENCODE_SERVERS.pop(conv_id, None)
        _session_event_queues_ref.pop(conv_id, None)


@pytest.mark.asyncio
async def test_events_compact_on_opencode_native_calls_compact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """opencode-native compact calls ``POST /compact`` with no model and returns 200.

    The 200 is load-bearing: the Omnigent server reads it to skip its own
    compaction for this session.
    """
    resp, client = await _drive_opencode_native_compact(
        monkeypatch, tmp_path, conv_id="f67241520c2101c4de5f81b976467bad"
    )
    assert resp.status_code == 200, resp.text
    assert client.compact_calls == ["ses_x"]
    assert client.closed


@pytest.mark.asyncio
async def test_events_compact_on_opencode_native_503_when_compact_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failing ``/compact`` surfaces 503 so the server does not double-compact."""
    from omnigent.harnesses.opencode_native.client import OpenCodeClientError

    resp, client = await _drive_opencode_native_compact(
        monkeypatch,
        tmp_path,
        conv_id="309c268a432cb4dbda4e8c15585578ee",
        compact_error=OpenCodeClientError("compact failed: 409"),
    )
    assert resp.status_code == 503, resp.text
    assert resp.json().get("error") == "opencode_native_compact_failed"
    assert client.closed


@pytest.mark.asyncio
async def test_events_compact_on_opencode_native_503_when_not_active(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With no live server the compact reports 503 and never calls OpenCode."""
    resp, client = await _drive_opencode_native_compact(
        monkeypatch,
        tmp_path,
        conv_id="90c89e7e5e9131aa4bb062fd427927ae",
        with_server=False,
    )
    assert resp.status_code == 503, resp.text
    assert client.compact_calls == []


@pytest.mark.asyncio
async def test_events_clear_on_opencode_native_relaunches_fresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``/clear`` relaunches OpenCode without resuming, forking, or seeding."""
    from omnigent.runner import native_controls
    from tests.runner.helpers import make_test_terminal_instance

    conv_id = "4f1c2b7e9d0a4c55b1e7a3c9d2f60a18"
    spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    calls: list[dict[str, Any]] = []

    async def _fake_auto_create(*args: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(native_controls, "_auto_create_opencode_terminal", _fake_auto_create)
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("opencode", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("opencode", "main")] = instance
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )
    async with _runner_client(app) as http_client:
        create_resp = await http_client.post(
            "/v1/sessions", json={"session_id": conv_id, "agent_id": "ag_1"}
        )
        assert create_resp.status_code == 201, create_resp.text
        resp = await http_client.post(f"/v1/sessions/{conv_id}/events", json={"type": "clear"})

    assert resp.status_code == 200, resp.text
    assert len(calls) == 1
    assert calls[0]["fresh"] is True


@pytest.mark.asyncio
async def test_events_clear_on_opencode_native_waits_for_an_idle_reap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``/clear`` relaunches OpenCode only after a reap holding the terminal lock finishes.

    The relaunch reuses the session's tool relay before it registers the new pane;
    interleaved with a reap, the reap would close the relay the new pane uses.
    """
    from omnigent.runner.app import _session_event_queues_ref
    from tests.runner.helpers import make_test_terminal_instance

    conv_id = f"conv_{uuid.uuid4().hex[:12]}"
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("opencode", "main", tmp_path)
    terminal_registry._by_conversation[conv_id] = {("opencode", "main"): instance}
    relaunches: list[str] = []

    async def _record_relaunch(session_id: str, *args: object, **kwargs: object) -> None:
        del args, kwargs
        relaunches.append(session_id)

    monkeypatch.setattr(native_controls, "_auto_create_opencode_terminal", _record_relaunch)
    app, _pm = await _build_app_for_spec(
        _harness_spec("opencode-native"), terminal_registry=terminal_registry
    )
    async with _runner_client(app) as http_client:
        create_resp = await http_client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        _drain_session_event_queue(_session_event_queues_ref.get(conv_id))
        lock = app.state.native_terminal_ensure_locks["opencode"].setdefault(
            conv_id, asyncio.Lock()
        )
        async with lock:
            clear = asyncio.create_task(
                http_client.post(f"/v1/sessions/{conv_id}/events", json={"type": "clear"})
            )
            await asyncio.sleep(0.2)
            assert not relaunches, "/clear relaunched OpenCode while the lock was held"
        resp = await clear
        assert resp.status_code == 200, resp.text
        assert relaunches == [conv_id]


@pytest.mark.asyncio
async def test_events_compact_on_non_native_session_is_204_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Non-native sessions accept compact and 204 without side effects.

    The Omnigent server forwards ``compact`` to ``/events`` for every harness,
    so the runner must accept the event and 204 — never reach the
    slash-command injector. The server then returns a 400 to the client
    (SDK harnesses control their own context; AP-side compaction is not available).
    """

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
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

    app, _ = await _build_app_for_spec(default_spec)

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
    # bridge_id != conv_id, stored on the ``omnigent.harnesses.claude_native.main
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
        ({"type": "model_change", "model": "claude-opus-4-7"}, "inject_model_command"),
    ],
    ids=["effort_change", "model_change"],
)
async def test_events_native_dispatch_resolves_bridge_id_via_label_lookup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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

    captured_bridge_dir: list[Any] = []

    def _fake_inject(bridge_dir: Any, **kwargs: Any) -> Any:
        """Record the bridge_dir the dispatch resolved."""
        captured_bridge_dir.append(bridge_dir)
        if "model" in kwargs:
            model = kwargs["model"]
            return claude_native_bridge.ClaudeModelSwitchResult(
                model, model, model, model, "native-session"
            )
        return None

    monkeypatch.setattr(claude_native_bridge, inject_attr, _fake_inject)

    sentinel_bridge_id = "bridge_from_fork_label_xyz"

    async def _fake_bridge_id_lookup(*, server_client: Any, session_id: str) -> str:
        """Pretend the session's bridge_id label is the sentinel."""
        del server_client, session_id
        return sentinel_bridge_id

    monkeypatch.setattr(
        native_controls,
        "_claude_native_bridge_id_for_session",
        _fake_bridge_id_lookup,
    )

    resp = await _post_claude_control(
        monkeypatch, tmp_path, _fake_inject, event_payload=event_payload
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


_BOOT_SESSION_ID = "68c7c1acc5eeec3978c5e62043da51a5"


async def _post_claude_control(
    monkeypatch: pytest.MonkeyPatch,
    config_home: Path,
    select: Callable[..., Any],
    *,
    model: str = "claude-opus-4-7",
    pane_status: str = "idle",
    catalog_rows: list[dict[str, object]] | None = None,
    event_payload: dict[str, Any] | None = None,
    before_control: Callable[[httpx.AsyncClient], Awaitable[None]] | None = None,
) -> httpx.Response:
    """Exercise the existing events boundary with an explicit picker result."""
    isolate_provider_resolution(monkeypatch, config_home)

    def relay_unavailable(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("Model-control test comment relay is unavailable")

    monkeypatch.setattr(claude_native_bridge, "start_tool_relay", relay_unavailable)
    native_create_calls: list[object] = []
    launch_calls: list[str] = []

    async def refuse_auto_create(*args: Any, **kwargs: Any) -> None:
        native_create_calls.append((args, kwargs))
        raise AssertionError("The model-control test must not enter native terminal creation")

    async def launch_stub(ctx: Any) -> SessionResourceView:
        launch_calls.append(ctx.session_id)
        return SessionResourceView(
            id="terminal_claude_main", type="terminal", session_id=ctx.session_id, name="claude"
        )

    for module in ("omnigent.runner.native", "omnigent.runner.native.orchestration"):
        monkeypatch.setattr(f"{module}._auto_create_claude_terminal", refuse_auto_create)
        monkeypatch.setattr(f"{module}._launch_claude", launch_stub)
    monkeypatch.setattr(claude_native_bridge, "inject_model_command", select)
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.main.resolve_native_claude_config", lambda **kwargs: None
    )
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.main.stored_claude_catalog_rows", lambda _: catalog_rows
    )
    app, _ = await _build_app_for_spec(_harness_spec("claude-native"))
    async with _runner_client(app) as client:
        created = await client.post(
            "/v1/sessions",
            json={"session_id": _BOOT_SESSION_ID, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert created.status_code == 201, created.text
        app.state.native_pane_status[_BOOT_SESSION_ID] = pane_status
        if before_control is not None:
            await before_control(client)
        response = await client.post(
            f"/v1/sessions/{_BOOT_SESSION_ID}/events",
            json=event_payload
            if event_payload is not None
            else {"type": "model_change", "model": model},
        )
    assert launch_calls == [_BOOT_SESSION_ID]
    assert not native_create_calls
    return response


@pytest.mark.parametrize("pane_status", ["idle", "running"], ids=["idle", "busy"])
async def test_events_model_change_requires_the_same_immediate_picker_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pane_status: str
) -> None:
    from omnigent.runner.app import _session_event_queues_ref

    calls: list[str] = []

    async def drain_creation_events(_client: httpx.AsyncClient) -> None:
        _drain_session_event_queue(_session_event_queues_ref.get(_BOOT_SESSION_ID))

    def select(_bridge: Path, *, model: str, timeout_s: float, ready_timeout_s: float) -> Any:
        calls.append(model)
        assert 0 < timeout_s <= native_controls._CLAUDE_CONTROL_BUDGET_S
        assert 0 < ready_timeout_s <= native_controls._CLAUDE_CONTROL_PROMPT_WAIT_S
        assert _injection_deadline() is not None
        return claude_native_bridge.ClaudeModelSwitchResult(
            model, model, model, model, "native-session"
        )

    response = await _post_claude_control(
        monkeypatch,
        tmp_path,
        select,
        pane_status=pane_status,
        before_control=drain_creation_events,
    )

    assert response.status_code == 204, response.text
    assert calls == ["claude-opus-4-7"]
    assert _drain_session_event_queue(_session_event_queues_ref.get(_BOOT_SESSION_ID)) == []


@pytest.mark.parametrize("receipt_kind", ["absent", "different-target"])
async def test_events_model_change_does_not_accept_old_status_or_ack_without_a_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    receipt_kind: str,
) -> None:
    monkeypatch.setattr(
        claude_native_bridge, "read_claude_status_model", lambda _: "claude-opus-4-7"
    )
    monkeypatch.setattr(claude_native_bridge, "model_switch_acknowledged", lambda *a, **k: True)

    def select(*args: Any, **kwargs: Any) -> Any:
        if receipt_kind == "absent":
            return None
        return claude_native_bridge.ClaudeModelSwitchResult(
            "another-model", "another-model", "another-model", "another-model", "native-session"
        )

    response = await _post_claude_control(monkeypatch, tmp_path, select)
    assert response.status_code == 503, response.text
    assert response.json()["error"] == "claude_native_model_failed"


async def test_events_model_change_metadata_failure_refuses_before_the_picker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_record(*args: Any, **kwargs: Any) -> None:
        raise OSError("private metadata-write details")

    monkeypatch.setattr(claude_native_bridge, "record_model_vocabulary", fail_record)
    response = await _post_claude_control(
        monkeypatch,
        tmp_path,
        lambda *a, **k: pytest.fail("The picker ran after its descriptor update failed"),
        catalog_rows=[{"id": "claude-opus-4-7", "model": "claude-opus-4-7"}],
    )
    assert response.status_code == 503, response.text
    assert response.json()["error"] == "claude_native_model_failed"
    assert "private metadata-write details" not in response.text


@pytest.mark.parametrize("stop", ["deadline", "cancellation"])
async def test_events_model_change_settles_held_metadata_lock_without_late_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stop: str
) -> None:
    import threading

    from omnigent.native import native_bridge_common

    started = threading.Event()
    finished = threading.Event()
    metadata: list[tuple[Path, bytes]] = []
    locks = ExitStack()
    record = claude_native_bridge.record_model_vocabulary
    monkeypatch.setattr(
        native_controls, "_CLAUDE_CONTROL_BUDGET_S", 0.2 if stop == "deadline" else 5.0
    )

    async def hold_metadata_lock(_client: httpx.AsyncClient) -> None:
        bridge_dir = claude_native_bridge.prepare_bridge_dir(_BOOT_SESSION_ID, workspace=tmp_path)
        config_path = bridge_dir / "bridge.json"
        metadata.append((config_path, config_path.read_bytes()))
        locks.enter_context(native_bridge_common.bridge_dir_preparation_lock(bridge_dir))

    def record_until_stopped(*args: Any, **kwargs: Any) -> None:
        try:
            assert _injection_deadline() is not None
            started.set()
            record(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(claude_native_bridge, "record_model_vocabulary", record_until_stopped)
    task = asyncio.create_task(
        _post_claude_control(
            monkeypatch,
            tmp_path,
            lambda *a, **k: pytest.fail("The picker ran after metadata preparation stopped"),
            catalog_rows=[{"id": "claude-opus-4-7", "model": "claude-opus-4-7"}],
            before_control=hold_metadata_lock,
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        if stop == "cancellation":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        else:
            response = await asyncio.wait_for(task, 2)
            assert response.status_code == 503, response.text
            assert response.json()["error"] == "claude_native_model_failed"
        assert finished.is_set()
        assert len(metadata) == 1
        config_path, original = metadata[0]
        assert config_path.read_bytes() == original
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        locks.close()
        if started.is_set():
            assert await asyncio.to_thread(finished.wait, 2)
    assert config_path.read_bytes() == original


@pytest.mark.parametrize("phase", ["preflight", "picker", "commit", "status"])
async def test_events_model_change_refuses_unsupported_queued_or_unconfirmed_states(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise claude_native_bridge.ClaudeModelSwitchRefused(
            "Session-only model selection refused; inspect the terminal before retrying.",
            phase=phase,
            unsupported=phase == "preflight",
            model_may_have_changed=phase in {"commit", "status"},
        )

    response = await _post_claude_control(monkeypatch, tmp_path, refuse, pane_status="running")

    assert response.status_code == 503, response.text
    if phase in {"commit", "status"}:
        assert response.json()["error"] == "claude_native_model_unconfirmed"
        assert "may have changed" in response.json()["detail"]
        assert "Check the terminal's current model before retrying" in response.json()["detail"]
    else:
        assert response.json()["error"] == (
            "claude_native_model_unsupported"
            if phase == "preflight"
            else "claude_native_model_failed"
        )
        assert "inspect the terminal" in response.json()["detail"]


@pytest.mark.parametrize("selected", ["private/model-b[large]", "private-claude-opus[1m]"])
async def test_bound_claude_switch_preserves_private_model_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, selected: str
) -> None:
    from omnigent.inference_config import inference_config_scope

    calls: list[str] = []

    def select(_bridge: Path, *, model: str, **kwargs: Any) -> Any:
        calls.append(model)
        return claude_native_bridge.ClaudeModelSwitchResult(
            model, model, model, model, "native-session"
        )

    config = {
        "providers": {"gateway": {"kind": "gateway"}},
        "inference": {
            "harnesses": {
                "claude-native": {
                    "provider": "gateway",
                    "default_model": selected,
                    "model_allowlist": [selected],
                }
            }
        },
    }
    with inference_config_scope(config):
        response = await _post_claude_control(monkeypatch, tmp_path, select, model=selected)

    assert response.status_code == 204, response.text
    assert calls == [selected]


@pytest.mark.parametrize("catalog_source", ["cached", "stored"])
async def test_events_empty_catalog_overrides_stale_bridge_picker_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    catalog_source: str,
) -> None:
    stale_model = "system.ai.glm-5-3"
    stale_options: list[dict[str, object]] = [
        {
            "value": stale_model,
            "model": stale_model,
            "display_name": "GLM",
            "enabled": True,
            "provenance": "structured",
        }
    ]
    stale_rows: list[dict[str, object]] = [
        {"id": stale_model, "model": stale_model, "_claude_picker_options": stale_options}
    ]
    calls: list[str] = []

    async def empty_catalog(_config: Any) -> list[dict[str, object]]:
        return []

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.main.claude_launch_catalog", empty_catalog
    )

    async def prepare_stale_bridge(client: httpx.AsyncClient) -> None:
        bridge_dir = claude_native_bridge.prepare_bridge_dir(
            _BOOT_SESSION_ID,
            workspace=tmp_path,
            picker_values=[stale_model],
            picker_options=stale_options,
        )
        if catalog_source == "cached":
            listing = await client.get(f"/v1/sessions/{_BOOT_SESSION_ID}/claude-model-options")
            assert listing.status_code == 200, listing.text
            assert listing.json() == {"models": []}
            # The POST must use the empty cache even if bridge metadata has gone stale.
            claude_native_bridge.record_model_vocabulary(
                bridge_dir,
                launch_env=None,
                launch_model=None,
                picker_values=[stale_model],
                picker_options=stale_options,
            )
        assert claude_native_bridge.read_model_picker_values(bridge_dir) == [stale_model]
        assert claude_native_bridge.read_model_picker_options(bridge_dir) == stale_options

    def refuse(bridge_dir: Path, *, model: str, **kwargs: Any) -> None:
        calls.append(model)
        assert claude_native_bridge.read_model_picker_values(bridge_dir) == []
        assert claude_native_bridge.read_model_picker_options(bridge_dir) == []
        raise claude_native_bridge.ClaudeModelSwitchRefused(
            "The requested model is absent from the session's picker catalog.",
            phase="preflight",
            unsupported=True,
        )

    response = await _post_claude_control(
        monkeypatch,
        tmp_path,
        refuse,
        model=stale_model,
        catalog_rows=stale_rows if catalog_source == "cached" else [],
        before_control=prepare_stale_bridge,
    )

    assert response.status_code == 503, response.text
    assert response.json()["error"] == "claude_native_model_unsupported"
    assert calls == [stale_model]


_PASSWORD_PANE_TAIL = (
    " Last terminal output:\n"
    "✔ Unity Gateway connected\n"
    "• Enter password to configure settings for Claude Code.\n"
    "Password:"
)


@pytest.mark.parametrize(
    ("error", "expected_detail", "waiting_on_person"),
    [
        pytest.param(
            claude_native_bridge.ClaudePromptTimeout(
                "Claude Code terminal did not become ready within 9.0s. "
                "The message was not delivered." + _PASSWORD_PANE_TAIL
            ),
            "Claude Code's input box never appeared. It may still be starting, or waiting "
            "on a prompt such as a password. Check the terminal, then switch models again."
            + _PASSWORD_PANE_TAIL,
            False,
            id="launcher-password-prompt",
        ),
        pytest.param(
            claude_native_bridge.ClaudeSignInPending(
                "Claude Code is waiting for a sign-in in this session's terminal.",
                title="Claude Code can't start until you sign in to Databricks",
                remediation="Open the sign-in link and sign in.",
            ),
            "Claude Code can't start until you sign in to Databricks. "
            "Finish the sign-in in the terminal, then switch models again.",
            True,
            id="sign-in",
        ),
        pytest.param(
            claude_native_bridge.ClaudeUserPromptPending(
                "Answer the pending Claude question or permission request before "
                "changing settings."
            ),
            "Claude is waiting for an answer to a question or prompt. "
            "Answer it, then switch models again.",
            True,
            id="permission-prompt",
        ),
        pytest.param(
            claude_native_bridge.ClaudeTerminalDialog(
                "Claude Code is waiting for an answer in its terminal (Trust this folder?), "
                "so the message was not delivered." + _PASSWORD_PANE_TAIL
            ),
            "Claude Code's terminal is showing a prompt or dialog. Open the terminal, "
            "answer or dismiss it, then switch models again." + _PASSWORD_PANE_TAIL,
            True,
            id="dialog",
        ),
        pytest.param(
            claude_native_bridge.ClaudeTerminalExited(
                "The Claude Code terminal has exited (status 1)." + _PASSWORD_PANE_TAIL,
                exit_status="1",
            ),
            "The Claude Code terminal has exited. Relaunch it, then switch models again."
            + _PASSWORD_PANE_TAIL,
            False,
            id="exited",
        ),
    ],
)
async def test_events_model_change_names_the_step_a_blocked_terminal_needs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: Exception,
    expected_detail: str,
    waiting_on_person: bool,
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise claude_native_bridge.ClaudeModelSwitchRefused(str(error)) from error

    response = await _post_claude_control(monkeypatch, tmp_path, refuse)

    assert response.status_code == 503, response.text
    expected: dict[str, object] = {
        "error": "claude_native_model_failed",
        "detail": expected_detail,
    }
    if waiting_on_person:
        expected["waiting_on_person"] = True
    assert response.json() == expected


async def test_cancelled_model_control_drains_the_worker_without_late_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    keys: list[str] = []

    def operation() -> None:
        started.set()
        try:
            assert release.wait(2)
            claude_native_bridge._check_injection_cancelled()
            keys.append("late s")
        finally:
            finished.set()

    deadline = native_controls._ClaudeControlDeadline()
    task = asyncio.create_task(native_controls._drive_claude_pane(deadline, operation))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set() and deadline.is_set()
        assert keys == []
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _injection_deadline() -> float | None:
    """The instant the bridge call running now stops sending keys at, if bound."""
    flag = claude_native_bridge._INJECTION_CANCEL_EVENT.get()
    return flag.at if isinstance(flag, native_controls._ClaudeControlDeadline) else None


def test_claude_control_deadline_wait_returns_once_the_budget_lapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``wait`` cannot block past the budget, so a blocked waiter sees the deadline too."""
    monkeypatch.setattr(native_controls, "_CLAUDE_CONTROL_BUDGET_S", 0.2)
    deadline = native_controls._ClaudeControlDeadline()

    started = time.monotonic()
    assert deadline.wait() is True
    assert deadline.wait(5.0) is True
    assert time.monotonic() - started < 2.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [{"type": "effort_change", "effort": "high"}, {"type": "compact"}],
    ids=["effort_change", "compact"],
)
async def test_events_settings_controls_cap_their_prompt_wait(
    monkeypatch: pytest.MonkeyPatch,
    event: dict[str, Any],
) -> None:
    """Effort and compact stop waiting for a booting prompt inside the server's budget.

    Uncapped, a live-but-booting pane earns the slow-boot extension (up to
    180s), far past the ~20s the server waits for these controls. The whole
    injection also stops sending keys once that budget is spent, so a slow
    submit or dialog cannot apply the control after the server reported it failed.
    """
    caps: list[float | None] = []
    key_deadlines: list[float | None] = []

    def _fake_inject(
        bridge_dir: Any, *, command: str, ready_timeout_s: float | None = None, **_kwargs: Any
    ) -> None:
        del bridge_dir, command
        caps.append(ready_timeout_s)
        key_deadlines.append(_injection_deadline())

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)
    app, _ = await _build_app_for_spec(_harness_spec("claude-native"))
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": _BOOT_SESSION_ID, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        resp = await client.post(f"/v1/sessions/{_BOOT_SESSION_ID}/events", json=event)

    assert resp.status_code in (200, 204), resp.text
    assert caps == [pytest.approx(native_controls._CLAUDE_CONTROL_PROMPT_WAIT_S)]
    (key_deadline,) = key_deadlines
    assert key_deadline is not None
    assert key_deadline - time.monotonic() == pytest.approx(
        native_controls._CLAUDE_CONTROL_BUDGET_S, abs=5.0
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

    captured: list[Any] = []

    def _fake_inject(bridge_dir: Any, *, model: str, timeout_s: float) -> None:
        """Record the call and return without touching tmux."""
        captured.append((bridge_dir, model, timeout_s))

    monkeypatch.setattr(kiro_native_bridge, "inject_model_command", _fake_inject)

    native_spec = _harness_spec("kiro-native")

    app, _ = await _build_app_for_spec(native_spec)

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
    tmp_path: Path,
    model_value: str | None,
) -> None:
    """
    Null / empty / whitespace-only model values 204 without typing.

    Pins that the empty-value validation lives in the runner native
    handler, not in the Omnigent server.
    """

    def _fake_inject(*args: Any, **kwargs: Any) -> None:
        """Fail the test if the runner reaches inject for an empty value."""
        raise AssertionError(
            f"inject_model_command must not be called for model={model_value!r}; "
            f"the native handler should skip empty / null values."
        )

    resp = await _post_claude_control(
        monkeypatch,
        tmp_path,
        _fake_inject,
        event_payload={"type": "model_change", "model": model_value},
    )

    assert resp.status_code == 204, (
        f"Native model_change with empty / null value must return "
        f"204 (no-op); got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_events_model_change_on_native_session_returns_503_when_bridge_not_ready(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A bridge failure returns 503 with controlled client detail."""

    def _fake_inject(
        bridge_dir: Any,
        *,
        model: str,
        timeout_s: float,
        ready_timeout_s: float | None = None,
    ) -> None:
        """Simulate the bridge-not-ready path."""
        raise RuntimeError("tmux target is not advertised")

    resp = await _post_claude_control(monkeypatch, tmp_path, _fake_inject)

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
    tmp_path: Path,
) -> None:
    """
    Non-native sessions accept model_change and 204 without side effects.

    In-process harnesses re-read the persisted ``model_override`` on
    each turn (or via the per-event override). Omnigent server is harness-
    agnostic and POSTs model_change for every PATCH, so the runner
    must accept the event with a 204 — never reach the slash-command
    injector.
    """

    isolate_provider_resolution(monkeypatch, tmp_path)

    def _fake_inject(*args: Any, **kwargs: Any) -> None:
        """Fail the test if a non-native session reaches the injector."""
        raise AssertionError(
            "Claude model selection must never run for non-native "
            "sessions — model_change is a no-op for in-process harnesses."
        )

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)
    monkeypatch.setattr(claude_native_bridge, "inject_model_command", _fake_inject)

    # Default harness (in-process LLM loop), NOT claude-native.
    default_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={}),
    )

    app, _ = await _build_app_for_spec(default_spec)

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

    captured: list[tuple[Any, str, str | None, float]] = []

    def _fake_inject(
        bridge_dir: Any,
        *,
        model: str,
        expected_display_name: str | None,
        timeout_s: float,
    ) -> None:
        """Record the call and return without touching tmux."""
        captured.append((bridge_dir, model, expected_display_name, timeout_s))

    monkeypatch.setattr(cursor_native_bridge, "inject_model_command", _fake_inject)

    native_spec = _harness_spec("cursor-native")

    app, _ = await _build_app_for_spec(native_spec)

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
    _bridge_dir, model, expected_display_name, timeout_s = captured[0]
    assert model == "gpt-5.2", f"Expected the model id passed through, got {model!r}."
    assert expected_display_name is None
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

    def _fake_inject(bridge_dir: Any, *, model: str, timeout_s: float) -> None:
        """Fail the test if the runner reaches inject for an empty value."""
        del bridge_dir, timeout_s
        raise AssertionError(f"inject_model_command must not be called for model={model_value!r}.")

    monkeypatch.setattr(cursor_native_bridge, "inject_model_command", _fake_inject)

    native_spec = _harness_spec("cursor-native")

    app, _ = await _build_app_for_spec(native_spec)

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

    def _fake_inject(
        bridge_dir: Any,
        *,
        model: str,
        expected_display_name: str | None,
        timeout_s: float,
    ) -> None:
        """Simulate the bridge-not-ready path."""
        del bridge_dir, model, expected_display_name, timeout_s
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr(cursor_native_bridge, "inject_model_command", _fake_inject)

    native_spec = _harness_spec("cursor-native")

    app, _ = await _build_app_for_spec(native_spec)

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

    native_spec = _harness_spec("cursor-native")

    app, _ = await _build_app_for_spec(native_spec)

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


def _body_carries_text(body: dict[str, Any], needle: str) -> bool:
    """Return whether *needle* appears in any ``input_text`` block of *body*.

    The runner forwards history as nested ``message`` items, so the
    ``input_text`` block carrying the text can sit one or more levels deep
    inside ``content`` lists; walk them recursively.

    :param body: A harness request body (from ``posted_bodies``).
    :param needle: Substring to search for in ``input_text`` blocks.
    :returns: ``True`` if any ``input_text`` block contains *needle*.
    """

    def _walk(node: Any) -> bool:
        if isinstance(node, dict):
            if node.get("type") == "input_text" and needle in (node.get("text") or ""):
                return True
            return _walk(node.get("content"))
        if isinstance(node, list):
            return any(_walk(child) for child in node)
        return False

    return _walk(body.get("content"))


@pytest.mark.asyncio
async def test_events_compact_on_claude_sdk_session_dispatches_compact_turn() -> None:
    """
    POST ``/events`` with ``{"type":"compact"}`` on a claude-sdk session
    dispatches a ``/compact`` turn to the harness and returns 200.

    The Claude SDK owns its own context window in the harness subprocess;
    the only effective way to compact it is to send the literal ``/compact``
    slash command to the live client, which triggers native compaction (the
    same PreCompact path the auto-compact machinery already handles and whose
    ``response.compaction.completed`` the executor already emits). The runner
    turns the compact control into a resumed ``/compact`` turn and forwards it
    to the harness like any user message.

    The 200 (not 204) is load-bearing: the Omnigent server reads it to know
    the harness handled the control and skips its own (transcript-only,
    ineffective for SDK) compaction. A regression returning 204 would make
    the server surface a 400 "not available for this session type" error.
    """
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]
    hc = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(hc)

    sdk_spec = _harness_spec("claude-sdk")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the claude-sdk spec for any agent_id."""
        del agent_id, session_id
        return sdk_spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    sid = "b1d0f6a2c3e14f5a9b8c7d6e5f403122"
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": sid, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text

        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={"type": "compact"},
        )

        # The turn runs in the background; wait for it to forward the
        # synthesized /compact message to the harness.
        for _ in range(100):
            if hc.posted_bodies:
                break
            await asyncio.sleep(0.02)

    # 200 = handled by the harness (server skips its own compaction).
    # 204 would make the server return 400 "not available for this
    # session type" — the regression this pins against.
    assert resp.status_code == 200, (
        f"claude-sdk compact must return 200 (handled) from /events; "
        f"got {resp.status_code}: {resp.text}"
    )
    # Exactly one forwarded turn. 0 = compact didn't dispatch; 2+ = it
    # dispatched more than a single compact turn.
    assert len(hc.posted_bodies) == 1, (
        f"Expected exactly one forwarded turn from claude-sdk compact, "
        f"got {len(hc.posted_bodies)}: {hc.posted_bodies}"
    )
    # Body contract: the literal ``/compact`` is what the Claude CLI
    # interprets as the slash command that runs native compaction. Plain
    # prompt text (missing slash) would land as a normal chat turn instead.
    assert _body_carries_text(hc.posted_bodies[0], "/compact"), (
        f"The forwarded turn must carry the literal '/compact' slash command "
        f"so the SDK runs native compaction; got {hc.posted_bodies[0]}"
    )


@pytest.mark.asyncio
async def test_events_compact_on_claude_sdk_buffers_behind_active_turn() -> None:
    """
    Compact on a busy claude-sdk session buffers ``/compact`` for the next
    turn instead of starting a second concurrent one.

    The harness holds a single live client per conversation, so a ``/compact``
    issued while a turn is in flight must not race it. The runner buffers the
    synthesized ``/compact`` message; the normal continuation drain runs it
    once the active turn ends. It still returns 200 (handled) so the server
    skips its own compaction.
    """
    hc = _ScriptedHarnessClient([])
    pm = _FakeProcessManager(hc)

    sdk_spec = _harness_spec("claude-sdk")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the claude-sdk spec for any agent_id."""
        del agent_id, session_id
        return sdk_spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    sid = "c2e1a7b3d4f05a6b8c9d0e1f20314233"
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": sid, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text

        # Simulate an in-flight turn holding the slot (a non-Task sentinel so
        # nothing tries to await/cancel it during teardown).
        active_turns = app.state.active_turns
        sentinel = object()
        active_turns[sid] = sentinel

        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={"type": "compact"},
        )

    # 200 = handled (buffered for the next turn); the server skips its own
    # compaction just as it would for an immediately-dispatched compact.
    assert resp.status_code == 200, (
        f"claude-sdk compact on a busy session must still return 200; "
        f"got {resp.status_code}: {resp.text}"
    )
    # The active slot must be untouched — no second turn was started.
    assert app.state.active_turns.get(sid) is sentinel, (
        "compact must not start a second turn while one is in flight; the "
        "active-turn slot was replaced."
    )
    # The /compact message was buffered for the continuation drain.
    buffered = app.state.session_message_buffers.get(sid) or []
    assert len(buffered) == 1, f"Expected exactly one buffered /compact message, got {buffered!r}."
    assert _body_carries_text(buffered[0], "/compact"), (
        f"Buffered message must carry the literal '/compact'; got {buffered[0]!r}."
    )
    # Nothing was forwarded to the harness yet — it runs after the active turn.
    assert hc.posted_bodies == [], (
        f"Buffered compact must not forward to the harness immediately; got {hc.posted_bodies!r}."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", ["active_turn", "buffered_message", "ingesting", None])
async def test_events_clear_on_claude_sdk_releases_the_idle_client(
    busy: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A server-coordinated ``clear`` drops an idle claude-sdk session's client and history.

    The server continues the work in a fresh session, so the old session's
    SDK client must not keep the old context warm. A turn in flight, or a
    message waiting for one, refuses with 409 and leaves everything intact.
    A clear that arrives while a message is still being ingested waits for
    it, then sees the turn it started.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.app import _session_histories_ref

    app, pm = await _build_app_for_spec(_harness_spec("claude-sdk"))
    ingest_held, release_ingest = asyncio.Event(), asyncio.Event()
    finish_turn = asyncio.Event()
    if busy == "ingesting":
        resolve_content = runner_app._resolve_forwarded_message_content

        async def held_ingest(*args: Any, **kwargs: Any) -> Any:
            ingest_held.set()
            await release_ingest.wait()
            return await resolve_content(*args, **kwargs)

        monkeypatch.setattr(runner_app, "_resolve_forwarded_message_content", held_ingest)
        stream = pm._client.stream

        def held_stream(*args: Any, **kwargs: Any) -> Any:
            opened = stream(*args, **kwargs)

            class _HeldTurn:
                status_code = 200

                async def __aenter__(self) -> Any:
                    await finish_turn.wait()
                    return await opened.__aenter__()

                async def __aexit__(self, *exc: Any) -> None:
                    await opened.__aexit__(*exc)

            return _HeldTurn()

        monkeypatch.setattr(pm._client, "stream", held_stream)
    sid = "d3f2b8c4e5a16b7c9d0e1f2a31425344"
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": sid, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        history = [{"type": "message", "role": "user", "content": "earlier"}]
        _session_histories_ref[sid] = history
        if busy == "active_turn":
            app.state.active_turns[sid] = object()
        elif busy == "buffered_message":
            app.state.session_message_buffers[sid] = [{"type": "message"}]
        message = None
        if busy == "ingesting":
            message = asyncio.create_task(
                client.post(
                    f"/v1/sessions/{sid}/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "next"}],
                    },
                )
            )
            await asyncio.wait_for(ingest_held.wait(), timeout=5)

        clear = asyncio.create_task(
            client.post(
                f"/v1/sessions/{sid}/events",
                json={"type": "clear", "continue_in_new_session": True},
            )
        )
        if message is not None:
            # The clear queues behind the ingest instead of releasing under it.
            await asyncio.sleep(0.05)  # custom-lint: disable=sleep-then-assert -- non-event
            assert not clear.done() and pm.released == []
            release_ingest.set()
            assert (await asyncio.wait_for(message, timeout=5)).status_code < 300
        resp = await asyncio.wait_for(clear, timeout=5)
        finish_turn.set()
        turn = app.state.active_turns.get(sid) if message is not None else None
        if turn is not None:
            await asyncio.wait_for(asyncio.shield(turn), timeout=5)

    try:
        if busy is None:
            assert resp.status_code == 200, resp.text
            assert sid in pm.released
            assert sid not in _session_histories_ref
        else:
            assert resp.status_code == 409, resp.text
            assert resp.json()["error"] == "claude_sdk_clear_busy"
            assert sid not in pm.released
            assert _session_histories_ref.get(sid) is history
    finally:
        _session_histories_ref.pop(sid, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("queued_ahead", ["message", "held_ticket"])
async def test_cancelled_claude_sdk_clear_does_not_block_later_input(
    monkeypatch: pytest.MonkeyPatch, queued_ahead: str
) -> None:
    """A clear cancelled while queued behind an ingest leaves the gate usable.

    Tunnel teardown cancels in-flight requests. The cancelled clear's place in
    the ingest gate is given up once the message ahead of it finishes, so the
    next input for the session still runs. With an earlier ticket that is
    never served still ahead of it, the clear's retirement waits; deleting the
    session still ends it.
    """
    from omnigent.runner import app as runner_app

    gate: dict[str, Any] = {}
    build = runner_app.build_native_controls

    def keep_gate(**kwargs: Any) -> Any:
        gate["next_seq"] = kwargs["_ingest_next_seq"]
        return build(**kwargs)

    monkeypatch.setattr(runner_app, "build_native_controls", keep_gate)
    app, pm = await _build_app_for_spec(_harness_spec("claude-sdk"))
    sid = "f5b4cad6e7f38d9e1f2a3b4c53647566"
    ingest_held, release_ingest = asyncio.Event(), asyncio.Event()
    resolve_content = runner_app._resolve_forwarded_message_content

    async def held_ingest(*args: Any, **kwargs: Any) -> Any:
        if not ingest_held.is_set():
            ingest_held.set()
            await release_ingest.wait()
        return await resolve_content(*args, **kwargs)

    monkeypatch.setattr(runner_app, "_resolve_forwarded_message_content", held_ingest)
    message = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "next"}],
    }
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": sid, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        ahead = asyncio.create_task(client.post(f"/v1/sessions/{sid}/events", json=message))
        await asyncio.wait_for(ingest_held.wait(), timeout=5)
        held_ticket = queued_ahead == "held_ticket"
        if held_ticket:
            # An input between the message and the clear holds its place unserved.
            gate["next_seq"][sid] += 1
        clear = asyncio.create_task(
            client.post(
                f"/v1/sessions/{sid}/events",
                json={"type": "clear", "continue_in_new_session": True},
            )
        )
        for _ in range(20):  # Let the clear take its place in line.
            await asyncio.sleep(0)  # custom-lint: disable=sleep-then-assert -- queue it
        clear.cancel()
        with pytest.raises(asyncio.CancelledError):
            await clear
        release_ingest.set()
        assert (await asyncio.wait_for(ahead, timeout=5)).status_code < 300
        if held_ticket:
            retiring = [
                task for task in asyncio.all_tasks() if task.get_name() == f"retire-ingest-{sid}"
            ]
            assert retiring and not any(task.done() for task in retiring)
            deleted = await client.delete(f"/v1/sessions/{sid}")
            assert deleted.status_code == 200, deleted.text
            await asyncio.wait_for(asyncio.gather(*retiring), timeout=5)
            return
        turn = app.state.active_turns.get(sid)
        if turn is not None:
            await asyncio.wait_for(asyncio.shield(turn), timeout=5)

        later = await asyncio.wait_for(
            client.post(
                f"/v1/sessions/{sid}/events",
                json={"type": "clear", "continue_in_new_session": True},
            ),
            timeout=5,
        )
        followed = await asyncio.wait_for(
            client.post(
                f"/v1/sessions/{sid}/events",
                json={**message, "content": [{"type": "input_text", "text": "after the clear"}]},
            ),
            timeout=5,
        )
        turn = app.state.active_turns.get(sid)
        if turn is not None:
            await asyncio.wait_for(asyncio.shield(turn), timeout=5)

    assert later.status_code == 200, later.text
    assert sid in pm.released
    # A message after it still reaches the harness.
    assert followed.status_code < 300, followed.text
    assert any("after the clear" in json.dumps(body) for body in pm._client.posted_bodies)


@pytest.mark.asyncio
async def test_queued_claude_sdk_clear_ends_when_its_session_is_deleted_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clear whose turn comes just as its session is deleted does not strand.

    Ordering: the input ahead finishes and passes the gate to the clear, then
    the session is deleted (dropping its gate) before the clear resumes. The
    clear must notice its gate is gone, answer, and not recreate the gate.
    """
    from omnigent.runner import app as runner_app

    gate: dict[str, Any] = {}
    build = runner_app.build_native_controls

    def keep_gate(**kwargs: Any) -> Any:
        gate.update(
            cond=kwargs["_ingest_cond"],
            next_seq=kwargs["_ingest_next_seq"],
            now_serving=kwargs["_ingest_now_serving"],
        )
        return build(**kwargs)

    monkeypatch.setattr(runner_app, "build_native_controls", keep_gate)
    app, _pm = await _build_app_for_spec(_harness_spec("claude-sdk"))
    sid = "b7c6dce8f9a05b1c2d3e4f5a67584930"
    async with _runner_client(app) as client:
        created = await client.post(
            "/v1/sessions",
            json={"session_id": sid, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert created.status_code == 201, created.text
        # An input ahead holds ticket 0.
        gate["next_seq"][sid] = 1
        cond = gate["cond"].setdefault(sid, asyncio.Condition())
        clear = asyncio.create_task(
            client.post(
                f"/v1/sessions/{sid}/events",
                json={"type": "clear", "continue_in_new_session": True},
            )
        )
        for _ in range(20):  # Let the clear take ticket 1 and wait.
            await asyncio.sleep(0)  # custom-lint: disable=sleep-then-assert -- queue it
        assert gate["next_seq"][sid] == 2 and not clear.done()

        # The input ahead finishes and passes the gate to the clear ...
        async with cond:
            gate["now_serving"][sid] = 1
            cond.notify_all()
        # ... and, before the clear resumes, deletion drops the session's gate.
        gate["now_serving"].pop(sid, None)
        gate["next_seq"].pop(sid, None)
        gate["cond"].pop(sid, None)
        async with cond:
            cond.notify_all()

        answered = await asyncio.wait_for(clear, timeout=5)

    assert answered.status_code == 410, answered.text
    assert sid not in gate["now_serving"] and sid not in gate["next_seq"]
    assert sid not in gate["cond"]


@pytest.mark.asyncio
async def test_deleting_a_session_the_runner_never_set_up_is_harmless() -> None:
    """The server's rollback of an unconfirmed continuation can repeat its delete."""
    app, _pm = await _build_app_for_spec(_harness_spec("claude-sdk"))
    async with _runner_client(app) as client:
        responses = [
            await client.delete("/v1/sessions/a6b5cbd7e8f49a0b1c2d3e4f56473829") for _ in range(2)
        ]
    assert [response.status_code for response in responses] == [200, 200]


@pytest.mark.asyncio
async def test_events_legacy_clear_on_claude_sdk_stays_unsupported() -> None:
    """
    An unmarked ``clear`` (an older server's) keeps claude-sdk's unsupported 204.

    An older server treats 200 as a completed clear without moving the work
    to a new session, so the next message would restore the old context.
    The runner must leave the client and history alone and answer 204.
    """
    from omnigent.runner.app import _session_histories_ref

    app, pm = await _build_app_for_spec(_harness_spec("claude-sdk"))
    sid = "e4a3c9d5f6b27c8d0e1f2a3b42536455"
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": sid, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        history = [{"type": "message", "role": "user", "content": "earlier"}]
        _session_histories_ref[sid] = history

        resp = await client.post(f"/v1/sessions/{sid}/events", json={"type": "clear"})

    try:
        assert resp.status_code == 204, resp.text
        assert sid not in pm.released
        assert _session_histories_ref.get(sid) is history
    finally:
        _session_histories_ref.pop(sid, None)


async def _accumulate_session_events(
    sid: str,
    *,
    until_types: set[str],
    timeout_s: float = 3.0,
) -> list[dict[str, Any]]:
    """Drain a session's event queue repeatedly until a terminal type appears.

    The compact turn runs in the background, so its events land on the queue
    over time. Accumulate across polls (the drain is destructive) and stop once
    any of ``until_types`` is seen or the timeout elapses.

    :param sid: Conversation id whose queue to drain.
    :param until_types: Event ``type`` values that end the wait.
    :param timeout_s: Upper bound on how long to poll.
    :returns: Every dict event seen, in arrival order.
    """
    from omnigent.runner.app import _session_event_queues_ref

    seen: list[dict[str, Any]] = []
    waited = 0.0
    while waited < timeout_s:
        seen.extend(_drain_session_event_queue(_session_event_queues_ref.get(sid)))
        if any(e.get("type") in until_types for e in seen):
            return seen
        await asyncio.sleep(0.02)
        waited += 0.02
    return seen


def _build_claude_sdk_compact_app(
    sse_frames: list[str],
) -> tuple[Any, _FakeProcessManager, _ScriptedHarnessClient]:
    """Wire a runner app whose claude-sdk turn streams *sse_frames*.

    :param sse_frames: The SSE frames the scripted harness relays for the
        dispatched ``/compact`` turn.
    :returns: ``(app, process_manager, harness_client)``.
    """
    hc = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(hc)

    sdk_spec = _harness_spec("claude-sdk")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return sdk_spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    return app, pm, hc


@pytest.mark.asyncio
async def test_events_compact_on_claude_sdk_publishes_in_progress_up_front() -> None:
    """A claude-sdk `/compact` publishes `response.compaction.in_progress` at once.

    Native `/compact` shows a "Compacting conversation…" spinner immediately;
    the claude-sdk turn otherwise only surfaces the generic running status
    ("Working…") until the executor's own late `in_progress` (fired when the
    SDK PreCompact hook hits). Publishing up front gives the same instant
    feedback as the other harnesses.
    """
    frames = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]
    app, _pm, _hc = _build_claude_sdk_compact_app(frames)

    sid = "d3f0a1b2c3d4e5f60718293a4b5c6d7e"
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": sid, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        from omnigent.runner.app import _session_event_queues_ref as _queues

        _drain_session_event_queue(_queues.get(sid))

        resp = await client.post(f"/v1/sessions/{sid}/events", json={"type": "compact"})
        assert resp.status_code == 200, resp.text

        events = await _accumulate_session_events(
            sid, until_types={"response.compaction.in_progress"}
        )

    in_progress = [e for e in events if e.get("type") == "response.compaction.in_progress"]
    assert in_progress, (
        f"claude-sdk compact must publish response.compaction.in_progress up front so the "
        f"'Compacting conversation…' spinner shows immediately; got events {events!r}"
    )


@pytest.mark.asyncio
async def test_events_compact_on_claude_sdk_swallows_executor_duplicate_in_progress() -> None:
    """The executor's own `in_progress` is swallowed so the web shows one spinner.

    The runner publishes an up-front `in_progress`; the executor then emits its
    own when the PreCompact hook fires. Two spinners would leave one stranded
    (the web removes only a single `compaction_loading` on `completed`), so the
    relay must drop the executor's duplicate. On a real compaction (a
    `completed` arrives), no `failed` is published.
    """
    frames = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse({"type": "response.compaction.in_progress"}),
        _sse(
            {
                "type": "response.compaction.completed",
                "summary": "compacted",
                "total_tokens": 1234,
                "compacted_messages": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hi"}],
                    }
                ],
            }
        ),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]
    app, _pm, _hc = _build_claude_sdk_compact_app(frames)

    sid = "e4a1b2c3d4e5f60718293a4b5c6d7e8f"
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": sid, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        from omnigent.runner.app import _session_event_queues_ref as _queues

        _drain_session_event_queue(_queues.get(sid))

        resp = await client.post(f"/v1/sessions/{sid}/events", json={"type": "compact"})
        assert resp.status_code == 200, resp.text

        events = await _accumulate_session_events(
            sid, until_types={"response.compaction.completed"}
        )

    in_progress = [e for e in events if e.get("type") == "response.compaction.in_progress"]
    completed = [e for e in events if e.get("type") == "response.compaction.completed"]
    failed = [e for e in events if e.get("type") == "response.compaction.failed"]
    assert len(in_progress) == 1, (
        f"Exactly one in_progress must reach the web (up-front published, executor's "
        f"duplicate swallowed); got {len(in_progress)}: {events!r}"
    )
    assert len(completed) == 1, f"Expected the completed event to pass through; got {events!r}"
    assert failed == [], (
        f"No failed must be published when compaction actually completes; got {events!r}"
    )


@pytest.mark.asyncio
async def test_events_compact_on_claude_sdk_publishes_failed_when_no_compaction() -> None:
    """When the `/compact` turn ends with no compaction, publish `failed`.

    The up-front spinner is only cleared by `completed` (→ marker) or `failed`
    (→ removed). If the turn ends without the executor reporting a compaction
    (e.g. nothing to compact), the runner must publish `failed` so the spinner
    is not stranded.
    """
    frames = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]
    app, _pm, _hc = _build_claude_sdk_compact_app(frames)

    sid = "f5b2c3d4e5f60718293a4b5c6d7e8f90"
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": sid, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        from omnigent.runner.app import _session_event_queues_ref as _queues

        _drain_session_event_queue(_queues.get(sid))

        resp = await client.post(f"/v1/sessions/{sid}/events", json={"type": "compact"})
        assert resp.status_code == 200, resp.text

        events = await _accumulate_session_events(sid, until_types={"response.compaction.failed"})

    failed = [e for e in events if e.get("type") == "response.compaction.failed"]
    completed = [e for e in events if e.get("type") == "response.compaction.completed"]
    assert failed, (
        f"A compact turn that ends without a compaction must publish failed to clear the "
        f"stranded spinner; got {events!r}"
    )
    assert completed == [], f"No completed should appear when nothing compacted; got {events!r}"


@pytest.mark.asyncio
async def test_events_compact_on_claude_sdk_clears_flag_on_pre_stream_failure() -> None:
    """A `/compact` turn that fails before streaming still clears the spinner.

    The up-front `response.compaction.in_progress` and the in-progress flag are
    set before the turn task runs, but the loop that clears them runs only once
    streaming starts. A setup-phase failure (here: no live harness) ends the turn
    before that loop, so cleanup cannot live only inside the stream loop:
    `_on_proxy_stream_end` — reached on every turn-end path — must publish
    `failed` and discard the flag. Otherwise the spinner is stranded and the
    stale flag corrupts the NEXT turn's compaction signalling (a real
    `in_progress` is swallowed, or an unrelated turn emits a spurious `failed`).
    """

    class _FailingProcessManager(_FakeProcessManager):
        """Process manager that fails to hand out a client once armed."""

        def __init__(self, client: _ScriptedHarnessClient) -> None:
            super().__init__(client)
            self.fail_get_client = False

        async def get_client(
            self, conversation_id: str, harness: str, env: Any = None
        ) -> _ScriptedHarnessClient:
            if self.fail_get_client:
                raise RuntimeError("no live harness")
            return await super().get_client(conversation_id, harness, env)

    hc = _ScriptedHarnessClient([])
    pm = _FailingProcessManager(hc)
    sdk_spec = _harness_spec("claude-sdk")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return sdk_spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    sid = "a6c3d4e5f60718293a4b5c6d7e8f9012"
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": sid, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        from omnigent.runner.app import _session_event_queues_ref as _queues

        _drain_session_event_queue(_queues.get(sid))

        # Arm the setup-phase failure, then dispatch /compact. The handler still
        # returns 200 (the turn runs in the background); the failure lands inside
        # the turn task, before any streaming.
        pm.fail_get_client = True
        resp = await client.post(f"/v1/sessions/{sid}/events", json={"type": "compact"})
        assert resp.status_code == 200, resp.text

        events = await _accumulate_session_events(sid, until_types={"response.compaction.failed"})

    failed = [e for e in events if e.get("type") == "response.compaction.failed"]
    assert failed, (
        f"A /compact turn that fails during setup (before streaming) must still publish "
        f"failed to clear the up-front spinner; got {events!r}"
    )
    # The flag must not leak past the turn: a stale entry would swallow the next
    # turn's real in_progress or make an unrelated turn emit a spurious failed.
    assert sid not in app.state.sdk_compact_inprogress, (
        "the in-progress flag must be cleared on the setup-failure path; a leaked "
        "entry corrupts later compaction signalling for this conversation."
    )


@pytest.mark.asyncio
async def test_events_compact_on_claude_sdk_buffered_compact_dispatches_standalone() -> None:
    """A buffered /compact ahead of a follow-up message still dispatches on its own.

    The non-native continuation drain coalesces the whole buffer and dispatches
    only the last body. Left unguarded, a /compact buffered behind an active turn
    with a user message queued after it would be buried in history (never the
    turn prompt) and silently no-op — the runner already returned 200, so the
    server won't fall back. The drain must dispatch a buffered /compact as its
    own turn instead.
    """
    frames = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]
    app, _pm, hc = _build_claude_sdk_compact_app(frames)

    sid = "b7d4e5f60718293a4b5c6d7e8f901234"
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": sid, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text

        # Post-active-turn state: a /compact buffered first, then a follow-up
        # user message queued after it (the ordering that would bury /compact).
        app.state.session_message_buffers[sid] = [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "/compact"}],
                "conversation_id": sid,
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "a follow-up message"}],
                "conversation_id": sid,
            },
        ]

        # Drain as the prior turn's continuation would, then wait for the turns
        # it dispatches to reach the harness.
        await app.state.check_and_start_next_turn(sid)

        for _ in range(200):
            if len(hc.posted_bodies) >= 2:
                break
            await asyncio.sleep(0.02)

    # The fix drains one at a time when a /compact is buffered, so /compact
    # dispatches as its OWN turn and the follow-up as a SECOND turn (the scripted
    # /compact turn completes immediately, kicking off the next drain) → two
    # dispatches. The buggy coalescing drain folds both into ONE turn (follow-up
    # as the prompt, /compact buried in history, never run as a slash command)
    # → a single dispatch. Assert on the count, not body content: the harness
    # fake captures the shared history list by reference, so a later append is
    # visible on an already-captured body.
    n = len(hc.posted_bodies)
    assert n == 2, (
        "a buffered /compact must dispatch as its own turn (two dispatches: /compact, then "
        f"the follow-up), not be coalesced into one; got {n} dispatch(es)"
    )
    assert _body_carries_text(hc.posted_bodies[0], "/compact"), (
        "the first dispatched turn must carry the /compact command"
    )


@pytest.mark.asyncio
async def test_events_compact_on_claude_sdk_buffered_compact_failed_fallback() -> None:
    """A buffered /compact that drains and ends with no compaction still clears the spinner.

    When a buffered /compact dispatches as its own turn, the executor can emit
    `response.compaction.in_progress` (the PreCompact hook) without a matching
    `response.compaction.completed` (e.g. the compaction-complete event resolves
    to None). The buffered path must set `_sdk_compact_inprogress` like the idle
    path so `_on_proxy_stream_end` publishes `response.compaction.failed` and the
    "Compacting…" spinner is not stranded — and so the relay swallows the
    executor's own in_progress, leaving exactly one spinner on the web.
    """
    frames = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse({"type": "response.compaction.in_progress"}),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]
    app, _pm, _hc = _build_claude_sdk_compact_app(frames)

    sid = "c8e5f60718293a4b5c6d7e8f90123456"
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": sid, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert create_resp.status_code == 201, create_resp.text
        from omnigent.runner.app import _session_event_queues_ref as _queues

        _drain_session_event_queue(_queues.get(sid))

        # A /compact buffered behind an active turn drains as its own turn.
        app.state.session_message_buffers[sid] = [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "/compact"}],
                "conversation_id": sid,
            },
        ]
        await app.state.check_and_start_next_turn(sid)

        events = await _accumulate_session_events(sid, until_types={"response.compaction.failed"})

    failed = [e for e in events if e.get("type") == "response.compaction.failed"]
    in_progress = [e for e in events if e.get("type") == "response.compaction.in_progress"]
    completed = [e for e in events if e.get("type") == "response.compaction.completed"]
    assert failed, (
        "a buffered /compact that ends without a compaction must publish failed to clear the "
        "spinner (the busy path must set the in-progress flag like the idle path); "
        f"got types {[e.get('type') for e in events]}"
    )
    assert len(in_progress) == 1, (
        "exactly one in_progress must reach the web (up-front published, executor's duplicate "
        f"swallowed); got {len(in_progress)} from types {[e.get('type') for e in events]}"
    )
    assert completed == [], "no completed should appear when nothing compacted"
