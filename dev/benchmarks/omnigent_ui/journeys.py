"""Browser user-journey definitions and the runner that times them.

A :class:`UIJourney` names a user-facing browser flow, an optional ``setup``
run once, an optional per-repetition ``prepare`` (untimed — positions the page
at the journey's starting state), a ``measure`` coroutine (the timed unit), and
an optional ``teardown``. :func:`run_ui_journey` boots each journey's pages,
runs ``warmup`` throwaway reps then ``runs``×``iterations`` timed reps, times the
awaited stop-assertion inside ``measure`` with :func:`time.perf_counter`, and
tallies each timed rep's network requests (see :mod:`netcapture`).

Isolation is per journey:

- ``fresh_context`` — a new browser context + page per rep, so cold-visit
  journeys (``landing_load``, ``new_session_first_token``) measure a true first
  paint. Prerequisite sessions are created (untimed) in ``prepare``.
- ``shared_page`` — one page reused across a journey's reps, so JS module state
  survives. ``switch_sessions`` needs this: switching is client-side navigation
  via the sidebar link, and a full reload would reset the store.

The four journeys:

- ``landing_load`` — navigate to ``/`` and wait for the landing composer.
- ``new_session_first_token`` — on a fresh bound session, type into the
  in-session composer and time to the first streamed assistant token.
- ``switch_sessions`` — click between two seeded sessions in the sidebar
  (client-side) and time to the target conversation rendering.
- ``fork_session`` — fork from an assistant response and time to the forked
  conversation rendering.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, cast

import httpx
from playwright.async_api import Page, expect

from dev.benchmarks.omnigent.measure import RunResult

from .environment import UIEnvironment
from .netcapture import NetCapture, RepCapture, aggregate_network
from .ui_driver import (
    ASSISTANT_BUBBLE,
    COMPOSER_PLACEHOLDER,
    FORK_FROM_RESPONSE,
    FORK_SUBMIT,
    LANDING_INPUT,
    SEND_BUTTON_NAME,
    USER_BUBBLE,
    UIDriver,
    read_browser_timing,
    sidebar_session_link,
)

Isolation = Literal["fresh_context", "shared_page"]

# Opaque per-journey context returned by ``setup`` and threaded to the other
# hooks; each hook casts it to the concrete type it expects.
JourneyContext = object

# Per-assertion budgets. A UI stop-assertion waits on real render/stream work,
# so these are generous enough to absorb a cold first paint without masking a
# true hang (mirrors the e2e_ui suite's timeouts).
_ASSERT_TIMEOUT_MS = 30_000
_FIRST_TOKEN_TIMEOUT_MS = 60_000

# Iteration cap: browser journeys cost ~1s+ per rep (real render + stream), so a
# large --iterations tuned for the millisecond HTTP journeys would overrun CI.
# Take a few samples per run and lean on --runs for repeats.
_UI_MAX_ITERATIONS = 8

# Seeded history items per switch-journey session — enough to render a
# conversation the switch can wait on, cheap to seed over HTTP.
_SWITCH_SEED_ITEMS = 4

# Deterministic mock reply streamed word-by-word for the turn-driving journeys.
_REPLY_TEXT = "Hello there, this is a mock benchmark reply."
_TURN_PROMPT = "Say hello."


@dataclass
class UIJourney:
    """One benchmarkable browser journey.

    :param name: Stable id used on the CLI and as the report key.
    :param isolation: ``fresh_context`` (new page per rep) or ``shared_page``
        (one page reused, preserving JS state).
    :param measure: Coroutine performing exactly one timed browser operation,
        given ``(env, page, ctx)``. Its awaited stop-assertion bounds the timer.
    :param setup: Coroutine run once before timing; its return is the ``ctx``.
    :param prepare: Coroutine run before every rep, OUTSIDE the timer, to
        position the page at the journey's starting state.
    :param teardown: Coroutine run once after timing, given ``ctx``.
    :param capture_nav_timing: Read Navigation Timing + FCP after each timed
        rep (only meaningful for journeys whose ``measure`` is a navigation).
    :param max_iterations: Upper bound clamping ``--iterations`` down (never up).
    :param description: One-liner for ``--list`` / the report.
    """

    name: str
    isolation: Isolation
    measure: Callable[[UIEnvironment, Page, JourneyContext], Awaitable[None]]
    setup: Callable[[UIEnvironment], Awaitable[JourneyContext]] | None = None
    prepare: Callable[[UIEnvironment, Page, JourneyContext], Awaitable[None]] | None = None
    teardown: Callable[[UIEnvironment, JourneyContext], Awaitable[None]] | None = None
    capture_nav_timing: bool = False
    max_iterations: int = _UI_MAX_ITERATIONS
    description: str = ""

    async def run_setup(self, env: UIEnvironment) -> JourneyContext:
        return await self.setup(env) if self.setup is not None else None

    async def run_prepare(self, env: UIEnvironment, page: Page, ctx: JourneyContext) -> None:
        if self.prepare is not None:
            await self.prepare(env, page, ctx)

    async def run_teardown(self, env: UIEnvironment, ctx: JourneyContext) -> None:
        if self.teardown is not None:
            await self.teardown(env, ctx)


@dataclass
class _Sink:
    """Where a timed rep records its latency, network tally, and browser timing."""

    result: RunResult
    net_reps: list[RepCapture]
    timings: list[dict[str, float]]


async def _one_rep(
    journey: UIJourney,
    env: UIEnvironment,
    driver: UIDriver,
    ctx: JourneyContext,
    shared_page: Page | None,
    sink: _Sink | None,
) -> None:
    """Run one repetition: acquire a page, prepare (untimed), then time ``measure``.

    *sink* is ``None`` for warmup reps (everything discarded). For a timed rep,
    a failure in ``prepare`` or ``measure`` is recorded as a failure reason
    rather than aborting the run.
    """
    if journey.isolation == "fresh_context":
        page_ctx = await driver.new_context()
        page = await page_ctx.new_page()
    else:
        page_ctx = None
        assert shared_page is not None
        page = shared_page

    netcap = NetCapture(page)
    try:
        try:
            await journey.run_prepare(env, page, ctx)
        except Exception as exc:  # noqa: BLE001 — a prepare failure is a data point
            if sink is not None:
                sink.result.record_failure(f"prepare:{exc.__class__.__name__}")
            return

        netcap.start()
        start = time.perf_counter()
        try:
            await journey.measure(env, page, ctx)
        except Exception as exc:  # noqa: BLE001 — any measure failure is recorded
            netcap.stop()
            if sink is not None:
                sink.result.record_failure(exc.__class__.__name__)
            return
        elapsed_ms = (time.perf_counter() - start) * 1000
        rep = netcap.stop()

        if sink is not None:
            sink.result.latencies_ms.append(elapsed_ms)
            sink.net_reps.append(rep)
            if journey.capture_nav_timing:
                timing = await read_browser_timing(page)
                if timing is not None:
                    sink.timings.append(timing)
    finally:
        if page_ctx is not None:
            await driver.close_context(page_ctx)


async def run_ui_journey(
    journey: UIJourney,
    env: UIEnvironment,
    driver: UIDriver,
    *,
    runs: int,
    iterations: int,
    warmup: int,
) -> tuple[list[RunResult], list[RepCapture], list[dict[str, float]]]:
    """Run a journey's warmup + timed reps; return per-run results + network + timing.

    :returns: ``(results, net_reps, browser_timings)`` — one :class:`RunResult`
        per timed run, the flat list of every timed rep's network tally, and any
        captured browser-timing samples.
    """
    ctx = await journey.run_setup(env)
    results: list[RunResult] = []
    net_reps: list[RepCapture] = []
    timings: list[dict[str, float]] = []

    shared_page: Page | None = None
    shared_ctx = None
    try:
        if journey.isolation == "shared_page":
            shared_ctx = await driver.new_context()
            shared_page = await shared_ctx.new_page()

        for _ in range(warmup):
            with contextlib.suppress(Exception):  # warmup errors are non-fatal
                await _one_rep(journey, env, driver, ctx, shared_page, sink=None)

        for _ in range(runs):
            result = RunResult()
            sink = _Sink(result=result, net_reps=net_reps, timings=timings)
            wall_start = time.perf_counter()
            for _ in range(iterations):
                await _one_rep(journey, env, driver, ctx, shared_page, sink=sink)
            result.wall_time = time.perf_counter() - wall_start
            results.append(result)
    finally:
        if shared_ctx is not None:
            await driver.close_context(shared_ctx)
        await journey.run_teardown(env, ctx)

    return results, net_reps, timings


# ── shared setup helpers ─────────────────────────────────────


async def _setup_agent(env: UIEnvironment) -> str:
    """Register the benchmark agent and return its id."""
    name = await env.ensure_agent()
    return await env.agent_id(name)


async def _ensure_streaming_reply(env: UIEnvironment) -> None:
    """Set a reset-surviving streaming fallback so driven turns emit deltas."""
    await env.set_mock_fallback(_REPLY_TEXT, stream=True)


async def _delete_sessions(env: UIEnvironment, ids: list[str]) -> None:
    """Best-effort DELETE of sessions created during a run (untimed teardown)."""
    assert env.client is not None
    for sid in ids:
        with contextlib.suppress(httpx.HTTPError):
            await env.client.delete(f"/v1/sessions/{sid}")


# ── journey 1: landing page load ─────────────────────────────


async def _measure_landing_load(_env: UIEnvironment, page: Page, _ctx: JourneyContext) -> None:
    """Navigate to ``/`` and wait for the landing composer to be interactive."""
    await page.goto("/", wait_until="commit")
    await expect(page.locator(LANDING_INPUT)).to_be_visible(timeout=_ASSERT_TIMEOUT_MS)


# ── journey 2: new session → first streamed token ────────────


@dataclass
class _FirstTokenCtx:
    """First-token context: the agent to chat with + sessions to clean up."""

    agent_id: str
    created_ids: list[str] = field(default_factory=list)


async def _setup_first_token(env: UIEnvironment) -> _FirstTokenCtx:
    """Register a streaming-reply agent for the first-token journey."""
    await _ensure_streaming_reply(env)
    agent_id = await _setup_agent(env)
    return _FirstTokenCtx(agent_id=agent_id)


async def _prepare_first_token(env: UIEnvironment, page: Page, ctx: JourneyContext) -> None:
    """Create a fresh bound session and open it, ready for the first message."""
    fc = cast(_FirstTokenCtx, ctx)
    session_id = await env.create_bound_session(fc.agent_id)
    fc.created_ids.append(session_id)
    await page.goto(f"/c/{session_id}", wait_until="commit")
    await expect(page.get_by_placeholder(COMPOSER_PLACEHOLDER)).to_be_visible(
        timeout=_ASSERT_TIMEOUT_MS
    )


async def _measure_first_token(_env: UIEnvironment, page: Page, _ctx: JourneyContext) -> None:
    """Send the first message and wait for the first streamed assistant token.

    Stops on a real assistant bubble carrying non-whitespace text — NOT the
    ``working-indicator`` shimmer (which would let the timer stop on the spinner
    before any token streamed).
    """
    composer = page.get_by_placeholder(COMPOSER_PLACEHOLDER)
    await composer.fill(_TURN_PROMPT)
    await page.get_by_role("button", name=SEND_BUTTON_NAME, exact=True).click()
    assistant = page.locator(ASSISTANT_BUBBLE).first
    await expect(assistant).to_be_visible(timeout=_FIRST_TOKEN_TIMEOUT_MS)
    await expect(assistant).to_have_text(re.compile(r"\S"), timeout=_FIRST_TOKEN_TIMEOUT_MS)


async def _teardown_first_token(env: UIEnvironment, ctx: JourneyContext) -> None:
    await _delete_sessions(env, cast(_FirstTokenCtx, ctx).created_ids)


# ── journey 3: switch between sessions (client-side) ─────────


@dataclass
class _SwitchCtx:
    """Switch-journey context: the two sessions clicked between in the sidebar."""

    session_a: str
    session_b: str


async def _setup_switch(env: UIEnvironment) -> _SwitchCtx:
    """Create two bound sessions with a little seeded history to render."""
    agent_id = await _setup_agent(env)
    session_a = await env.create_bound_session(agent_id)
    session_b = await env.create_bound_session(agent_id)
    await env.seed_items(session_a, _SWITCH_SEED_ITEMS)
    await env.seed_items(session_b, _SWITCH_SEED_ITEMS)
    return _SwitchCtx(session_a=session_a, session_b=session_b)


async def _prepare_switch(_env: UIEnvironment, page: Page, ctx: JourneyContext) -> None:
    """Position on session A, via a full load on first rep then client-side after.

    Waits until session B's sidebar link is present so the timed switch has a
    stable click target.
    """
    sc = cast(_SwitchCtx, ctx)
    if "/c/" not in page.url:
        await page.goto(f"/c/{sc.session_a}", wait_until="commit")
    else:
        await page.locator(sidebar_session_link(sc.session_a)).click()
    await page.wait_for_url(
        re.compile(rf"/c/{re.escape(sc.session_a)}"), timeout=_ASSERT_TIMEOUT_MS
    )
    await expect(page.locator(sidebar_session_link(sc.session_b))).to_be_visible(
        timeout=_ASSERT_TIMEOUT_MS
    )


async def _measure_switch(_env: UIEnvironment, page: Page, ctx: JourneyContext) -> None:
    """Click session B's sidebar link and wait for its conversation to render."""
    sc = cast(_SwitchCtx, ctx)
    await page.locator(sidebar_session_link(sc.session_b)).click()
    await page.wait_for_url(
        re.compile(rf"/c/{re.escape(sc.session_b)}"), timeout=_ASSERT_TIMEOUT_MS
    )
    await expect(page.locator(USER_BUBBLE).first).to_be_visible(timeout=_ASSERT_TIMEOUT_MS)


