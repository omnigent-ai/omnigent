"""Native-pane reap conformance, parametrized over the production harness registry.

Every native harness the registry declares reapable must satisfy one contract,
so a new harness is covered the moment it is registered:

* a real turn, run through the runner's routes and ended through the idle
  channel(s) the harness declares (``status_owner``), leaves a pane that is
  reaped one idle window after its last output, never sooner;
* the same when the idle edge is lost, an interrupt's idle is lost, or a relay
  re-asserts ``running`` forever;
* the reap tears down every sidecar (forwarder, relay, vendor server, prompt
  waiter) and every piece of per-session status state;
* a pane is never reaped while its turn is live, while a human is asked
  something (runner ASK, server elicitation, the harness's own prompt, a
  relayed ``blocked_on``), while a sub-agent works for it, or while a client
  is attached, however silent the pane and whatever the status says; and it
  is reaped once that ends;
* a harness the registry declares exempt is never offered to the reaper;
* the runner's idle watchdog waits for the turn while the reaper keeps its
  pane for work still going on: a lost idle holds it until the reap (or, for
  a pane the reaper never judges, until the ceiling after its last evidence),
  a printing turn for as long as it prints, and a silent turn the harness
  reports active within the ceiling.

The runner is real (``create_runner_app``, its HTTP routes, the resource
registry, the claude status poller, the tool relay, the MCP proxy approval
path); only tmux, the pane's watcher thread, the harness subprocess stream,
each harness's vendor state and the Omnigent server are faked. The reaper and
the status book share one fake monotonic clock, and tmux's ``window_activity``
and the pane's agent-output stamp are measured on it. ``_KNOWN_GAPS`` would
list (harness, scenario) pairs that cannot pass yet as strict xfails; it must
stay empty.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omnigent.harness_plugins import (
    _BUILTIN_NATIVE_PROVIDERS,
    NativeHarnessProvider,
    native_agents,
    native_providers,
)
from omnigent.inner import terminal as inner_terminal
from omnigent.native import prompt_parks
from omnigent.runner import app as runner_app
from omnigent.runner import pending_approvals
from omnigent.runner.app import _session_event_queues_ref
from omnigent.runner.native.interrupt import _UNIFORM_INTERRUPT
from omnigent.runner.proxy_mcp_manager import ProxyMcpManager
from omnigent.runner.session_status import StatusSource
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import pane_reaper as pane_reaper_module
from omnigent.terminals.pane_reaper import PANE_OUTPUT_BUSY_WINDOW_S
from tests.runner.conftest import _FakeProcessManager, _sse
from tests.terminals.native_pane_rig import (
    FakeServerClient,
    PaneRig,
    PlantedSidecars,
    TmuxFakes,
    build_pane_rig,
    plant_sidecars,
    report_harness_state,
)

# Rows come from the production registry accessors, not a copied list.
_AGENTS = {agent.key: agent for agent in native_agents()}
_PROVIDERS: dict[str, NativeHarnessProvider] = {
    provider.key: provider for provider in native_providers() if provider.key in _AGENTS
}
_BUILTIN_KEYS = [p.key for p in _BUILTIN_NATIVE_PROVIDERS]
_REAPABLE_KEYS = [key for key, p in _PROVIDERS.items() if p.pane_reap == "reap"]
_EXEMPT_KEYS = [key for key, p in _PROVIDERS.items() if p.pane_reap != "reap"]

_IDLE_WINDOW_S = 3600.0
_INTERVAL_S = 60.0
_AGENT_ID = "0f3a9c5e2b7d4e61a8c9d0b1e2f3a4b5"
_ELICITATION_ID = "elicit_conformance"

# (harness, scenario) -> why it cannot pass yet. "*" matches every scenario.
# Must stay empty: a harness that should never be reaped is declared exempt in
# the harness registry (with a reason) instead of listed here.
_KNOWN_GAPS: dict[tuple[str, str], str] = {}


# ── registry-level guarantees ────────────────────────────────────────────


def test_the_suite_covers_every_builtin_native_harness() -> None:
    assert set(_BUILTIN_KEYS) <= set(_PROVIDERS)
    assert len(_BUILTIN_KEYS) >= 12
    assert sorted(_REAPABLE_KEYS + _EXEMPT_KEYS) == sorted(_PROVIDERS)
    assert "devin" in _REAPABLE_KEYS
    for key in _REAPABLE_KEYS:
        assert _PROVIDERS[key].status_owner in _OWNER_CHANNELS, key


def test_no_known_gaps_remain() -> None:
    assert _KNOWN_GAPS == {}


def test_every_exemption_is_declared_with_a_reason() -> None:
    for key in _EXEMPT_KEYS:
        assert _PROVIDERS[key].pane_reap == "exempt"
        assert _PROVIDERS[key].pane_reap_exempt_reason
    assert [k for k in _EXEMPT_KEYS if k in _BUILTIN_KEYS] == ["kimi"]


def _xfail_gap(request: pytest.FixtureRequest, key: str, scenario: str) -> None:
    reason = _KNOWN_GAPS.get((key, scenario)) or _KNOWN_GAPS.get((key, "*"))
    if reason is not None:
        request.applymarker(pytest.mark.xfail(strict=True, reason=reason))


# ── the harness's declared channels ──────────────────────────────────────

# status_owner -> the local channel that reports the agent's own turn edges.
# ``forwarder`` harnesses have none (the runner's idle is not the agent's);
# ``runner_and_forwarder`` ones publish a runner idle right after injecting.
_OWNER_CHANNELS: dict[str, str | None] = {
    "status_file": "status_file",
    "pane": "pty",
    "forwarder": None,
    "runner_and_forwarder": None,
}


def _local_channel(key: str) -> str | None:
    owner = _PROVIDERS[key].status_owner
    assert owner is not None, f"{key} declares no status_owner"
    return _OWNER_CHANNELS[owner]


def _idle_cases() -> Iterator[Any]:
    for key in _REAPABLE_KEYS:
        orders = ("relay",)
        if _local_channel(key) is not None:
            orders = ("local", "relay", "local_then_relay", "relay_then_local")
        for order in orders:
            yield pytest.param(key, order, id=f"{key}-{order}")


def _lost_cases() -> Iterator[Any]:
    for key in _REAPABLE_KEYS:
        states = ("idle", "unknown") if _PROVIDERS[key].pane_turn_probe else ("none",)
        for state in states:
            yield pytest.param(key, state, id=f"{key}-vendor_{state}")


# ── fakes at the harness-process and server boundaries ───────────────────


class _Ok:
    status_code = 200
    headers: dict[str, str] = {}
    content = b""

    def raise_for_status(self) -> None:
        return None


class _TurnStream:
    """The harness subprocess: one response per turn, optionally held open."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.release.set()
        self.turns = 0

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        del method, url, json, timeout
        self.turns += 1
        response_id = f"resp_{self.turns}"
        release = self.release

        class _Handle:
            status_code = 200

            async def aiter_text(self) -> Any:
                yield _sse({"type": "response.created", "response": {"id": response_id}})
                await release.wait()
                yield _sse({"type": "response.completed", "response": {"id": response_id}})

        class _Ctx:
            status_code = 200

            async def __aenter__(self) -> _Handle:
                return _Handle()

            async def __aexit__(self, *_exc: object) -> None:
                return None

        return _Ctx()

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> _Ok:
        del url, json, timeout
        return _Ok()


