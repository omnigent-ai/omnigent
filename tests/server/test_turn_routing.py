"""Tests for the in-harness first-message turn-routing endpoint."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from omnigent.inner.hook_scripts.subagent_router import read_router_endpoint
from omnigent.runner import turn_routing
from omnigent.runner.turn_routing import (
    ADVERTISEMENT_FILE,
    MARKER_FILE,
    TurnRouteDecision,
    TurnRouteRequest,
    ensure_session_turn_router,
    make_server_relay_resolver,
    read_pending_replay,
    resolve_turn_route,
    schedule_pending_replay_recovery,
    schedule_replay,
    shutdown_session_turn_router,
    start_turn_router,
    turn_routing_marker_present,
    write_pending_replay,
    write_turn_routing_marker,
)

ROUTED_MODEL = "gpt-5.6-luna"
LAUNCH_MODEL = "gpt-5.6-sol"


def _mark(bridge_dir: Path, session_id: str = "conv_1") -> None:
    """Write the block marker the way the harness hooks write it."""
    write_turn_routing_marker(bridge_dir, session_id=session_id, decision_id="decision-1")


@dataclass
class _FakeConv:
    model_override: str | None = None
    cost_control_mode_override: str | None = "on"
    parent_conversation_id: str | None = None
    labels: dict[str, str] = field(default_factory=dict)


def _routed_labels() -> dict[str, str]:
    from omnigent.runner.subagent_routing import ROUTING_DECISION_LABEL_KEY

    return {ROUTING_DECISION_LABEL_KEY: "decision-1"}


@dataclass
class _Recorder:
    pinned: list[str] = field(default_factory=list)
    chips: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    routed: list[tuple[str | None, str]] = field(default_factory=list)
    pin_ok: bool = True

    async def route(self, harness: str | None, prompt: str) -> tuple[str | None, dict[str, Any]]:
        self.routed.append((harness, prompt))
        return ROUTED_MODEL, {"rationale": "short lookup", "model": ROUTED_MODEL}

    async def pin(self, model: str) -> bool:
        self.pinned.append(model)
        return self.pin_ok

    async def persist(self, model: str, verdict: dict[str, Any]) -> None:
        self.chips.append((model, verdict))


def _request(**overrides: Any) -> TurnRouteRequest:
    kwargs: dict[str, Any] = {
        "harness": "codex-native",
        "prompt": "What testing framework does this project use?",
        "turn_id": "turn_1",
        "model": LAUNCH_MODEL,
    }
    kwargs.update(overrides)
    return TurnRouteRequest(**kwargs)


# ── Policy ──────────────────────────────────────────────────────────


async def test_routes_pins_and_records_one_chip() -> None:
    rec = _Recorder()
    decision = await resolve_turn_route(
        "conv_1",
        _request(),
        conv=_FakeConv(),
        route_turn=rec.route,
        pin=rec.pin,
        persist=rec.persist,
    )
    assert decision.action == "route"
    assert decision.model == ROUTED_MODEL
    assert decision.terminal is True
    assert decision.rationale == "short lookup"
    assert rec.pinned == [ROUTED_MODEL]
    assert len(rec.chips) == 1
    assert rec.chips[0][0] == ROUTED_MODEL
    assert rec.routed == [("codex-native", "What testing framework does this project use?")]


async def test_an_earlier_routing_decision_is_the_authoritative_no_op() -> None:
    """Server state — not the hook's local marker — is what stops re-routing."""
    rec = _Recorder()
    decision = await resolve_turn_route(
        "conv_1",
        _request(),
        conv=_FakeConv(model_override=ROUTED_MODEL, labels=_routed_labels()),
        route_turn=rec.route,
        pin=rec.pin,
        persist=rec.persist,
    )
    assert decision.action == "allow"
    # Terminal so the hook writes its marker and stops asking.
    assert decision.terminal is True
    assert rec.routed == []
    assert rec.pinned == []
    assert rec.chips == []


async def test_the_forwarders_model_mirror_is_not_treated_as_a_pin() -> None:
    """codex mirrors ``config.toml``'s model into ``model_override``.

    It lands about a second into the first turn — before this hook's round
    trip finishes — and the mirrored value need not even be the model the
    thread runs. Treating it as a pin would stop every bare launch from
    ever routing, so only the decision label gates.
    """
    rec = _Recorder()
    decision = await resolve_turn_route(
        "conv_1",
        _request(model="databricks-gpt-5-5"),
        conv=_FakeConv(model_override="gpt-5.6-sol"),
        route_turn=rec.route,
        pin=rec.pin,
        persist=rec.persist,
    )
    assert decision.action == "route"
    assert rec.pinned == [ROUTED_MODEL]


async def test_routing_disabled_is_a_terminal_no_op() -> None:
    """
    "Routing is off" ends the hook's involvement for this session.

    The hook is only registered for a session that launched with routing on, so
    this answer means it was turned off afterwards. Left non-terminal, every
    later prompt paid a full round trip (25s worst case on a degraded server)
    to be told the same thing.
    """
    rec = _Recorder()
    decision = await resolve_turn_route(
        "conv_1",
        _request(),
        conv=_FakeConv(cost_control_mode_override=None),
        route_turn=rec.route,
        pin=rec.pin,
        persist=rec.persist,
    )
    assert decision.action == "allow"
    assert decision.terminal is True
    assert rec.routed == []
    assert rec.pinned == []
    assert rec.chips == []


async def test_a_routed_parent_routes_its_child() -> None:
    rec = _Recorder()
    decision = await resolve_turn_route(
        "conv_child",
        _request(),
        conv=_FakeConv(cost_control_mode_override=None, parent_conversation_id="conv_parent"),
        parent=_FakeConv(),
        route_turn=rec.route,
        pin=rec.pin,
        persist=rec.persist,
    )
    assert decision.action == "route"


async def test_a_missing_conversation_allows_unrouted() -> None:
    rec = _Recorder()
    decision = await resolve_turn_route(
        "conv_1", _request(), conv=None, route_turn=rec.route, pin=rec.pin
    )
    assert decision.action == "allow"
    assert decision.terminal is False


async def test_a_router_outage_allows_the_turn_unrouted() -> None:
    async def _boom(harness: str | None, prompt: str) -> tuple[str | None, dict[str, Any] | None]:
        del harness, prompt
        raise RuntimeError("router down")

    rec = _Recorder()
    decision = await resolve_turn_route(
        "conv_1",
        _request(),
        conv=_FakeConv(),
        route_turn=_boom,
        pin=rec.pin,
        persist=rec.persist,
    )
    assert decision.action == "allow"
    assert "routing unavailable" in decision.rationale
    assert rec.pinned == []
    assert rec.chips == []


async def test_no_verdict_allows_the_turn_unrouted() -> None:
    async def _none(harness: str | None, prompt: str) -> tuple[str | None, dict[str, Any] | None]:
        del harness, prompt
        return None, None

    rec = _Recorder()
    decision = await resolve_turn_route(
        "conv_1", _request(), conv=_FakeConv(), route_turn=_none, pin=rec.pin, persist=rec.persist
    )
    assert decision.action == "allow"
    assert rec.chips == []


async def test_a_failed_pin_declines_the_route() -> None:
    """An unpinned route would re-route on every later prompt."""
    rec = _Recorder(pin_ok=False)
    decision = await resolve_turn_route(
        "conv_1",
        _request(),
        conv=_FakeConv(),
        route_turn=rec.route,
        pin=rec.pin,
        persist=rec.persist,
    )
    assert decision.action == "allow"
    assert rec.chips == []


