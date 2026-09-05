"""Regression e2e: the Pi executor reports a 120 s idle *read timeout* as
``Pi process ended without response.``.

Real incident: a pi-harness sub-agent whose model provider went quiet for
longer than the 120 s idle read budget (provider retry backoff / a slow
reasoning model) was reported to its orchestrator as a *dead* process. The
child was in fact alive and still working, so the parent marked the unit failed
and moved on.

The defect lives in real product code in ``omnigent/inner/pi_executor.py``:

- ``_PiRpcSession.read_line`` returns ``None`` **both** on EOF and on
  ``asyncio.TimeoutError`` (idle timeout). The two are indistinguishable to the
  caller.
- ``PiExecutor.run_turn``'s turn loop reads with a **hard-coded** ``120.0``
  literal and, on ``None`` with nothing streamed yet, emits
  ``ExecutorError("Pi process ended without response.")`` **without ever
  consulting** ``rpc.process.returncode`` — asserting the process "ended" when
  it may be alive.
- The 120 s budget is not configurable (no ``OMNIGENT_PI_IDLE_TIMEOUT_S`` hook).
- A timeout that fires *after* partial output is silently converted into a
  truncated ``TurnComplete`` with no warning.

These three tests drive the **real** ``PiExecutor.run_turn`` /
``_PiRpcSession`` code paths. A real OS subprocess stands in for
``pi --mode rpc``: it acks the ``prompt`` command over pi's JSONL RPC and then
stays **alive and silent**, faithfully modelling a provider stuck in retry
backoff (the child neither exits nor emits its next line). Only the OS spawn is
redirected (``_create_subprocess_exec``) so the real turn loop's ``read_line``,
timeout handling, and error/complete emission all run for real — exactly the
technique used by ``tests/e2e/test_pi_interrupt_reap_budget_race_e2e.py``. No
``pi`` CLI is required.

Each test asserts the *correct* post-fix behaviour, so it **FAILS** on the
buggy build (the reproduction) and **PASSES** once the executor distinguishes
timeout from EOF, reports liveness, honours ``OMNIGENT_PI_IDLE_TIMEOUT_S``, and
warns on truncation.

The idle budget is set to a small value via ``OMNIGENT_PI_IDLE_TIMEOUT_S`` so a
fixed build settles in a couple of seconds; on the buggy build the env var is
ignored and the loop blocks for the hard-coded 120 s, tripping the bounded
``asyncio.wait_for`` guard below (itself a symptom of the not-configurable
facet).
"""

from __future__ import annotations

import asyncio
import logging
import sys

import pytest

import omnigent.inner.pi_executor as pi_mod
from omnigent.inner.executor import ExecutorError, TurnComplete
from omnigent.inner.pi_executor import PiExecutor

# Small idle budget the fixed executor should honour via the env var. On the
# buggy build the env var is ignored and the loop waits the hard-coded 120 s.
_IDLE_BUDGET_S = "3"
# Upper bound on how long we let a *single* turn run. Comfortably above the
# honoured budget, far below the hard-coded 120 s literal, so an un-honoured
# budget trips this guard instead of hanging CI for two minutes.
_TURN_BOUND_S = 25.0

# A real subprocess that speaks pi's JSONL RPC: read commands from stdin, ack a
# ``prompt`` like real pi does, optionally stream one partial text delta, then
# go **silent while staying alive** (models a provider stuck in retry backoff).
# ``{emit_partial}`` is substituted per test.
_PI_STANDIN_SRC = r"""
import json, sys, time
EMIT_PARTIAL = {emit_partial}
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        cmd = json.loads(line)
    except Exception:
        continue
    if cmd.get("type") == "prompt":
        sys.stdout.write(json.dumps(
            {{"type": "response", "command": "prompt", "success": True, "id": cmd.get("id")}}
        ) + "\n")
        sys.stdout.flush()
        if EMIT_PARTIAL:
            delta = {{"type": "text_delta", "delta": "partial answer so far"}}
            sys.stdout.write(json.dumps(
                {{"type": "message_update", "assistantMessageEvent": delta}}
            ) + "\n")
            sys.stdout.flush()
        # Provider stall: alive, but no further RPC line ever arrives.
        time.sleep(600)
"""


def _install_pi_standin(monkeypatch: pytest.MonkeyPatch, *, emit_partial: bool) -> None:
    """Redirect the executor's OS spawn to a real alive+silent pi stand-in."""
    src = _PI_STANDIN_SRC.format(emit_partial=repr(emit_partial))

    async def _spawn(*_args: object, **_kwargs: object) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            src,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    monkeypatch.setattr(pi_mod, "_create_subprocess_exec", _spawn)


async def _drive_turn(executor: PiExecutor) -> tuple[list[object], int | None]:
    """Drive one real ``run_turn`` and return (events, child_returncode_at_end).

    The child's returncode is sampled after the turn settles so a test can
    prove the pi process was still alive when the turn ended.
    """
    events: list[object] = []

    async def _drain() -> None:
        async for event in executor.run_turn(
            [{"role": "user", "content": "hello"}], [], "You are a helpful assistant."
        ):
            events.append(event)

    await asyncio.wait_for(_drain(), timeout=_TURN_BOUND_S)

    child_returncode: int | None = None
    for state in getattr(executor, "_session_states", {}).values():
        proc = getattr(state.rpc, "process", None)
        if proc is not None:
            child_returncode = proc.returncode
    return events, child_returncode