class _Response:
    def __init__(self, body: dict[str, Any]) -> None:
        self.status_code = 200
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body

    def raise_for_status(self) -> None:
        return None


class _Clock:
    """One fake monotonic clock for the reaper, the status book and tmux."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class _TerminalTime:
    """``omnigent.inner.terminal``'s ``time``: the fake clock, plus real elapsed time.

    The pane's agent-output stamp is taken and aged on it, so output ages on
    the book's clock; the real part keeps any deadline loop in that module
    finite.
    """

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock
        self._base = time.monotonic()

    def monotonic(self) -> float:
        return self._clock.now + (time.monotonic() - self._base)

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


# ── driver ───────────────────────────────────────────────────────────────


class _Pane:
    """One native pane of one harness, driven through the runner's routes."""

    def __init__(
        self,
        rig: PaneRig,
        clock: _Clock,
        tmux: TmuxFakes,
        stream: _TurnStream,
        process_manager: _FakeProcessManager,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        self.rig = rig
        self.clock = clock
        self.tmux = tmux
        self.stream = stream
        self.process_manager = process_manager
        self.monkeypatch = monkeypatch
        self.tmp_path = tmp_path
        self.key = rig.agent.key
        self.conv = rig.conv_id
        self.sidecars: PlantedSidecars | None = None
        self.relay_file: Path | None = None
        self.turn_ended_at = clock.now

    @property
    def local_channel(self) -> str | None:
        return _local_channel(self.key)

    def printed(self) -> None:
        """The agent printed: tmux stamps ``window_activity``, the idle watcher agent output."""
        self.tmux.printed()
        instance = self.rig.terminal_registry.get(self.conv, self.rig.agent.terminal_name, "main")
        if instance is not None:
            # What the pane's idle watcher does when a tick sees agent output.
            instance._last_agent_output_at = inner_terminal.time.monotonic()

    async def post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.rig.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            return await client.post(path, json=body)

    async def event(self, body: dict[str, Any]) -> httpx.Response:
        return await self.post(f"/v1/sessions/{self.conv}/events", body)

    # session and turn

    async def open(self, *, printed: bool = True) -> None:
        """Create the session; *printed* ``False``: the pane went quiet long ago."""
        resp = await self.post("/v1/sessions", {"session_id": self.conv, "agent_id": _AGENT_ID})
        assert resp.status_code == 201, resp.text
        if printed:
            self.printed()
        if _PROVIDERS[self.key].pane_reap == "reap":
            assert self.rig.listed(), f"{self.key} pane is not offered to the reaper"
        await self.scan()  # the reaper's first sight of the pane

    async def start_turn(
        self,
        *,
        hold_runner_turn: bool = False,
        relay_running_first: bool = False,
        agent_running: bool = True,
    ) -> None:
        """A user message through the events route, then the agent's own running.

        *relay_running_first* delivers the forwarder's relayed ``running``
        before the local edge, so the wire dedup swallows the local one.
        """
        if hold_runner_turn:
            self.stream.release.clear()
        self.printed()
        resp = await self.event(
            {
                "type": "message",
                "agent_id": _AGENT_ID,
                "content": [{"type": "input_text", "text": "do the thing"}],
            }
        )
        assert resp.status_code == 202, resp.text
        if not hold_runner_turn:
            await self.runner_turn_done()
        else:
            for _ in range(200):
                if self.process_manager.has_active_turn(self.conv):
                    break
                await asyncio.sleep(0.01)
            assert self.conv in self.rig.app.state.active_turns
        if relay_running_first:
            await self.relay("running")
        if agent_running:
            await self.agent_running()
        if self.sidecars is None:
            self.sidecars = plant_sidecars(
                self.rig.app, self.conv, self.tmp_path, harness_key=self.key
            )
            binding = self.rig.app.state.session_comment_relays.get(self.conv)
            if binding is not None and binding.relay is not self.sidecars.relay:
                self.relay_file = binding.bridge_dir / "tool_relay.json"
                assert self.relay_file.exists()

    async def runner_turn_done(self) -> None:
        self.stream.release.set()
        turn = self.rig.app.state.active_turns.get(self.conv)
        if isinstance(turn, asyncio.Task):
            await asyncio.wait_for(turn, timeout=10)
        for _ in range(200):
            if self.conv not in self.rig.app.state.active_turns:
                break
            await asyncio.sleep(0.01)
        assert self.conv not in self.rig.app.state.active_turns

    async def agent_running(self) -> None:
        """The agent starts working, as its declared status channel reports it."""
        if self.local_channel == "status_file":
            self.rig.write_claude_status("busy")
            await self.rig.fire("on_tick")
        elif self.local_channel == "pty":
            await self.rig.fire("on_activity")
        else:
            await self.relay("running")
        current = self.rig.book.current(self.conv)
        assert current is not None and current.status == "running"

    async def work(self, intervals: int = 5) -> None:
        """The agent works for a while: the pane prints and every scan spares it."""
        for _ in range(intervals):
            self.clock.now += _INTERVAL_S
            self.printed()
            await self.scan()
            assert self.rig.alive()

    async def local_idle(self) -> None:
        if self.local_channel == "status_file":
            self.rig.write_claude_status("idle")
            await self.rig.fire("on_tick")
        else:
            await self.rig.fire("on_idle")

    async def relay(self, status: str, **data: str) -> None:
        resp = await self.event(
            {"type": "external_session_status", "data": {"status": status, **data}}
        )
        assert resp.status_code == 204, resp.text

    async def end_turn(self, order: str | None = None) -> None:
        """The agent's last output, then its idle through *order* (``None``: lost)."""
        self.printed()
        self.turn_ended_at = self.clock.now
        steps: dict[str, tuple[Callable[[], Awaitable[None]], ...]] = {
            "local": (self.local_idle,),
            "relay": (lambda: self.relay("idle"),),
            "local_then_relay": (self.local_idle, lambda: self.relay("idle")),
            "relay_then_local": (lambda: self.relay("idle"), self.local_idle),
        }
        for step in steps[order] if order is not None else ():
            await step()
        if order is not None:
            self.vendor("idle")

    def end_turn_order(self) -> str:
        """The harness's primary idle channel."""
        return "local" if self.local_channel is not None else "relay"

    def vendor(self, state: str) -> None:
        """What the harness's own state (status file, app-server, bridge) reports."""
        if state != "none":
            report_harness_state(self.rig, self.monkeypatch, self.tmp_path, state)

    # the runner's idle watchdog

    def runner_held(self) -> bool:
        """Whether the runner's idle watchdog would wait (``has_active_work``)."""
        return bool(self.rig.app.state.has_active_work())

    async def drop_prompt_waiter(self) -> None:
        """Retire the planted claude prompt waiter, which holds the runner by itself.

        What is left holding the runner is the session's turn.
        """
        waiter = self.rig.app.state.claude_prompt_waiters.pop(self.conv)
        waiter.cancel()
        await asyncio.wait({waiter})

    # reaper

    async def scan(self) -> None:
        await self.rig.reaper._scan_once()

    async def scan_until_reaped(self, *, windows: float) -> float | None:
        """Scan every interval for *windows* idle windows; the clock at the reap."""
        start = self.clock.now
        while self.clock.now - start < windows * _IDLE_WINDOW_S + 2 * _INTERVAL_S:
            self.clock.now += _INTERVAL_S
            await self.scan()
            if not self.rig.alive():
                return self.clock.now
        return None

    async def assert_reaped_after(
        self, since: float, *, windows: int = 1, full_window: bool = True
    ) -> float:
        """Reaped within bound of *since*, and no sooner than one idle window.

        The bound is *windows* idle windows plus the output-busy window (scans
        that saw fresh output re-arm the clock) plus one scan per window.
        *full_window* ``False`` drops the lower bound, for a hold only the
        pre-reap deep check sees: its clock re-arms at the last spare, not
        at the release.
        """
        reaped_at = await self.scan_until_reaped(windows=windows + 1)
        assert reaped_at is not None, f"{self.key} pane was never reaped"
        elapsed = reaped_at - since
        if full_window:
            assert elapsed >= _IDLE_WINDOW_S, f"reaped {elapsed:.0f}s after its last evidence"
        bound = windows * (_IDLE_WINDOW_S + _INTERVAL_S) + PANE_OUTPUT_BUSY_WINDOW_S
        assert elapsed <= bound, f"reaped {elapsed:.0f}s after its last evidence (> {bound})"
        return elapsed

    async def assert_never_reaped(self, *, windows: int = 3) -> None:
        assert await self.scan_until_reaped(windows=windows) is None, (
            f"{self.key} pane was reaped while it was still needed"
        )

    def assert_torn_down(self) -> None:
        """No pane, sidecar or per-session status state is left behind."""
        assert self.rig.closed == [self.conv]
        assert self.sidecars is not None
        assert self.sidecars.leftovers(self.rig.app) == []
        if self.relay_file is not None:
            assert not self.relay_file.exists(), "real tool relay still advertised"
        assert self.rig.book.current(self.conv) is None
        assert self.rig.resources.session_turn_is_active(self.conv) is False
        assert prompt_parks.oldest_open_age_s(self.conv) is None

    def cleanup(self) -> None:
        self.stream.release.set()
        prompt_parks.clear_session(self.conv)
        for child in runner_app._subagent_work_by_parent.pop(self.conv, set()):
            runner_app._subagent_work_by_child.pop(child, None)
        if self.sidecars is not None:
            self.sidecars.discard()
        self.rig.drain()

    # human waits

    async def open_runner_ask(self) -> asyncio.Task[str]:
        """A relayed tool call the server gates on ASK, parked on the runner."""
        server = self.rig.server
        base_post = server.post

        async def _post(url: str, **kwargs: Any) -> Any:
            if not url.endswith("/mcp"):
                return await base_post(url, **kwargs)
            params = kwargs["json"]["params"]
            if "inputResponses" not in params:
                return _Response(
                    {
                        "jsonrpc": "2.0",
                        "id": kwargs["json"]["id"],
                        "result": {
                            "resultType": "input_required",
                            "inputRequests": {_ELICITATION_ID: {"method": "elicitation/create"}},
                            "requestState": "opaque",
                        },
                    }
                )
            return _Response(
                {
                    "jsonrpc": "2.0",
                    "id": kwargs["json"]["id"],
                    "result": {"content": [{"type": "text", "text": "done"}]},
                }
            )

        self.monkeypatch.setattr(server, "post", _post)
        # What every native relay executor does for a relayed tool call.
        manager = ProxyMcpManager(
            self.conv,
            server,  # type: ignore[arg-type]
            publish_event=lambda *_a: None,
            execution_registry=self.rig.app.state.mcp_execution_registry,
        )
        call = asyncio.create_task(manager.call_tool(None, "github__create_issue", {"t": "x"}))
        for _ in range(200):
            if pending_approvals.has_pending(self.conv):
                break
            await asyncio.sleep(0)
        assert pending_approvals.has_pending(self.conv)
        return call

    async def answer_runner_ask(self, call: asyncio.Task[str]) -> None:
        resp = await self.event(
            {"type": "approval", "data": {"elicitation_id": _ELICITATION_ID, "action": "accept"}}
        )
        assert resp.status_code < 300, resp.text
        assert await asyncio.wait_for(call, timeout=5) == "done"
        assert not pending_approvals.has_pending(self.conv)


async def _pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, *, printed: bool = True
) -> _Pane:
    clock = _Clock()
    monkeypatch.setattr(pane_reaper_module, "time", SimpleNamespace(monotonic=clock))
    monkeypatch.setattr(inner_terminal, "time", _TerminalTime(clock))
    tmux = TmuxFakes(clock=clock)
    stream = _TurnStream()
    process_manager = _FakeProcessManager(stream)  # type: ignore[arg-type]
    spec = AgentSpec(
        spec_version=1,
        name="conformance",
        executor=ExecutorSpec(type="omnigent", config={"harness": _AGENTS[key].harness}),
    )

    async def _resolve(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    rig = await build_pane_rig(
        tmp_path,
        monkeypatch,
        key=key,
        idle_timeout_s=_IDLE_WINDOW_S,
        tmux=tmux,
        server=FakeServerClient(),
        process_manager=process_manager,
        status_clock=clock,
        spec_resolver=_resolve,
    )
    pane = _Pane(rig, clock, tmux, stream, process_manager, monkeypatch, tmp_path)
    await pane.open(printed=printed)
    return pane


@pytest.fixture
def pane_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[str], Awaitable[_Pane]]]:
    made: list[_Pane] = []

    async def _make(key: str, *, printed: bool = True) -> _Pane:
        pane = await _pane(tmp_path, monkeypatch, key, printed=printed)
        made.append(pane)
        return pane

    yield _make
    for pane in made:
        pane.cleanup()


