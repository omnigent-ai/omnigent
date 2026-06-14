"""Tests for :class:`omnigent.inner.cursor_acp.AcpClient`.

Drives the client against a bidirectional fake ``cursor-agent acp`` process:
the fake parses each JSON-RPC request the client writes to stdin and feeds back
correlated responses (and ``session/update`` notifications) on stdout, so the
client's id correlation, notification queue, and server-request auto-reply are
exercised without a real cursor-agent. The subprocess spawn is stubbed via the
``cursor_acp._create_subprocess_exec`` seam.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from omnigent.inner import cursor_acp
from omnigent.inner.cursor_acp import AcpClient, AcpError, _pick_allow_option

Responder = Callable[[dict[str, Any]], list[str]]


class _FeedableReader:
    """An async byte stream the fake process feeds on demand."""

    def __init__(self) -> None:
        self._q: asyncio.Queue[bytes] = asyncio.Queue()

    def feed(self, data: bytes) -> None:
        self._q.put_nowait(data)

    def feed_eof(self) -> None:
        self._q.put_nowait(b"")

    async def read(self, _n: int = -1) -> bytes:
        return await self._q.get()


class _FakeStdin:
    """Captures the client's writes and invokes a responder per JSON-RPC line."""

    def __init__(self, on_message: Callable[[dict[str, Any]], None]) -> None:
        self._buf = bytearray()
        self._on_message = on_message
        self.sent: list[dict[str, Any]] = []

    def write(self, data: bytes) -> None:
        self._buf.extend(data)
        while b"\n" in self._buf:
            idx = self._buf.find(b"\n")
            line = bytes(self._buf[:idx])
            del self._buf[: idx + 1]
            msg = json.loads(line.decode("utf-8"))
            self.sent.append(msg)
            self._on_message(msg)

    async def drain(self) -> None:
        pass


class _FakeAcpProcess:
    def __init__(self, responder: Responder) -> None:
        self.stdout = _FeedableReader()
        self.stderr = _FeedableReader()
        self.stderr.feed_eof()
        self.returncode: int | None = None
        self.pid = 4242
        self._responder = responder
        self.stdin = _FakeStdin(self._on_message)

    def _on_message(self, msg: dict[str, Any]) -> None:
        # Only client→server *requests* (those with a method) get responses;
        # replies the client sends to our server-requests are just recorded.
        if "method" in msg:
            for line in self._responder(msg):
                self.stdout.feed((line + "\n").encode("utf-8"))

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


def _resp(rid: Any, result: dict[str, Any]) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": rid, "result": result})


def _notif(update: dict[str, Any]) -> str:
    # Real ACP shape: the update object is nested under params.update.
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": "s", "update": update},
        }
    )


def _patch_spawn(monkeypatch: Any, responder: Responder) -> _FakeAcpProcess:
    proc = _FakeAcpProcess(responder)

    async def _fake_spawn(*_args: Any, **_kwargs: Any) -> _FakeAcpProcess:
        return proc

    monkeypatch.setattr(cursor_acp, "_create_subprocess_exec", _fake_spawn)
    return proc


def _make_client() -> AcpClient:
    return AcpClient("/usr/bin/cursor-agent", env={"PATH": "/usr/bin"}, cwd="/tmp/x")


# ---------------------------------------------------------------------------


def test_pick_allow_option() -> None:
    assert _pick_allow_option([{"optionId": "allow_once", "kind": "allow_once"}]) == "allow_once"
    assert _pick_allow_option([{"optionId": "x", "kind": "reject"}]) == "x"  # first fallback
    assert _pick_allow_option([]) == "allow"


async def test_handshake_session_and_prompt(monkeypatch: Any) -> None:
    def responder(req: dict[str, Any]) -> list[str]:
        rid, method = req.get("id"), req.get("method")
        if method == "initialize":
            return [_resp(rid, {"agentCapabilities": {}, "authMethods": []})]
        if method == "session/new":
            return [_resp(rid, {"sessionId": "sess-1"})]
        if method == "session/prompt":
            return [
                _notif({"sessionUpdate": "agent_message_chunk", "content": {"text": "Hi"}}),
                _notif({"sessionUpdate": "agent_message_chunk", "content": {"text": " there"}}),
                _resp(rid, {"stopReason": "end_turn"}),
            ]
        return []

    _patch_spawn(monkeypatch, responder)
    client = _make_client()
    try:
        init = await client.start()
        assert "agentCapabilities" in init
        sid = await client.new_session(cwd="/tmp/x", model="gpt-5.4-mini")
        assert sid == "sess-1"

        items = [
            item async for item in client.prompt_stream(sid, [{"type": "text", "text": "hi"}])
        ]
    finally:
        await client.close()

    updates = [p for kind, p in items if kind == "update"]
    results = [p for kind, p in items if kind == "result"]
    assert [u["content"]["text"] for u in updates] == ["Hi", " there"]
    assert len(results) == 1 and results[0]["stopReason"] == "end_turn"


async def test_session_new_requires_session_id(monkeypatch: Any) -> None:
    def responder(req: dict[str, Any]) -> list[str]:
        if req.get("method") == "initialize":
            return [_resp(req.get("id"), {})]
        if req.get("method") == "session/new":
            return [_resp(req.get("id"), {})]  # no sessionId
        return []

    _patch_spawn(monkeypatch, responder)
    client = _make_client()
    try:
        await client.start()
        try:
            await client.new_session(cwd="/tmp/x", model=None)
            raise AssertionError("expected AcpError")
        except AcpError:
            pass
    finally:
        await client.close()


async def test_permission_request_auto_allowed(monkeypatch: Any) -> None:
    def responder(req: dict[str, Any]) -> list[str]:
        method = req.get("method")
        if method == "initialize":
            return [_resp(req.get("id"), {})]
        if method == "session/new":
            return [_resp(req.get("id"), {"sessionId": "s"})]
        if method == "session/prompt":
            # Server asks for permission mid-turn, then completes.
            return [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 999,
                        "method": "session/request_permission",
                        "params": {
                            "options": [
                                {"optionId": "allow_once", "kind": "allow_once"},
                                {"optionId": "reject", "kind": "reject_once"},
                            ]
                        },
                    }
                ),
                _resp(req.get("id"), {"stopReason": "end_turn"}),
            ]
        return []

    proc = _patch_spawn(monkeypatch, responder)
    client = _make_client()
    try:
        await client.start()
        sid = await client.new_session(cwd="/tmp/x", model=None)
        _ = [item async for item in client.prompt_stream(sid, [{"type": "text", "text": "go"}])]
    finally:
        await client.close()

    # The client auto-replied to the permission request selecting an allow option.
    replies = [m for m in proc.stdin.sent if m.get("id") == 999 and "result" in m]
    assert len(replies) == 1
    assert replies[0]["result"]["outcome"]["optionId"] == "allow_once"


async def test_request_failure_raises_acp_error(monkeypatch: Any) -> None:
    def responder(req: dict[str, Any]) -> list[str]:
        if req.get("method") == "initialize":
            return [
                json.dumps(
                    {"jsonrpc": "2.0", "id": req.get("id"), "error": {"code": -1, "message": "no"}}
                )
            ]
        return []

    _patch_spawn(monkeypatch, responder)
    client = _make_client()
    try:
        try:
            await client.start()
            raise AssertionError("expected AcpError")
        except AcpError:
            pass
    finally:
        await client.close()
