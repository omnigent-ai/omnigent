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


# Slack accepts up to 40,000 characters in a message ``text`` and rejects
# anything larger with ``msg_too_long`` (docs.slack.dev chat.postMessage).
SLACK_HARD_LIMIT = 40000


class LimitEnforcingSlackClient(FakeSlackClient):
    """Fake that mimics Slack rejecting oversized ``text`` with msg_too_long."""

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self._enforce_limit(kwargs)
        return await super().chat_postMessage(**kwargs)

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self._enforce_limit(kwargs)
        return await super().chat_update(**kwargs)

    @staticmethod
    def _enforce_limit(kwargs: dict[str, Any]) -> None:
        if len(str(kwargs.get("text") or "")) > SLACK_HARD_LIMIT:
            raise RuntimeError("msg_too_long")


class FlakyUpdateSlackClient(FakeSlackClient):
    """Fake whose streaming ``chat_update`` calls fail before the final one.

    The final delivery re-edits the placeholder to the first chunk; only the
    interim progress updates raise, so we can prove a progress-update failure
    never aborts the turn or clobbers the real answer.
    """

    def __init__(self, fail_first: int = 1) -> None:
        super().__init__()
        self._remaining_failures = fail_first

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise RuntimeError("msg_too_long")
        return await super().chat_update(**kwargs)


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


class StreamingLongAnswerClient(FakeOmnigentClient):
    """Streams a long answer as deltas, then reports it as the final item.

    Mirrors the real session where an orchestrator streams a multi-thousand
    character synthesis: the interim progress updates carry the whole
    accumulated text, which overflows Slack's ``text`` ceiling.
    """

    async def run_turn(self, session_id: str, text: str) -> AsyncIterator[dict[str, Any]]:
        self.turns.append((session_id, text))
        # Two deltas so the accumulated progress text is multi-thousand chars.
        half = "a" * 3000
        yield {"type": "response.output_text.delta", "delta": half}
        yield {"type": "response.output_text.delta", "delta": half}
        yield {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": self.final_text}],
            },
        }
        yield {"type": "response.completed", "response": {"status": "completed"}}


async def test_streaming_progress_update_failure_does_not_clobber_answer(
    tmp_path: Path,
) -> None:
    store = await _store(tmp_path)
    slack = FlakyUpdateSlackClient(fail_first=1)
    omnigent = StreamingLongAnswerClient(final_text="the real answer")
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
    await service.shutdown()

    # A failed progress update must not surface as an error answer.
    assert slack.updates[-1]["text"] == "the real answer"


async def test_long_streamed_answer_never_exceeds_slack_limit(tmp_path: Path) -> None:
    from omnigent_slack.text import SLACK_MESSAGE_CHAR_LIMIT

    # Every delivered chunk stays within Slack's hard ceiling.
    assert SLACK_MESSAGE_CHAR_LIMIT <= SLACK_HARD_LIMIT

    store = await _store(tmp_path)
    slack = LimitEnforcingSlackClient()
    long_answer = "y" * (SLACK_MESSAGE_CHAR_LIMIT * 2 + 100)
    omnigent = StreamingLongAnswerClient(final_text=long_answer)
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
    for _ in range(50):
        if len(slack.posts) >= 3:
            break
        await asyncio.sleep(0.02)
    await service.shutdown()

    # No update or post exceeded the hard limit (else the fake would have
    # raised), every chunk honors the best-practice ceiling, and the full
    # answer was preserved across parts.
    parts = [slack.updates[-1]["text"]]
    parts.extend(post["text"] for post in slack.posts[1:])
    assert all(len(part) <= SLACK_MESSAGE_CHAR_LIMIT for part in parts)
    assert "".join(parts) == long_answer


async def test_turn_error_posts_separate_reply_and_keeps_answer(tmp_path: Path) -> None:
    """An error after content streamed must not erase the delivered answer.

    The failure is reported as its own thread reply so the user keeps both the
    real answer and the failure notice.
    """
    store = await _store(tmp_path)
    slack = FakeSlackClient()

    class ErroringAfterAnswerClient(FakeOmnigentClient):
        async def run_turn(self, session_id: str, text: str) -> AsyncIterator[dict[str, Any]]:
            self.turns.append((session_id, text))
            yield {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": self.final_text}],
                },
            }
            yield {
                "type": "response.failed",
                "response": {"error": {"message": "boom"}},
            }

    omnigent = ErroringAfterAnswerClient(final_text="the real answer")
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
    for _ in range(50):
        if len(slack.posts) >= 2:
            break
        await asyncio.sleep(0.02)
    await service.shutdown()

    # The placeholder holds the real answer, not the error.
    assert slack.updates[-1]["text"] == "the real answer"
    # The failure is a separate reply in the same thread.
    failure_posts = [p for p in slack.posts if "failed" in str(p.get("text", ""))]
    assert len(failure_posts) == 1
    assert "boom" in failure_posts[0]["text"]
    assert failure_posts[0]["thread_ts"] == "100.1"


async def test_turn_error_without_answer_uses_placeholder(tmp_path: Path) -> None:
    """When nothing streamed, the error surfaces in the placeholder itself."""
    store = await _store(tmp_path)
    slack = FakeSlackClient()

    class ErroringNoAnswerClient(FakeOmnigentClient):
        async def run_turn(self, session_id: str, text: str) -> AsyncIterator[dict[str, Any]]:
            self.turns.append((session_id, text))
            yield {
                "type": "response.failed",
                "response": {"error": {"message": "boom"}},
            }

        async def latest_assistant_text(self, session_id: str) -> str | None:
            return None

    omnigent = ErroringNoAnswerClient()
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
    await service.shutdown()

    assert "boom" in slack.updates[-1]["text"]
    # No extra failure reply when there was no answer to preserve.
    assert len(slack.posts) == 1


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