# ── idle through each declared channel, in either order ──────────────────


@pytest.mark.parametrize(("key", "order"), list(_idle_cases()))
async def test_a_real_turn_ended_through_its_channels_is_reaped_one_window_later(
    pane_factory: Callable[[str], Awaitable[_Pane]],
    request: pytest.FixtureRequest,
    key: str,
    order: str,
) -> None:
    _xfail_gap(request, key, "idle")
    pane = await pane_factory(key)
    await pane.start_turn()
    await pane.work()
    await pane.end_turn(order)
    claim = pane.rig.book.claim(pane.conv, include_relay=True)
    assert claim is None, f"{order} idle left a running claim: {claim}"
    await pane.assert_reaped_after(pane.turn_ended_at)
    pane.assert_torn_down()


@pytest.mark.parametrize("key", _REAPABLE_KEYS)
async def test_a_pane_first_seen_after_a_runner_restart_gets_one_full_window(
    pane_factory: Callable[..., Awaitable[_Pane]], key: str
) -> None:
    # A fresh runner adopts a pane that went quiet hours ago; its book is empty.
    pane = await pane_factory(key, printed=False)
    first_seen = pane.clock.now
    assert pane.tmux.output_age_s is not None and pane.tmux.output_age_s > _IDLE_WINDOW_S
    assert pane.rig.book.current(pane.conv) is None
    reaped_at = await pane.scan_until_reaped(windows=2)
    assert reaped_at is not None
    assert _IDLE_WINDOW_S <= reaped_at - first_seen <= _IDLE_WINDOW_S + _INTERVAL_S
    assert pane.rig.closed == [pane.conv]


