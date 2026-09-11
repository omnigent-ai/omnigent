"""E2E regression: a connect blip during a stream reconnect must not orphan a turn.

The Slack integration's stream-reconnect loop
(``omnigent_slack.omnigent._run_turn_once``) historically counted only
``StreamInterruptedError`` against its bounded reconnect budget. A
``ServerUnreachableError`` raised while *re-opening* the stream — a single
transient connect failure during the reconnect the budget exists for — escapes
the loop entirely, so the Slack user gets the public "server unreachable"
notice while the healthy turn keeps running server-side, orphaned.

The journey is faithful and network-level: a real ``omnigent server`` +
runner (the e2e ``live_server`` stack, mock LLM), fronted by a real TCP proxy
the Slack client dials — the same reverse-proxy topology the reconnect budget
was designed for. The test drives a real turn through the Slack client, has
the proxy sever the live SSE stream mid-turn (the anticipated proxy cap), and
refuses exactly ONE stream re-open (the blip); the proxy is healthy again
immediately after. The refusal drops the connection before any response byte,
so the client sees a transport error *before* the stream connects — exactly
the ``ServerUnreachableError`` the reconnect loop mishandles.

The assertions encode the CORRECT post-fix behavior — the blip is absorbed by
the reconnect budget and the turn completes — so this test fails on the buggy
build (``run_turn`` raises ``ServerUnreachableError`` and the turn is orphaned)
and passes once the connect blip is counted against the same budget.

Runs entirely against the mock LLM server — no real credentials needed::

    .venv/bin/python -m pytest tests/e2e/test_slack_stream_reconnect_connect_blip.py -v
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import socketserver
import sys
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest

from tests.e2e.conftest import (
    _REPO_ROOT,
    configure_mock_llm,
    create_runner_bound_session,
    register_inline_agent,
    release_mock_gate,
    reset_mock_llm,
)

try:
    from omnigent_slack.omnigent import OmnigentClient, ServerUnreachableError
except ModuleNotFoundError:  # Checkout without the omnigent-slack editable install.
    sys.path.insert(0, str(_REPO_ROOT / "integrations" / "slack" / "src"))
    from omnigent_slack.omnigent import OmnigentClient, ServerUnreachableError

# The scripted answer the mock LLM releases once the gate opens. The turn is
# healthy the whole time — only the client's view of it is under test.
_ANSWER = "The Byzantine essay, delivered after one proxy blip."

# How long to wait for the client to reconnect through the blip. Post-fix the
# reconnect backoff is ~1s * attempt; on the buggy build the turn task dies
# with ServerUnreachableError almost immediately after the blip.
_RECONNECT_OBSERVE_TIMEOUT_S = 60.0


def _is_stream_request_line(line: bytes) -> bool:
    """Whether an HTTP request line targets the long-lived SSE stream endpoint."""
    return line.startswith(b"GET ") and b"/stream" in line.split(b" ", 2)[1]


class _BlippingProxy:
    """A reverse proxy that can sever live streams and refuse one stream re-open.

    Every accepted TCP connection is piped byte-for-byte to the backend, with
    the client→backend direction scanned in-band for HTTP request lines (so
    detection survives keep-alive connection reuse and never times out an idle
    SSE tail). Two controls model the reverse-proxy faults the reconnect budget
    exists for:

    * ``sever_live_connections`` kills every live piped connection — a proxy
      max-duration cap cutting a long-lived SSE response mid-turn. The client
      sees this as a mid-stream drop (``StreamInterruptedError``).
    * ``refuse_next_stream_open`` arms a one-shot fault: the next request line
      targeting ``GET .../stream`` is dropped before it reaches the backend and
      the client connection is closed before any response byte. httpx surfaces
      that as a transport error *before* the stream connects, which the Slack
      client classifies as ``ServerUnreachableError`` — the connect blip.

    Every other request, and every later stream open, passes through untouched.
    """

    def __init__(self, backend_host: str, backend_port: int) -> None:
        self._backend = (backend_host, backend_port)
        self._lock = threading.Lock()
        self._refuse_stream_opens = 0
        self.refused_stream_opens = 0
        self.forwarded_stream_opens = 0
        self._live_socks: set[socket.socket] = set()
        proxy = self

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                proxy._handle(self.request)

        class _Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = _Server(("127.0.0.1", 0), _Handler)
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _handle(self, client: socket.socket) -> None:
        # No socket timeout: a live SSE tail legitimately blocks for many
        # seconds between heartbeats, and a recv timeout here would tear the
        # idle stream down and manufacture spurious mid-stream drops.
        client.settimeout(None)
        try:
            backend = socket.create_connection(self._backend, timeout=10.0)
        except OSError:
            with contextlib.suppress(OSError):
                client.close()
            return
        backend.settimeout(None)
        with self._lock:
            self._live_socks.update((client, backend))
        try:
            t1 = threading.Thread(
                target=self._pipe_client_to_backend, args=(client, backend), daemon=True
            )
            t2 = threading.Thread(target=self._pipe, args=(backend, client), daemon=True)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
        finally:
            with self._lock:
                self._live_socks.discard(client)
                self._live_socks.discard(backend)
            for sock in (client, backend):
                with contextlib.suppress(OSError):
                    sock.close()

    def _sever_pair(self, client: socket.socket, backend: socket.socket) -> None:
        for sock in (client, backend):
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()

    def _pipe_client_to_backend(self, client: socket.socket, backend: socket.socket) -> None:
        """Forward client→backend bytes, scanning request lines for the blip.

        Keeps a small carryover of the trailing partial line so a request line
        split across two recv chunks is still classified. When the one-shot
        blip is armed and a stream request line appears, the connection is
        severed *before* the request reaches the backend, so the client's
        stream open fails at the transport layer with no response.
        """
        carry = b""
        try:
            while True:
                data = client.recv(65536)
                if not data:
                    break
                scan = carry + data
                lines = scan.split(b"\r\n")
                carry = lines[-1][-2048:]
                severed = False
                for line in lines[:-1]:
                    if not _is_stream_request_line(line):
                        continue
                    with self._lock:
                        if self._refuse_stream_opens > 0:
                            self._refuse_stream_opens -= 1
                            self.refused_stream_opens += 1
                            severed = True
                        else:
                            self.forwarded_stream_opens += 1
                    if severed:
                        break
                if severed:
                    # The connect blip: drop before the request reaches the
                    # backend so the client sees a transport error pre-connect.
                    self._sever_pair(client, backend)
                    return
                backend.sendall(data)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                backend.shutdown(socket.SHUT_WR)

    @staticmethod
    def _pipe(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            # A severed peer tears the pipe down; the handler closes both ends.
            pass
        finally:
            with contextlib.suppress(OSError):
                dst.shutdown(socket.SHUT_WR)

    def refuse_next_stream_open(self) -> None:
        """Arm the one-shot connect blip for the next ``GET .../stream``."""
        with self._lock:
            self._refuse_stream_opens += 1

    def sever_live_connections(self) -> None:
        """Cut every live piped connection (proxy cap severing the SSE tail)."""
        with self._lock:
            socks = list(self._live_socks)
        for sock in socks:
            # shutdown() interrupts blocked recv()s immediately so both peers
            # see the tunnel die right away (close() alone can leave them
            # blocked).
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


async def _wait_for_gate_pending(mock_llm_server_url: str, timeout: float = 60.0) -> None:
    """Poll until the mock LLM has a request parked on its gate (turn is live)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = httpx.get(f"{mock_llm_server_url}/gate/pending", timeout=2.0)
        resp.raise_for_status()
        if resp.json().get("pending"):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"No mock LLM request parked on the gate within {timeout}s")


