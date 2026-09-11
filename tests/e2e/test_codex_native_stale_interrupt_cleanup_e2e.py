"""Regression e2e: codex-native abnormal-exit interrupt with a stale turn id.

Journey (all through real product code): a web user message starts native
Codex turn 1 (``turn/start``; the bridge records it as active). Codex then
moves on to its own next turn — the user drives the embedded Codex TUI pane
directly, or Codex starts an internal follow-on turn — while Omnigent's
bridge state still records turn 1. The next web message steers the recorded
turn (``turn/steer`` with ``expectedTurnId=turn_1``); the app-server rejects
it with its JSON-RPC ``-32600`` mismatch (``expected active turn id turn_1
but found turn_2``), which is not the retryable ``no active turn to steer``
idle semantic, so the web turn dies abnormally. The adapter's abnormal-exit
cleanup then interrupts the abandoned inner session with the same stale
recorded turn id, the app-server rejects that too, and ``_safe_interrupt``
logs the ERROR::

    abnormal-exit interrupt of inner session <key> failed or timed out

with a ``CodexAppServerResponseError`` traceback. One staleness thus produces
both a failed user turn and a failed cleanup.

The test drives the REAL ``ExecutorAdapter`` -> ``CodexNativeExecutor`` ->
``CodexAppServerClient`` stack over a real loopback WebSocket. Only the
upstream ``codex app-server`` wire peer is faked, because the real Codex CLI
requires an authenticated account to run turns (unavailable in CI) and the
staleness window is a nondeterministic race against a live TUI. The fake
implements only the app-server's documented turn bookkeeping: assign turn ids
on ``turn/start`` and reject a ``turn/steer``/``turn/interrupt`` whose turn id
does not match the active turn with the production ``-32600`` error message.

The assertions state the *correct* post-fix contract, so the test FAILS on
the buggy build (reproducing the failure) and PASSES once the abnormal-exit
cleanup tolerates Codex having moved past the bridge-recorded turn — the
recorded turn is no longer active, so there is nothing left for the cleanup
to interrupt, and that expected condition must not surface as the
ERROR-level failure signature.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest
import websockets
from websockets.asyncio.server import Server, ServerConnection

from omnigent.harnesses.codex_native.bridge import (
    CODEX_NATIVE_REQUEST_SESSION_ID_ENV_VAR,
    CodexNativeBridgeState,
    read_bridge_state,
    write_bridge_state,
)
from omnigent.inner.codex_native_executor import CodexNativeExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.runtime.harnesses._scaffold import TurnContext
from omnigent.server.schemas import CreateResponseRequest

_ADAPTER_LOGGER = "omnigent.runtime.harnesses._executor_adapter"

#: Stable prefix of the abnormal-exit cleanup failure log (the KPI signature).
_FAILURE_LOG_PREFIX = "abnormal-exit interrupt of inner session"


class _FakeCodexAppServer:
    """Wire-faithful stand-in for ``codex app-server`` turn bookkeeping.

    Speaks the app-server's JSON-RPC-over-WebSocket protocol on a loopback
    port. Tracks one active turn: ``turn/start`` assigns the next turn id;
    ``turn/steer`` and ``turn/interrupt`` validate the caller's turn id
    against the active one and reject a mismatch with the production
    ``-32600`` error shape.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.active_turn: str | None = None
        self._turn_seq = 0
        self._server: Server | None = None
        self.url = ""

    async def start(self) -> None:
        """Bind a loopback port and start serving."""
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]  # type: ignore[index]
        self.url = f"ws://127.0.0.1:{port}"

    async def stop(self) -> None:
        """Stop serving and release the port."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def advance_to_next_turn(self) -> str:
        """Model Codex moving on to its own next turn (e.g. a TUI-driven turn).

        The bridge state is deliberately NOT updated: this models the window
        where Codex's active turn has changed but Omnigent's recorded turn id
        has not caught up (the forwarder lags or the update raced).

        :returns: The new active turn id.
        """
        self._turn_seq += 1
        self.active_turn = f"turn_{self._turn_seq}"
        return self.active_turn

    async def _handle(self, ws: ServerConnection) -> None:
        """Answer one client connection's JSON-RPC requests."""
        async for raw in ws:
            message = json.loads(raw)
            request_id = message.get("id")
            if request_id is None:
                continue  # notification, e.g. "initialized"
            method = message.get("method")
            params = message.get("params") or {}
            self.requests.append((method, params))
            error: dict[str, Any] | None = None
            result: dict[str, Any] = {}
            if method == "turn/start":
                self._turn_seq += 1
                self.active_turn = f"turn_{self._turn_seq}"
                result = {"turn": {"id": self.active_turn}}
            elif method == "turn/steer":
                expected = params.get("expectedTurnId")
                if self.active_turn is None:
                    error = {"code": -32600, "message": "no active turn to steer"}
                elif expected != self.active_turn:
                    error = {
                        "code": -32600,
                        "message": (
                            f"expected active turn id {expected} but found {self.active_turn}"
                        ),
                    }
                else:
                    result = {"turnId": self.active_turn}
            elif method == "turn/interrupt":
                turn_id = params.get("turnId")
                if turn_id == "":
                    result = {}  # startup interrupt: always accepted
                elif turn_id != self.active_turn:
                    error = {
                        "code": -32600,
                        "message": (
                            f"expected active turn id {turn_id} but found {self.active_turn}"
                        ),
                    }
                else:
                    self.active_turn = None
                    result = {}
            envelope: dict[str, Any] = {"id": request_id}
            if error is not None:
                envelope["error"] = error
            else:
                envelope["result"] = result
            await ws.send(json.dumps(envelope))