async def _teardown_switch(env: UIEnvironment, ctx: JourneyContext) -> None:
    sc = cast(_SwitchCtx, ctx)
    await _delete_sessions(env, [sc.session_a, sc.session_b])


# ── journey 4: fork a session ────────────────────────────────


@dataclass
class _ForkCtx:
    """Fork-journey context: the source session + the forks to clean up."""

    source_id: str
    fork_ids: list[str] = field(default_factory=list)


async def _setup_fork(env: UIEnvironment) -> _ForkCtx:
    """Create a bound session and drive one turn so it has an assistant bubble."""
    await _ensure_streaming_reply(env)
    agent_id = await _setup_agent(env)
    source_id = await env.create_bound_session(agent_id)
    await env.drive_turn(source_id, _TURN_PROMPT)
    return _ForkCtx(source_id=source_id)


async def _prepare_fork(_env: UIEnvironment, page: Page, ctx: JourneyContext) -> None:
    """Open the source conversation and wait for its assistant bubble to render."""
    fc = cast(_ForkCtx, ctx)
    await page.goto(f"/c/{fc.source_id}", wait_until="commit")
    await expect(page.locator(ASSISTANT_BUBBLE).first).to_be_visible(
        timeout=_FIRST_TOKEN_TIMEOUT_MS
    )