# ── the idle edge is lost ────────────────────────────────────────────────


@pytest.mark.parametrize(("key", "vendor"), list(_lost_cases()))
async def test_a_real_turn_whose_idle_is_lost_is_still_reaped(
    pane_factory: Callable[[str], Awaitable[_Pane]],
    request: pytest.FixtureRequest,
    key: str,
    vendor: str,
) -> None:
    _xfail_gap(request, key, "lost_idle")
    pane = await pane_factory(key)
    await pane.start_turn()
    await pane.work()
    await pane.end_turn(None)
    assert pane.rig.book.claim(pane.conv, include_relay=True) is not None
    pane.vendor(vendor)
    # One extra window only when the harness cannot answer for the stale claim.
    elapsed = await pane.assert_reaped_after(
        pane.turn_ended_at, windows=2 if vendor == "unknown" else 1
    )
    if vendor == "unknown":
        assert elapsed >= 2 * _IDLE_WINDOW_S, "an unanswered stale claim got no extra window"
    pane.assert_torn_down()


@pytest.mark.parametrize("key", [k for k in _REAPABLE_KEYS if _PROVIDERS[k].pane_turn_probe])
async def test_a_probe_that_cannot_answer_never_delays_a_finished_turn(
    pane_factory: Callable[[str], Awaitable[_Pane]],
    request: pytest.FixtureRequest,
    key: str,
) -> None:
    _xfail_gap(request, key, "probe_unknown")
    pane = await pane_factory(key)
    await pane.start_turn()
    await pane.work()
    await pane.end_turn(pane.end_turn_order())
    pane.vendor("unknown")  # the harness's own state is gone or unreadable
    await pane.assert_reaped_after(pane.turn_ended_at)
    pane.assert_torn_down()