async def test_a_failed_chip_persist_still_routes() -> None:
    async def _boom(model: str, verdict: dict[str, Any]) -> None:
        del model, verdict
        raise RuntimeError("store down")

    rec = _Recorder()
    decision = await resolve_turn_route(
        "conv_1", _request(), conv=_FakeConv(), route_turn=rec.route, pin=rec.pin, persist=_boom
    )
    assert decision.action == "route"


# ── Wire types ──────────────────────────────────────────────────────


def test_request_requires_a_harness_and_a_prompt() -> None:
    with pytest.raises(ValueError, match="harness"):
        TurnRouteRequest.from_payload({"prompt": "hi"})
    with pytest.raises(ValueError, match="prompt"):
        TurnRouteRequest.from_payload({"harness": "codex-native", "prompt": "   "})
    parsed = TurnRouteRequest.from_payload(
        {"harness": "codex-native", "prompt": "hi", "turn_id": "t1", "model": LAUNCH_MODEL}
    )
    assert (parsed.turn_id, parsed.model) == ("t1", LAUNCH_MODEL)


def test_a_route_with_no_model_degrades_to_allow() -> None:
    """The hook has nothing to switch to, so it must not block the prompt."""
    assert TurnRouteDecision.from_payload({"action": "route"}).action == "allow"
    assert TurnRouteDecision.from_payload({"action": "nonsense"}).action == "allow"


# ── Loopback relay ──────────────────────────────────────────────────


def _post(url: str, body: dict[str, Any], token: str | None) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


async def test_loopback_endpoint_serves_decisions_and_checks_token(tmp_path: Path) -> None:
    canned = TurnRouteDecision(
        action="route", rationale="short lookup", model=ROUTED_MODEL, terminal=True
    )
    seen: list[TurnRouteRequest] = []

    async def _resolver(session_id: str, req: TurnRouteRequest) -> TurnRouteDecision:
        del session_id
        seen.append(req)
        return canned

    router = start_turn_router(
        bridge_dir=tmp_path,
        session_id="conv_1",
        resolver=_resolver,
        loop=asyncio.get_running_loop(),
    )
    try:
        advertised = read_router_endpoint(tmp_path, filename=ADVERTISEMENT_FILE)
        assert advertised is not None
        assert advertised.session_id == "conv_1"
        url = f"{advertised.url}/v1/sessions/conv_1/route-turn"
        body = {"harness": "codex-native", "prompt": "hello", "turn_id": "t1"}

        status, payload = await asyncio.to_thread(_post, url, body, advertised.token)
        assert status == 200
        assert payload == canned.to_payload()
        assert seen[0].prompt == "hello"

        status, _ = await asyncio.to_thread(_post, url, body, "wrong-token")
        assert status == 401
        status, _ = await asyncio.to_thread(_post, url, body, None)
        assert status == 401
        assert len(seen) == 1

        status, _ = await asyncio.to_thread(
            _post, f"{advertised.url}/v1/sessions/other/route-turn", body, advertised.token
        )
        assert status == 404

        status, _ = await asyncio.to_thread(_post, url, {"prompt": "hi"}, advertised.token)
        assert status == 400
    finally:
        router.close()
    assert not (tmp_path / ADVERTISEMENT_FILE).exists()


async def test_loopback_endpoint_allows_when_the_resolver_errors(tmp_path: Path) -> None:
    async def _resolver(session_id: str, req: TurnRouteRequest) -> TurnRouteDecision:
        del session_id, req
        raise RuntimeError("resolver exploded")

    router = start_turn_router(
        bridge_dir=tmp_path,
        session_id="conv_1",
        resolver=_resolver,
        loop=asyncio.get_running_loop(),
    )
    try:
        advertised = read_router_endpoint(tmp_path, filename=ADVERTISEMENT_FILE)
        assert advertised is not None
        status, payload = await asyncio.to_thread(
            _post,
            f"{advertised.url}/v1/sessions/conv_1/route-turn",
            {"harness": "codex-native", "prompt": "hello"},
            advertised.token,
        )
    finally:
        router.close()
    assert status == 200
    assert payload["action"] == "allow"


async def test_ensure_session_turn_router_skips_unsupported_harnesses(tmp_path: Path) -> None:
    # Only harnesses carrying the ``route-turn`` hook get an endpoint; the
    # rest would advertise one nothing reads.
    assert (
        ensure_session_turn_router(
            "conv_x", bridge_dir=tmp_path, server_client=object(), harness="pi-native"
        )
        is None
    )
    assert (
        ensure_session_turn_router(
            "conv_x", bridge_dir=tmp_path, server_client=None, harness="codex-native"
        )
        is None
    )
    assert not (tmp_path / ADVERTISEMENT_FILE).exists()


@pytest.mark.parametrize("harness", ["codex-native", "claude-native"])
async def test_ensure_session_turn_router_serves_both_hook_harnesses(
    tmp_path: Path, harness: str
) -> None:
    router = ensure_session_turn_router(
        f"conv_{harness}",
        bridge_dir=tmp_path,
        server_client=_FakeServerClient(),
        harness=harness,
    )
    assert router is not None
    try:
        advertised = json.loads((tmp_path / ADVERTISEMENT_FILE).read_text(encoding="utf-8"))
        assert advertised["url"] == router.url
        assert advertised["session_id"] == f"conv_{harness}"
    finally:
        shutdown_session_turn_router(f"conv_{harness}", router)


async def test_ensure_session_turn_router_is_idempotent(tmp_path: Path) -> None:
    first = ensure_session_turn_router(
        "conv_y", bridge_dir=tmp_path, server_client=_FakeServerClient(), harness="codex-native"
    )
    assert first is not None
    try:
        second = ensure_session_turn_router(
            "conv_y",
            bridge_dir=tmp_path,
            server_client=_FakeServerClient(),
            harness="codex-native",
        )
        assert second is first
    finally:
        shutdown_session_turn_router("conv_y", first)
    # Safe to tear down twice.
    shutdown_session_turn_router("conv_y", first)


# ── Server relay + replay ───────────────────────────────────────────


@dataclass
class _FakeResponse:
    payload: dict[str, Any]

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


@dataclass
class _FakeServerClient:
    verdict: dict[str, Any] = field(default_factory=lambda: {"action": "allow", "rationale": ""})
    posts: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail: bool = False
    items: list[dict[str, Any]] | None = None
    items_fail: bool = False

    async def post(self, url: str, *, json: dict[str, Any], timeout: float) -> _FakeResponse:
        del timeout
        self.posts.append((url, json))
        if self.fail:
            raise RuntimeError("server unreachable")
        return _FakeResponse(self.verdict)

    async def get(self, url: str, *, params: dict[str, Any], timeout: float) -> _FakeResponse:
        del url, params, timeout
        if self.items_fail:
            raise RuntimeError("server unreachable")
        return _FakeResponse({"object": "list", "data": self.items or []})


async def test_server_relay_failure_allows_the_turn(tmp_path: Path) -> None:
    client = _FakeServerClient(fail=True)
    resolver = make_server_relay_resolver(client, bridge_dir=tmp_path)
    decision = await resolver("conv_1", _request())
    assert decision.action == "allow"
    assert "unreachable" in decision.rationale


