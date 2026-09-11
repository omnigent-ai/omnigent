"""End-to-end: a web message must survive a codex-native active-turn-id desync.

Production codex-native sessions fail user turns with::

    turn surfaced to UI as failed for ... (harness=codex-native): {'code':
    'runner_error', 'message': "inner executor error: Codex native executor
    error: {'code': -32600, 'message': 'expected active turn id `A` but
    found `B`'}"}

The journey: the user has a codex-native session whose Codex thread is
running a turn; the bridge's recorded ``active_turn_id`` has drifted from
the turn Codex actually considers active (Codex rotated turns between the
bridge write and the injection); the user sends a message from the web UI.
``CodexNativeExecutor.run_turn`` steers with the stale recorded id, the
app-server rejects it with the structured ``-32600 "expected active turn id
`A` but found `B`"`` error, and ``_inject_codex_turn`` re-raises it (its
stale-steer recovery only matches ``"no active turn to steer"``), so the
user's turn dies in the web UI.

Why fault-injection instead of driving the live TUI
----------------------------------------------------
The desync window is a narrow production race (the forwarder's bridge-state
write racing the executor's read), not a state a scripted UI drive can hit
deterministically, and this environment has no Codex credential or reachable
model endpoint to run a real signed-in codex-native web session. So, like
``test_host_codex_native_e2e.py::test_codex_native_stale_completed_turn_recovers_with_new_turn``
does for the stale-*completed*-turn variant, this test recreates the proven
desync state directly against **real** components: a real ``codex
app-server`` (spawned from the ``codex`` CLI on PATH) whose model provider
points at a local sink that never answers — keeping the started turn
authoritatively *active* — and a real bridge state recording a different,
stale turn id. The real ``CodexNativeExecutor`` then injects a user message
over the real WebSocket JSON-RPC wire, exactly the production stack::

    run_turn -> _inject_codex_turn -> _steer_codex_turn -> client.request

While the bug is live the executor yields ``ExecutorError("Codex native
executor error: {'code': -32600, 'message': 'expected active turn id ...
but found ...'}")`` — the exact logged failure. After a fix the message
must reach the turn Codex names as active (Codex's rejection is
authoritative about it) and the user's turn must complete.

Self-contained and offline: needs only the ``codex`` CLI on PATH — no
login, no model access, no server. Run with::

    .venv/bin/python -m pytest tests/e2e/test_codex_native_turn_desync_e2e.py -v
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerClient,
    CodexAppServerResponseError,
)
from omnigent.harnesses.codex_native.bridge import (
    CODEX_NATIVE_REQUEST_SESSION_ID_ENV_VAR,
    CodexNativeBridgeState,
    read_bridge_state,
    write_bridge_state,
)
from omnigent.inner.codex_native_executor import CodexNativeExecutor
from omnigent.inner.executor import ExecutorError, TurnComplete

# A well-formed UUIDv7-shaped turn id that is never the real active turn:
# the bridge's stale record, standing in for the id the forwarder wrote
# before Codex rotated turns.
_STALE_TURN_ID = "01a00000-0000-7000-8000-000000000000"

_APP_SERVER_CONNECT_TIMEOUT_S = 30.0
# turn/start returns before the app-server registers the turn as
# steerable, so the stale-steer precondition is polled until it draws
# the structured desync rejection rather than "no active turn to steer".
_TURN_STEERABLE_TIMEOUT_S = 15.0
# connect() runs the ws handshake plus the initialize request with no
# internal timeout; attaching while the app-server is still booting can
# hang it, so every attempt is individually bounded and retried.
_CONNECT_ATTEMPT_TIMEOUT_S = 10.0
_RPC_TIMEOUT_S = 15.0


def _free_port() -> int:
    """
    Allocate one free loopback TCP port.

    :returns: A currently-unbound port number.
    """
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def _sink_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """
    Absorb a model-provider connection without ever responding.

    Codex's model request hangs on this sink, so the turn it belongs to
    stays *active* on the app-server for the whole test — the precondition
    the stale steer needs to be rejected with the desync error rather than
    "no active turn to steer".

    :param reader: Connected stream reader.
    :param writer: Connected stream writer.
    """
    try:
        while await reader.read(65536):
            pass
    except Exception:
        pass
    finally:
        writer.close()


def _write_codex_home(home: Path, sink_port: int) -> None:
    """
    Seed a scratch ``CODEX_HOME`` whose model provider is the local sink.

    :param home: Scratch CODEX_HOME directory.
    :param sink_port: Loopback port of the never-answering sink server.
    """
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "sk-test-offline"}))
    (home / "config.toml").write_text(
        'model = "gpt-5.1-codex"\n'
        'model_provider = "sink"\n'
        "\n"
        "[model_providers.sink]\n"
        'name = "Sink"\n'
        f'base_url = "http://127.0.0.1:{sink_port}/v1"\n'
        'wire_api = "responses"\n'
        'env_key = "OPENAI_API_KEY"\n'
    )


def _spawn_app_server(home: Path, ws_url: str, stderr_log: Path) -> subprocess.Popen[bytes]:
    """
    Spawn the real ``codex app-server`` listening on *ws_url*.

    :param home: Scratch CODEX_HOME directory.
    :param ws_url: Loopback WebSocket listen URL, e.g. ``"ws://127.0.0.1:9876"``.
    :param stderr_log: File capturing the app-server's stderr for diagnostics.
    :returns: The spawned app-server subprocess handle.
    """
    env = os.environ.copy()
    env["CODEX_HOME"] = str(home)
    env["OPENAI_API_KEY"] = "sk-test-offline"
    # The sink must be dialed directly, never through a CI egress proxy.
    env["NO_PROXY"] = "127.0.0.1,localhost," + env.get("NO_PROXY", "")
    env["no_proxy"] = env["NO_PROXY"]
    codex = shutil.which("codex")
    assert codex is not None
    with open(stderr_log, "wb") as log_fh:
        return subprocess.Popen(
            [codex, "app-server", "--listen", ws_url],
            env=env,
            cwd=str(home),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )


async def _connect_when_ready(
    ws_url: str,
    proc: subprocess.Popen[bytes],
    stderr_log: Path,
) -> CodexAppServerClient:
    """
    Connect (with the initialize handshake) once the app-server listens.

    :param ws_url: App-server WebSocket URL.
    :param proc: App-server subprocess, polled so a crash fails fast.
    :param stderr_log: The app-server stderr log, quoted on failure.
    :returns: A connected client.
    """
    deadline = asyncio.get_running_loop().time() + _APP_SERVER_CONNECT_TIMEOUT_S
    while True:
        if proc.poll() is not None:
            pytest.fail(
                "codex app-server exited before accepting connections: "
                f"{stderr_log.read_text(errors='replace')[-2000:]}"
            )
        client = CodexAppServerClient(ws_url=ws_url, client_name="omnigent-desync-e2e")
        try:
            await asyncio.wait_for(client.connect(), _CONNECT_ATTEMPT_TIMEOUT_S)
            return client
        except (OSError, asyncio.TimeoutError):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.close(), 5.0)
            if asyncio.get_running_loop().time() > deadline:
                pytest.fail(
                    "codex app-server never accepted a connection: "
                    f"{stderr_log.read_text(errors='replace')[-2000:]}"
                )
            await asyncio.sleep(0.1)


async def _request(
    client: CodexAppServerClient, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    """
    Issue one bounded driver RPC.

    :param client: Connected app-server client.
    :param method: JSON-RPC method, e.g. ``"turn/start"``.
    :param params: JSON-RPC params.
    :returns: The decoded response envelope.
    """
    return await asyncio.wait_for(client.request(method, params), _RPC_TIMEOUT_S)


async def _collect_turn_events(executor: CodexNativeExecutor, text: str) -> list[Any]:
    """
    Run one executor turn for a web user message and collect its events.

    :param executor: Native Codex executor under test.
    :param text: User text to send, e.g. ``"follow up"``.
    :returns: Events yielded by :meth:`CodexNativeExecutor.run_turn`.
    """
    events: list[Any] = []
    async for event in executor.run_turn(
        [{"role": "user", "content": [{"type": "input_text", "text": text}]}],
        [],
        "",
    ):
        events.append(event)
    return events


@pytest.mark.skipif(
    shutil.which("codex") is None,
    reason="codex-native turn-desync e2e needs the `codex` CLI on PATH",
)
@pytest.mark.timeout(120)
async def test_web_message_survives_active_turn_id_desync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale bridge turn id must not fail the user's web message.

    Recreates the production desync against a real codex app-server: Codex
    is running turn B while the bridge records stale turn A. The web
    message injected through the real executor must be delivered to the
    turn Codex names as active — never surfaced to the UI as::

        Codex native executor error: {'code': -32600, 'message':
        'expected active turn id `A` but found `B`'}
    """
    # The executor only injects when no other Omnigent session owns the
    # thread; this test is the owning session.
    monkeypatch.delenv(CODEX_NATIVE_REQUEST_SESSION_ID_ENV_VAR, raising=False)

    sink_conns: list[asyncio.StreamWriter] = []

    async def _sink(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sink_conns.append(writer)
        await _sink_connection(reader, writer)

    sink_port = _free_port()
    sink = await asyncio.start_server(_sink, "127.0.0.1", sink_port)
    home = tmp_path / "codex-home"
    _write_codex_home(home, sink_port)
    ws_url = f"ws://127.0.0.1:{_free_port()}"
    stderr_log = tmp_path / "app-server.stderr.log"
    proc = _spawn_app_server(home, ws_url, stderr_log)
    driver: CodexAppServerClient | None = None
    try:
        driver = await _connect_when_ready(ws_url, proc, stderr_log)

        # Real thread + real turn B: Codex's authoritative active turn.
        # Its model request hangs on the sink, so B stays active.
        response = await _request(driver, "thread/start", {})
        thread_id = response["result"]["thread"]["id"]
        response = await _request(
            driver,
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": "keep working"}],
                "environments": [{"environmentId": "local", "cwd": str(tmp_path)}],
            },
        )
        turn_b = response["result"]["turn"]["id"]
        assert isinstance(turn_b, str) and turn_b
        assert turn_b != _STALE_TURN_ID

        # Precondition: Codex must consider B active and reject a stale
        # steer with the structured desync error this bug is about. The
        # app-server registers a started turn as steerable asynchronously
        # after turn/start returns, so poll until the probe stops drawing
        # the not-yet-registered "no active turn to steer".
        deadline = asyncio.get_running_loop().time() + _TURN_STEERABLE_TIMEOUT_S
        while True:
            with pytest.raises(CodexAppServerResponseError) as rejection:
                await _request(
                    driver,
                    "turn/steer",
                    {
                        "threadId": thread_id,
                        "expectedTurnId": _STALE_TURN_ID,
                        "input": [{"type": "text", "text": "probe"}],
                    },
                )
            if "expected active turn id" in str(rejection.value):
                break
            if asyncio.get_running_loop().time() > deadline:
                pytest.fail(
                    "codex app-server never produced the desync rejection; "
                    f"the started turn never became steerable: {rejection.value}"
                )
            await asyncio.sleep(0.05)

        # The proven production desync state: Codex runs B, the bridge
        # still records A (the forwarder's write raced the rotation).
        bridge_dir = tmp_path / "bridge"
        bridge_dir.mkdir()
        write_bridge_state(
            bridge_dir,
            CodexNativeBridgeState(
                session_id="conv_desync_e2e",
                socket_path=ws_url,
                thread_id=thread_id,
                codex_home=str(home),
                active_turn_id=_STALE_TURN_ID,
            ),
        )

        # The user's web message, injected through the real executor —
        # the exact production path (run_turn -> _inject_codex_turn ->
        # _steer_codex_turn -> client.request).
        executor = CodexNativeExecutor(bridge_dir=bridge_dir)
        events = await _collect_turn_events(executor, "please also update the docs")

        errors = [event for event in events if isinstance(event, ExecutorError)]
        assert not errors, (
            "web message died in the active-turn-id desync instead of being "
            f"delivered to Codex's live turn: {errors[0].message!r}"
        )
        assert [type(event) for event in events] == [TurnComplete]

        # The injection must have resynchronized onto the turn Codex named
        # as active — steering B, never double-starting a new turn.
        state = read_bridge_state(bridge_dir)
        assert state is not None
        assert state.active_turn_id == turn_b, (
            "injection did not resync onto Codex's authoritative active turn: "
            f"bridge records {state.active_turn_id!r}, codex runs {turn_b!r}"
        )
    finally:
        if driver is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(driver.close(), 10.0)
        proc.kill()
        proc.wait()
        for writer in sink_conns:
            with contextlib.suppress(Exception):
                writer.close()
        sink.close()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(sink.wait_closed(), 5.0)
