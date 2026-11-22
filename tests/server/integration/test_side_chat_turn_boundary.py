"""A side chat forked while its source runs copies history up to the running turn's start.

These drive the real server routes and store: web sends, transcript mirrors,
status edges and the side chat fork. Only the runner and the native terminal
are stand-ins, since no agent runs here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

from omnigent.runtime import pending_inputs
from omnigent.server.routes import sessions as sessions_module
from omnigent.server.routes._sessions.helpers import _NativeTerminalEnsureOutcome
from omnigent.server.routes._sessions.orchestration import _relay_runner_stream_once
from omnigent.server.routes.sessions import routes_core
from omnigent.server.schemas import BackgroundTaskInfo
from omnigent.spec.types import SkillSpec
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.tools.builtins.load_skill import format_skill_meta_text
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_CODENAME = "Remember the codename KESTREL"
_NOTED = "Noted"


def _runner(request: httpx.Request) -> httpx.Response:
    """Accept every forwarded turn, and resolve the one skill the tests invoke."""
    if request.url.path.endswith("/skills/resolve"):
        skill = SkillSpec(name="grill-me", description="Stress-test a plan.", content="Probe it.")
        return httpx.Response(200, json={"meta_text": format_skill_meta_text(skill, "the parser")})
    return httpx.Response(202, json={"queued": True})


@contextlib.asynccontextmanager
async def _routed_runner(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> AsyncIterator[None]:
    """Route every session to *handler* as its runner, with a native terminal that is ready."""
    monkeypatch.setattr(
        sessions_module,
        "_ensure_native_terminal_ready",
        AsyncMock(return_value=_NativeTerminalEnsureOutcome(error=None)),
    )
    monkeypatch.setattr(
        sessions_module, "_ensure_runner_session_initialized", AsyncMock(return_value=True)
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runner"
    ) as client:
        monkeypatch.setattr(sessions_module, "_get_runner_client", AsyncMock(return_value=client))
        monkeypatch.setattr(
            "omnigent.server.routes._sessions.orchestration._get_runner_client",
            AsyncMock(return_value=client),
        )
        yield


@pytest_asyncio.fixture
async def runner(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Route every session to a runner and native terminal that take everything."""
    async with _routed_runner(monkeypatch, _runner):
        yield


async def _session(client: httpx.AsyncClient, *, native: bool) -> str:
    """Create a Claude Code terminal session, or an SDK one that has a skill."""
    if native:
        agent = await create_test_agent(client, name="claude-native-ui")
        labels = {"omnigent.ui": "terminal", "omnigent.wrapper": "claude-code-native-ui"}
    else:
        agent = await create_test_agent(
            client,
            name="skill-agent",
            skills=[{"name": "grill-me", "description": "Stress-test a plan.", "content": "x"}],
        )
        labels = {}
    created = await client.post("/v1/sessions", json={"agent_id": agent["id"], "labels": labels})
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


@pytest_asyncio.fixture
async def native_session(client: httpx.AsyncClient, runner: None) -> AsyncIterator[str]:
    """A Claude Code terminal session."""
    session_id = await _session(client, native=True)
    try:
        yield session_id
    finally:
        for pending in pending_inputs.snapshot_for(session_id):
            pending_inputs.resolve(session_id, pending["pending_id"])


async def _post(client: httpx.AsyncClient, session_id: str, event: dict[str, Any]) -> None:
    response = await client.post(f"/v1/sessions/{session_id}/events", json=event)
    assert response.status_code in (200, 202), response.text


async def _send(client: httpx.AsyncClient, session_id: str, text: str) -> None:
    """Send *text* from the web composer."""
    content = [{"type": "input_text", "text": text}]
    data = {"role": "user", "content": content, "stable_id": uuid.uuid4().hex}
    await _post(client, session_id, {"type": "message", "data": data})


async def _mirror(
    client: httpx.AsyncClient,
    session_id: str,
    role: str,
    text: str,
    turn: str,
    *,
    is_meta: bool = False,
) -> None:
    """Persist one transcript message the way the Claude Code forwarder does."""
    kind = "input_text" if role == "user" else "output_text"
    data: dict[str, Any] = {"role": role, "content": [{"type": kind, "text": text}]}
    if role == "assistant":
        data["agent"] = "claude-native-ui"
    if is_meta:
        data["is_meta"] = True
    event = {
        "item_type": "message",
        "item_data": data,
        "response_id": turn,
        "source_id": f"claude-record-{uuid.uuid4().hex}:0:message",
    }
    await _post(client, session_id, {"type": "external_conversation_item", "data": event})