async def test_server_relay_arms_the_replay_only_for_a_routed_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    armed: list[str] = []
    monkeypatch.setattr(
        turn_routing,
        "schedule_replay",
        lambda session_id, **kwargs: armed.append(session_id),
    )
    client = _FakeServerClient(verdict={"action": "allow", "rationale": "off"})
    assert (await make_server_relay_resolver(client, bridge_dir=tmp_path)("c1", _request())).action
    assert armed == []

    client = _FakeServerClient(
        verdict={"action": "route", "model": ROUTED_MODEL, "rationale": "x", "terminal": True}
    )
    decision = await make_server_relay_resolver(client, bridge_dir=tmp_path)("c1", _request())
    assert decision.action == "route"
    assert armed == ["c1"]
    # The prompt reaches the server relay, which owns the routing decision.
    assert client.posts[0][0] == "/v1/sessions/c1/hooks/route-turn"
    assert client.posts[0][1]["prompt"] == _request().prompt


async def test_replay_is_abandoned_when_the_hook_never_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No marker means the hook fell open — replaying would double-run."""
    monkeypatch.setattr(turn_routing, "REPLAY_MARKER_WAIT_S", 0.2)
    client = _FakeServerClient()
    await schedule_replay(
        "conv_1",
        prompt="hello",
        bridge_dir=tmp_path,
        blocked_turn_id="turn_1",
        server_client=client,
        idle=lambda _turn: True,
    )
    assert client.posts == []


async def test_replay_delivers_the_prompt_through_the_events_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(turn_routing, "REPLAY_MARKER_WAIT_S", 1.0)
    monkeypatch.setattr(turn_routing, "REPLAY_IDLE_WAIT_S", 5.0)
    monkeypatch.setattr(turn_routing, "REPLAY_IDLE_GRACE_S", 0.05)
    monkeypatch.setattr(turn_routing, "REPLAY_POLL_S", 0.01)
    _mark(tmp_path)
    client = _FakeServerClient()
    await schedule_replay(
        "conv_1",
        prompt="hello there",
        bridge_dir=tmp_path,
        blocked_turn_id="turn_1",
        server_client=client,
        idle=lambda _turn: True,
    )
    assert client.posts == [
        (
            "/v1/sessions/conv_1/events",
            {
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello there"}],
                },
            },
        )
    ]


async def test_a_routed_replay_carries_the_model_in_band(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The routed model rides the replayed turn.

    Claude's hook cannot switch the model — no hook output carries one and
    there is no channel into a running TUI — so the switch has to arrive
    with the replayed turn, where the executor types ``/model`` before the
    message. Without this the replay lands on the launch model and the
    session shows a routing chip it never honoured.
    """
    monkeypatch.setattr(turn_routing, "REPLAY_MARKER_WAIT_S", 1.0)
    monkeypatch.setattr(turn_routing, "REPLAY_IDLE_GRACE_S", 0.05)
    monkeypatch.setattr(turn_routing, "REPLAY_POLL_S", 0.01)
    _mark(tmp_path)
    client = _FakeServerClient()
    await schedule_replay(
        "conv_1",
        prompt="hello there",
        bridge_dir=tmp_path,
        blocked_turn_id=None,
        server_client=client,
        model=ROUTED_MODEL,
        idle=lambda _turn: True,
    )
    assert client.posts[0][1]["model_override"] == ROUTED_MODEL


