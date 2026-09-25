"""Unit tests for the per-harness native pane turn probes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harness_plugins import native_providers
from omnigent.native import native_dispatch
from omnigent.runner.native.pane_probe_types import NativeProbeContext, TurnProbe, TurnState
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.runner.session_status import SessionStatusBook

_CONV = "conv_probe"


def _ctx(
    key: str,
    *,
    registry: SessionResourceRegistry | None = None,
    forwarder_alive: bool = False,
    cheap_only: bool = False,
    labels: Mapping[str, str] | None = None,
) -> NativeProbeContext:
    async def _labels() -> Mapping[str, str]:
        return dict(labels or {})

    return NativeProbeContext(
        session_id=_CONV,
        harness_key=key,
        resource_registry=registry or SessionResourceRegistry(),
        status_book=SessionStatusBook(),
        session_labels=_labels,
        forwarder_alive=forwarder_alive,
        timeout_s=1.0,
        cheap_only=cheap_only,
    )


def test_probe_harnesses_declare_resolvable_probes() -> None:
    declared = {p.key for p in native_providers() if p.pane_turn_probe is not None}
    assert declared == {"claude", "codex", "antigravity", "opencode", "devin"}
    for provider in native_providers():
        if provider.pane_turn_probe is not None:
            assert callable(native_dispatch.resolve_hook(provider, "pane_turn_probe"))


# ── claude: Claude's own status file ────────────────────────────────────────


@pytest.mark.parametrize(
    ("record", "state", "blocked_on"),
    [
        ({"status": "busy"}, TurnState.ACTIVE, None),
        (
            {"status": "waiting", "waitingFor": "permission prompt"},
            TurnState.PARKED,
            "permission prompt",
        ),
        ({"status": "idle"}, TurnState.INACTIVE, None),
        ({"status": "shell"}, TurnState.INACTIVE, None),
        ({"status": "mystery"}, TurnState.UNKNOWN, None),
    ],
)
async def test_claude_probe_reads_the_status_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record: dict[str, str],
    state: TurnState,
    blocked_on: str | None,
) -> None:
    from omnigent.harnesses.claude_native.pane_probe import probe_pane_turn

    path = tmp_path / "123.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    registry = SessionResourceRegistry()
    monkeypatch.setattr(registry, "status_poller_path", lambda _sid: path)
    probe = await probe_pane_turn(_ctx("claude", registry=registry, cheap_only=True))
    assert probe is not None
    assert probe.state is state
    assert probe.blocked_on == blocked_on


async def test_claude_probe_is_unknown_until_the_file_resolves() -> None:
    from omnigent.harnesses.claude_native.pane_probe import probe_pane_turn

    probe = await probe_pane_turn(_ctx("claude"))
    assert probe == TurnProbe(TurnState.UNKNOWN, "vendor", detail="status file unresolved")


# ── codex: thread/read on the app-server ────────────────────────────────────


class _CodexClient:
    def __init__(self, response: object = None, error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def connect(self) -> None:
        if isinstance(self.error, (ConnectionRefusedError, FileNotFoundError)):
            raise self.error

    async def request(self, method: str, params: dict[str, Any]) -> object:
        self.requests.append((method, params))
        if self.error is not None:
            raise self.error
        return self.response

    async def close(self) -> None:
        return None


def _codex_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: _CodexClient,
    *,
    session_id: str = _CONV,
    **state: Any,
) -> None:
    from omnigent.harnesses.codex_native import app_server, bridge

    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "codex")
    bridge.write_bridge_state(
        bridge.bridge_dir_for_bridge_id(_CONV),
        bridge.CodexNativeBridgeState(
            session_id=session_id,
            socket_path="ws://127.0.0.1:1",
            thread_id="thread_1",
            codex_home=str(tmp_path),
            **state,
        ),
    )
    monkeypatch.setattr(app_server, "client_for_transport", lambda *_a, **_k: client)


def _thread(status: dict[str, Any]) -> dict[str, Any]:
    return {"id": 1, "result": {"thread": {"id": "thread_1", "status": status}}}


@pytest.mark.parametrize(
    ("status", "state", "blocked_on"),
    [
        ({"type": "notLoaded"}, TurnState.INACTIVE, None),
        ({"type": "idle"}, TurnState.INACTIVE, None),
        ({"type": "systemError"}, TurnState.INACTIVE, None),
        ({"type": "active", "activeFlags": []}, TurnState.ACTIVE, None),
        (
            {"type": "active", "activeFlags": ["waitingOnApproval"]},
            TurnState.PARKED,
            "waitingOnApproval",
        ),
        (
            {"type": "active", "activeFlags": ["waitingOnUserInput"]},
            TurnState.PARKED,
            "waitingOnUserInput",
        ),
    ],
)
async def test_codex_probe_maps_every_thread_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: dict[str, Any],
    state: TurnState,
    blocked_on: str | None,
) -> None:
    from omnigent.harnesses.codex_native.pane_probe import probe_pane_turn

    client = _CodexClient(_thread(status))
    _codex_setup(tmp_path, monkeypatch, client)
    probe = await probe_pane_turn(_ctx("codex"))
    assert probe is not None
    assert (probe.state, probe.authority, probe.blocked_on) == (state, "vendor", blocked_on)
    assert client.requests == [("thread/read", {"threadId": "thread_1", "includeTurns": False})]


async def test_codex_probe_refused_app_server_is_inactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.harnesses.codex_native.pane_probe import probe_pane_turn

    _codex_setup(tmp_path, monkeypatch, _CodexClient(error=ConnectionRefusedError()))
    probe = await probe_pane_turn(_ctx("codex"))
    assert probe is not None and probe.state is TurnState.INACTIVE


@pytest.mark.parametrize(("forwarder_alive", "state"), [(True, "active"), (False, "unknown")])
async def test_codex_probe_falls_back_to_the_bridge_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forwarder_alive: bool, state: str
) -> None:
    from omnigent.harnesses.codex_native.pane_probe import probe_pane_turn

    _codex_setup(tmp_path, monkeypatch, _CodexClient(error=TimeoutError()), active_turn_id="t1")
    probe = await probe_pane_turn(_ctx("codex", forwarder_alive=forwarder_alive))
    assert probe is not None
    assert probe.state == state
    assert probe.authority == "inferred"


async def test_codex_probe_never_resumes_and_skips_cheap_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.harnesses.codex_native.pane_probe import probe_pane_turn

    client = _CodexClient({"result": {}})
    _codex_setup(tmp_path, monkeypatch, client)
    assert await probe_pane_turn(_ctx("codex", cheap_only=True)) is None
    probe = await probe_pane_turn(_ctx("codex"))
    assert probe is not None and probe.state is TurnState.UNKNOWN
    assert [method for method, _ in client.requests] == ["thread/read"]


async def test_codex_probe_without_bridge_state_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.harnesses.codex_native import bridge
    from omnigent.harnesses.codex_native.pane_probe import probe_pane_turn

    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "empty")
    probe = await probe_pane_turn(_ctx("codex"))
    assert probe is not None and probe.state is TurnState.UNKNOWN


@pytest.mark.parametrize(
    ("relation", "state"),
    [
        ("tui_moved_there", TurnState.ACTIVE),
        ("status_kept_after_the_tui_was_lost", TurnState.ACTIVE),
        ("unrelated", TurnState.UNKNOWN),
    ],
)
async def test_codex_probe_reads_the_thread_it_serves_for_a_rotated_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relation: str, state: TurnState
) -> None:
    """After a ``/clear`` rotation the shared bridge state names the new session.

    The launching session's app-server runs the new session's thread, so the
    launching session's probe reads it: while the moved TUI lives, and after
    it was lost with the rotated session's status kept for that server. A
    state naming a session this one does not serve answers UNKNOWN.
    """
    from omnigent.harnesses.codex_native.pane_probe import probe_pane_turn

    client = _CodexClient(_thread({"type": "active", "activeFlags": []}))
    _codex_setup(tmp_path, monkeypatch, client, session_id="conv_rotated")
    registry = SessionResourceRegistry()
    # The registry's two records of which session's sidecars serve another.
    if relation == "tui_moved_there":
        registry._sidecar_homes["conv_rotated"] = _CONV
    elif relation == "status_kept_after_the_tui_was_lost":
        registry._vendor_turn_homes["conv_rotated"] = _CONV
    probe = await probe_pane_turn(_ctx("codex", registry=registry))
    assert probe is not None and probe.state is state
    expected = [] if state is TurnState.UNKNOWN else ["thread/read"]
    assert [method for method, _ in client.requests] == expected


# ── antigravity: agy's cascade run status ───────────────────────────────────


@pytest.mark.parametrize(
    ("setup", "state"),
    [
        ({"status": "CASCADE_RUN_STATUS_RUNNING"}, TurnState.ACTIVE),
        ({"status": "CASCADE_RUN_STATUS_IDLE"}, TurnState.INACTIVE),
        ("no-port", TurnState.UNKNOWN),
        ("http-error", TurnState.UNKNOWN),
        ("no-cascade", TurnState.UNKNOWN),
    ],
)
async def test_antigravity_probe(
    monkeypatch: pytest.MonkeyPatch, setup: object, state: TurnState
) -> None:
    from omnigent.harnesses.antigravity_native import reader, rpc
    from omnigent.harnesses.antigravity_native.pane_probe import probe_pane_turn

    monkeypatch.setattr(
        reader, "_resolve_cascade_id", lambda _d: None if setup == "no-cascade" else "c1"
    )
    monkeypatch.setattr(reader, "_resolve_rpc_port", lambda _c: None if setup == "no-port" else 7)

    def _trajectories(_port: int) -> dict[str, object]:
        if setup == "http-error":
            raise httpx.ConnectError("agy gone")
        return {"trajectorySummaries": {"c1": setup}}

    monkeypatch.setattr(rpc, "get_all_cascade_trajectories", _trajectories)
    assert await probe_pane_turn(_ctx("antigravity", cheap_only=True)) is None
    probe = await probe_pane_turn(_ctx("antigravity"))
    assert probe is not None and probe.state is state


# ── opencode: pending permissions, then bridge status ───────────────────────


@pytest.mark.parametrize(
    ("permissions", "status", "forwarder_alive", "state"),
    [
        ([{"id": "p1", "sessionID": "ses_1"}], "busy", True, TurnState.PARKED),
        ([{"id": "p1", "sessionID": "ses_other"}], "busy", True, TurnState.ACTIVE),
        ([], "busy", False, TurnState.UNKNOWN),
        ([], "idle", False, TurnState.INACTIVE),
        (None, "busy", True, TurnState.ACTIVE),
    ],
)
async def test_opencode_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    permissions: list[dict[str, str]] | None,
    status: str,
    forwarder_alive: bool,
    state: TurnState,
) -> None:
    from omnigent.harnesses.opencode_native import bridge
    from omnigent.harnesses.opencode_native.client import OpenCodeClient
    from omnigent.harnesses.opencode_native.pane_probe import probe_pane_turn

    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "opencode")
    bridge.write_bridge_state(
        bridge.bridge_dir_for_bridge_id(_CONV),
        bridge.OpenCodeNativeBridgeState(
            session_id=_CONV,
            server_base_url="http://127.0.0.1:1",
            opencode_session_id="ses_1",
            active_message_id="msg_1" if status == "busy" else None,
            status=status,
        ),
    )

    async def _list(self: OpenCodeClient) -> list[dict[str, str]]:
        if permissions is None:
            raise httpx.ConnectError("serve gone")
        return permissions

    monkeypatch.setattr(OpenCodeClient, "list_permissions", _list)
    probe = await probe_pane_turn(_ctx("opencode", forwarder_alive=forwarder_alive))
    assert probe is not None and probe.state is state


# ── devin: the hook event log ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("events", "state"),
    [
        ([{"hook_event_name": "UserPromptSubmit"}], TurnState.ACTIVE),
        (
            [{"hook_event_name": "UserPromptSubmit"}, {"hook_event_name": "PreToolUse"}],
            TurnState.ACTIVE,
        ),
        (
            [{"hook_event_name": "UserPromptSubmit"}, {"hook_event_name": "Stop"}],
            TurnState.INACTIVE,
        ),
        (
            [{"hook_event_name": "UserPromptSubmit", "omnigent_policy_blocked": True}],
            TurnState.INACTIVE,
        ),
        ([], TurnState.INACTIVE),
        (None, TurnState.UNKNOWN),
    ],
)
async def test_devin_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    events: list[dict[str, object]] | None,
    state: TurnState,
) -> None:
    from omnigent.harnesses.devin_native import bridge
    from omnigent.harnesses.devin_native.pane_probe import probe_pane_turn

    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "devin")
    if events is not None:
        bridge_dir = bridge.prepare_bridge_dir(_CONV)
        bridge.hooks_path(bridge_dir).touch()
        for event in events:
            bridge.record_hook_event(bridge_dir, event)
    probe = await probe_pane_turn(_ctx("devin", cheap_only=True))
    assert probe is not None
    assert probe.state is state
    assert probe.authority == "inferred"


async def test_devin_probe_dates_the_open_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from omnigent.harnesses.devin_native import bridge
    from omnigent.harnesses.devin_native.pane_probe import probe_pane_turn

    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "devin")
    bridge_dir = bridge.prepare_bridge_dir(_CONV)
    lines = [
        {"recorded_at": 100.0, "payload": {"hook_event_name": "UserPromptSubmit"}},
        {"recorded_at": 150.0, "payload": {"hook_event_name": "Stop"}},
        {"recorded_at": 200.0, "payload": {"hook_event_name": "UserPromptSubmit"}},
        {"recorded_at": 250.0, "payload": {"hook_event_name": "PreToolUse"}},
    ]
    bridge.hooks_path(bridge_dir).write_text("".join(json.dumps(line) + "\n" for line in lines))
    probe = await probe_pane_turn(_ctx("devin", cheap_only=True))
    assert probe is not None and probe.state is TurnState.ACTIVE
    # The open prompt's own record time, so it can be ordered against an
    # accepted interrupt.
    assert probe.started_wall == 200.0
