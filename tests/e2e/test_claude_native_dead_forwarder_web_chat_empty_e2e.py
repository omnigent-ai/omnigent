"""E2E regression: with the native Claude
harness, assistant turns never reach the web Chat view when the transcript
forwarder has died but the tmux pane is still alive.

Guarded bug
-----------
A daemon-owned ``omnigent claude`` (``claude-native``) session defers transcript
forwarding to the RUNNER process: a background forwarder task tails Claude's
JSONL transcript and mirrors every user/assistant record into the server's
conversation store via ``external_conversation_item`` POSTs. The web **Chat**
view renders from that store; the web **Terminal** tab reads the live tmux pane
directly.

If the runner-owned forwarder task dies (crash, transport error, token expiry)
while the tmux pane keeps running, the two surfaces desync exactly as reported:
the Terminal tab still shows Claude's replies (it reads the pane), but the Chat
tab stays empty of assistant content (the store never receives it) even though
the user's own messages show. On ``main`` the forwarder is (re)started only when
the terminal pane is (re)created -- a **live pane + dead forwarder** is never
healed, so every subsequent web turn's assistant reply is lost from Chat.

The seam
--------
A web message turn for a native session flows through the runner's
``POST /v1/sessions/{id}/events`` (non-streaming) -> ``_run_turn_bg`` ->
``_run_turn_bg_setup_and_stream``, which calls ``_ensure_native_terminal_for_turn``
before delivering the turn. That self-heal probes the registered pane: on a LIVE
pane it returns early ("pane is registered and alive -- nothing to heal") and, on
``main``, does NOTHING to the forwarder. So a session whose pane is alive but
whose forwarder task is gone keeps taking turns while its assistant replies never
reach the conversation store the web Chat renders.

The fix must restart the transcript forwarder from that
live-pane branch (and register it in ``_AUTO_FORWARDER_TASKS`` for teardown), so a
web turn re-establishes the store mirror and the assistant reply reaches Chat.

What this drives
----------------
The REAL runner app (``create_runner_app``) over ASGI, with a REAL
``TerminalRegistry`` holding a LIVE claude pane, a REAL claude-native session, and
the REAL turn path (``POST /v1/sessions/{id}/events`` -> ``_run_turn_bg`` ->
``_ensure_native_terminal_for_turn``). ``_auto_create_claude_terminal`` is stubbed
at session create so NO forwarder is started there -- reproducing the dead-forwarder
state -- and a live pane is planted directly. ``RUNNER_SERVER_URL`` points at a
recording HTTP server standing in for the Omnigent server's conversation store;
the assistant reply is appended to Claude's transcript AFTER the turn (as a real
Claude reply would arrive), and the test asserts that reply reaches the store the
web Chat renders from.

Expected (fixed): the assistant reply reaches the conversation store. Buggy
(``main``): the live-pane turn never restarts the dead forwarder, so the assistant
reply is never mirrored -- the web Chat stays empty while the terminal has it. This
test then FAILS with the assistant marker absent from every conversation-store POST.

Environment fidelity
--------------------
The reported symptom (web Chat empty for a native Claude session) is
environment-independent runner/forwarder product behavior; it is driven here at
its exact production seam. No LLM and no real Claude CLI are invoked.

Run::

    .venv/bin/python -m pytest \\
        tests/e2e/test_claude_native_dead_forwarder_web_chat_empty_e2e.py -v
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import queue
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from omnigent.entities.session_resources import SessionResourceView
from omnigent.harnesses.claude_native.bridge import (
    bridge_dir_for_conversation_id,
    prepare_bridge_dir,
    record_hook_event,
)
from omnigent.runner import create_runner_app
from omnigent.runner.native import orchestration as native_orchestration
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import TerminalRegistry
from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient, _sse
from tests.runner.helpers import NullServerClient, make_test_terminal_instance

# A 32-hex conversation id + agent id (the runner keys sessions by these).
_CONV_ID = "cafedeadcafedeadcafedeadcafedead"
_AGENT_ID = "abadcafeabadcafeabadcafeabadcafe"

# The claude session id pinned in the bridge state, so hook-driven identity
# checks accept the transcript path and the later append.
_CLAUDE_SESSION_ID = "claude-live-pane-dead-forwarder"

# The user's message shows in web Chat on ``main`` (it does not depend on the
# forwarder); the assistant reply reaches Chat ONLY via the forwarder mirror.
_USER_MARKER = "user-message-that-shows-in-chat"
_ASSISTANT_MARKER = "assistant-reply-that-must-reach-chat"

# Wait budgets. On the FIXED build the live-pane turn restarts the forwarder,
# which registers a task and begins tailing within ~a second; on ``main`` the
# task never appears, so these are upper bounds before the negative assertion.
_FORWARDER_START_TIMEOUT_S = 8.0
_FORWARDER_START_POLL_S = 0.1
_MARKER_TIMEOUT_S = 20.0
_MARKER_POLL_S = 0.2


class _StoreRecordingServer(ThreadingHTTPServer):
    """Recording HTTP server standing in for the Omnigent conversation store."""

    requests: queue.Queue[dict[str, Any]]


class _StoreRecordingHandler(BaseHTTPRequestHandler):
    """Record every POST body; 200 everything so the forwarder proceeds."""

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _record_and_ok(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError):
            payload = json.loads(raw.decode("utf-8"))
            if isinstance(payload, dict):
                cast(_StoreRecordingServer, self.server).requests.put(payload)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self) -> None:
        self._record_and_ok()

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")


def _drain_bodies(server: _StoreRecordingServer) -> list[dict[str, Any]]:
    """Drain and return every conversation-store POST body recorded so far."""
    bodies: list[dict[str, Any]] = []
    while True:
        try:
            bodies.append(server.requests.get_nowait())
        except queue.Empty:
            return bodies


def _marker_in_bodies(bodies: list[dict[str, Any]], marker: str) -> bool:
    """True when any recorded conversation-store POST carries *marker*."""
    return any(marker in json.dumps(body) for body in bodies)


def _native_spec() -> AgentSpec:
    """A claude-native agent spec (the ``omnigent claude`` shape)."""
    return AgentSpec(
        spec_version=1,
        name="dead-forwarder-claude-native",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )


def _user_record(uuid: str, text: str) -> dict[str, Any]:
    """A ``type=user`` transcript record in the shape a live Claude CLI writes."""
    return {
        "type": "user",
        "isSidechain": False,
        "uuid": uuid,
        "message": {"role": "user", "content": text},
        "promptSource": "typed",
        "userType": "external",
    }


def _assistant_record(uuid: str, text: str) -> dict[str, Any]:
    """A ``type=assistant`` transcript record with a text content block."""
    return {
        "type": "assistant",
        "isSidechain": False,
        "uuid": uuid,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _seed_prior_transcript(bridge_dir: Path, transcript_path: Path) -> None:
    """Write the pre-turn transcript (the user's message) + point hooks at it.

    Represents the state right after the user sent a message and it appeared in
    Chat: the transcript holds the user record, and a recorded ``Stop`` hook
    reports the transcript path (+ pins the claude session id) so the forwarder
    resolves the file to tail on its first poll.
    """
    transcript_path.write_text(
        json.dumps(_user_record("seeded-user-uuid", _USER_MARKER)) + "\n",
        encoding="utf-8",
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": _CLAUDE_SESSION_ID,
            "transcript_path": str(transcript_path),
        },
    )


def _append_assistant_reply(bridge_dir: Path, transcript_path: Path) -> None:
    """Append Claude's reply to the transcript, as a real assistant turn would.

    A healthy forwarder tailing the transcript mirrors this into the store; a
    dead-and-never-restarted forwarder does not, which is the reported loss.
    """
    with transcript_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(_assistant_record("appended-assistant-uuid", _ASSISTANT_MARKER)) + "\n"
        )
    # A finished assistant turn fires a Stop hook; re-record it (same identity)
    # so any hook-driven rescan also sees the appended reply.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": _CLAUDE_SESSION_ID,
            "transcript_path": str(transcript_path),
        },
    )


async def _live_forwarder_task(conv_id: str) -> asyncio.Task[object] | None:
    """Return the registered, still-running forwarder task for *conv_id*, if any."""
    task = native_orchestration._AUTO_FORWARDER_TASKS.get(conv_id)
    if task is not None and not task.done():
        return task
    return None


def _install_auto_create_stub(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    """Stub claude-native auto-create so session create starts NO forwarder.

    This reproduces the dead-forwarder state: the session is a real claude-native
    conversation, but the forwarder that would mirror its transcript into the
    store was never started (or died) -- and nothing has recreated the pane to
    restart it. The test plants the live pane itself.
    """

    async def _stub_auto_create(
        session_id: str,
        resource_registry: object,
        publish_event: object,
        **_kwargs: object,
    ) -> SessionResourceView:
        del resource_registry, publish_event
        calls.append(session_id)
        return SessionResourceView(
            id="terminal_claude_main",
            type="terminal",
            session_id=session_id,
            name="claude",
        )

    async def _stub_launch_claude(ctx: Any) -> SessionResourceView:
        return await _stub_auto_create(ctx.session_id, ctx.resource_registry, ctx.publish_event)

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_claude_terminal",
        _stub_auto_create,
    )
    monkeypatch.setattr(
        "omnigent.runner.native._auto_create_claude_terminal",
        _stub_auto_create,
    )
    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._launch_claude",
        _stub_launch_claude,
    )
    monkeypatch.setattr("omnigent.runner.native._launch_claude", _stub_launch_claude)


@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_live_pane_dead_forwarder_loses_assistant_reply_from_web_chat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A web turn on a live-pane / dead-forwarder claude-native session must
    re-establish the transcript mirror so the assistant reply reaches Chat.

    Journey (the reporter's): run ``omnigent claude``; open the web UI; from the
    Chat tab send a prompt. The terminal shows Claude's reply, but the Chat tab
    stays empty of assistant content (the user's own message shows). Only the
    transcript forwarder mirrors assistant replies into the conversation store
    the Chat renders from, and when that forwarder has died while the tmux pane
    is still alive, no web turn restarts it.

    Expected: the assistant reply reaches the conversation store. Buggy
    (``main``): the live-pane self-heal returns early without touching the
    forwarder, so the reply is never mirrored -- this test FAILS with the
    assistant marker absent from every store POST while it is present in the
    transcript (the terminal's source).
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    calls: list[str] = []
    _install_auto_create_stub(monkeypatch, calls)

    # Clear any stale registry entry for this conv id from a prior run.
    native_orchestration._AUTO_FORWARDER_TASKS.pop(_CONV_ID, None)

    bridge_dir: Path | None = None
    server = _StoreRecordingServer(("127.0.0.1", 0), _StoreRecordingHandler)
    server.requests = queue.Queue()
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    host, port = server.server_address[0], int(server.server_address[1])
    store_url = f"http://{host}:{port}"

    # The forwarder-restart seam reads RUNNER_SERVER_URL to find the store; point
    # it at the recording server that stands in for the Omnigent conversation store.
    monkeypatch.setenv("RUNNER_SERVER_URL", store_url)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return _native_spec()

    registry = TerminalRegistry()
    app = create_runner_app(
        process_manager=_FakeProcessManager(  # type: ignore[arg-type]
            _ScriptedHarnessClient(
                [
                    _sse({"type": "response.created", "response": {"id": "resp_heal"}}),
                    _sse({"type": "response.completed", "response": {"id": "resp_heal"}}),
                ]
            )
        ),
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=registry,
        runner_workspace=workspace,
    )

    transport = httpx.ASGITransport(app=app)
    try:
        # Root the bridge dir under the production claude-native bridge root, then
        # seed the pre-turn transcript (the user's message) and the Stop hook that
        # tells the forwarder which file to tail.
        bridge_dir = prepare_bridge_dir(_CONV_ID, workspace=workspace)
        assert bridge_dir == bridge_dir_for_conversation_id(_CONV_ID)
        transcript_path = bridge_dir / "transcript.jsonl"
        _seed_prior_transcript(bridge_dir, transcript_path)

        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            # Create the claude-native session (auto-create stubbed -> no forwarder).
            create_resp = await client.post(
                "/v1/sessions",
                json={"session_id": _CONV_ID, "agent_id": _AGENT_ID},
            )
            assert create_resp.status_code == 201, create_resp.text
            calls.clear()

            # Plant a LIVE claude pane: registered + is_alive() True. This is the
            # exact state the self-heal short-circuits on ("pane registered and
            # alive -- nothing to heal").
            pane = make_test_terminal_instance("claude", "main", tmp_path, running=True)
            assert await pane.is_alive() is True
            registry._by_conversation.setdefault(_CONV_ID, {})[("claude", "main")] = pane
            assert registry.get(_CONV_ID, "claude", "main") is not None

            # Precondition: the forwarder is dead/absent -- no live task registered.
            assert await _live_forwarder_task(_CONV_ID) is None, (
                "precondition failed: a forwarder task is already registered; "
                "the dead-forwarder state was not established"
            )

            # Drive the reported web journey: send a Chat message. The non-stream
            # path schedules _run_turn_bg -> _run_turn_bg_setup_and_stream ->
            # _ensure_native_terminal_for_turn (the live-pane self-heal seam).
            turn_resp = await client.post(
                f"/v1/sessions/{_CONV_ID}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": _AGENT_ID,
                    "content": [{"type": "input_text", "text": "please reply"}],
                },
            )
            assert turn_resp.status_code == 202, turn_resp.text

            # A correct fix restarts the forwarder from the live-pane branch and
            # registers it; wait for that task to appear (never happens on main).
            deadline = time.monotonic() + _FORWARDER_START_TIMEOUT_S
            forwarder_seen = False
            while time.monotonic() < deadline:
                if await _live_forwarder_task(_CONV_ID) is not None:
                    forwarder_seen = True
                    break
                await asyncio.sleep(_FORWARDER_START_POLL_S)

            # Let a freshly-started forwarder resolve the transcript + fix its
            # cursor, THEN append Claude's reply so it is tailed regardless of the
            # forwarder's start-at-end/offset choice (models the reply arriving).
            await asyncio.sleep(0.75)
            _append_assistant_reply(bridge_dir, transcript_path)

            # Poll the conversation store (the web Chat's source) for the reply.
            marker_deadline = time.monotonic() + _MARKER_TIMEOUT_S
            got_marker = False
            recorded: list[dict[str, Any]] = []
            while time.monotonic() < marker_deadline:
                recorded.extend(_drain_bodies(server))
                if _marker_in_bodies(recorded, _ASSISTANT_MARKER):
                    got_marker = True
                    break
                await asyncio.sleep(_MARKER_POLL_S)

        # Invariant: the terminal side still has the reply (the transcript is the
        # terminal's source) -- the loss is only in the web conversation store.
        assert _ASSISTANT_MARKER in transcript_path.read_text(encoding="utf-8"), (
            "seed invariant: the assistant reply must remain in the transcript "
            "(the terminal's source)"
        )

        # The bug: a live-pane web turn never restarted the dead forwarder, so
        # the assistant reply the user is waiting for is never mirrored into the
        # conversation store the web Chat renders from -- the reported "terminal
        # fine, Chat empty of assistant turns" desync.
        store_summary = [
            b.get("type") for b in recorded if isinstance(b, dict)
        ] or "no conversation-store POSTs received"
        assert got_marker, (
            "the assistant reply never reached the conversation store the web "
            f"Chat renders from: '{_ASSISTANT_MARKER}' is in Claude's transcript "
            "(the terminal view) but was never mirrored via an "
            "external_conversation_item POST (the web Chat view). The live-pane "
            "message turn did not restart the dead transcript forwarder "
            f"(forwarder task restarted={forwarder_seen}); store POST types seen: "
            f"{store_summary}."
        )
    finally:
        with contextlib.suppress(Exception):
            await native_orchestration._cancel_auto_forwarder_task(_CONV_ID)
        native_orchestration._AUTO_FORWARDER_TASKS.pop(_CONV_ID, None)
        with contextlib.suppress(Exception):
            await transport.aclose()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        if bridge_dir is not None:
            shutil.rmtree(bridge_dir, ignore_errors=True)