async def test_the_relay_records_and_replays_the_model_it_routed_to(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    armed: list[dict[str, Any]] = []
    monkeypatch.setattr(
        turn_routing,
        "schedule_replay",
        lambda session_id, **kwargs: armed.append(kwargs),
    )
    client = _FakeServerClient(
        verdict={"action": "route", "model": ROUTED_MODEL, "rationale": "x", "terminal": True}
    )
    await make_server_relay_resolver(client, bridge_dir=tmp_path)("c1", _request())
    assert armed[0]["model"] == ROUTED_MODEL
    # The crash-recovery record carries it too, so a relaunched runner still
    # delivers the prompt on the routed model rather than the launch one.
    pending = turn_routing.read_pending_replay(tmp_path)
    assert pending is not None
    assert pending.model == ROUTED_MODEL


async def test_replay_waits_for_the_blocked_turn_to_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delivering mid-abort would be steered into the dying turn."""
    monkeypatch.setattr(turn_routing, "REPLAY_MARKER_WAIT_S", 1.0)
    monkeypatch.setattr(turn_routing, "REPLAY_IDLE_WAIT_S", 5.0)
    monkeypatch.setattr(turn_routing, "REPLAY_POLL_S", 0.01)
    _mark(tmp_path)
    active: list[str | None] = ["turn_1", "turn_1", "turn_1", None]
    client = _FakeServerClient()

    def _cleared(blocked: str | None) -> bool:
        current = active.pop(0) if active else None
        return current != blocked

    await schedule_replay(
        "conv_1",
        prompt="hello",
        bridge_dir=tmp_path,
        blocked_turn_id="turn_1",
        server_client=client,
        idle=_cleared,
    )
    assert active == []
    assert len(client.posts) == 1


async def test_replay_delivers_even_if_the_turn_never_clears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing the user's prompt is worse than delivering it early."""
    monkeypatch.setattr(turn_routing, "REPLAY_MARKER_WAIT_S", 1.0)
    monkeypatch.setattr(turn_routing, "REPLAY_IDLE_WAIT_S", 0.2)
    monkeypatch.setattr(turn_routing, "REPLAY_POLL_S", 0.01)
    _mark(tmp_path)
    client = _FakeServerClient()
    await schedule_replay(
        "conv_1",
        prompt="hello",
        bridge_dir=tmp_path,
        blocked_turn_id="turn_1",
        server_client=client,
        idle=lambda _turn: False,
    )
    assert len(client.posts) == 1


# ── Pending-replay record + crash recovery ──────────────────────────


def _fast_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(turn_routing, "REPLAY_MARKER_WAIT_S", 0.2)
    monkeypatch.setattr(turn_routing, "REPLAY_IDLE_WAIT_S", 0.5)
    monkeypatch.setattr(turn_routing, "REPLAY_IDLE_GRACE_S", 0.02)
    monkeypatch.setattr(turn_routing, "REPLAY_POLL_S", 0.01)


async def test_a_routed_verdict_records_the_pending_replay(tmp_path: Path) -> None:
    """The prompt must be on disk before the hook can block it away."""
    client = _FakeServerClient(
        verdict={"action": "route", "model": ROUTED_MODEL, "rationale": "x", "terminal": True}
    )
    await make_server_relay_resolver(client, bridge_dir=tmp_path)("conv_1", _request())
    pending = read_pending_replay(tmp_path)
    assert pending is not None
    assert pending.session_id == "conv_1"
    assert pending.prompt == _request().prompt


async def test_a_delivered_replay_clears_the_pending_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast_replay(monkeypatch)
    _mark(tmp_path)
    write_pending_replay(tmp_path, session_id="conv_1", prompt="hello", blocked_turn_id="t1")
    await schedule_replay(
        "conv_1",
        prompt="hello",
        bridge_dir=tmp_path,
        blocked_turn_id="t1",
        server_client=_FakeServerClient(),
        idle=lambda _turn: True,
    )
    assert read_pending_replay(tmp_path) is None


async def test_a_failed_delivery_keeps_the_record_for_the_next_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The routed model is applied but the prompt is not lost."""
    _fast_replay(monkeypatch)
    _mark(tmp_path)
    write_pending_replay(tmp_path, session_id="conv_1", prompt="hello", blocked_turn_id="t1")
    await schedule_replay(
        "conv_1",
        prompt="hello",
        bridge_dir=tmp_path,
        blocked_turn_id="t1",
        server_client=_FakeServerClient(fail=True),
        idle=lambda _turn: True,
    )
    pending = read_pending_replay(tmp_path)
    assert pending is not None and pending.prompt == "hello"


async def test_a_hook_that_fell_open_clears_the_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No marker means the prompt already ran, so nothing is owed."""
    _fast_replay(monkeypatch)
    write_pending_replay(tmp_path, session_id="conv_1", prompt="hello", blocked_turn_id="t1")
    client = _FakeServerClient()
    await schedule_replay(
        "conv_1",
        prompt="hello",
        bridge_dir=tmp_path,
        blocked_turn_id="t1",
        server_client=client,
        idle=lambda _turn: True,
    )
    assert client.posts == []
    assert read_pending_replay(tmp_path) is None


async def test_recovery_replays_a_prompt_a_dead_launch_never_delivered(
    tmp_path: Path,
) -> None:
    _mark(tmp_path)
    write_pending_replay(tmp_path, session_id="conv_1", prompt="hello", blocked_turn_id="t1")
    client = _FakeServerClient(items=[])
    task = schedule_pending_replay_recovery(
        "conv_1",
        bridge_dir=tmp_path,
        server_client=client,
        ready=lambda: True,
    )
    assert task is not None
    await task
    assert client.posts == [
        (
            "/v1/sessions/conv_1/events",
            {
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
            },
        )
    ]
    assert read_pending_replay(tmp_path) is None


async def test_recovery_does_not_redeliver_a_prompt_that_already_ran(
    tmp_path: Path,
) -> None:
    _mark(tmp_path)
    write_pending_replay(tmp_path, session_id="conv_1", prompt="hello", blocked_turn_id="t1")
    client = _FakeServerClient(
        items=[
            {
                "id": "msg_1",
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            }
        ]
    )
    task = schedule_pending_replay_recovery(
        "conv_1",
        bridge_dir=tmp_path,
        server_client=client,
        ready=lambda: True,
    )
    assert task is not None
    await task
    assert client.posts == []
    assert read_pending_replay(tmp_path) is None


async def test_recovery_keeps_the_record_when_it_cannot_check_the_session(
    tmp_path: Path,
) -> None:
    """An unreadable session is 'unknown' — a double-run is worse than a retry."""
    _mark(tmp_path)
    write_pending_replay(tmp_path, session_id="conv_1", prompt="hello", blocked_turn_id="t1")
    client = _FakeServerClient(items_fail=True)
    task = schedule_pending_replay_recovery(
        "conv_1",
        bridge_dir=tmp_path,
        server_client=client,
        ready=lambda: True,
    )
    assert task is not None
    await task
    assert client.posts == []
    assert read_pending_replay(tmp_path) is not None


async def test_recovery_is_a_no_op_without_a_record_or_marker(tmp_path: Path) -> None:
    client = _FakeServerClient(items=[])
    assert (
        schedule_pending_replay_recovery(
            "conv_1", bridge_dir=tmp_path, server_client=client, ready=lambda: True
        )
        is None
    )
    # A record with no marker means the hook fell open: drop it, send nothing.
    write_pending_replay(tmp_path, session_id="conv_1", prompt="hello", blocked_turn_id="t1")
    assert (
        schedule_pending_replay_recovery(
            "conv_1", bridge_dir=tmp_path, server_client=client, ready=lambda: True
        )
        is None
    )
    assert read_pending_replay(tmp_path) is None


async def test_recovery_ignores_another_session_sharing_the_bridge_dir(
    tmp_path: Path,
) -> None:
    """A fork inherits the parent's bridge id; its prompt is not ours to send."""
    _mark(tmp_path)
    write_pending_replay(tmp_path, session_id="conv_other", prompt="hello", blocked_turn_id="t1")
    assert (
        schedule_pending_replay_recovery(
            "conv_1",
            bridge_dir=tmp_path,
            server_client=_FakeServerClient(items=[]),
            ready=lambda: True,
        )
        is None
    )
    assert read_pending_replay(tmp_path) is not None


# ── claude-native: settle probe + the runner-side actuator ──────────────


def test_in_harness_turn_routing_covers_both_native_harnesses() -> None:
    """The CLI's bare ``--smart-routing`` gate reads this predicate."""
    from omnigent.runner.turn_routing import supports_in_harness_turn_routing

    assert supports_in_harness_turn_routing("codex-native")
    assert supports_in_harness_turn_routing("claude-native")
    assert not supports_in_harness_turn_routing("claude")
    assert not supports_in_harness_turn_routing(None)


def test_settle_probe_reads_the_pane_for_claude_and_the_turn_id_for_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The two harnesses block at different depths, so they settle differently.

    Codex has a turn open by the time its hook runs; claude blocks before any
    turn exists, so its only honest signal is the pane being idle again.
    """
    asked: list[Path] = []
    monkeypatch.setattr(
        "omnigent.claude_native_bridge.claude_pane_ready",
        lambda bridge_dir: bool(asked.append(bridge_dir)) or True,
    )

    claude_probe = turn_routing._settle_probe(tmp_path, "claude-native")
    assert claude_probe("ignored") is True
    assert asked == [tmp_path]

    # No codex bridge state in this dir, which reads as "nothing active".
    codex_probe = turn_routing._settle_probe(tmp_path, "codex-native")
    assert codex_probe("turn_1") is True


async def test_replay_switches_the_claude_pane_before_delivering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Claude's model switch happens in the replay, not in its hook.

    The hook's own pane is frozen waiting on the hook subprocess, so
    keystrokes sent from there would queue behind the block. Order is
    load-bearing: the pane must already be on the routed model when the
    prompt arrives, or the turn runs on the launch model.
    """
    monkeypatch.setattr(turn_routing, "REPLAY_MARKER_WAIT_S", 1.0)
    monkeypatch.setattr(turn_routing, "REPLAY_POLL_S", 0.01)
    _mark(tmp_path)
    switched: list[tuple[Path, str, bool]] = []
    client = _FakeServerClient()

    def _inject(
        bridge_dir: Path,
        *,
        command: str,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        assert not client.posts, "the pane must be switched before the prompt lands"
        switched.append((bridge_dir, command, auto_confirm))

    monkeypatch.setattr(
        "omnigent.claude_native_bridge.inject_slash_command", _inject, raising=False
    )
    monkeypatch.setattr(
        "omnigent.claude_native_bridge.read_model_env",
        lambda _dir: {"ANTHROPIC_DEFAULT_SONNET_MODEL": "databricks-claude-sonnet-5"},
    )

    await schedule_replay(
        "conv_1",
        prompt="hello",
        bridge_dir=tmp_path,
        blocked_turn_id=None,
        server_client=client,
        harness="claude-native",
        model="databricks-claude-sonnet-5",
        idle=lambda _turn: True,
    )

    # The vocabulary spelling, not the catalog id: ``/model`` rejects a bare
    # gateway id and silently keeps the old model.
    assert switched == [(tmp_path, "/model sonnet", True)]
    assert len(client.posts) == 1


async def test_replay_still_delivers_when_the_claude_switch_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A switch that does not take must not cost the user their prompt."""
    monkeypatch.setattr(turn_routing, "REPLAY_MARKER_WAIT_S", 1.0)
    monkeypatch.setattr(turn_routing, "REPLAY_POLL_S", 0.01)
    _mark(tmp_path)
    client = _FakeServerClient()

    def _boom(
        _bridge_dir: Path,
        *,
        command: str,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        del command, auto_confirm, confirm_hint
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr("omnigent.claude_native_bridge.inject_slash_command", _boom, raising=False)
    monkeypatch.setattr(
        "omnigent.claude_native_bridge.read_model_env",
        lambda _dir: {"ANTHROPIC_DEFAULT_SONNET_MODEL": "databricks-claude-sonnet-5"},
    )

    await schedule_replay(
        "conv_1",
        prompt="hello",
        bridge_dir=tmp_path,
        blocked_turn_id=None,
        server_client=client,
        harness="claude-native",
        model="databricks-claude-sonnet-5",
        idle=lambda _turn: True,
    )

    assert len(client.posts) == 1


async def test_replay_skips_the_switch_for_an_unspeakable_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A model this pane has no ``/model`` spelling for is left alone.

    Typing a value the CLI will not take leaves the pane on its old model
    while the recorded decision claims the routed one.
    """
    monkeypatch.setattr(turn_routing, "REPLAY_MARKER_WAIT_S", 1.0)
    monkeypatch.setattr(turn_routing, "REPLAY_POLL_S", 0.01)
    _mark(tmp_path)
    client = _FakeServerClient()
    monkeypatch.setattr(
        "omnigent.claude_native_bridge.inject_slash_command",
        lambda *a, **k: pytest.fail("must not touch the pane"),
        raising=False,
    )
    monkeypatch.setattr(
        "omnigent.claude_native_bridge.read_model_env",
        lambda _dir: {"ANTHROPIC_DEFAULT_SONNET_MODEL": "databricks-claude-sonnet-5"},
    )

    await schedule_replay(
        "conv_1",
        prompt="hello",
        bridge_dir=tmp_path,
        blocked_turn_id=None,
        server_client=client,
        harness="claude-native",
        model="databricks-claude-opus-4-8",
        idle=lambda _turn: True,
    )

    assert len(client.posts) == 1


async def test_replay_leaves_the_model_alone_for_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex's hook already switched the thread over its app-server RPC."""
    monkeypatch.setattr(turn_routing, "REPLAY_MARKER_WAIT_S", 1.0)
    monkeypatch.setattr(turn_routing, "REPLAY_POLL_S", 0.01)
    _mark(tmp_path)
    client = _FakeServerClient()
    monkeypatch.setattr(
        "omnigent.claude_native_bridge.inject_slash_command",
        lambda *a, **k: pytest.fail("codex must not drive the claude pane"),
        raising=False,
    )

    await schedule_replay(
        "conv_1",
        prompt="hello",
        bridge_dir=tmp_path,
        blocked_turn_id=None,
        server_client=client,
        harness="codex-native",
        model="gpt-5.6-luna",
        idle=lambda _turn: True,
    )

    assert len(client.posts) == 1


async def test_server_relay_hands_the_routed_model_to_the_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The replay is the claude actuator, so it needs the pick."""
    armed: list[dict[str, Any]] = []
    monkeypatch.setattr(
        turn_routing,
        "schedule_replay",
        lambda session_id, **kwargs: armed.append({"session_id": session_id, **kwargs}),
    )
    client = _FakeServerClient(
        verdict={"action": "route", "model": ROUTED_MODEL, "rationale": "x", "terminal": True}
    )
    resolver = make_server_relay_resolver(client, bridge_dir=tmp_path, harness="claude-native")

    await resolver("c1", _request())

    assert armed[0]["model"] == ROUTED_MODEL
    assert armed[0]["harness"] == "claude-native"


async def test_replay_skips_the_switch_when_the_pane_is_already_there(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A pane already on the routed model gets no ``/model`` echo.

    Block-and-replay puts that echo ahead of the session's very first turn,
    where weak models have been observed refusing the replayed prompt, so a
    redundant switch is worth not typing.
    """
    monkeypatch.setattr(turn_routing, "REPLAY_MARKER_WAIT_S", 1.0)
    monkeypatch.setattr(turn_routing, "REPLAY_POLL_S", 0.01)
    _mark(tmp_path)
    client = _FakeServerClient()
    monkeypatch.setattr(
        "omnigent.claude_native_bridge.read_claude_status_model",
        lambda _dir: "databricks-claude-sonnet-5",
    )
    monkeypatch.setattr(
        "omnigent.claude_native_bridge.inject_slash_command",
        lambda *a, **k: pytest.fail("no switch when the pane is already on it"),
        raising=False,
    )

    await schedule_replay(
        "conv_1",
        prompt="hello",
        bridge_dir=tmp_path,
        blocked_turn_id=None,
        server_client=client,
        harness="claude-native",
        model="databricks-claude-sonnet-5",
        idle=lambda _turn: True,
    )

    assert len(client.posts) == 1


# ── Timeout ladder ──────────────────────────────────────────────────
#
# Each hop's budget must exceed the hop it waits on, or the inner fail-open can
# never run: the outer layer kills the inner one first and the user is left with
# a wedged pane instead of an unrouted turn. These are relations, not values —
# a future retune stays green as long as it keeps the ordering.


def test_the_timeout_ladder_is_strictly_decreasing_inwards() -> None:
    """Add #25: the hook's budget > the relay's > the server hop's."""
    from omnigent.runner.turn_routing import (
        HARNESS_HOOK_TIMEOUT_S,
        HOOK_REQUEST_TIMEOUT_S,
        RELAY_TIMEOUT_S,
        SERVER_HOP_TIMEOUT_S,
        SETTINGS_UPDATE_TIMEOUT_S,
    )

    # Hop 1 registers on the harness and must cover the hook script's whole
    # life: the verdict round trip plus (codex only) the settings update.
    assert HARNESS_HOOK_TIMEOUT_S > HOOK_REQUEST_TIMEOUT_S + SETTINGS_UPDATE_TIMEOUT_S
    # Hop 2a waits on hop 3, which waits on hop 4.
    assert HOOK_REQUEST_TIMEOUT_S > RELAY_TIMEOUT_S > SERVER_HOP_TIMEOUT_S


def test_the_router_clients_own_timeout_sits_inside_the_hook_budget() -> None:
    """
    Add #26: the router's HTTP timeout is the innermost wait, not the outermost.

    ``ExternalRoutingClient`` is called from inside the server hop, so its own
    per-call timeout has to be the tightest number in the stack. If it were
    larger than the hook's budget, a slow router would degrade to "harness killed
    the hook" (prompt at risk) instead of "routing unavailable" (turn runs).
    """
    import inspect

    from omnigent.runner.turn_routing import HOOK_REQUEST_TIMEOUT_S
    from omnigent.server.smart_routing import ExternalRoutingClient

    default = (
        inspect.signature(ExternalRoutingClient.__init__).parameters["request_timeout"].default
    )
    assert isinstance(default, int | float)
    assert 0 < default < HOOK_REQUEST_TIMEOUT_S


def test_every_routing_budget_stays_in_single_digits() -> None:
    """
    The owner's ruling: a fail-open that takes 30s is blocking in practice.

    The relation tests above only pin the ORDERING, which a 45s ladder
    satisfies just as well as a 15s one. This pins the magnitude: the two
    hazard paths — a first-message hook holding a typed prompt and a spawn gate
    holding an agent's tool call — must give up within seconds, so every
    request budget on them is a single digit and the harness-registered kill is
    well inside a quarter of a minute.
    """
    from omnigent.inner.hook_scripts.subagent_router import (
        HOOK_TIMEOUT_S as SPAWN_HOOK_TIMEOUT_S,
    )
    from omnigent.inner.hook_scripts.subagent_router import (
        REQUEST_TIMEOUT_S as SPAWN_REQUEST_TIMEOUT_S,
    )
    from omnigent.runner import subagent_routing, turn_routing
    from omnigent.server.smart_routing import ROUTING_REQUEST_TIMEOUT_S

    # The routing call itself, the innermost wait on every path.
    assert ROUTING_REQUEST_TIMEOUT_S <= 8.0
    # Each hook's own HTTP budget — what the user actually waits out.
    for budget in (
        turn_routing.HOOK_REQUEST_TIMEOUT_S,
        turn_routing.RELAY_TIMEOUT_S,
        turn_routing.SERVER_HOP_TIMEOUT_S,
        turn_routing.SETTINGS_UPDATE_TIMEOUT_S,
        subagent_routing.HOOK_REQUEST_TIMEOUT_S,
        subagent_routing.RELAY_TIMEOUT_S,
        subagent_routing.SERVER_HOP_TIMEOUT_S,
        SPAWN_REQUEST_TIMEOUT_S,
    ):
        assert 0 < budget < 10.0, budget
    # The outermost hop is the harness's own kill, which only needs enough
    # headroom for the hook script to reach its fail-open branch.
    assert turn_routing.HARNESS_HOOK_TIMEOUT_S <= 15
    assert SPAWN_HOOK_TIMEOUT_S <= 15


def test_the_subagent_ladder_is_strictly_decreasing_inwards() -> None:
    """
    The spawn gate's own four hops, mirroring the turn ladder's relations.

    ``subagent_routing`` restates the hook script's budget for its docstring
    cross-reference (the script is stdlib-only and cannot import it), so the
    two spellings must agree or the documented ladder is fiction.
    """
    from omnigent.inner.hook_scripts.subagent_router import HOOK_TIMEOUT_S, REQUEST_TIMEOUT_S
    from omnigent.runner.subagent_routing import (
        HOOK_REQUEST_TIMEOUT_S,
        RELAY_TIMEOUT_S,
        SERVER_HOP_TIMEOUT_S,
    )
    from omnigent.server.smart_routing import ROUTING_REQUEST_TIMEOUT_S

    assert HOOK_REQUEST_TIMEOUT_S == REQUEST_TIMEOUT_S
    assert HOOK_TIMEOUT_S > REQUEST_TIMEOUT_S > RELAY_TIMEOUT_S > SERVER_HOP_TIMEOUT_S
    assert SERVER_HOP_TIMEOUT_S > ROUTING_REQUEST_TIMEOUT_S


def test_the_routing_call_is_the_innermost_wait_on_the_turn_path() -> None:
    """
    The turn ladder bottoms out on the routing call, not the other way round.

    ``SERVER_HOP_TIMEOUT_S`` is the runner's wait on the server relay route,
    and the routing client runs inside it. Equal numbers are not enough: the
    hop has to outlast the call so the server's own fail-open answer ("no
    verdict") reaches the runner instead of the runner giving up first and
    reporting the vaguer "routing server unreachable".
    """
    from omnigent.runner.turn_routing import SERVER_HOP_TIMEOUT_S
    from omnigent.server.smart_routing import ROUTING_REQUEST_TIMEOUT_S

    assert SERVER_HOP_TIMEOUT_S > ROUTING_REQUEST_TIMEOUT_S


def test_the_builtin_judge_shares_the_external_routers_budget() -> None:
    """
    The judge is a routing call too, so it cannot inherit the ``llm:`` timeout.

    ``PolicyLLMClient`` defaults to a 300s request timeout and multiplies it by
    every configured fallback model. Left alone, picking the built-in judge as
    the routing source turned a fail-open into a multi-minute hang, on exactly
    the paths a wedged router is supposed to cost seconds.
    """
    import inspect

    from omnigent.server.smart_routing import ROUTING_REQUEST_TIMEOUT_S, LLMRoutingClient

    source = inspect.getsource(LLMRoutingClient.route)
    # Both bounds: the adapter's own per-call HTTP timeout, and a wait_for that
    # covers the fallback chain and any pre-request token refresh.
    assert "timeout=ROUTING_REQUEST_TIMEOUT_S" in source
    assert "asyncio.wait_for" in source
    assert ROUTING_REQUEST_TIMEOUT_S <= 8.0


# ── Route-once under load, and the manual-pin gap ───────────────────


async def test_two_concurrent_first_prompts_are_not_serialised_by_a_lock() -> None:
    """
    Add #27: pins the known concurrency gap — there is no per-session lock.

    ``resolve_turn_route`` reads the gate off the conversation row, and the row
    only carries a decision label once the first call has finished persisting.
    Two prompts submitted inside one routing round trip therefore both pass the
    gate. That is recorded as a low residual (registry row 92 / breakage X13),
    not fixed: the pin write is idempotent, both calls land the same arm, and a
    lock here would have to span the runner and the server.

    This test exists so the gap stays *deliberate*. If a lock is added, it fails
    and the residual can be closed instead of quietly drifting.
    """
    started = asyncio.Event()
    release = asyncio.Event()
    rec = _Recorder()

    async def _slow_route(harness: str | None, prompt: str) -> tuple[str | None, dict[str, Any]]:
        started.set()
        await release.wait()
        return await rec.route(harness, prompt)

    conv = _FakeConv()
    first = asyncio.create_task(
        resolve_turn_route(
            "conv_1",
            _request(),
            conv=conv,
            route_turn=_slow_route,
            pin=rec.pin,
            persist=rec.persist,
        )
    )
    await started.wait()
    second = asyncio.create_task(
        resolve_turn_route(
            "conv_1",
            _request(turn_id="turn_2", prompt="and another thing"),
            conv=conv,
            route_turn=_slow_route,
            pin=rec.pin,
            persist=rec.persist,
        )
    )
    release.set()
    decisions = await asyncio.gather(first, second)

    # Both route: no lock, and the row the gate reads was not updated in between.
    assert [d.action for d in decisions] == ["route", "route"]
    # But they agree on the arm, so the pane is never asked for two models, and
    # the pin write is the same value twice.
    assert {d.model for d in decisions} == {ROUTED_MODEL}
    assert rec.pinned == [ROUTED_MODEL, ROUTED_MODEL]


async def test_a_second_prompt_after_the_decision_lands_declines() -> None:
    """Add #27, the half that IS closed: serialised prompts route exactly once."""
    rec = _Recorder()
    conv = _FakeConv()
    first = await resolve_turn_route(
        "conv_1", _request(), conv=conv, route_turn=rec.route, pin=rec.pin, persist=rec.persist
    )
    assert first.action == "route"
    # What the server writes once the decision is persisted.
    conv.labels.update(_routed_labels())

    second = await resolve_turn_route(
        "conv_1",
        _request(turn_id="turn_2"),
        conv=conv,
        route_turn=rec.route,
        pin=rec.pin,
        persist=rec.persist,
    )
    assert second.action == "allow"
    assert second.terminal is True
    assert len(rec.routed) == 1


async def test_a_manually_pinned_session_with_routing_on_is_still_hook_routed_once() -> None:
    """
    Add #28: pins the row-91 gap, deliberately unfixed.

    The composer gate declines a manually pinned session (its gate is
    ``effective_runner_override is None``), but this hook cannot use that check:
    the codex forwarder mirrors ``config.toml``'s launch model into
    ``model_override`` about a second into the first turn, so a present
    ``model_override`` cannot tell a user's pin from the mirror (row 89 — across
    8 live sessions the mirror won the race in 6, so a presence gate would have
    wrongly declined routing 6 times out of 8).

    So a bare session the user pinned, with Smart Routing left on, gets routed
    once by the hook. Closing it needs pin provenance the mirror destroys.
    Asserted here so it is a recorded decision rather than an accident.
    """
    rec = _Recorder()
    decision = await resolve_turn_route(
        "conv_1",
        _request(),
        # A real user pin: a model set, and NO routing-decision label.
        conv=_FakeConv(model_override="databricks-gpt-5-6-sol"),
        route_turn=rec.route,
        pin=rec.pin,
        persist=rec.persist,
    )

    assert decision.action == "route"
    assert decision.model == ROUTED_MODEL
    assert len(rec.routed) == 1
    # And exactly once: the decision label it writes is what closes the gate, so
    # the pin is overridden one time and never again.
    second = await resolve_turn_route(
        "conv_1",
        _request(turn_id="turn_2"),
        conv=_FakeConv(model_override=ROUTED_MODEL, labels=_routed_labels()),
        route_turn=rec.route,
        pin=rec.pin,
        persist=rec.persist,
    )
    assert second.action == "allow"
    assert len(rec.routed) == 1


# ── D1: the recovery dedup is a structural compare, not a substring ──


@pytest.mark.parametrize(
    ("case", "prompt"),
    [
        ("multiline", "fix the bug\n\nthen run the tests"),
        ("quote", 'rename "old_name" to new_name'),
        ("non-ascii", "explain the café heuristic — briefly"),
        ("backslash", r"match the \d+ in the regex"),
    ],
)
async def test_recovery_does_not_redeliver_an_escaped_prompt_that_already_ran(
    tmp_path: Path,
    case: str,
    prompt: str,
) -> None:
    """
    A prompt whose JSON form is escaped is still recognized as already run.

    The dedup used to ask ``prompt in json.dumps(page)``. Serializing escapes
    the text — ``\\n``, ``\\"``, ``\\uXXXX``, ``\\\\`` — so the recovery never
    found the prompt's OWN recorded copy and delivered it a second time. Any
    prompt with a newline in it (most real ones) got a duplicate first turn on
    every crash-recovered session.
    """
    del case
    _mark(tmp_path)
    write_pending_replay(tmp_path, session_id="conv_1", prompt=prompt, blocked_turn_id="t1")
    client = _FakeServerClient(
        items=[
            {
                "id": "msg_1",
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }
        ]
    )

    task = schedule_pending_replay_recovery(
        "conv_1", bridge_dir=tmp_path, server_client=client, ready=lambda: True
    )
    assert task is not None
    await task

    assert client.posts == []
    assert read_pending_replay(tmp_path) is None


async def test_recovery_still_delivers_a_short_prompt_the_page_merely_mentions(
    tmp_path: Path,
) -> None:
    """
    A short prompt must not match some unrelated corner of the page.

    ``"continue" in json.dumps(page)`` was true for an assistant's prose, an
    item id, or a tool argument — so the recovery concluded the prompt had
    already run and dropped it. The prompt was gone for good: the hook had
    blocked it, so it was persisted nowhere else, and the marker stopped any
    later hook from asking again.
    """
    _mark(tmp_path)
    write_pending_replay(tmp_path, session_id="conv_1", prompt="continue", blocked_turn_id="t1")
    client = _FakeServerClient(
        items=[
            {
                "id": "msg_continue_1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Shall I continue with the plan?"}],
            },
            {
                "id": "fc_1",
                "type": "function_call",
                "name": "Bash",
                "arguments": '{"command": "make continue"}',
            },
        ]
    )

    task = schedule_pending_replay_recovery(
        "conv_1", bridge_dir=tmp_path, server_client=client, ready=lambda: True
    )
    assert task is not None
    await task

    assert [url for url, _body in client.posts] == ["/v1/sessions/conv_1/events"]
    assert client.posts[0][1]["data"]["content"][0]["text"] == "continue"