async def _status(client: httpx.AsyncClient, session_id: str, status: str) -> None:
    """Report the session's status the way the Claude Code forwarder does."""
    data: dict[str, Any] = {"status": status}
    if status == "idle":
        data["turn_completed"] = True
    await _post(client, session_id, {"type": "external_session_status", "data": data})


async def _exchange(client: httpx.AsyncClient, session_id: str) -> None:
    """A finished turn: a web request that Claude Code takes up and answers."""
    await _send(client, session_id, _CODENAME)
    await _status(client, session_id, "running")
    await _mirror(client, session_id, "user", _CODENAME, "claude_turn_1")
    await _mirror(client, session_id, "assistant", _NOTED, "claude_turn_1")
    await _status(client, session_id, "idle")


async def _side_chat_history(client: httpx.AsyncClient, session_id: str) -> list[str]:
    """Fork a side chat from *session_id* and return the history it starts with."""
    response = await client.post(f"/v1/sessions/{session_id}/fork", json={"side_chat": True})
    assert response.status_code == 201, response.text
    history = []
    for item in response.json()["items"]:
        data = item["data"]
        if item["type"] == "slash_command":
            history.append(f"/{data['name']} {data['arguments']}")
        elif item["type"] == "message":
            history.extend(part["text"] for part in data["content"])
    return history


@pytest.mark.parametrize("follow_up", ["steered_in", "sent_before_it_ran"])
async def test_side_chat_leaves_out_a_turn_with_a_follow_up(
    client: httpx.AsyncClient, native_session: str, follow_up: str
) -> None:
    """A follow-up steered in, or sent before the turn is seen running, keeps its request out."""
    await _exchange(client, native_session)
    await _send(client, native_session, "Run the long command")
    if follow_up == "sent_before_it_ran":
        await _send(client, native_session, "Also check the parser")
    await _status(client, native_session, "running")
    await _mirror(client, native_session, "user", "Run the long command", "claude_turn_2")
    await _mirror(client, native_session, "assistant", "Running it now", "claude_turn_2")
    if follow_up == "steered_in":
        await _send(client, native_session, "Also check the parser")
    await _mirror(client, native_session, "user", "Also check the parser", "claude_turn_2")
    await _mirror(client, native_session, "assistant", "Checking the parser too", "claude_turn_2")

    assert await _side_chat_history(client, native_session) == [_CODENAME, _NOTED]


@pytest.mark.parametrize("turn_end", ["idle", "waiting_on_background_agents"])
async def test_side_chat_keeps_the_exchange_before_a_task_notification_turn(
    client: httpx.AsyncClient, native_session: str, turn_end: str
) -> None:
    """A turn a background task's notification starts leaves the finished exchange whole."""
    request, reply = "Survey the repo in the background", "Started a background agent"
    await _exchange(client, native_session)
    await _send(client, native_session, request)
    await _status(client, native_session, "running")
    await _mirror(client, native_session, "user", request, "claude_turn_2")
    await _mirror(client, native_session, "assistant", reply, "claude_turn_2")
    if turn_end == "idle":
        await _status(client, native_session, "idle")
    else:
        survey = BackgroundTaskInfo(id="agent_1", type="subagent", status="running")
        sessions_module._publish_status(native_session, "waiting", background_agents=[survey])
        # The turn is over while the background agent works.
        finished = [_CODENAME, _NOTED, request, reply]
        assert await _side_chat_history(client, native_session) == finished

    await _status(client, native_session, "running")
    notice = "<task-notification><status>completed</status></task-notification>"
    await _mirror(client, native_session, "user", notice, "claude_turn_3", is_meta=True)
    await _mirror(client, native_session, "assistant", "The survey is done", "claude_turn_3")

    assert await _side_chat_history(client, native_session) == [_CODENAME, _NOTED, request, reply]