@pytest.mark.parametrize("key", _REAPABLE_KEYS)
async def test_an_interrupt_whose_idle_is_lost_is_still_reaped(
    pane_factory: Callable[[str], Awaitable[_Pane]],
    request: pytest.FixtureRequest,
    key: str,
) -> None:
    _xfail_gap(request, key, "interrupt")
    pane = await pane_factory(key)
    await pane.start_turn()
    await pane.work()
    # The user interrupts; the agent stops, but no idle edge reaches the runner.
    pane.vendor("idle")
    uniform = _UNIFORM_INTERRUPT.get(key)
    if uniform is not None:
        module = importlib.import_module(uniform.module)
        pane.monkeypatch.setattr(module, uniform.inject_fn, lambda *_a, **_k: None)
    if key == "claude":
        from omnigent.harnesses.claude_native import bridge as claude_bridge

        pane.monkeypatch.setattr(claude_bridge, "inject_interrupt", lambda *_a, **_k: None)
    resp = await pane.event({"type": "interrupt"})
    assert resp.status_code == 204, resp.text
    await pane.end_turn(None)
    await pane.assert_reaped_after(pane.turn_ended_at)
    pane.assert_torn_down()


@pytest.mark.parametrize(
    "key", [k for k in _REAPABLE_KEYS if k in _UNIFORM_INTERRUPT and _PROVIDERS[k].pane_turn_probe]
)
async def test_an_interrupt_whose_turn_end_the_harness_never_logged_is_still_reaped(
    pane_factory: Callable[[str], Awaitable[_Pane]], key: str
) -> None:
    # The harness's own state still shows the interrupted turn as open (devin:
    # a prompt with no Stop in its hook log). Only the idle the interrupt route
    # records tells the reaper the turn ended.
    pane = await pane_factory(key)
    await pane.start_turn()
    await pane.work()
    pane.vendor("active")
    _stub_native_controls(pane)
    resp = await pane.event({"type": "interrupt"})
    assert resp.status_code == 204, resp.text
    assert pane.rig.book.last_control_idle_at(pane.conv) is not None
    await pane.end_turn(None)
    await pane.assert_reaped_after(pane.turn_ended_at)
    pane.assert_torn_down()


@pytest.mark.parametrize(
    "key", [k for k in _REAPABLE_KEYS if _PROVIDERS[k].status_owner == "runner_and_forwarder"]
)
async def test_the_runners_own_idle_is_recorded_for_a_runner_and_forwarder_harness(
    pane_factory: Callable[[str], Awaitable[_Pane]], key: str
) -> None:
    # The runner publishes idle right after injecting the prompt; the book must
    # hear it (and not only the forwarder's later relay).
    pane = await pane_factory(key)
    await pane.start_turn(agent_running=False)
    current = pane.rig.book.current(pane.conv)
    assert current is not None and current.status == "idle", current
    assert StatusSource.RUNNER in current.sources


@pytest.mark.parametrize("key", _REAPABLE_KEYS)
async def test_a_late_and_reasserted_relay_running_cannot_pin_a_pane(
    pane_factory: Callable[[str], Awaitable[_Pane]],
    request: pytest.FixtureRequest,
    key: str,
) -> None:
    _xfail_gap(request, key, "late_relay")
    pane = await pane_factory(key)
    await pane.start_turn()
    await pane.end_turn(pane.end_turn_order())
    reaped_at = None
    while pane.clock.now - pane.turn_ended_at <= 3 * _IDLE_WINDOW_S:
        await pane.relay("running")  # a forwarder re-posting a finished turn
        pane.clock.now += _INTERVAL_S
        await pane.scan()
        if not pane.rig.alive():
            reaped_at = pane.clock.now
            break
    assert reaped_at is not None, f"{key} pane pinned by a relayed running"
    elapsed = reaped_at - pane.turn_ended_at
    assert _IDLE_WINDOW_S <= elapsed <= _IDLE_WINDOW_S + _INTERVAL_S + PANE_OUTPUT_BUSY_WINDOW_S
    pane.assert_torn_down()