async def test_recovery_matches_only_the_whole_prompt_on_a_user_message(
    tmp_path: Path,
) -> None:
    """A user message that merely CONTAINS the prompt is not the prompt."""
    _mark(tmp_path)
    write_pending_replay(tmp_path, session_id="conv_1", prompt="ship it", blocked_turn_id="t1")
    client = _FakeServerClient(
        items=[
            {
                "id": "msg_1",
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "review the diff then ship it"}],
            }
        ]
    )

    task = schedule_pending_replay_recovery(
        "conv_1", bridge_dir=tmp_path, server_client=client, ready=lambda: True
    )
    assert task is not None
    await task

    assert len(client.posts) == 1


# ── D2: a pick equal to the live model is a terminal no-op ───────────


@pytest.mark.parametrize(
    ("case", "live_model", "routed_model"),
    [
        # Codex reports its live model as a dotted slug; the catalog uses
        # dashes and a prefix. A raw ``==`` never matched.
        ("codex-slug-vs-catalog-id", "gpt-5.6-luna", "databricks-gpt-5-6-luna"),
        ("codex-catalog-id-vs-slug", "databricks-gpt-5-6-luna", "gpt-5.6-luna"),
        # Claude's live model comes off the statusLine snapshot, which may or
        # may not carry the catalog prefix (or a ``[1m]`` suffix).
        ("claude-bare-vs-prefixed", "claude-sonnet-5", "databricks-claude-sonnet-5"),
        ("claude-prefixed-vs-bare", "databricks-claude-sonnet-5", "claude-sonnet-5"),
        ("claude-long-context-suffix", "databricks-claude-sonnet-5[1m]", "claude-sonnet-5"),
        # And the identical-spelling case, which did already work.
        ("identical", "gpt-5.6-luna", "gpt-5.6-luna"),
    ],
)
async def test_a_pick_equal_to_the_live_model_is_allowed_not_blocked(
    case: str,
    live_model: str,
    routed_model: str,
) -> None:
    """
    Nothing to switch means nothing to block and nothing to replay.

    The old code logged "already on routed model" and then fell through to
    ``action="route"`` anyway: the hook blocked the prompt, the runner replayed
    it, and the user watched their first turn vanish and come back — for a
    switch onto the model the pane was already running. The comparison was also
    on raw spellings, so it never fired for codex at all.
    """
    del case
    rec = _Recorder()

    async def _route(harness: str | None, prompt: str) -> tuple[str | None, dict[str, Any]]:
        rec.routed.append((harness, prompt))
        return routed_model, {"rationale": "cheap lookup", "model": routed_model}

    decision = await resolve_turn_route(
        "conv_1",
        _request(model=live_model),
        conv=_FakeConv(),
        route_turn=_route,
        pin=rec.pin,
        persist=rec.persist,
    )

    # Allowed, so the hook fast-paths: no block, and the relay arms no replay.
    assert decision.action == "allow"
    # Terminal AND naming the pick, so the chip and the marker agree with it.
    assert decision.terminal is True
    assert decision.model == routed_model
    assert decision.rationale == "cheap lookup"
    # The decision row and the pin still land — they are the route-once gate.
    assert rec.pinned == [routed_model]
    assert [model for model, _verdict in rec.chips] == [routed_model]


