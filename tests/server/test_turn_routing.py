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
    resolve_turn_route,
    schedule_replay,
    shutdown_session_turn_router,
    start_turn_router,
)

ROUTED_MODEL = "gpt-5.6-luna"
LAUNCH_MODEL = "gpt-5.6-sol"


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


async def test_routing_disabled_is_a_non_terminal_no_op() -> None:
    """The toggle can be flipped mid-session, so the hook must keep asking."""
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
    assert decision.terminal is False
    assert rec.routed == []


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
    assert (
        ensure_session_turn_router(
            "conv_x", bridge_dir=tmp_path, server_client=object(), harness="claude-native"
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

    async def post(self, url: str, *, json: dict[str, Any], timeout: float) -> _FakeResponse:
        del timeout
        self.posts.append((url, json))
        if self.fail:
            raise RuntimeError("server unreachable")
        return _FakeResponse(self.verdict)


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
    (tmp_path / MARKER_FILE).write_text("", encoding="utf-8")
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


async def test_replay_waits_for_the_blocked_turn_to_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delivering mid-abort would be steered into the dying turn."""
    monkeypatch.setattr(turn_routing, "REPLAY_MARKER_WAIT_S", 1.0)
    monkeypatch.setattr(turn_routing, "REPLAY_IDLE_WAIT_S", 5.0)
    monkeypatch.setattr(turn_routing, "REPLAY_POLL_S", 0.01)
    (tmp_path / MARKER_FILE).write_text("", encoding="utf-8")
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
    (tmp_path / MARKER_FILE).write_text("", encoding="utf-8")
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
