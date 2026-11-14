"""Slack agent sessions: the status a thread shows, and Slack's stop button.

With ``OMNIGENT_SLACK_AGENT_SESSIONS`` on, a turn marks its thread's Slack
session ``processing`` with a title and the initiator, ``suspended`` while a
card waits, and ``active`` on every ending. The owner's click on Slack's stop
button cancels the turn without the restart notice, reopens no reply, and
interrupts the server. Off, the bot makes no session calls at all.

Same harness as ``test_restart_abandoned_turn.py``: the real service and
``SQLiteStore``, the recording Slack client from ``fakes``, and an in-file
Omnigent client whose turn plays a script.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, TypeVar

import pytest
from fakes import RecordingSlackClient
from omnigent_slack.agent_sessions import session_title
from omnigent_slack.approvals import Verdict
from omnigent_slack.models import ThreadKey, UserConfig
from omnigent_slack.omnigent import ServerUnreachableError, SessionActivity, SessionInfo
from omnigent_slack.service import (
    _ACK_TEXT,
    _RESTART_ABANDONED_TEXT,
    _STOP_NOT_OWNER_TEXT,
    _STOPPED_TEXT,
    SlackOmnigentService,
)
from omnigent_slack.store import SQLiteStore

# In a script: wait until the bot has posted the card's verdict to the server.
AWAIT_VERDICT = "await-verdict"

_THREAD = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")


def _delta(text: str) -> dict[str, Any]:
    return {"type": "response.output_text.delta", "delta": text}


_APPROVAL_REQUEST = {
    "type": "response.elicitation_request",
    "elicitation_id": "elicit_1",
    "method": "elicitation/create",
    "params": {
        "mode": "form",
        "message": "Agent wants to call Edit(). Approve?",
        "policy_name": "require_approval",
        "content_preview": '{"name": "Edit"}',
    },
}

_APPROVAL_SCRIPT: list[Any] = [
    _delta("Let me edit that. "),
    _APPROVAL_REQUEST,
    AWAIT_VERDICT,
    {"type": "response.elicitation_resolved", "elicitation_id": "elicit_1"},
    _delta("Done."),
]


class ScriptedOmnigentClient:
    """An Omnigent client whose turn plays ``script``.

    A script holds stream events, gates (``asyncio.Event``) that pause the turn
    until the test sets them, and ``AWAIT_VERDICT``. A turn cancelled while it
    waits never resumes, which is what a stopped turn must look like.
    """

    def __init__(self, script: list[Any], *, create_error: Exception | None = None) -> None:
        self.script = script
        self.create_error = create_error
        self.turn_underway = asyncio.Event()
        self.verdict_posted = asyncio.Event()
        self.resolved: list[tuple[str, str, bool]] = []
        self.interrupted: list[str] = []

    async def get_session_activity(self, session_id: str) -> SessionActivity:
        return SessionActivity(status="idle", pending_elicitation=False)

    async def get_session_info(self, session_id: str) -> SessionInfo:
        return SessionInfo(harness="claude-native", agent_name="debby")

    async def create_session(
        self, agent_id: str, title: str, *, host_type: str = "external"
    ) -> str:
        if self.create_error is not None:
            raise self.create_error
        return "conv_1"

    async def launch_runner(
        self, session_id: str, *, workspace: str, host_id: str | None = None
    ) -> str:
        return "runner_1"

    async def latest_assistant_message(self, session_id: str) -> tuple[str, str] | None:
        return None

    async def resolve_elicitation(
        self,
        session_id: str,
        elicitation_id: str,
        *,
        accepted: bool,
        content: dict[str, Any] | None = None,
    ) -> None:
        self.resolved.append((session_id, elicitation_id, accepted))
        self.verdict_posted.set()

    async def interrupt(self, session_id: str) -> None:
        self.interrupted.append(session_id)

    async def run_turn(
        self,
        session_id: str,
        text: str,
        *,
        workspace: str | None = None,
        host_id: str | None = None,
        host_type: str = "external",
    ) -> AsyncIterator[dict[str, Any]]:
        self.turn_underway.set()
        for step in self.script:
            if step is AWAIT_VERDICT:
                await self.verdict_posted.wait()
            elif isinstance(step, asyncio.Event):
                await step.wait()
            else:
                yield step


class _Pool:
    """Returns the same client for every server URL (see test_service.FakePool)."""

    def __init__(self, client: ScriptedOmnigentClient) -> None:
        self._client = client

    async def get(self, server_url: str, user_id: str = "") -> ScriptedOmnigentClient:
        return self._client


class _Setup:
    """Inert setup flow: the user in these scenarios is already configured."""

    async def prompt_unconfigured(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("setup prompt for a configured user")

    async def prompt_relogin(self, *args: Any, **kwargs: Any) -> bool:
        raise AssertionError("re-login prompt outside an auth scenario")


async def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()
    await store.upsert_user_config(
        "T1",
        "U1",
        UserConfig(agent_id="ag_1", agent_name="debby", workspace="/tmp/workspace", host_id="h1"),
    )
    return store


def _service(
    store: SQLiteStore, omnigent: ScriptedOmnigentClient, *, agent_sessions: bool = True
) -> SlackOmnigentService:
    return SlackOmnigentService(
        store=store,
        pool=_Pool(omnigent),  # type: ignore[arg-type]
        setup=_Setup(),  # type: ignore[arg-type]
        server_url="http://omnigent.test",
        agent_sessions=agent_sessions,
    )


async def _mention(service: SlackOmnigentService, slack: RecordingSlackClient, text: str) -> None:
    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": f"<@B1> {text}"},
        client=slack,
        context={"bot_user_id": "B1"},
    )


async def _stop(service: SlackOmnigentService, slack: RecordingSlackClient, user: str) -> None:
    """Deliver Slack's ``agent_session_stopped`` for the thread, clicked by ``user``."""
    await service.handle_agent_session_stopped(
        body={"team_id": "T1", "event_id": f"EvStop-{user}"},
        event={
            "type": "agent_session_stopped",
            "channel": "C1",
            "thread_ts": "100.1",
            "user": user,
            "streaming_message_ts": ["stream-1"],
        },
        client=slack,
    )


