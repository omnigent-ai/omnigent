"""User-journey definitions and the runners that time them.

A :class:`Journey` names a user-facing operation, an optional per-journey
``setup`` that returns a context object, and a ``measure`` coroutine — the
timed unit. :func:`run_latency` times ``measure`` sequentially; journeys marked
``concurrency_safe`` can also be driven by :func:`run_throughput` with many
operations in flight.

v1 journeys are pure HTTP/API (server + DB, no runner, no LLM):

- ``list_sessions`` — the session-list read behind the sidebar/home.
- ``create_session`` — session creation cost (POST then DELETE).
- ``get_session`` — single-session snapshot load.
- ``load_conversation_history`` — history read, seeded runner-free via
  ``external_conversation_item`` (see :meth:`BenchEnvironment.seed_items`).
- ``fork_session`` — fork a session (deep-copy its items), then DELETE.
- ``add_comment`` — create a review comment on a file (DB write).

``read_runner_file`` needs a runner but no LLM turn: it plants a file in the
runner environment (setup) and times the server → runner filesystem read proxy.

The framework (``Journey`` + the two runners) is harness-agnostic and reused
verbatim by phase-2 full-turn journeys.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, cast

import httpx

from .environment import BenchEnvironment
from .measure import RunResult

# Per-journey context returned by ``setup`` and threaded to ``measure``. Its
# concrete type varies by journey (an agent id, a session id, or nothing), so
# it is opaque at the framework level; each measure op casts it as needed.
JourneyContext = object

JourneyKind = Literal["latency", "throughput"]

# Items requested per history-read page. Also the count self-seeded into a
# fallback session when the DB has no corpus (empty-DB smoke path).
_HISTORY_PAGE_LIMIT = 20
_HISTORY_SEED_ITEMS = _HISTORY_PAGE_LIMIT


@dataclass
class Journey:
    """One benchmarkable user journey.

    :param name: Stable identifier used on the CLI and as the report key.
    :param kind: ``"latency"`` (time each operation) or ``"throughput"``
        (fixed request count under concurrency). A latency journey that is
        ``concurrency_safe`` can additionally be run as throughput.
    :param measure: Coroutine performing exactly one timed operation, given
        the environment and the setup context.
    :param setup: Optional coroutine run once before timing; its return value
        is passed to ``measure`` (and ``teardown``) as ``ctx``.
    :param teardown: Optional coroutine run once after timing, given ``ctx``.
    :param concurrency_safe: Whether many ``measure`` calls may run at once
        against a shared setup (true for read-only / independent-write HTTP
        journeys).
    :param needs_runner: Whether this journey drives a full agent turn and so
        requires ``BenchEnvironment(with_runner=True)`` (mock LLM + runner).
        HTTP/DB journeys leave this ``False``.
    :param max_iterations: Upper bound on latency iterations for this journey,
        clamping ``--iterations`` down (never up). Full-turn journeys cost ~1s+
        per op, so 100+ iterations would blow the CI time budget; they cap at a
        few samples per run and lean on ``--runs`` for repeats. ``None`` (HTTP
        journeys) means no cap.
    :param description: Human-readable one-liner for ``--list``.
    """

    name: str
    kind: JourneyKind
    measure: Callable[[BenchEnvironment, JourneyContext], Awaitable[None]]
    setup: Callable[[BenchEnvironment], Awaitable[JourneyContext]] | None = None
    teardown: Callable[[BenchEnvironment, JourneyContext], Awaitable[None]] | None = None
    concurrency_safe: bool = False
    needs_runner: bool = False
    max_iterations: int | None = None
    description: str = ""

    async def run_setup(self, env: BenchEnvironment) -> JourneyContext:
        return await self.setup(env) if self.setup is not None else None

    async def run_teardown(self, env: BenchEnvironment, ctx: JourneyContext) -> None:
        if self.teardown is not None:
            await self.teardown(env, ctx)


# ── timed operation (shared by both runners) ─────────────────


async def _timed(
    journey: Journey, env: BenchEnvironment, ctx: JourneyContext, result: RunResult
) -> None:
    """Run one ``measure`` op, recording its latency or a failure reason."""
    start = time.perf_counter()
    try:
        await journey.measure(env, ctx)
    except httpx.HTTPStatusError as exc:
        result.record_failure(f"HTTP {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001 — any failure is a recorded data point
        result.record_failure(exc.__class__.__name__)
    else:
        result.latencies_ms.append((time.perf_counter() - start) * 1000)


# ── runners ──────────────────────────────────────────────────


async def run_latency(
    journey: Journey, env: BenchEnvironment, *, iterations: int, warmup: int
) -> RunResult:
    """Time *iterations* sequential operations after discarding *warmup*.

    Warmup operations run through the same path but are excluded from the
    result, so first-call import/JIT/connection costs don't skew the numbers.
    """
    ctx = await journey.run_setup(env)
    try:
        for _ in range(warmup):
            with contextlib.suppress(Exception):  # warmup errors are non-fatal
                await journey.measure(env, ctx)
        result = RunResult()
        wall_start = time.perf_counter()
        for _ in range(iterations):
            await _timed(journey, env, ctx, result)
        result.wall_time = time.perf_counter() - wall_start
        return result
    finally:
        await journey.run_teardown(env, ctx)


async def run_throughput(
    journey: Journey,
    env: BenchEnvironment,
    *,
    requests: int,
    concurrency: int,
    warmup: int,
) -> RunResult:
    """Fire *requests* operations with at most *concurrency* in flight.

    Wall time spans from the first dispatch to the last completion, so
    ``throughput`` reflects sustained req/s under load (MLflow's ``_run_once``
    shape, with an :class:`asyncio.Semaphore` gate).
    """
    ctx = await journey.run_setup(env)
    try:
        sem = asyncio.Semaphore(concurrency)

        async def _one(count_it: bool, result: RunResult) -> None:
            async with sem:
                if count_it:
                    await _timed(journey, env, ctx, result)
                else:
                    with contextlib.suppress(Exception):  # warmup errors are non-fatal
                        await journey.measure(env, ctx)

        if warmup:
            throwaway = RunResult()
            await asyncio.gather(*[_one(False, throwaway) for _ in range(warmup)])

        result = RunResult()
        wall_start = time.perf_counter()
        await asyncio.gather(*[_one(True, result) for _ in range(requests)])
        result.wall_time = time.perf_counter() - wall_start
        return result
    finally:
        await journey.run_teardown(env, ctx)


# ── journey implementations ──────────────────────────────────
#
# Setups return the context each measure op needs. Ops must be independent so
# concurrency-safe journeys don't interfere across in-flight calls.


# A token present in the seeded corpus (titles + item text, see seed.py
# _FRAGMENTS) so search_sessions exercises the LIKE path with real matches.
_SEARCH_TOKEN = "runner"


async def _setup_agent_id(env: BenchEnvironment) -> str:
    """Register the benchmark agent and return its id."""
    name = await env.ensure_agent()
    return await env.agent_id(name)


async def _setup_target_session(env: BenchEnvironment) -> str:
    """Return a session id to read: an existing corpus session if any, else make one.

    Real runs target a pre-seeded corpus (``seed.py``), so we read a
    representative existing session. When the DB is empty (e.g. the smoke test
    against a throwaway DB), fall back to creating one with a little history so
    the journey still exercises the read path.
    """
    assert env.client is not None
    listing = await env.client.get("/v1/sessions", params={"limit": 1})
    listing.raise_for_status()
    data = listing.json().get("data", [])
    if data:
        return str(data[0]["id"])
    # Empty DB: self-seed one session over HTTP (runner-free).
    name = await env.ensure_agent()
    agent_id = await env.agent_id(name)
    session_id = await env.create_session(agent_id)
    await env.seed_items(session_id, _HISTORY_SEED_ITEMS)
    return session_id


async def _measure_list_sessions(env: BenchEnvironment, _ctx: JourneyContext) -> None:
    assert env.client is not None
    resp = await env.client.get("/v1/sessions", params={"limit": 20})
    resp.raise_for_status()


async def _measure_search_sessions(env: BenchEnvironment, _ctx: JourneyContext) -> None:
    assert env.client is not None
    resp = await env.client.get(
        "/v1/sessions", params={"limit": 20, "search_query": _SEARCH_TOKEN}
    )
    resp.raise_for_status()


async def _measure_create_session(env: BenchEnvironment, ctx: JourneyContext) -> None:
    assert env.client is not None
    agent_id = cast(str, ctx)  # _setup_agent_id
    created = await env.client.post("/v1/sessions", json={"agent_id": agent_id})
    created.raise_for_status()
    # Delete inline so a long run doesn't accumulate unbounded sessions; the
    # POST is the operation of interest and dominates the timed span.
    session_id = created.json()["id"]
    deleted = await env.client.delete(f"/v1/sessions/{session_id}")
    deleted.raise_for_status()


async def _measure_get_session(env: BenchEnvironment, ctx: JourneyContext) -> None:
    assert env.client is not None
    session_id = cast(str, ctx)  # _setup_target_session
    resp = await env.client.get(f"/v1/sessions/{session_id}")
    resp.raise_for_status()


async def _measure_load_history(env: BenchEnvironment, ctx: JourneyContext) -> None:
    assert env.client is not None
    session_id = cast(str, ctx)  # _setup_target_session
    resp = await env.client.get(
        f"/v1/sessions/{session_id}/items",
        params={"order": "asc", "limit": _HISTORY_PAGE_LIMIT},
    )
    resp.raise_for_status()


@dataclass
class _ForkContext:
    """Fork-journey context: the session to fork + the forks to clean up.

    ``measure`` records each fork's id here instead of deleting it inline, so
    the DELETE stays out of the timed span; ``teardown`` removes them after.
    """

    source_id: str
    fork_ids: list[str]


async def _setup_fork_session(env: BenchEnvironment) -> _ForkContext:
    """Resolve a session to fork; start an empty fork-id collector."""
    source_id = await _setup_target_session(env)
    return _ForkContext(source_id=source_id, fork_ids=[])


async def _measure_fork_session(env: BenchEnvironment, ctx: JourneyContext) -> None:
    assert env.client is not None
    fork_ctx = cast(_ForkContext, ctx)  # _setup_fork_session
    forked = await env.client.post(f"/v1/sessions/{fork_ctx.source_id}/fork", json={})
    forked.raise_for_status()
    # Record the fork for teardown; deleting it here would fold the DELETE into
    # the timed span. The fork POST (a deep-copy of the source's items) is the
    # operation of interest.
    fork_ctx.fork_ids.append(forked.json()["id"])


async def _teardown_fork_session(env: BenchEnvironment, ctx: JourneyContext) -> None:
    """Delete every fork created during the run (best effort, untimed)."""
    assert env.client is not None
    fork_ctx = cast(_ForkContext, ctx)
    for fork_id in fork_ctx.fork_ids:
        with contextlib.suppress(httpx.HTTPError):
            await env.client.delete(f"/v1/sessions/{fork_id}")


# Anchor snapshot for the comment journey; the offsets below span it.
_COMMENT_ANCHOR = "benchmark"


async def _measure_add_comment(env: BenchEnvironment, ctx: JourneyContext) -> None:
    assert env.client is not None
    session_id = cast(str, ctx)  # _setup_target_session
    # Each POST creates an independent comment row. Unlike sessions, an
    # accumulating comment skews no measured read path, so there's no cleanup.
    # The file need not exist — the handler stores the path + offsets + body.
    resp = await env.client.post(
        f"/v1/sessions/{session_id}/comments",
        json={
            "path": "bench_target.py",
            "body": "benchmark review comment",
            "start_index": 0,
            "end_index": len(_COMMENT_ANCHOR),
            "anchor_content": _COMMENT_ANCHOR,
        },
    )
    resp.raise_for_status()


# ── runner (full-turn) journeys ──────────────────────────────
#
# These drive a real agent turn through the runner + mock LLM (with_runner=True,
# openai-agents). The mock is zero-latency, so every number is omnigent dispatch
# / streaming / cancel overhead, not model latency. Short deterministic replies.

# A multi-word reply so the streaming path emits several output_text deltas.
_TURN_REPLY = "Hello there, this is a mock benchmark reply."
_TURN_PROMPT = "Say hello."

# Iteration cap for full-turn journeys. At ~1s+ per turn, matching the HTTP
# journeys' iteration count would overrun the CI time budget, so we take a few
# samples per run and lean on --runs for repeats. Sessions accumulate across a
# run (a cold start never deletes its session), so a small count also keeps that
# drift negligible.
_RUNNER_MAX_ITERATIONS = 5

# Iteration cap for the runner filesystem read. It's a proxied localhost read,
# not a full turn, so it's far cheaper than the drive-a-turn journeys — a higher
# cap gives a usable p50/p99 while staying well within the CI time budget.
_RUNNER_FS_MAX_ITERATIONS = 50

# File planted by the read-runner-file setup and fetched by its measure op.
# ~1 KB — a modest, representative source file, not a stress case.
_RUNNER_FILE_PATH = "bench_read_target.txt"
_RUNNER_FILE_CONTENT = "benchmark file content line\n" * 40


async def _setup_turn_agent(env: BenchEnvironment, *, stream: bool = False) -> str:
    """Register the agent + a reset-surviving reply; return the agent id.

    The fallback survives per-call queue exhaustion, so every turn in the run
    gets the same reply regardless of how many turns consume the queue. When
    *stream* is set the reply emits per-word deltas (for the TTFT journey).
    """
    name = await env.ensure_agent()
    await env.set_mock_fallback(_TURN_REPLY, stream=stream)
    return await env.agent_id(name)


async def _setup_warm_session(env: BenchEnvironment) -> str:
    """Create+bind a session and drive one warm-up turn; return the session id.

    The warm-up pays the cold-start cost (runner spawn + executor construction)
    so the measured op times only steady-state per-turn overhead.
    """
    agent_id = await _setup_turn_agent(env)
    session_id = await env.create_bound_session(agent_id)
    await env.drive_turn(session_id, _TURN_PROMPT)
    return session_id


async def _setup_streaming_session(env: BenchEnvironment) -> str:
    """Warm session whose mock reply streams deltas — for the TTFT journey."""
    agent_id = await _setup_turn_agent(env, stream=True)
    session_id = await env.create_bound_session(agent_id)
    await env.drive_turn(session_id, _TURN_PROMPT)
    return session_id


async def _setup_interrupt_session(env: BenchEnvironment) -> str:
    """Create+bind a session for the interrupt journey; return the session id.

    Configures a ``block=True`` mock response so each turn parks in ``running``
    until the gate is released — giving the interrupt something to cancel
    mid-flight, deterministically.
    """
    name = await env.ensure_agent()
    agent_id = await env.agent_id(name)
    session_id = await env.create_bound_session(agent_id)
    await env.configure_mock([{"text": _TURN_REPLY, "block": True}])
    return session_id


async def _measure_session_cold_start(env: BenchEnvironment, ctx: JourneyContext) -> None:
    agent_id = cast(str, ctx)  # _setup_turn_agent
    session_id = await env.create_bound_session(agent_id)
    await env.drive_turn(session_id, _TURN_PROMPT)


async def _measure_warm_turn(env: BenchEnvironment, ctx: JourneyContext) -> None:
    session_id = cast(str, ctx)  # _setup_warm_session
    await env.drive_turn(session_id, _TURN_PROMPT)


async def _measure_time_to_first_token(env: BenchEnvironment, ctx: JourneyContext) -> None:
    session_id = cast(str, ctx)  # _setup_warm_session
    await env.time_to_first_delta(session_id, _TURN_PROMPT)


async def _measure_interrupt(env: BenchEnvironment, ctx: JourneyContext) -> None:
    session_id = cast(str, ctx)  # _setup_interrupt_session
    await env.drive_and_interrupt(session_id)


async def _setup_runner_file_session(env: BenchEnvironment) -> str:
    """Bind a session to the runner and plant a file to read; return its id.

    No turn is driven and no mock reply is configured — the measured op is a
    filesystem read proxied to the runner, which never calls the LLM.
    """
    name = await env.ensure_agent()
    agent_id = await env.agent_id(name)
    session_id = await env.create_bound_session(agent_id)
    await env.write_runner_file(session_id, _RUNNER_FILE_PATH, _RUNNER_FILE_CONTENT)
    return session_id


async def _measure_read_runner_file(env: BenchEnvironment, ctx: JourneyContext) -> None:
    session_id = cast(str, ctx)  # _setup_runner_file_session
    await env.read_runner_file(session_id, _RUNNER_FILE_PATH)


# ── registry ─────────────────────────────────────────────────

ALL_JOURNEYS: dict[str, Journey] = {
    j.name: j
    for j in (
        Journey(
            name="list_sessions",
            kind="latency",
            measure=_measure_list_sessions,
            concurrency_safe=True,
            description="GET /v1/sessions — session list read.",
        ),
        Journey(
            name="create_session",
            kind="latency",
            measure=_measure_create_session,
            setup=_setup_agent_id,
            concurrency_safe=True,
            description="POST /v1/sessions then DELETE — session create.",
        ),
        Journey(
            name="get_session",
            kind="latency",
            measure=_measure_get_session,
            setup=_setup_target_session,
            concurrency_safe=True,
            description="GET /v1/sessions/{id} — single-session snapshot.",
        ),
        Journey(
            name="load_conversation_history",
            kind="latency",
            measure=_measure_load_history,
            setup=_setup_target_session,
            concurrency_safe=True,
            description="GET /v1/sessions/{id}/items — conversation history read.",
        ),
        Journey(
            name="search_sessions",
            kind="latency",
            measure=_measure_search_sessions,
            concurrency_safe=True,
            description="GET /v1/sessions?search_query= — unindexed LIKE over titles + items.",
        ),
        Journey(
            name="fork_session",
            kind="latency",
            measure=_measure_fork_session,
            setup=_setup_fork_session,
            teardown=_teardown_fork_session,
            concurrency_safe=True,
            description="POST /v1/sessions/{id}/fork — session fork (deep-copy); DELETE untimed.",
        ),
        Journey(
            name="add_comment",
            kind="latency",
            measure=_measure_add_comment,
            setup=_setup_target_session,
            concurrency_safe=True,
            description="POST /v1/sessions/{id}/comments — create a review comment.",
        ),
        # Runner (full-turn) journeys — with_runner=True, openai-agents, mock LLM.
        Journey(
            name="session_cold_start",
            kind="latency",
            measure=_measure_session_cold_start,
            setup=_setup_turn_agent,
            needs_runner=True,
            max_iterations=_RUNNER_MAX_ITERATIONS,
            description="Create+bind a fresh session and drive its first turn to idle.",
        ),
        Journey(
            name="warm_turn",
            kind="latency",
            measure=_measure_warm_turn,
            setup=_setup_warm_session,
            needs_runner=True,
            max_iterations=_RUNNER_MAX_ITERATIONS,
            description="Drive a turn on an already-warm session (steady-state overhead).",
        ),
        Journey(
            name="time_to_first_token",
            kind="latency",
            measure=_measure_time_to_first_token,
            setup=_setup_streaming_session,
            needs_runner=True,
            max_iterations=_RUNNER_MAX_ITERATIONS,
            description="Post a turn; time to the first streamed output_text delta.",
        ),
        Journey(
            name="interrupt",
            kind="latency",
            measure=_measure_interrupt,
            setup=_setup_interrupt_session,
            needs_runner=True,
            max_iterations=_RUNNER_MAX_ITERATIONS,
            description="Interrupt a running (gated) turn; time to cancellation.",
        ),
        Journey(
            name="read_runner_file",
            kind="latency",
            measure=_measure_read_runner_file,
            setup=_setup_runner_file_session,
            needs_runner=True,
            max_iterations=_RUNNER_FS_MAX_ITERATIONS,
            description="GET .../environments/default/filesystem/{path} — runner file read proxy.",
        ),
    )
}


def resolve_journeys(names: list[str] | None) -> list[Journey]:
    """Resolve requested journey *names* (or all when ``None``/empty).

    :raises KeyError: If a requested name isn't registered.
    """
    if not names:
        return list(ALL_JOURNEYS.values())
    resolved = []
    for name in names:
        if name not in ALL_JOURNEYS:
            raise KeyError(f"unknown journey {name!r}; known: {', '.join(ALL_JOURNEYS)}")
        resolved.append(ALL_JOURNEYS[name])
    return resolved