def _persisted_assistant_text(client: httpx.Client, session_id: str) -> str:
    """Concatenate all persisted assistant message text for *session_id*."""
    resp = client.get(f"/v1/sessions/{session_id}")
    resp.raise_for_status()
    parts: list[str] = []
    for item in resp.json().get("items", []):
        data = item.get("data") if isinstance(item.get("data"), dict) else item
        if item.get("type") == "message" and data.get("role") == "assistant":
            for block in data.get("content", []) or []:
                text = block.get("text")
                if text:
                    parts.append(text)
    return "\n".join(parts)


@pytest.mark.timeout(300)
async def test_connect_blip_during_stream_reconnect_stays_within_budget(
    live_server: str,
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """One refused connect during a stream reconnect must not end a healthy turn.

    Journey (from the bug report): a Slack user's turn is streaming through a
    reverse proxy → the proxy severs the long-lived stream mid-turn (the
    anticipated cap the reconnect budget exists for) → the client's re-open
    hits a single transient connect blip → the proxy is healthy again.
    Expected: the blip is counted against the same consecutive-reconnect
    budget ``StreamInterruptedError`` uses, the next re-open succeeds, and the
    turn's answer arrives. Actual (bug): ``ServerUnreachableError`` escapes
    the reconnect loop on the first blip, the user is told the server is
    unreachable, and the still-running turn is orphaned.
    """
    reset_mock_llm(mock_llm_server_url)

    model = f"mock-slack-blip-{uuid.uuid4().hex[:6]}"
    agent_name = register_inline_agent(
        http_client,
        name=f"slack-blip-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a helpful assistant. Answer concisely.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )
    # The gated response keeps the turn running server-side until released, so
    # the stream is guaranteed to be severed MID-turn.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _ANSWER, "block": True}],
        key=model,
    )
    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )

    parsed = urlparse(live_server)
    assert parsed.hostname is not None and parsed.port is not None
    proxy = _BlippingProxy(parsed.hostname, parsed.port)
    # The Slack service's client dials the operator-configured server URL —
    # here the proxy, exactly like a proxy-fronted deployment.
    client = OmnigentClient(f"http://127.0.0.1:{proxy.port}")
    events: list[dict[str, Any]] = []

    async def _consume_turn() -> None:
        async for event in client.run_turn(session_id, "Write the Byzantine essay."):
            events.append(event)

    turn = asyncio.ensure_future(_consume_turn())
    try:
        # The turn is live once the mock LLM parks on its gate; the client's
        # stream (opened through the proxy) is tailing it.
        await _wait_for_gate_pending(mock_llm_server_url)
        deadline = time.monotonic() + 30.0
        while proxy.forwarded_stream_opens < 1 and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        assert proxy.forwarded_stream_opens >= 1, "client never opened the stream via the proxy"

        # Arm the one-shot blip FIRST so the re-open that follows the severed
        # stream deterministically hits it, then cut the live stream.
        proxy.refuse_next_stream_open()
        proxy.sever_live_connections()

        # Post-fix, the client counts the blip against the reconnect budget and
        # the NEXT re-open is forwarded. On the buggy build the turn task dies
        # with ServerUnreachableError instead.
        deadline = time.monotonic() + _RECONNECT_OBSERVE_TIMEOUT_S
        while time.monotonic() < deadline:
            if turn.done():
                exc = turn.exception()
                if isinstance(exc, ServerUnreachableError):
                    snapshot = http_client.get(f"/v1/sessions/{session_id}")
                    server_status = (
                        snapshot.json().get("status") if snapshot.status_code == 200 else "?"
                    )
                    pytest.fail(
                        "A single transient connect failure during a stream "
                        "reconnect escaped the retry budget: run_turn raised "
                        "ServerUnreachableError on the blip (budget allows 6 "
                        "consecutive reconnect attempts), while the turn was "
                        f"still healthy server-side (session status={server_status!r}). "
                        "The Slack user gets the public 'server unreachable' notice "
                        f"and the still-running turn is orphaned. Error: {exc}"
                    )
                if exc is not None:
                    raise exc
                raise AssertionError(
                    "run_turn ended without the turn finishing: the reconnect "
                    "never happened and no answer was produced. Events seen: "
                    f"{[e.get('type') for e in events]}"
                )
            if proxy.refused_stream_opens >= 1 and proxy.forwarded_stream_opens >= 2:
                break
            await asyncio.sleep(0.2)
        assert proxy.refused_stream_opens == 1, "the armed connect blip never fired"
        assert proxy.forwarded_stream_opens >= 2, (
            "client never re-opened the stream after the one-shot blip; the "
            "reconnect budget was not applied to the connect failure"
        )

        # The proxy is healthy and the client is tailing again — let the turn
        # finish and the answer flow.
        release_mock_gate(mock_llm_server_url)
        await asyncio.wait_for(turn, timeout=120.0)

        persisted = _persisted_assistant_text(http_client, session_id)
        assert _ANSWER in persisted, (
            f"turn did not complete with the scripted answer; persisted assistant "
            f"text: {persisted!r}"
        )
    finally:
        if not turn.done():
            turn.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await turn
        # Unblock the mock gate so the orphaned server-side turn can finish and
        # the session-scoped runner isn't wedged for later tests.
        with contextlib.suppress(Exception):
            release_mock_gate(mock_llm_server_url)
        await client.aclose()
        proxy.close()