async def _measure_fork(_env: UIEnvironment, page: Page, ctx: JourneyContext) -> None:
    """Fork from the first assistant response and wait for the clone to render."""
    fc = cast(_ForkCtx, ctx)
    first_assistant = page.locator(ASSISTANT_BUBBLE).first
    await first_assistant.hover()
    await first_assistant.locator(FORK_FROM_RESPONSE).click()
    await page.locator(FORK_SUBMIT).click()
    # Land on a DIFFERENT /c/<id> — a 32-hex id that is not the source.
    await page.wait_for_url(
        re.compile(rf"/c/(?!{re.escape(fc.source_id)})[0-9a-f]{{32}}"),
        timeout=_ASSERT_TIMEOUT_MS,
    )
    await expect(page.locator(USER_BUBBLE).first).to_be_visible(timeout=_ASSERT_TIMEOUT_MS)
    fork_id = page.url.rsplit("/c/", 1)[1].split("?", 1)[0]
    fc.fork_ids.append(fork_id)


async def _teardown_fork(env: UIEnvironment, ctx: JourneyContext) -> None:
    fc = cast(_ForkCtx, ctx)
    await _delete_sessions(env, [fc.source_id, *fc.fork_ids])


# ── registry ─────────────────────────────────────────────────

ALL_UI_JOURNEYS: dict[str, UIJourney] = {
    j.name: j
    for j in (
        UIJourney(
            name="landing_load",
            isolation="fresh_context",
            measure=_measure_landing_load,
            capture_nav_timing=True,
            description="Navigate to / and wait for the landing composer (cold first paint).",
        ),
        UIJourney(
            name="new_session_first_token",
            isolation="fresh_context",
            setup=_setup_first_token,
            prepare=_prepare_first_token,
            measure=_measure_first_token,
            teardown=_teardown_first_token,
            description="On a fresh bound session, send a message; time to the first "
            "streamed assistant token.",
        ),
        UIJourney(
            name="switch_sessions",
            isolation="shared_page",
            setup=_setup_switch,
            prepare=_prepare_switch,
            measure=_measure_switch,
            teardown=_teardown_switch,
            description="Click between two seeded sessions in the sidebar (client-side "
            "nav); time to the target conversation rendering.",
        ),
        UIJourney(
            name="fork_session",
            isolation="fresh_context",
            setup=_setup_fork,
            prepare=_prepare_fork,
            measure=_measure_fork,
            teardown=_teardown_fork,
            description="Fork from an assistant response; time to the forked "
            "conversation rendering.",
        ),
    )
}


def resolve_ui_journeys(names: list[str] | None) -> list[UIJourney]:
    """Resolve requested journey *names* (or all when ``None``/empty).

    :raises KeyError: If a requested name isn't registered.
    """
    if not names:
        return list(ALL_UI_JOURNEYS.values())
    resolved = []
    for name in names:
        if name not in ALL_UI_JOURNEYS:
            raise KeyError(f"unknown journey {name!r}; known: {', '.join(ALL_UI_JOURNEYS)}")
        resolved.append(ALL_UI_JOURNEYS[name])
    return resolved


def summarize_browser_timing(samples: list[dict[str, float]]) -> dict[str, float]:
    """Mean of each browser-timing metric across *samples* (empty when none)."""
    if not samples:
        return {}
    import statistics

    keys = set().union(*samples)
    out: dict[str, float] = {}
    for key in keys:
        values = [s[key] for s in samples if isinstance(s.get(key), (int, float))]
        if values:
            out[key] = statistics.mean(values)
    return out


def build_network_block(net_reps: list[RepCapture]) -> dict[str, object]:
    """Public wrapper around :func:`aggregate_network` for the report builder."""
    return aggregate_network(net_reps)