def _statuses(slack: RecordingSlackClient) -> list[str]:
    return [body["status"] for method, body in slack.api_calls if method.startswith("agents.")]


_T = TypeVar("_T")


async def _poll_until(condition: Callable[[], _T], what: str) -> _T:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10.0
    while not (result := condition()):
        if loop.time() >= deadline:
            raise AssertionError(f"Timed out waiting for {what}")
        await asyncio.sleep(0.02)
    return result


async def _wait_for_turns(service: SlackOmnigentService) -> None:
    tasks = list(service._turn_tasks)
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10.0)


def test_session_title_is_plain_and_short() -> None:
    assert session_title(
        "ask <@U2> to review <https://github.com/o/r/pull/1|PR 1>  in <#C3|ops>"
    ) == ("ask to review PR 1 in ops")
    long_title = session_title("word " * 40)
    assert len(long_title) == 80
    assert long_title.endswith("…")


async def test_turn_marks_the_session_processing_then_active(tmp_path: Path) -> None:
    slack = RecordingSlackClient()
    omnigent = ScriptedOmnigentClient([_delta("Here is the build summary.")])
    service = _service(await _store(tmp_path), omnigent)

    await _mention(service, slack, "summarize   the <https://ci.example/1|build>")
    await _wait_for_turns(service)

    assert _statuses(slack) == ["processing", "active"]
    method, first = slack.api_calls[0]
    assert method == "agents.sessions.setStatus"
    assert first == {
        "channel_id": "C1",
        "thread_ts": "100.1",
        "status": "processing",
        "title": "summarize the build",
        "initiator_user_id": "U1",
    }
    assert slack.streamed_text == "Here is the build summary."


async def test_failed_turn_still_leaves_the_session_active(tmp_path: Path) -> None:
    slack = RecordingSlackClient()
    omnigent = ScriptedOmnigentClient([], create_error=ServerUnreachableError("down"))
    service = _service(await _store(tmp_path), omnigent)

    await _mention(service, slack, "summarize the build")
    await _wait_for_turns(service)

    assert _statuses(slack) == ["processing", "active"]
    assert any("couldn't reach your Omnigent server" in str(p.get("text")) for p in slack.posts)


async def test_card_suspends_the_session_until_it_is_answered(tmp_path: Path) -> None:
    slack = RecordingSlackClient()
    omnigent = ScriptedOmnigentClient(_APPROVAL_SCRIPT)
    service = _service(await _store(tmp_path), omnigent)

    await _mention(service, slack, "edit the config")
    await _poll_until(lambda: _statuses(slack)[-1:] == ["suspended"], "the session to suspend")
    assert any(p.get("blocks") for p in slack.posts), "the card is up while suspended"
    # The seal before the card keeps the session working instead of Slack's
    # default ``active``.
    assert slack.streams[0].stop_kwargs == {"session_status": "processing"}

    await service.handle_elicitation_action(
        session_id="conv_1", elicitation_id="elicit_1", verdict=Verdict(accepted=True)
    )
    await _wait_for_turns(service)

    assert omnigent.resolved == [("conv_1", "elicit_1", True)]
    assert _statuses(slack) == ["processing", "suspended", "processing", "active"]
    # The final stop leaves Slack's default; the turn's end then sets ``active``.
    assert slack.streams[-1].stop_kwargs == {}
    assert slack.streams[-1].text == "Done."


