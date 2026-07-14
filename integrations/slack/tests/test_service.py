import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from omnigent_slack.models import ThreadKey
from omnigent_slack.service import SlackOmnigentService
from omnigent_slack.store import SQLiteStore


class FakeSlackClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posts.append(kwargs)
        return {"ok": True, "ts": f"bot-{len(self.posts)}"}

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self.updates.append(kwargs)
        return {"ok": True}


class FakeOmnigentClient:
    def __init__(self, final_text: str = "hello final") -> None:
        self.created: list[tuple[str, str]] = []
        self.bound: list[str] = []
        self.turns: list[tuple[str, str]] = []
        self.next_session_id = "conv_1"
        self.final_text = final_text

    async def create_session(self, agent_id: str, title: str) -> str:
        self.created.append((agent_id, title))
        return self.next_session_id

    async def bind_random_runner(self, session_id: str) -> str:
        self.bound.append(session_id)
        return "runner_1"

    async def run_turn(self, session_id: str, text: str) -> AsyncIterator[dict[str, Any]]:
        self.turns.append((session_id, text))
        yield {"type": "response.output_text.delta", "delta": "hel"}
        yield {"type": "response.output_text.delta", "delta": "lo"}
        yield {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": self.final_text}],
            },
        }
        yield {"type": "response.completed", "response": {"status": "completed"}}

    async def latest_assistant_text(self, session_id: str) -> str | None:
        return None


async def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()
    return store


async def _wait_for_updates(client: FakeSlackClient, count: int) -> None:
    for _ in range(50):
        if len(client.updates) >= count:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"Timed out waiting for {count} updates")


async def test_app_mention_creates_session_and_posts_response(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    service = SlackOmnigentService(
        store=store,
        omnigent=omnigent,  # type: ignore[arg-type]
        omnigent_agent_id="ag_1",
        update_interval_seconds=0,
    )

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> hello"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_updates(slack, 2)
    await service.shutdown()

    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    assert await store.get_session_id(key) == "conv_1"
    assert omnigent.created[0][0] == "ag_1"
    assert omnigent.bound == ["conv_1"]
    assert omnigent.turns == [("conv_1", "hello")]
    assert slack.posts[0]["thread_ts"] == "100.1"
    assert slack.updates[-1]["text"] == "hello final"


async def test_long_answer_is_split_across_thread_replies(tmp_path: Path) -> None:
    from omnigent_slack.text import SLACK_MESSAGE_CHAR_LIMIT

    store = await _store(tmp_path)
    slack = FakeSlackClient()
    long_answer = "x" * (SLACK_MESSAGE_CHAR_LIMIT * 2 + 100)
    omnigent = FakeOmnigentClient(final_text=long_answer)
    service = SlackOmnigentService(
        store=store,
        omnigent=omnigent,  # type: ignore[arg-type]
        omnigent_agent_id="ag_1",
        update_interval_seconds=0,
    )

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> hello"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_updates(slack, 1)
    # Placeholder update + two overflow replies = original placeholder post + 2.
    for _ in range(50):
        if len(slack.posts) >= 3:
            break
        await asyncio.sleep(0.02)
    await service.shutdown()

    # Every message stays within Slack's limit and the full answer is preserved.
    parts = [slack.updates[-1]["text"]]
    parts.extend(post["text"] for post in slack.posts[1:])
    assert all(len(part) <= SLACK_MESSAGE_CHAR_LIMIT for part in parts)
    assert "".join(parts) == long_answer
    # Overflow replies land in the same thread.
    assert all(post["thread_ts"] == "100.1" for post in slack.posts[1:])


async def test_empty_app_mention_prompts_without_creating_session(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    service = SlackOmnigentService(
        store=store,
        omnigent=omnigent,  # type: ignore[arg-type]
        omnigent_agent_id="ag_1",
    )

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1>"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await service.shutdown()

    assert omnigent.created == []
    assert omnigent.bound == []
    assert "Send a message" in slack.posts[0]["text"]


async def test_thread_reply_reuses_existing_session(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    await store.upsert_session(key, "conv_existing", "title")
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    service = SlackOmnigentService(
        store=store,
        omnigent=omnigent,  # type: ignore[arg-type]
        omnigent_agent_id="ag_1",
        update_interval_seconds=0,
    )

    await service.handle_message(
        body={"team_id": "T1", "event_id": "Ev2"},
        event={
            "channel": "C1",
            "thread_ts": "100.1",
            "ts": "101.1",
            "user": "U1",
            "text": "next",
        },
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_updates(slack, 2)
    await service.shutdown()

    assert omnigent.created == []
    assert omnigent.bound == []
    assert omnigent.turns == [("conv_existing", "next")]
    assert slack.updates[-1]["text"] == "hello final"


async def test_duplicate_event_is_ignored(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    service = SlackOmnigentService(
        store=store,
        omnigent=omnigent,  # type: ignore[arg-type]
        omnigent_agent_id="ag_1",
        update_interval_seconds=0,
    )
    body = {"team_id": "T1", "event_id": "Ev1"}
    event = {"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> hello"}

    await service.handle_app_mention(
        body=body,
        event=event,
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await service.handle_app_mention(
        body=body,
        event=event,
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_updates(slack, 2)
    await service.shutdown()

    assert len(omnigent.turns) == 1


async def test_generic_message_with_bot_mention_is_ignored(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    await store.upsert_session(key, "conv_existing", "title")
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    service = SlackOmnigentService(
        store=store,
        omnigent=omnigent,  # type: ignore[arg-type]
        omnigent_agent_id="ag_1",
        update_interval_seconds=0,
    )

    await service.handle_message(
        body={"team_id": "T1", "event_id": "Ev2"},
        event={
            "channel": "C1",
            "thread_ts": "100.1",
            "ts": "101.1",
            "user": "U1",
            "text": "<@B1> next",
        },
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await service.shutdown()

    assert omnigent.turns == []
    assert slack.posts == []