async def test_side_chat_leaves_out_a_request_delivered_while_it_reads_history(
    client: httpx.AsyncClient, native_session: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request Claude Code takes up and answers during the fork's history read stays out."""
    await _exchange(client, native_session)
    await _send(client, native_session, "Run the long command")
    loop = asyncio.get_running_loop()
    list_items = SqlAlchemyConversationStore.list_items
    delivered = False

    async def deliver() -> None:
        await _status(client, native_session, "running")
        await _mirror(client, native_session, "user", "Run the long command", "claude_turn_2")
        await _mirror(client, native_session, "assistant", "Running it now", "claude_turn_2")

    def deliver_then_list(
        store: SqlAlchemyConversationStore, session_id: str, *args: Any, **kwargs: Any
    ) -> Any:
        nonlocal delivered
        lookup = kwargs.get("limit") == routes_core._SIDE_CHAT_REQUEST_LOOKBACK
        if session_id == native_session and lookup and not delivered:
            delivered = True
            asyncio.run_coroutine_threadsafe(deliver(), loop).result(timeout=30)
        return list_items(store, session_id, *args, **kwargs)

    monkeypatch.setattr(SqlAlchemyConversationStore, "list_items", deliver_then_list)

    assert await _side_chat_history(client, native_session) == [_CODENAME, _NOTED]
    assert delivered


async def test_side_chat_leaves_out_a_request_beyond_its_history_lookup(
    client: httpx.AsyncClient, native_session: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long-running turn's request is left out however much work followed it."""
    monkeypatch.setattr(routes_core, "_SIDE_CHAT_REQUEST_LOOKBACK", 2)
    await _exchange(client, native_session)
    await _send(client, native_session, "Run the long command")
    await _status(client, native_session, "running")
    await _mirror(client, native_session, "user", "Run the long command", "claude_turn_2")
    for step in ("Reading the code", "Running the tests", "Fixing a failure"):
        await _mirror(client, native_session, "assistant", step, "claude_turn_2")

    assert await _side_chat_history(client, native_session) == [_CODENAME, _NOTED]


async def _sdk_turn(
    client: httpx.AsyncClient, session_id: str, send: Callable[[], Any], reply: str, turn: str
) -> None:
    await send()
    sessions_module._publish_status(session_id, "running")
    await _mirror(client, session_id, "assistant", reply, turn)


@pytest.mark.parametrize("follow_up", ["steered_in", "sent_before_it_ran"])
@pytest.mark.parametrize("request_kind", ["message", "skill"])
async def test_sdk_side_chat_leaves_out_a_turn_with_a_follow_up(
    client: httpx.AsyncClient, runner: None, request_kind: str, follow_up: str
) -> None:
    """An SDK session's running request stays out whenever its follow-up was sent."""
    session_id = await _session(client, native=False)
    await _sdk_turn(client, session_id, lambda: _send(client, session_id, _CODENAME), _NOTED, "r1")
    sessions_module._publish_status(session_id, "idle")
    if request_kind == "message":
        await _send(client, session_id, "Review the parser")
    else:
        skill = {"kind": "skill", "name": "grill-me", "arguments": "the parser"}
        await _post(client, session_id, {"type": "slash_command", "data": skill})
    if follow_up == "sent_before_it_ran":
        await _send(client, session_id, "Also check the lexer")
    # The runner took the request but its running edge has not reached the server yet.
    assert await _side_chat_history(client, session_id) == [_CODENAME, _NOTED]
    sessions_module._publish_status(session_id, "running")
    await _mirror(client, session_id, "assistant", "Reviewing it", "r2")
    if follow_up == "steered_in":
        await _send(client, session_id, "Also check the lexer")
    await _mirror(client, session_id, "assistant", "Checking the lexer too", "r2")

    assert await _side_chat_history(client, session_id) == [_CODENAME, _NOTED]


class _RunnerStream:
    """One runner event stream: the ready heartbeat, then *events*, then its end."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    def stream(self, method: str, path: str, *, timeout: Any) -> _RunnerStream:
        return self

    async def __aenter__(self) -> _RunnerStream:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def aiter_text(self) -> AsyncIterator[str]:
        yield 'data: {"type": "session.heartbeat"}\n\n'
        for event in self._events:
            yield f"data: {json.dumps(event)}\n\n"
        yield "data: [DONE]\n\n"


class _TurnRunner:
    """A runner that answers a message the way the real one does: it starts a turn or waits."""

    def __init__(self) -> None:
        self.running: set[str] = set()
        self.held: dict[str, list[str]] = {}
        self.message_received = asyncio.Event()
        self._next_reply_gate: asyncio.Event | None = None

    def hold_next_reply(self) -> asyncio.Event:
        """Hold the reply to the next message the runner takes until the returned event is set."""
        self._next_reply_gate = asyncio.Event()
        self.message_received.clear()
        return self._next_reply_gate

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or not request.url.path.endswith("/events"):
            return _runner(request)
        session_id = request.url.path.split("/")[3]
        if session_id in self.running:
            item_id = json.loads(request.content)["persisted_item_id"]
            self.held.setdefault(session_id, []).append(item_id)
            detail = "Message buffered; active turn will process it."
            reply = httpx.Response(202, json={"status": "buffered", "detail": detail})
        else:
            self.running.add(session_id)
            reply = httpx.Response(202, json={"status": "accepted", "detail": "Turn started."})
        gate, self._next_reply_gate = self._next_reply_gate, None
        self.message_received.set()
        if gate is not None:
            await gate.wait()
        return reply

    def steer(self, session_id: str) -> None:
        """Take the oldest held message into the running turn."""
        self.held[session_id].pop(0)

    async def continue_turn(
        self,
        session_id: str,
        store: SqlAlchemyConversationStore,
        *,
        names_items: bool = True,
        take: int | None = None,
    ) -> None:
        """Start a turn with held messages as the turn before it ends, with no idle edge.

        :param names_items: Name the messages on the running edge; older runners do not.
        :param take: How many of the oldest held messages the turn takes up, e.g. one at a
            time while a ``/compact`` waits among them, or none for the compaction turn.
            ``None`` takes them all.
        """
        held = self.held.get(session_id, [])
        taken = held[:] if take is None else held[:take]
        del held[: len(taken)]
        edge: dict[str, Any] = {"type": "session.status", "status": "running"}
        if names_items:
            edge["input_item_ids"] = taken
        stream = _RunnerStream([edge])
        await _relay_runner_stream_once(session_id, stream, store)  # type: ignore[arg-type]

    def end_turn(self, session_id: str) -> None:
        """End the running turn, which took up every message held for it."""
        self.running.discard(session_id)
        self.held.pop(session_id, None)
        sessions_module._publish_status(session_id, "idle")


@pytest_asyncio.fixture
async def turn_runner(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_TurnRunner]:
    """Route every session to a runner that starts turns and holds messages like the real one."""
    turns = _TurnRunner()
    async with _routed_runner(monkeypatch, turns):
        yield turns


@pytest.mark.parametrize("names_items", [True, False], ids=["runner_names_items", "older_runner"])
@pytest.mark.parametrize("held_kind", ["message", "skill"])
async def test_sdk_side_chat_leaves_out_the_messages_a_runner_held_for_its_next_turn(
    client: httpx.AsyncClient,
    turn_runner: _TurnRunner,
    held_kind: str,
    names_items: bool,
    db_uri: str,
) -> None:
    """Messages a runner held while a turn ran start the next turn; a side chat leaves them out."""
    session_id = await _session(client, native=False)
    await _sdk_turn(client, session_id, lambda: _send(client, session_id, _CODENAME), _NOTED, "r1")
    turn_runner.end_turn(session_id)
    await _sdk_turn(
        client, session_id, lambda: _send(client, session_id, "Review the parser"), "On it", "r2"
    )
    # A runner that cannot steer holds both for the turn after this one.
    if held_kind == "message":
        await _send(client, session_id, "Also check the lexer")
    else:
        skill = {"kind": "skill", "name": "grill-me", "arguments": "the parser"}
        await _post(client, session_id, {"type": "slash_command", "data": skill})
    await _send(client, session_id, "And the docs")
    await _mirror(client, session_id, "assistant", "The parser looks fine", "r2")
    store = SqlAlchemyConversationStore(db_uri)
    await turn_runner.continue_turn(session_id, store, names_items=names_items)
    await _mirror(client, session_id, "assistant", "Checking the rest", "r3")
    await _send(client, session_id, "Then summarize")

    history = await _side_chat_history(client, session_id)
    assert history == [_CODENAME, _NOTED, "Review the parser", "On it"]


async def test_sdk_side_chat_cuts_before_a_held_skill_that_follows_a_steered_message(
    client: httpx.AsyncClient, turn_runner: _TurnRunner, db_uri: str
) -> None:
    """A held skill that starts the next turn after a steered message cuts before its command."""
    session_id = await _session(client, native=False)
    await _sdk_turn(client, session_id, lambda: _send(client, session_id, _CODENAME), _NOTED, "r1")
    turn_runner.end_turn(session_id)
    await _sdk_turn(
        client, session_id, lambda: _send(client, session_id, "Review the parser"), "On it", "r2"
    )
    await _send(client, session_id, "Also check the lexer")
    turn_runner.steer(session_id)
    skill = {"kind": "skill", "name": "grill-me", "arguments": "the parser"}
    await _post(client, session_id, {"type": "slash_command", "data": skill})
    await _mirror(client, session_id, "assistant", "Both look fine", "r2")
    await turn_runner.continue_turn(session_id, SqlAlchemyConversationStore(db_uri))
    await _mirror(client, session_id, "assistant", "Grilling it", "r3")

    history = await _side_chat_history(client, session_id)
    assert history == [_CODENAME, _NOTED, "Review the parser", "On it", "Also check the lexer"]


async def test_sdk_side_chat_keeps_a_message_a_runner_steered_into_a_finished_turn(
    client: httpx.AsyncClient, turn_runner: _TurnRunner
) -> None:
    """A held message that the ended turn took up stays in a later turn's side chat."""
    session_id = await _session(client, native=False)
    await _sdk_turn(client, session_id, lambda: _send(client, session_id, _CODENAME), _NOTED, "r1")
    turn_runner.end_turn(session_id)
    # The runner's reply to the request comes back only after its turn has ended.
    reply = turn_runner.hold_next_reply()
    sending = asyncio.create_task(_send(client, session_id, "Review the parser"))
    await asyncio.wait_for(turn_runner.message_received.wait(), timeout=30)
    sessions_module._publish_status(session_id, "running")
    await _mirror(client, session_id, "assistant", "On it", "r2")
    await _send(client, session_id, "Also check the lexer")
    await _mirror(client, session_id, "assistant", "Both look fine", "r2")
    turn_runner.end_turn(session_id)
    reply.set()
    await sending
    finished = ["Review the parser", "On it", "Also check the lexer", "Both look fine"]

    # Also while the next message is persisted but still on its way to the runner.
    reply = turn_runner.hold_next_reply()
    sending = asyncio.create_task(_send(client, session_id, "Now add tests"))
    await asyncio.wait_for(turn_runner.message_received.wait(), timeout=30)
    try:
        assert await _side_chat_history(client, session_id) == [_CODENAME, _NOTED, *finished]
    finally:
        reply.set()
        await sending
    sessions_module._publish_status(session_id, "running")
    await _mirror(client, session_id, "assistant", "Adding", "r3")

    history = await _side_chat_history(client, session_id)
    assert history == [_CODENAME, _NOTED, *finished]


async def test_sdk_side_chat_cuts_before_each_held_message_a_runner_takes_up_alone(
    client: httpx.AsyncClient, turn_runner: _TurnRunner, db_uri: str
) -> None:
    """Held messages the runner takes up one turn at a time each start their own turn."""
    session_id = await _session(client, native=False)
    await _sdk_turn(client, session_id, lambda: _send(client, session_id, _CODENAME), _NOTED, "r1")
    turn_runner.end_turn(session_id)
    await _sdk_turn(
        client, session_id, lambda: _send(client, session_id, "Review the parser"), "On it", "r2"
    )
    await _send(client, session_id, "Also check the lexer")
    skill = {"kind": "skill", "name": "grill-me", "arguments": "the parser"}
    await _post(client, session_id, {"type": "slash_command", "data": skill})
    await _mirror(client, session_id, "assistant", "The parser looks fine", "r2")
    store = SqlAlchemyConversationStore(db_uri)
    # A /compact queued between them makes the runner take each up alone, with no idle edge.
    await turn_runner.continue_turn(session_id, store, take=1)
    await _mirror(client, session_id, "assistant", "The lexer looks fine", "r3")
    finished = [_CODENAME, _NOTED, "Review the parser", "On it", "Also check the lexer"]
    await turn_runner.continue_turn(session_id, store, take=0)
    # The compaction turn answers no request: only the skill still held stays out.
    assert await _side_chat_history(client, session_id) == finished
    await turn_runner.continue_turn(session_id, store, take=1)
    await _mirror(client, session_id, "assistant", "Grilling it", "r4")

    assert await _side_chat_history(client, session_id) == finished


async def test_sdk_side_chat_keeps_the_exchange_before_a_compaction_turn(
    client: httpx.AsyncClient, turn_runner: _TurnRunner, db_uri: str
) -> None:
    """A turn that takes up no stored request, e.g. a buffered /compact, answers none."""
    session_id = await _session(client, native=False)
    await _sdk_turn(client, session_id, lambda: _send(client, session_id, _CODENAME), _NOTED, "r1")
    turn_runner.end_turn(session_id)
    await _sdk_turn(
        client, session_id, lambda: _send(client, session_id, "Review the parser"), "On it", "r2"
    )
    await _mirror(client, session_id, "assistant", "The parser looks fine", "r2")
    store = SqlAlchemyConversationStore(db_uri)
    await turn_runner.continue_turn(session_id, store, take=0)
    finished = [_CODENAME, _NOTED, "Review the parser", "On it", "The parser looks fine"]

    assert await _side_chat_history(client, session_id) == finished
    # A message sent during it waits for a later turn, so it stays out.
    await _send(client, session_id, "Then add tests")
    assert await _side_chat_history(client, session_id) == finished