async def test_a_no_op_verdict_arms_no_replay(tmp_path: Path) -> None:
    """The relay only owes a replay for a verdict that blocked something."""
    client = _FakeServerClient(
        verdict={
            "action": "allow",
            "model": ROUTED_MODEL,
            "rationale": "already there",
            "terminal": True,
        }
    )

    decision = await make_server_relay_resolver(client, bridge_dir=tmp_path)("conv_1", _request())

    assert decision.action == "allow"
    assert read_pending_replay(tmp_path) is None


async def test_the_no_op_verdicts_decision_row_closes_the_gate() -> None:
    """The label the no-op wrote is what stops the next prompt re-routing."""
    rec = _Recorder()
    conv = _FakeConv()

    async def _route(harness: str | None, prompt: str) -> tuple[str | None, dict[str, Any]]:
        rec.routed.append((harness, prompt))
        return LAUNCH_MODEL, {"rationale": "cheap lookup", "model": LAUNCH_MODEL}

    first = await resolve_turn_route(
        "conv_1",
        _request(model=LAUNCH_MODEL),
        conv=conv,
        route_turn=_route,
        pin=rec.pin,
        persist=rec.persist,
    )
    assert first.action == "allow"
    # What the server writes once the decision persists.
    conv.labels.update(_routed_labels())

    second = await resolve_turn_route(
        "conv_1",
        _request(turn_id="turn_2", model=LAUNCH_MODEL),
        conv=conv,
        route_turn=_route,
        pin=rec.pin,
        persist=rec.persist,
    )
    assert second.action == "allow"
    assert len(rec.routed) == 1