@pytest.mark.parametrize("key", [key for key in _REAPABLE_KEYS if _local_channel(key) is not None])
async def test_local_edges_the_wire_dedup_swallows_are_still_recorded(
    pane_factory: Callable[[str], Awaitable[_Pane]],
    request: pytest.FixtureRequest,
    key: str,
) -> None:
    _xfail_gap(request, key, "dedup")
    pane = await pane_factory(key)
    await pane.start_turn(relay_running_first=True)
    local = StatusSource.STATUS_FILE if pane.local_channel == "status_file" else StatusSource.PTY
    claim = pane.rig.book.claim(pane.conv)
    assert claim is not None and local in claim.sources, claim
    await pane.work()
    await pane.end_turn("relay_then_local")
    current = pane.rig.book.current(pane.conv)
    assert current is not None and current.status == "idle"
    assert local in current.sources, current
    await pane.assert_reaped_after(pane.turn_ended_at)
    pane.assert_torn_down()


# The session.status events v0.15.0 queued for these sequences, by the
# harness's status owner (checked byte-identical against the release): the
# status book only records, it never publishes.
_V0_15_WIRE: dict[tuple[str, str], list[str]] = {
    ("status_file", "local_first"): ["running"],
    ("status_file", "relay_first"): ["idle"],
    ("pane", "local_first"): ["running"],
    ("pane", "relay_first"): ["idle"],
    ("forwarder", "local_first"): ["running"],
    ("forwarder", "relay_first"): ["running"],
    ("runner_and_forwarder", "local_first"): ["running", "idle"],
    ("runner_and_forwarder", "relay_first"): ["running", "idle"],
}


@pytest.mark.parametrize("order", ["local_first", "relay_first"])
@pytest.mark.parametrize("key", sorted(_PROVIDERS))
async def test_the_wire_is_unchanged_while_the_book_records_every_edge(
    pane_factory: Callable[[str], Awaitable[_Pane]], key: str, order: str
) -> None:
    pane = await pane_factory(key)
    await pane.start_turn(agent_running=False)
    local = pane.local_channel is not None

    async def _local(status: str) -> None:
        if not local:
            return
        if status == "running":
            await pane.agent_running()
        else:
            await pane.local_idle()

    steps = [(_local, "running"), (pane.relay, "running"), (pane.relay, "idle"), (_local, "idle")]
    if order == "relay_first":
        steps = [steps[1], steps[0], steps[3], steps[2]]
    for step, status in steps:
        await step(status)
    queue = _session_event_queues_ref.get(pane.conv)
    wire = []
    while queue is not None and not queue.empty():
        event = queue.get_nowait()
        if isinstance(event, dict) and event.get("type") == "session.status":
            wire.append(event)
    owner = _PROVIDERS[key].status_owner
    assert owner is not None
    assert wire == [{"type": "session.status", "status": s} for s in _V0_15_WIRE[owner, order]]
    # ...while the book heard both channels and ended the turn.
    current = pane.rig.book.current(pane.conv)
    assert current is not None and current.status == "idle"
    assert StatusSource.RELAY in current.sources
    if local:
        local_source = (
            StatusSource.STATUS_FILE if pane.local_channel == "status_file" else StatusSource.PTY
        )
        assert local_source in current.sources
    assert pane.rig.book.claim(pane.conv, include_relay=True) is None


# What v0.15.0 queued for: turn, the agent's running edge, a relayed running,
# then an interrupt and a stop_session (checked byte-identical against the
# release). Pinned per built-in: a new harness must pin its own.
_V0_15_CONTROL_WIRE: dict[str, list[str]] = {
    "claude": ["running", "idle"],
    "codex": ["running"],
    "pi": ["running"],
    "opencode": ["running", "idle"],
    "cursor": ["running", "idle"],
    "kiro": ["running", "idle"],
    "goose": ["running", "idle"],
    "antigravity": ["running"],
    "qwen": ["running", "idle"],
    "kimi": ["running", "idle"],
    "hermes": ["running", "idle"],
    "devin": ["running", "idle", "idle", "idle"],
}


def _stub_native_controls(pane: _Pane) -> None:
    """Fake each harness's interrupt/stop at its tmux or app-server boundary."""
    from omnigent.harnesses.claude_native import bridge as claude_bridge
    from omnigent.runner.native.interrupt import _UNIFORM_STOP

    def _noop(*_a: object, **_k: object) -> None:
        return None

    pane.monkeypatch.setattr(claude_bridge, "inject_interrupt", _noop)
    pane.monkeypatch.setattr(claude_bridge, "kill_session", _noop)
    for uniform in _UNIFORM_INTERRUPT.values():
        pane.monkeypatch.setattr(importlib.import_module(uniform.module), uniform.inject_fn, _noop)
    for stop in _UNIFORM_STOP.values():
        pane.monkeypatch.setattr(importlib.import_module(stop.module), "kill_session", _noop)


@pytest.mark.parametrize("key", _BUILTIN_KEYS)
async def test_interrupt_and_stop_publish_what_v0_15_did(
    pane_factory: Callable[[str], Awaitable[_Pane]], key: str
) -> None:
    pane = await pane_factory(key)
    _stub_native_controls(pane)
    pane.vendor("idle")  # codex: bridge state for the interrupt path
    await pane.start_turn()
    await pane.relay("running")
    for control in ("interrupt", "stop_session"):
        resp = await pane.event({"type": control})
        assert resp.status_code == 204, resp.text
    queue = _session_event_queues_ref.get(pane.conv)
    wire = []
    while queue is not None and not queue.empty():
        event = queue.get_nowait()
        if isinstance(event, dict) and event.get("type") == "session.status":
            wire.append(event["status"])
    assert wire == _V0_15_CONTROL_WIRE[key]


# ── never reaped while needed, reaped once that ends ────────────────────