async def test_owner_stop_cancels_the_turn_without_restart_notice_or_reopen(
    tmp_path: Path,
) -> None:
    slack = RecordingSlackClient()
    release = asyncio.Event()
    omnigent = ScriptedOmnigentClient([_delta("Working through it"), release, _delta(" more")])
    service = _service(await _store(tmp_path), omnigent)

    await _mention(service, slack, "summarize the build")
    await _poll_until(lambda: slack.streams, "the reply to start streaming")
    # Slack stops the streaming reply itself, then sends the event.
    slack.stream.stopped_by_slack = True
    await _stop(service, slack, "U1")
    await _wait_for_turns(service)

    assert omnigent.interrupted == ["conv_1"]
    assert len(slack.streams) == 1
    texts = [p.get("text") for p in slack.posts]
    assert _STOPPED_TEXT in texts
    assert _RESTART_ABANDONED_TEXT not in texts
    assert _ACK_TEXT not in texts
    assert _statuses(slack) == ["processing", "active"]

    # Were the turn still reading, this delta would reopen the reply.
    release.set()
    await asyncio.sleep(0.05)
    assert not slack.streams[1:], "the stopped turn reopened its reply"


async def test_stop_closes_a_reply_reopened_before_the_stop_arrived(tmp_path: Path) -> None:
    slack = RecordingSlackClient()
    first_gap, second_gap = asyncio.Event(), asyncio.Event()
    omnigent = ScriptedOmnigentClient(
        [_delta("Working through it"), first_gap, _delta(" more"), second_gap]
    )
    service = _service(await _store(tmp_path), omnigent)

    await _mention(service, slack, "summarize the build")
    await _poll_until(lambda: slack.streams, "the reply to start streaming")
    slack.stream.stopped_by_slack = True
    # The next delta lands before Slack's event and reopens the reply.
    first_gap.set()
    await _poll_until(lambda: len(slack.streams) == 2, "the reply to reopen")
    await _stop(service, slack, "U1")
    await _wait_for_turns(service)

    assert len(slack.streams) == 2
    assert slack.streams[1].stopped, "the reopened segment must not be left streaming"
    assert omnigent.interrupted == ["conv_1"]


async def test_only_the_owner_can_stop_the_turn(tmp_path: Path) -> None:
    slack = RecordingSlackClient()
    release = asyncio.Event()
    omnigent = ScriptedOmnigentClient([_delta("Working through it."), release, _delta(" Done.")])
    service = _service(await _store(tmp_path), omnigent)

    await _mention(service, slack, "summarize the build")
    await _poll_until(lambda: slack.streams, "the reply to start streaming")
    slack.stream.stopped_by_slack = True
    await _stop(service, slack, "U2")
    release.set()
    await _wait_for_turns(service)

    assert omnigent.interrupted == []
    assert [(e["user"], e["text"]) for e in slack.ephemerals] == [("U2", _STOP_NOT_OWNER_TEXT)]
    # The turn carries on into a fresh reply.
    assert slack.streams[-1].text == " Done."
    assert _STOPPED_TEXT not in [p.get("text") for p in slack.posts]


async def test_stop_with_no_running_turn_interrupts_and_marks_the_session_active(
    tmp_path: Path,
) -> None:
    # After a restart the bot runs no turn for the thread, but Slack may still
    # show it working while the server's turn goes on.
    store = await _store(tmp_path)
    await store.upsert_session(_THREAD, "conv_1", "Slack thread", owner_user_id="U1")
    slack = RecordingSlackClient()
    omnigent = ScriptedOmnigentClient([])
    service = _service(store, omnigent)

    await _stop(service, slack, "U1")

    assert omnigent.interrupted == ["conv_1"]
    assert _statuses(slack) == ["active"]


async def test_sessions_off_makes_no_session_calls(tmp_path: Path) -> None:
    slack = RecordingSlackClient()
    omnigent = ScriptedOmnigentClient(_APPROVAL_SCRIPT)
    service = _service(await _store(tmp_path), omnigent, agent_sessions=False)

    await _mention(service, slack, "edit the config")
    await _poll_until(lambda: any(p.get("blocks") for p in slack.posts), "the card")
    await service.handle_elicitation_action(
        session_id="conv_1", elicitation_id="elicit_1", verdict=Verdict(accepted=True)
    )
    await _wait_for_turns(service)

    assert slack.api_calls == []
    assert [stream.stop_kwargs for stream in slack.streams] == [{}, {}]
    assert slack.streams[-1].text == "Done."


async def test_feature_disabled_never_fails_a_turn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A workspace without agent sessions answers every call with this error.
    slack = RecordingSlackClient()
    slack.api_call_error = "feature_disabled"
    omnigent = ScriptedOmnigentClient(_APPROVAL_SCRIPT)
    service = _service(await _store(tmp_path), omnigent)

    with caplog.at_level(logging.WARNING, logger="omnigent_slack.service"):
        await _mention(service, slack, "edit the config")
        await _poll_until(lambda: any(p.get("blocks") for p in slack.posts), "the card")
        await service.handle_elicitation_action(
            session_id="conv_1", elicitation_id="elicit_1", verdict=Verdict(accepted=True)
        )
        await _wait_for_turns(service)

    assert _statuses(slack) == ["processing", "suspended", "processing", "active"]
    assert slack.streams[-1].text == "Done."
    assert "error=feature_disabled" in caplog.text