# ── D4: the block marker is scoped to the session that wrote it ──────


def test_a_clear_rotation_can_route_again_while_the_same_session_still_skips(
    tmp_path: Path,
) -> None:
    """
    A bridge dir outlives the conversation keyed on it.

    ``/clear`` re-keys the superseded session to ``{id}-cleared`` and hands the
    SAME live dir to a brand-new conversation, and a fork inherits its parent's
    bridge id. A bare marker file therefore made every later conversation in
    the pane fast-skip on a verdict that was never theirs: after one routed
    first message, nothing in that pane could ever route again, and the routing
    it never got was attributed to the old session id.

    Scope: this covers the marker, which is the only gate that was silently
    wrong. Whether the replacement conversation then GETS routed is decided by
    the server — and the ``/clear`` rotation currently copies the old session's
    labels (routing-decision id included) while dropping its
    ``cost_control_mode_override``, so today the answer is a truthful, recorded
    "no" instead of a silent skip.
    """
    write_turn_routing_marker(tmp_path, session_id="conv_old", decision_id="decision-1")

    # The session that wrote it still skips — route-once is untouched.
    assert turn_routing_marker_present(tmp_path, "conv_old") is True
    # The /clear replacement, and a fork, each get their own first-message
    # routing rather than inheriting the old verdict.
    assert turn_routing_marker_present(tmp_path, "conv_new") is False
    assert turn_routing_marker_present(tmp_path, "conv_old-cleared") is False
    # And the decision it belongs to is on disk for diagnosis.
    assert json.loads((tmp_path / MARKER_FILE).read_text())["decision_id"] == "decision-1"