def _ctx(response_id: str) -> TurnContext:
    """Build a minimal live turn context for ``ExecutorAdapter.run_turn``."""
    return TurnContext(
        response_id=response_id,
        event_queue=asyncio.Queue(),
        cancelled=asyncio.Event(),
    )


def _request(text: str) -> CreateResponseRequest:
    """Build a web-shaped user message request."""
    return CreateResponseRequest(model="agent", input=text)


async def test_abnormal_exit_interrupt_tolerates_codex_moving_past_recorded_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The abnormal-exit cleanup must not fail when the recorded turn is stale.

    Once Codex has moved past the bridge-recorded turn, that recorded turn is
    no longer active — there is nothing left for the abandoned-session cleanup
    to interrupt, so the cleanup must settle without emitting the ERROR-level
    ``abnormal-exit interrupt ... failed or timed out`` failure signature.
    """
    monkeypatch.delenv(CODEX_NATIVE_REQUEST_SESSION_ID_ENV_VAR, raising=False)
    fake = _FakeCodexAppServer()
    await fake.start()
    try:
        write_bridge_state(
            tmp_path,
            CodexNativeBridgeState(
                session_id="conv_stale_interrupt",
                socket_path=fake.url,
                thread_id="thread_stale_interrupt",
                codex_home=str(tmp_path / "codex-home"),
                active_turn_id=None,
                cwd=str(tmp_path),
            ),
        )
        executor = CodexNativeExecutor(bridge_dir=tmp_path)
        adapter = ExecutorAdapter(executor_factory=lambda: executor)

        with caplog.at_level(logging.INFO, logger=_ADAPTER_LOGGER):
            # Web message 1: the real turn/start records turn_1 in bridge state.
            await adapter.run_turn(_request("hello"), _ctx("resp_1"))
            state = read_bridge_state(tmp_path)
            assert state is not None and state.active_turn_id == "turn_1", (
                f"first web turn must record the started turn; bridge={state!r}"
            )

            # Codex moves on to turn_2 (a TUI-driven or internal follow-on
            # turn) while the bridge still records turn_1.
            fake.advance_to_next_turn()
            assert fake.active_turn == "turn_2"

            # Web message 2: steering the recorded turn_1 is rejected with the
            # -32600 mismatch, so the web turn dies abnormally.
            with pytest.raises(RuntimeError, match="inner executor error"):
                await adapter.run_turn(_request("follow up"), _ctx("resp_2"))

            # The abnormal exit detached the executor and scheduled the
            # bounded interrupt + reap; wait for it to settle.
            cleanup = adapter._abandoned_executor_cleanup
            assert cleanup is not None, (
                "abnormal exit must schedule the abandoned-executor cleanup"
            )
            reaped = await asyncio.wait_for(asyncio.shield(cleanup), timeout=15)

        # Positive controls: the journey really drove the stale-steer mismatch
        # through the wire, and the reap itself succeeded.
        steers = [params for method, params in fake.requests if method == "turn/steer"]
        assert steers and steers[0]["expectedTurnId"] == "turn_1", (
            f"expected a stale steer of turn_1; requests={fake.requests!r}"
        )
        assert reaped is True, "abandoned-executor reap (close_session/close) must succeed"

        # Regression assertion (the reproduction): the cleanup of an already
        # superseded turn is an expected condition and must not emit the
        # ERROR-level failure signature.
        failures = [
            record
            for record in caplog.records
            if record.name == _ADAPTER_LOGGER
            and record.levelno >= logging.ERROR
            and _FAILURE_LOG_PREFIX in record.getMessage()
        ]
        assert not failures, (
            "abnormal-exit cleanup must tolerate Codex having moved past the "
            "bridge-recorded turn (the recorded turn is no longer active, so "
            "there is nothing left to interrupt); instead it failed:\n"
            f"{failures[0].getMessage()}\n{failures[0].exc_text or ''}"
        )
    finally:
        await fake.stop()
