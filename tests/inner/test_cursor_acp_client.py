"""Deterministic tests for :class:`CursorAcpClient` against a fake ACP server.

Drives the real client (subprocess spawn, stdio JSON-RPC, reader loop, per-session
queue, ``_TurnEnd`` sentinel, agent->client request handling on a task, EOF) against
a stdlib-only fake ``cursor-agent acp`` — no real cursor-agent or network. Each test
runs its own event loop via :func:`asyncio.run` and wraps awaits in
:func:`asyncio.wait_for` so a regression fails fast instead of hanging the suite.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path

import pytest

from omnigent.inner.cursor_acp_client import CursorAcpClient, CursorAcpError

# A fake ACP server. Behavior keys off the prompt text:
#   "ERR"  -> respond to session/prompt with a JSON-RPC error
#   "EXIT" -> exit mid-turn (no response) to exercise the reader's EOF path
#   "PERM" -> stream a chunk, send a session/request_permission, then echo the
#             client's chosen optionId (proves the agent->client round-trip)
#   else   -> stream chunks A,B,C then echo "[<prompt>]" (multi-turn isolation)
_FAKE_SERVER = """
import json, sys

def send(o):
    sys.stdout.write(json.dumps(o) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        caps = {"loadSession": True}
        send({"jsonrpc": "2.0", "id": mid,
              "result": {"protocolVersion": 1, "agentCapabilities": caps}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "fake-sess-1"}})
    elif method == "session/load":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif method == "session/prompt":
        params = msg["params"]
        sid = params["sessionId"]
        text = "".join(b.get("text", "") for b in params.get("prompt", []))

        def upd(t):
            u = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": t}}
            send({"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": sid, "update": u}})

        if "ERR" in text:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": "boom"}})
        elif "EXIT" in text:
            sys.exit(0)
        elif "PERM" in text:
            upd("before ")
            send({"jsonrpc": "2.0", "id": 9001, "method": "session/request_permission",
                  "params": {"options": [{"optionId": "deny", "kind": "reject_once"},
                                         {"optionId": "allow", "kind": "allow_once"}]}})
            reply = None
            for l2 in sys.stdin:
                l2 = l2.strip()
                if not l2:
                    continue
                m2 = json.loads(l2)
                if m2.get("id") == 9001:
                    reply = m2
                    break
            chosen = (reply or {}).get("result", {}).get("outcome", {}).get("optionId", "?")
            upd("chose:" + chosen)
            send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
        else:
            for t in ("A", "B", "C"):
                upd(t)
            upd("[" + text + "]")
            send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
"""


@pytest.fixture
def fake_binary(tmp_path: Path) -> str:
    """Write the fake ACP server as an executable script and return its path.

    The client invokes ``<binary> acp``; a shebang pointing at the test
    interpreter runs the script regardless of the ``acp`` argv.
    """
    script = tmp_path / "fake_cursor_agent"
    script.write_text(f"#!{sys.executable}\n{_FAKE_SERVER}")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRUSR)
    return str(script)


async def _collect(client: CursorAcpClient, session_id: str, text: str) -> str:
    out = ""
    async for update in client.prompt(session_id, [{"type": "text", "text": text}]):
        if update.get("sessionUpdate") == "agent_message_chunk":
            out += update.get("content", {}).get("text", "")
    return out


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=15))


def test_basic_prompt_streams_and_completes(fake_binary: str) -> None:
    async def go():
        client = CursorAcpClient(binary=fake_binary)
        await client.start()
        try:
            sid = await client.new_session()
            text = await _collect(client, sid, "hello")
            assert text == "ABC[hello]"
            assert client.last_stop_reason == "end_turn"
        finally:
            await client.close()

    _run(go())


def test_multi_turn_same_session_no_crosstalk(fake_binary: str) -> None:
    async def go():
        client = CursorAcpClient(binary=fake_binary)
        await client.start()
        try:
            sid = await client.new_session()
            first = await _collect(client, sid, "first")
            second = await _collect(client, sid, "second")
            assert first == "ABC[first]"
            assert second == "ABC[second]"  # queue reused cleanly, no leftover
        finally:
            await client.close()

    _run(go())


def test_prompt_error_raises(fake_binary: str) -> None:
    async def go():
        client = CursorAcpClient(binary=fake_binary)
        await client.start()
        try:
            sid = await client.new_session()
            with pytest.raises(CursorAcpError):
                await _collect(client, sid, "ERR please")
        finally:
            await client.close()

    _run(go())


def test_agent_permission_request_is_answered(fake_binary: str) -> None:
    async def go():
        client = CursorAcpClient(binary=fake_binary)
        await client.start()
        try:
            sid = await client.new_session()
            text = await _collect(client, sid, "PERM")
            # The client auto-allowed -> the fake echoed the chosen optionId.
            assert "chose:allow" in text
            assert client.last_stop_reason == "end_turn"
        finally:
            await client.close()

    _run(go())


def test_eof_midturn_unblocks_prompt(fake_binary: str) -> None:
    async def go():
        client = CursorAcpClient(binary=fake_binary)
        await client.start()
        try:
            sid = await client.new_session()
            with pytest.raises(CursorAcpError):
                await _collect(client, sid, "EXIT now")
        finally:
            await client.close()

    _run(go())


def test_close_terminates_subprocess(fake_binary: str) -> None:
    async def go():
        client = CursorAcpClient(binary=fake_binary)
        await client.start()
        sid = await client.new_session()
        await _collect(client, sid, "hi")
        pid = client._proc.pid  # type: ignore[union-attr]
        await client.close()
        assert client._proc is not None and client._proc.returncode is not None
        # Second close is idempotent.
        await client.close()
        # Process is actually gone.
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    _run(go())