def test_a_marker_predating_the_scoping_reads_as_absent(tmp_path: Path) -> None:
    """A bare marker costs one round trip, which the server then declines.

    Route-once cannot regress on it: the authoritative gate is the session's
    routing-decision label, never this file.
    """
    (tmp_path / MARKER_FILE).write_text("", encoding="utf-8")

    assert turn_routing_marker_present(tmp_path, "conv_1") is False
    assert turn_routing.turn_routing_marker_session(tmp_path) is None


async def test_a_replay_waits_for_its_own_marker_not_a_forks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another session's marker is not the confirmation that WE blocked."""
    monkeypatch.setattr(turn_routing, "REPLAY_MARKER_WAIT_S", 0.2)
    monkeypatch.setattr(turn_routing, "REPLAY_POLL_S", 0.01)
    _mark(tmp_path, session_id="conv_other")
    client = _FakeServerClient()

    await schedule_replay(
        "conv_1",
        prompt="hello",
        bridge_dir=tmp_path,
        blocked_turn_id="turn_1",
        server_client=client,
        idle=lambda _turn: True,
    )

    assert client.posts == []


async def test_recovery_ignores_a_marker_another_session_left_behind(
    tmp_path: Path,
) -> None:
    """
    The fork-shared-dir guard now covers the marker too.

    The pending record was already session-checked; the marker beside it was
    not, so a fork's marker made our own recovery look confirmed.
    """
    _mark(tmp_path, session_id="conv_other")
    write_pending_replay(tmp_path, session_id="conv_1", prompt="hello", blocked_turn_id="t1")

    assert (
        schedule_pending_replay_recovery(
            "conv_1",
            bridge_dir=tmp_path,
            server_client=_FakeServerClient(items=[]),
            ready=lambda: True,
        )
        is None
    )
    # No marker of ours means the hook fell open, so nothing is owed.
    assert read_pending_replay(tmp_path) is None


# ── H2: a session that cannot route pays nothing per prompt ──────────


async def test_no_turn_router_is_started_for_a_session_with_routing_off(tmp_path: Path) -> None:
    """
    No advertisement is the switch that keeps the hook unregistered.

    Installed unconditionally, the hook put a routing round trip (25s worst
    case on a degraded server) in front of every submit of every native
    session, only ever to be told the session does not route.
    """
    router = ensure_session_turn_router(
        "conv_off",
        bridge_dir=tmp_path,
        server_client=_FakeServerClient(),
        harness="codex-native",
        routing_enabled=False,
    )
    try:
        assert router is None
        assert not (tmp_path / ADVERTISEMENT_FILE).exists()
    finally:
        shutdown_session_turn_router("conv_off", router)


# ── H4: every hook budget is strictly outside the call it waits on ───


def test_the_subagent_hook_budget_is_strictly_outside_its_request_budget() -> None:
    """
    A hook killed the instant its HTTP call gives up never fails open.

    ``claude-sdk`` registered its ``PreToolUse`` routing hook with the router
    call's OWN timeout, so the SDK could cancel the hook at the same moment the
    request timed out and the harness saw a dead hook instead of "no opinion".
    The other two paths already carried headroom.
    """
    from omnigent.inner.codex_executor import _CODEX_ROUTER_HOOK_TIMEOUT_SECONDS
    from omnigent.inner.hook_scripts.subagent_router import HOOK_TIMEOUT_S, REQUEST_TIMEOUT_S
    from omnigent.runner.subagent_routing import RELAY_TIMEOUT_S

    assert HOOK_TIMEOUT_S > REQUEST_TIMEOUT_S > RELAY_TIMEOUT_S
    # And the harness entries that register a budget agree with it.
    assert _CODEX_ROUTER_HOOK_TIMEOUT_SECONDS >= HOOK_TIMEOUT_S


def test_the_claude_sdk_spawn_hook_is_registered_outside_its_own_request() -> None:
    """The in-process claude-sdk hook reads the outer constant, not the inner."""
    import inspect

    from omnigent.inner import claude_sdk_executor
    from omnigent.inner.hook_scripts.subagent_router import HOOK_TIMEOUT_S, REQUEST_TIMEOUT_S

    source = inspect.getsource(claude_sdk_executor)
    assert "timeout=subagent_router.HOOK_TIMEOUT_S" in source
    assert "timeout=subagent_router.REQUEST_TIMEOUT_S" not in source
    assert HOOK_TIMEOUT_S > REQUEST_TIMEOUT_S
