"""Codex harness turns must fail promptly when the app-server closes stdout.

The request-driven ``codex`` harness owns a ``codex app-server`` subprocess and
reads its JSON-RPC stream in ``_CodexAppServerSession._reader_loop``. If stdout
hits EOF while a turn is in flight, pending RPC futures and the turn-event
consumer must be woken promptly with an error; a build without EOF wake-up
leaves the turn idle until the scaffold's idle watchdog (3600s by default).

These tests drive the real ``CodexExecutor`` against a scripted app-server
child that closes stdout mid-turn, mirroring the fake-``codex`` pattern of
``tests/e2e/omnigent/test_codex_gateway_auth_error.py``. They fail by hanging
past ``_WAKE_BOUND_S`` on a build without EOF wake-up, and pass when the turn
surfaces the loss promptly -- as an ``ExecutorError`` event or as the typed
transport error raised for the harness scaffold to recover from.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

import pytest

from omnigent import errors as omnigent_errors
from omnigent.inner.codex_executor import CodexExecutor
from omnigent.inner.executor import ExecutorError, ExecutorEvent

# Bound well under the 3600s idle watchdog but far above a prompt EOF wake:
# a build without EOF wake-up hangs indefinitely (times out here); a fixed
# build wakes the waiter within one poll interval, so this cleanly separates
# the two.
_WAKE_BOUND_S = 15.0


def _is_transport_closed_error(exc: BaseException) -> bool:
    # Resolved lazily so this test still runs -- and fails by hanging, not by
    # an import error -- on builds that predate the typed transport error.
    error_type = getattr(omnigent_errors, "HarnessTransportClosedError", None)
    return error_type is not None and isinstance(exc, error_type)


def _write_scripted_app_server(tmp_path: Path, *, mode: str) -> Path:
    """Write an executable that speaks just enough app-server JSON-RPC.

    It answers ``initialize`` / ``thread/start`` / ``turn/start`` and then closes
    stdout (EOF) mid-turn to reproduce the reported transport loss.

    ``mode="event_consumer"``: ack ``turn/start`` then exit -- the turn is in
    flight and the event consumer is left awaiting an event that never comes.
    ``mode="pending_rpc"``: read ``turn/start`` then exit WITHOUT replying --
    the pending RPC future is left unresolved.
    """
    script = tmp_path / "codex"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json
            import sys

            if "--version" in sys.argv:
                print("codex-cli 0.0.0-scripted")
                raise SystemExit(0)
            if len(sys.argv) < 2 or sys.argv[1] != "app-server":
                raise SystemExit(2)

            mode = {mode!r}
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                request = json.loads(line)
                method = request.get("method")
                if method == "turn/start" and mode == "pending_rpc":
                    sys.stdout.flush()
                    raise SystemExit(0)
                if method == "initialize":
                    result = {{}}
                elif method == "thread/start":
                    result = {{"thread": {{"id": "thread-1"}}}}
                elif method == "turn/start":
                    result = {{"turn": {{"id": "turn-1"}}}}
                else:
                    result = {{}}
                sys.stdout.write(json.dumps({{"id": request["id"], "result": result}}) + "\\n")
                sys.stdout.flush()
                if method == "turn/start" and mode == "event_consumer":
                    raise SystemExit(0)
            """
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


async def _drive_until_eof(tmp_path: Path, *, mode: str) -> list[ExecutorEvent]:
    """Drive a real ``CodexExecutor`` turn against the scripted child.

    Returns ``run_turn``'s events; raises ``asyncio.TimeoutError`` when the turn
    never wakes after the app-server closes stdout -- the bug.
    """
    codex_path = _write_scripted_app_server(tmp_path, mode=mode)
    cwd = str(tmp_path)
    executor = CodexExecutor(
        cwd=cwd,
        model="scripted-model",
        codex_path=str(codex_path),
        enable_web_search=False,
    )

    async def _collect() -> list[ExecutorEvent]:
        events: list[ExecutorEvent] = []
        async for event in executor.run_turn(
            [{"role": "user", "content": "hi", "session_id": "transport-eof"}],
            [],
            "",
        ):
            events.append(event)
        return events

    try:
        return await asyncio.wait_for(_collect(), timeout=_WAKE_BOUND_S)
    finally:
        await asyncio.wait_for(executor.close(), timeout=30)


@pytest.mark.timeout(120)
async def test_event_consumer_woken_when_app_server_closes_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn in flight whose app-server closes stdout must fail promptly.

    Fails on a build without EOF wake-up: ``run_turn`` blocks on
    ``self._events.get()`` forever because the reader loop exits on EOF without
    notifying the consumer.
    """
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "src-codex-home"))
    try:
        events = await _drive_until_eof(tmp_path, mode="event_consumer")
    except asyncio.TimeoutError:
        pytest.fail(
            "run_turn did not wake within "
            f"{_WAKE_BOUND_S}s after the app-server closed stdout mid-turn -- "
            "the turn-event consumer is stuck until the idle watchdog."
        )
    except Exception as exc:
        if not _is_transport_closed_error(exc):
            raise
    else:
        assert any(isinstance(event, ExecutorError) for event in events), (
            "expected an ExecutorError event or a typed transport error once "
            f"transport loss is detected on stdout EOF; got {events!r}"
        )


@pytest.mark.timeout(120)
async def test_pending_rpc_failed_when_app_server_closes_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending RPC must fail when the app-server closes stdout before replying.

    Fails on a build without EOF wake-up: the ``turn/start`` future in
    ``_pending_requests`` is never resolved after EOF, so ``run_turn`` hangs
    awaiting it.
    """
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "src-codex-home"))
    try:
        events = await _drive_until_eof(tmp_path, mode="pending_rpc")
    except asyncio.TimeoutError:
        pytest.fail(
            "run_turn did not wake within "
            f"{_WAKE_BOUND_S}s after the app-server closed stdout with a "
            "turn/start RPC in flight -- the pending future is never failed."
        )
    except Exception as exc:
        if not _is_transport_closed_error(exc):
            raise
    else:
        assert any(isinstance(event, ExecutorError) for event in events), (
            "expected an ExecutorError event or a typed transport error once "
            f"the in-flight RPC's transport is lost; got {events!r}"
        )