@pytest.mark.parametrize("key", _REAPABLE_KEYS)
async def test_a_live_runner_turn_is_never_reaped(
    pane_factory: Callable[[str], Awaitable[_Pane]], key: str
) -> None:
    pane = await pane_factory(key)
    await pane.start_turn(hold_runner_turn=True)
    await pane.assert_never_reaped()  # the pane is silent the whole time
    await pane.runner_turn_done()
    released = pane.clock.now
    await pane.end_turn(pane.end_turn_order())
    await pane.assert_reaped_after(released)
    pane.assert_torn_down()


@pytest.mark.parametrize("key", _REAPABLE_KEYS)
async def test_an_agent_still_working_after_its_runner_turn_is_never_reaped(
    pane_factory: Callable[[str], Awaitable[_Pane]], key: str
) -> None:
    pane = await pane_factory(key)
    await pane.start_turn()
    if _PROVIDERS[key].pane_turn_probe:
        # A long silent tool call: only the harness's own state says it works.
        pane.vendor("active")
        await pane.assert_never_reaped()
    else:
        # Pane-status harnesses are known to work by their output.
        await pane.work(intervals=int(3 * _IDLE_WINDOW_S / _INTERVAL_S))
    await pane.end_turn(pane.end_turn_order())
    await pane.assert_reaped_after(pane.turn_ended_at)
    pane.assert_torn_down()


_WAITS = (
    "runner_ask",
    "server_elicitation",
    "harness_prompt",
    "relayed_blocked_on",
    "attached_client",
    "running_child",
)


@pytest.mark.parametrize("wait", _WAITS)
@pytest.mark.parametrize("key", _REAPABLE_KEYS)
async def test_a_pane_waiting_on_a_human_or_a_child_is_never_reaped(
    pane_factory: Callable[[str], Awaitable[_Pane]], key: str, wait: str
) -> None:
    pane = await pane_factory(key)
    await pane.start_turn()
    # Worst case: every status channel already says the turn is over.
    await pane.end_turn(pane.end_turn_order())
    assert pane.rig.book.claim(pane.conv, include_relay=True) is None
    release: Callable[[], Awaitable[None]]
    if wait == "runner_ask":
        call = await pane.open_runner_ask()

        async def release() -> None:
            await pane.answer_runner_ask(call)

    elif wait == "server_elicitation":
        pane.rig.server.pending = [
            {"elicitation_id": "elicit_srv", "params": {"target_session_id": pane.conv}}
        ]

        async def release() -> None:
            pane.rig.server.pending = []

    elif wait == "harness_prompt":
        release = await _open_harness_prompt(pane)
    elif wait == "relayed_blocked_on":
        await pane.relay("running", blocked_on="permission prompt")

        async def release() -> None:
            await pane.relay("idle")

    elif wait == "running_child":
        child = runner_app.register_subagent_work(
            parent_session_id=pane.conv,
            child_session_id=f"{pane.conv}_child",
            agent="worker",
            title="long job",
        )
        child.status = "running"

        async def release() -> None:
            child.status = "completed"

    else:
        pane.tmux.clients = ["/dev/ttys001"]

        async def release() -> None:
            pane.tmux.clients = []

    await pane.assert_never_reaped()
    await release()
    released = pane.clock.now
    deep_check_only = wait == "server_elicitation" or (
        wait == "harness_prompt" and key in ("codex", "opencode")
    )
    await pane.assert_reaped_after(released, full_window=not deep_check_only)
    pane.assert_torn_down()


async def _open_harness_prompt(pane: _Pane) -> Callable[[], Awaitable[None]]:
    """The harness's own permission prompt owns the pane's input.

    claude, codex and opencode expose it in vendor state (status file
    ``waiting``, ``thread/read`` ``waitingOnApproval``, a pending opencode
    permission); every other harness mirrors it in the runner as a prompt park.
    """
    if pane.key == "claude":
        pane.vendor("parked")
        await pane.rig.fire("on_tick")
        blocked = pane.rig.book.blocked(pane.conv)
        assert blocked is not None and blocked[0] == "permission prompt"

        async def _answer() -> None:
            pane.vendor("idle")
            await pane.rig.fire("on_tick")

        return _answer
    if pane.key in ("codex", "opencode"):
        pane.vendor("parked")

        async def _answer_vendor() -> None:
            pane.vendor("idle")

        return _answer_vendor
    prompt_parks.open_park(pane.conv, f"{pane.key}:prompt_1")

    async def _answer_park() -> None:
        prompt_parks.close_park(pane.conv, f"{pane.key}:prompt_1")

    return _answer_park


# ── the runner's idle watchdog ───────────────────────────────────────────
#
# Native delivery returns once the prompt is typed, so the runner's idle
# watchdog holds for the agent's own turn. It must wait for as long as the
# reaper keeps the pane for work still going on, and let go no later than the
# reap. Each case retires the planted claude prompt waiter, which would hold
# the runner by itself.

_MAX_TURN_ENV = "OMNIGENT_NATIVE_PANE_MAX_TURN_S"


def _runner_hold_expiries(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if getattr(r, "event_name", None) == "runner_in_flight_hold_expired"
    ]


@pytest.mark.parametrize(("key", "vendor"), list(_lost_cases()))
async def test_a_lost_idle_holds_the_runner_until_the_reap_and_no_longer(
    pane_factory: Callable[[str], Awaitable[_Pane]],
    request: pytest.FixtureRequest,
    caplog: pytest.LogCaptureFixture,
    key: str,
    vendor: str,
) -> None:
    _xfail_gap(request, key, "runner_hold")
    pane = await pane_factory(key)
    await pane.start_turn()
    await pane.drop_prompt_waiter()
    assert pane.runner_held()
    await pane.work()
    await pane.end_turn(None)
    pane.vendor(vendor)
    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        while pane.rig.alive():
            assert pane.runner_held(), f"{key}: the runner let go of a turn the reaper keeps"
            assert pane.clock.now - pane.turn_ended_at <= 3 * _IDLE_WINDOW_S, "never reaped"
            pane.clock.now += _INTERVAL_S
            await pane.scan()
        # The reap reset the session's status (or a refutation ended the claim
        # just before it), so the runner can shut down at once.
        assert not pane.runner_held()
    assert _runner_hold_expiries(caplog) == []  # the reap ended it, not the ceiling
    pane.assert_torn_down()


