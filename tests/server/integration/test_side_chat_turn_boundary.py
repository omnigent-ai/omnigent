"""A side chat forked while its source runs copies history up to the running turn's start.

These drive the real server routes and store: web sends, transcript mirrors,
status edges and the side chat fork. Only the runner and the native terminal
are stand-ins, since no agent runs here.
"""

from __future__ import annotations

import asyncio
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


@pytest_asyncio.fixture
async def runner(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Route every session to a runner and native terminal that take everything."""
    monkeypatch.setattr(
        sessions_module,
        "_ensure_native_terminal_ready",
        AsyncMock(return_value=_NativeTerminalEnsureOutcome(error=None)),
    )
    monkeypatch.setattr(
        sessions_module, "_ensure_runner_session_initialized", AsyncMock(return_value=True)
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_runner), base_url="http://runner"
    ) as client:
        monkeypatch.setattr(sessions_module, "_get_runner_client", AsyncMock(return_value=client))
        monkeypatch.setattr(
            "omnigent.server.routes._sessions.orchestration._get_runner_client",
            AsyncMock(return_value=client),
        )
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


async def test_side_chat_leaves_out_the_turn_a_follow_up_was_steered_into(
    client: httpx.AsyncClient, native_session: str
) -> None:
    """A message sent into the running turn does not expose that turn's request."""
    await _exchange(client, native_session)
    await _send(client, native_session, "Run the long command")
    await _status(client, native_session, "running")
    await _mirror(client, native_session, "user", "Run the long command", "claude_turn_2")
    await _mirror(client, native_session, "assistant", "Running it now", "claude_turn_2")
    await _send(client, native_session, "Also check the parser")
    await _mirror(client, native_session, "user", "Also check the parser", "claude_turn_2")
    await _mirror(client, native_session, "assistant", "Checking the parser too", "claude_turn_2")

    assert await _side_chat_history(client, native_session) == [_CODENAME, _NOTED]


async def test_side_chat_leaves_out_a_turn_whose_follow_up_was_sent_before_it_ran(
    client: httpx.AsyncClient, native_session: str
) -> None:
    """A follow-up sent before the turn is seen running does not become the turn's start."""
    await _exchange(client, native_session)
    await _send(client, native_session, "Run the long command")
    await _send(client, native_session, "Also check the parser")
    await _status(client, native_session, "running")
    for role, text in (
        ("user", "Run the long command"),
        ("assistant", "Running it now"),
        ("user", "Also check the parser"),
        ("assistant", "Checking the parser too"),
    ):
        await _mirror(client, native_session, role, text, "claude_turn_2")

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


@pytest.mark.parametrize("request_kind", ["message", "skill"])
async def test_sdk_side_chat_leaves_out_the_turn_a_follow_up_was_steered_into(
    client: httpx.AsyncClient, runner: None, request_kind: str
) -> None:
    """An SDK session's running request stays out once a follow-up is steered in."""
    session_id = await _session(client, native=False)
    await _sdk_turn(client, session_id, lambda: _send(client, session_id, _CODENAME), _NOTED, "r1")
    sessions_module._publish_status(session_id, "idle")

    async def send_request() -> None:
        if request_kind == "message":
            await _send(client, session_id, "Review the parser")
        else:
            skill = {"kind": "skill", "name": "grill-me", "arguments": "the parser"}
            await _post(client, session_id, {"type": "slash_command", "data": skill})

    await _sdk_turn(client, session_id, send_request, "Reviewing it", "r2")
    await _send(client, session_id, "Also check the lexer")
    await _mirror(client, session_id, "assistant", "Checking the lexer too", "r2")

    assert await _side_chat_history(client, session_id) == [_CODENAME, _NOTED]


async def test_sdk_side_chat_leaves_out_a_turn_whose_follow_up_was_sent_before_it_ran(
    client: httpx.AsyncClient, runner: None
) -> None:
    """An SDK follow-up sent before the runner reports the turn running is not its start."""
    session_id = await _session(client, native=False)
    await _sdk_turn(client, session_id, lambda: _send(client, session_id, _CODENAME), _NOTED, "r1")
    sessions_module._publish_status(session_id, "idle")
    await _send(client, session_id, "Review the parser")
    await _send(client, session_id, "Also check the lexer")
    sessions_module._publish_status(session_id, "running")
    await _mirror(client, session_id, "assistant", "Reviewing both", "r2")

    assert await _side_chat_history(client, session_id) == [_CODENAME, _NOTED]


class _TurnRunner:
    """A runner that answers a message the way the real one does: it starts a turn or waits."""

    def __init__(self) -> None:
        self.running: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or not request.url.path.endswith("/events"):
            return _runner(request)
        session_id = request.url.path.split("/")[3]
        if session_id in self.running:
            detail = "Message buffered; active turn will process it."
            return httpx.Response(202, json={"status": "buffered", "detail": detail})
        self.running.add(session_id)
        return httpx.Response(202, json={"status": "accepted", "detail": "Turn started."})

    def start_turn(self, session_id: str) -> None:
        """Start the turn the runner owes the messages it held."""
        self.running.add(session_id)
        sessions_module._publish_status(session_id, "running")

    def end_turn(self, session_id: str) -> None:
        self.running.discard(session_id)
        sessions_module._publish_status(session_id, "idle")


@pytest_asyncio.fixture
async def turn_runner(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_TurnRunner]:
    """Route every session to a runner that starts turns and holds messages like the real one."""
    turns = _TurnRunner()
    monkeypatch.setattr(
        sessions_module, "_ensure_runner_session_initialized", AsyncMock(return_value=True)
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(turns), base_url="http://runner"
    ) as client:
        monkeypatch.setattr(sessions_module, "_get_runner_client", AsyncMock(return_value=client))
        monkeypatch.setattr(
            "omnigent.server.routes._sessions.orchestration._get_runner_client",
            AsyncMock(return_value=client),
        )
        yield turns


@pytest.mark.parametrize("held_kind", ["message", "skill"])
async def test_sdk_side_chat_leaves_out_the_messages_a_runner_held_for_its_next_turn(
    client: httpx.AsyncClient, turn_runner: _TurnRunner, held_kind: str
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
    turn_runner.end_turn(session_id)
    turn_runner.start_turn(session_id)
    await _mirror(client, session_id, "assistant", "Checking the rest", "r3")
    await _send(client, session_id, "Then summarize")

    history = await _side_chat_history(client, session_id)
    assert history == [_CODENAME, _NOTED, "Review the parser", "On it"]


async def test_sdk_side_chat_keeps_a_message_a_runner_steered_into_a_finished_turn(
    client: httpx.AsyncClient, turn_runner: _TurnRunner
) -> None:
    """A held message that the ended turn took up stays in a later turn's side chat."""
    session_id = await _session(client, native=False)
    await _sdk_turn(client, session_id, lambda: _send(client, session_id, _CODENAME), _NOTED, "r1")
    turn_runner.end_turn(session_id)
    await _sdk_turn(
        client, session_id, lambda: _send(client, session_id, "Review the parser"), "On it", "r2"
    )
    await _send(client, session_id, "Also check the lexer")
    await _mirror(client, session_id, "assistant", "Both look fine", "r2")
    turn_runner.end_turn(session_id)
    await _sdk_turn(
        client, session_id, lambda: _send(client, session_id, "Now add tests"), "Adding", "r3"
    )

    history = await _side_chat_history(client, session_id)
    finished = ["Review the parser", "On it", "Also check the lexer", "Both look fine"]
    assert history == [_CODENAME, _NOTED, *finished]