async def test_pi_idle_timeout_not_reported_as_process_death(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle timeout with no prior output must be reported as a
    timeout against a still-live process — not as ``Pi process ended without
    response.`` — and the configured ``OMNIGENT_PI_IDLE_TIMEOUT_S`` budget must
    be honoured.

    Buggy build: the env var is ignored (the loop blocks the hard-coded 120 s,
    tripping ``_TURN_BOUND_S``) and, even when it returns, it emits the EOF-style
    ``Pi process ended without response.`` while the child is still alive.
    """
    monkeypatch.setenv("OMNIGENT_PI_IDLE_TIMEOUT_S", _IDLE_BUDGET_S)
    _install_pi_standin(monkeypatch, emit_partial=False)

    executor = PiExecutor(model=None, pi_path=sys.executable)
    try:
        try:
            events, child_returncode = await _drive_turn(executor)
        except asyncio.TimeoutError:
            pytest.fail(
                "PiExecutor.run_turn did not settle within "
                f"{_TURN_BOUND_S:g}s even though OMNIGENT_PI_IDLE_TIMEOUT_S="
                f"{_IDLE_BUDGET_S}s was set: the idle budget is not configurable "
                "and the loop is blocking on the hard-coded 120s literal."
            )
    finally:
        await executor.close()

    # The stand-in never exited: the pi child was ALIVE when the turn ended, so
    # any message claiming the process "ended" is false.
    assert child_returncode is None, (
        f"test precondition: pi stand-in should be alive; returncode={child_returncode}"
    )

    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert errors, f"expected an ExecutorError from the idle timeout; got {events!r}"
    message = errors[0].message

    assert "Pi process ended without response" not in message, (
        "Idle read timeout was reported as process death: the pi child was still "
        f"alive (returncode is None) yet the error says it 'ended'. Got: {message!r} "
        "(timeout must be distinguished from EOF and liveness reported)."
    )
    assert "timeout" in message.lower(), (
        "The error should identify the failure as an idle timeout (not a generic "
        f"process-death message). Got: {message!r}"
    )


async def test_pi_idle_timeout_honours_short_budget_before_120s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The idle budget must be configurable via ``OMNIGENT_PI_IDLE_TIMEOUT_S``.

    With a 3 s budget the turn must settle well under the hard-coded 120 s
    literal. On the buggy build the env var has no effect, so the turn blocks
    for 120 s and this bounded drive fails.
    """
    monkeypatch.setenv("OMNIGENT_PI_IDLE_TIMEOUT_S", _IDLE_BUDGET_S)
    _install_pi_standin(monkeypatch, emit_partial=False)

    executor = PiExecutor(model=None, pi_path=sys.executable)
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        try:
            await _drive_turn(executor)
        except asyncio.TimeoutError:
            pytest.fail(
                f"OMNIGENT_PI_IDLE_TIMEOUT_S={_IDLE_BUDGET_S}s was ignored: the idle "
                f"read blocked past {_TURN_BOUND_S:g}s on the hard-coded 120s literal "
                "(the idle budget must be configurable)."
            )
    finally:
        await executor.close()

    elapsed = loop.time() - started
    assert elapsed < _TURN_BOUND_S, (
        f"idle timeout took {elapsed:.1f}s with a {_IDLE_BUDGET_S}s budget; the "
        "configured OMNIGENT_PI_IDLE_TIMEOUT_S is not being honoured."
    )


async def test_pi_idle_timeout_after_partial_output_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An idle timeout that fires *after* partial output must not be
    silently converted into a truncated ``TurnComplete`` — it must log a warning
    that the turn was cut short.

    Buggy build: the partial text is completed with no signal at all (no warning
    log), so an operator cannot tell a truncated turn from a complete one.
    """
    monkeypatch.setenv("OMNIGENT_PI_IDLE_TIMEOUT_S", _IDLE_BUDGET_S)
    _install_pi_standin(monkeypatch, emit_partial=True)

    executor = PiExecutor(model=None, pi_path=sys.executable)
    try:
        with caplog.at_level(logging.WARNING, logger="omnigent.inner.pi_executor"):
            try:
                events, child_returncode = await _drive_turn(executor)
            except asyncio.TimeoutError:
                pytest.fail(
                    "PiExecutor.run_turn did not settle within "
                    f"{_TURN_BOUND_S:g}s after partial output even though "
                    f"OMNIGENT_PI_IDLE_TIMEOUT_S={_IDLE_BUDGET_S}s was set."
                )
    finally:
        await executor.close()

    # The partial output is still delivered (the turn completes with what streamed).
    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert completes, f"expected the partial turn to complete; got {events!r}"
    assert completes[0].response == "partial answer so far", (
        f"partial streamed text should be preserved; got {completes[0].response!r}"
    )
    assert child_returncode is None, (
        f"test precondition: pi stand-in should be alive; returncode={child_returncode}"
    )

    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and "omnigent.inner.pi_executor" in r.name
    ]
    assert any("timeout" in w.lower() for w in warnings), (
        "An idle timeout after partial output was silently turned into a truncated "
        f"TurnComplete with no warning; captured warnings: {warnings!r} "
        "(a cut-short turn must be logged)."
    )