@pytest.mark.parametrize("key", _BUILTIN_KEYS)
async def test_a_printing_turn_holds_the_runner_past_two_idle_windows(
    pane_factory: Callable[[str], Awaitable[_Pane]],
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    # The ceiling at its floor, one idle window: past it only the pane's
    # output, never the turn's age, can keep the runner waiting.
    monkeypatch.setenv(_MAX_TURN_ENV, str(_IDLE_WINDOW_S))
    pane = await pane_factory(key)
    await pane.start_turn()
    await pane.drop_prompt_waiter()
    started = pane.clock.now
    while pane.clock.now - started <= 2 * _IDLE_WINDOW_S + _INTERVAL_S:
        await pane.work(intervals=1)  # prints; the reaper spares the pane
        assert pane.runner_held(), f"{key}: a printing turn stopped holding the runner"
    await pane.end_turn(pane.end_turn_order())
    assert not pane.runner_held()


@pytest.mark.parametrize("key", [k for k in _REAPABLE_KEYS if _PROVIDERS[k].pane_turn_probe])
async def test_a_silent_turn_its_harness_reports_active_holds_the_runner(
    pane_factory: Callable[[str], Awaitable[_Pane]],
    request: pytest.FixtureRequest,
    key: str,
) -> None:
    _xfail_gap(request, key, "runner_hold")
    pane = await pane_factory(key)
    await pane.start_turn()
    await pane.drop_prompt_waiter()
    pane.vendor("active")  # a long silent tool call, well within the ceiling
    started = pane.clock.now
    while pane.clock.now - started <= 3 * _IDLE_WINDOW_S:
        pane.clock.now += _INTERVAL_S
        await pane.scan()
        assert pane.rig.alive()
        assert pane.runner_held(), f"{key}: the runner let go of a turn its harness reports"
    await pane.end_turn(pane.end_turn_order())
    assert not pane.runner_held()
    await pane.assert_reaped_after(pane.turn_ended_at)
    pane.assert_torn_down()


@pytest.mark.parametrize("key", _BUILTIN_KEYS)
async def test_a_lost_idle_the_reaper_never_judges_holds_the_runner_until_the_ceiling(
    pane_factory: Callable[[str], Awaitable[_Pane]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    key: str,
) -> None:
    # kimi's pane is exempt; every other harness's pane has a client attached.
    # Nothing reaps or refutes the stale claim, so only the ceiling ends it.
    ceiling = 2 * _IDLE_WINDOW_S
    monkeypatch.setenv(_MAX_TURN_ENV, str(ceiling))
    pane = await pane_factory(key)
    if _PROVIDERS[key].pane_reap == "reap":
        pane.tmux.clients = ["/dev/ttys001"]
    await pane.start_turn()
    await pane.drop_prompt_waiter()
    await pane.work()
    await pane.end_turn(None)  # the last output, and the idle is lost
    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        while pane.clock.now - pane.turn_ended_at < ceiling - _INTERVAL_S:
            pane.clock.now += _INTERVAL_S
            await pane.scan()
            assert pane.rig.alive()
            assert pane.runner_held()
        pane.clock.now = pane.turn_ended_at + ceiling - 1.0
        assert pane.runner_held()
        assert _runner_hold_expiries(caplog) == []
        pane.clock.now += 1.0
        assert not pane.runner_held()
        assert not pane.runner_held()
    (expired,) = _runner_hold_expiries(caplog)
    assert expired.session_id == pane.conv  # type: ignore[attr-defined]
    assert expired.attributes["status"] == "running"  # type: ignore[attr-defined]
    assert expired.attributes["ceiling_s"] == ceiling  # type: ignore[attr-defined]
    assert pane.rig.alive()


# ── exemptions ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", _EXEMPT_KEYS)
async def test_an_exempt_harness_pane_is_never_offered_or_reaped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    key: str,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(pane_reaper_module, "time", SimpleNamespace(monotonic=clock))
    rig = await build_pane_rig(
        tmp_path, monkeypatch, key=key, idle_timeout_s=_IDLE_WINDOW_S, status_clock=clock
    )
    rig.app.state.session_harness_overrides[rig.conv_id] = rig.agent.harness
    await rig.fire("on_activity")
    await rig.fire("on_idle")
    sidecars = plant_sidecars(rig.app, rig.conv_id, tmp_path, harness_key=rig.agent.key)
    caplog.set_level("INFO", logger="omnigent.terminals.pane_reaper")
    try:
        assert not rig.listed()
        for _ in range(int(3 * _IDLE_WINDOW_S / _INTERVAL_S)):
            clock.now += _INTERVAL_S
            await rig.reaper._scan_once()
        assert rig.alive()
        assert sidecars.intact(rig.app)  # nor are its sidecars swept
        skipped = [r for r in caplog.records if "is not reaped" in r.getMessage()]
        assert len(skipped) == 1
        reason = _PROVIDERS[key].pane_reap_exempt_reason
        assert reason is not None and reason in skipped[0].getMessage()
    finally:
        sidecars.discard()
        rig.drain()
