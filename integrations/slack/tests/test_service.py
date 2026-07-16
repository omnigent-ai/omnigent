import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from omnigent_slack.models import ThreadKey, UserConfig
from omnigent_slack.omnigent import (
    AuthRequiredError,
    HostUnavailableError,
    OmnigentError,
    ServerUnreachableError,
)
from omnigent_slack.service import SlackOmnigentService
from omnigent_slack.store import SQLiteStore


class FakeStream:
    """Records a chat_stream lifecycle: appended deltas and the final stop text.

    Mirrors the SDK's in-memory buffering: ``append`` accumulates text and only
    "flushes" to Slack (returning a response) once the buffer reaches
    ``buffer_size``; until then it returns None, exactly like the real client.

    Set ``close_after`` to simulate Slack finalizing the message mid-turn: once
    that many deltas have been appended, further append/stop calls raise the same
    ``message_not_in_streaming_state`` error the real SDK surfaces. A fresh stream
    opened after that keeps streaming normally.
    """

    def __init__(
        self,
        client: "FakeSlackClient",
        start_kwargs: dict[str, Any],
        close_after: int | None = None,
        buffer_size: int = 256,
    ) -> None:
        self._client = client
        self.start_kwargs = start_kwargs
        self.appended: list[str] = []
        self.stopped = False
        self.stop_text: str | None = None
        self._close_after = close_after
        self.closed = False
        # Whether the placeholder ack was still live the moment this stream first
        # put content on screen (a mid-stream flush, or the finalizing stop for a
        # short answer that never filled the buffer).
        self.ack_live_when_visible: bool | None = None
        self._buffer_size = buffer_size
        self._pending = 0

    def _record_ack_state(self) -> None:
        if self.ack_live_when_visible is None:
            self.ack_live_when_visible = any(
                ack["ts"] not in self._client.deleted_ts for ack in self._client.acks
            )

    def _raise_closed(self) -> None:
        raise SlackApiError(
            "stream closed",
            AsyncSlackResponse(  # type: ignore[arg-type]
                client=None,
                http_verb="POST",
                api_url="https://slack.com/api/chat.appendStream",
                req_args={},
                data={"ok": False, "error": "message_not_in_streaming_state"},
                headers={},
                status_code=200,
            ),
        )

    async def append(self, *, markdown_text: str) -> dict[str, Any] | None:
        if self.closed:
            self._raise_closed()
        self.appended.append(markdown_text)
        if self._close_after is not None and len(self.appended) >= self._close_after:
            self.closed = True
        # Buffer until the SDK's threshold, then "flush" to Slack.
        self._pending += len(markdown_text)
        if self._pending < self._buffer_size:
            return None
        self._pending = 0
        self._record_ack_state()
        return {"ok": True}

    async def stop(self, *, markdown_text: str | None = None) -> dict[str, Any]:
        if self.closed:
            self._raise_closed()
        # stop() flushes via chat.startStream, so this is when a short buffered
        # answer first becomes visible.
        self._record_ack_state()
        self.stopped = True
        self.stop_text = markdown_text
        return {"ok": True}

    @property
    def text(self) -> str:
        """The full delivered message: streamed deltas plus any stop tail."""
        return "".join(self.appended) + (self.stop_text or "")


class FakeSlackClient:
    def __init__(self) -> None:
        # Live (not-yet-deleted) posts. The immediate "Working on it…" ack is
        # posted then deleted, so it lands here transiently and is removed by
        # chat_delete — leaving posts to reflect only durable replies.
        self.posts: list[dict[str, Any]] = []
        self.acks: list[dict[str, Any]] = []
        self.deleted_ts: list[str] = []
        self.streams: list[FakeStream] = []
        self._next_ts = 0
        # When set, every stream this client opens auto-closes after this many
        # appended deltas — simulating Slack finalizing the message mid-turn.
        self.stream_close_after: int | None = None

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self._next_ts += 1
        ts = f"bot-{self._next_ts}"
        entry = {**kwargs, "ts": ts}
        self.posts.append(entry)
        if kwargs.get("text") == "_Working on it…_":
            self.acks.append(entry)
        return {"ok": True, "ts": ts}

    async def chat_delete(self, **kwargs: Any) -> dict[str, Any]:
        ts = kwargs.get("ts")
        self.deleted_ts.append(str(ts))
        self.posts = [p for p in self.posts if p.get("ts") != ts]
        return {"ok": True}

    async def chat_stream(self, **kwargs: Any) -> FakeStream:
        # Only the first stream auto-closes (Slack finalizes the idle message);
        # the continuation the bot opens streams fresh, mirroring reality.
        close_after = self.stream_close_after if not self.streams else None
        stream = FakeStream(self, kwargs, close_after=close_after)
        self.streams.append(stream)
        return stream

    @property
    def stream(self) -> FakeStream:
        """The most recent stream (a turn opens one, or more if Slack closes it)."""
        return self.streams[-1]

    @property
    def streamed_text(self) -> str:
        """Concatenation of every stream's delivered text, across reopenings."""
        return "".join(s.text for s in self.streams)


class FakeOmnigentClient:
    def __init__(self, final_text: str = "hello final") -> None:
        self.created: list[tuple[str, str]] = []
        self.bound: list[str] = []
        self.launched: list[tuple[str, str, str | None]] = []
        self.turns: list[tuple[str, str]] = []
        self.next_session_id = "conv_1"
        self.final_text = final_text

    async def create_session(self, agent_id: str, title: str) -> str:
        self.created.append((agent_id, title))
        return self.next_session_id

    async def launch_runner(
        self, session_id: str, *, workspace: str, host_id: str | None = None
    ) -> str:
        self.bound.append(session_id)
        self.launched.append((session_id, workspace, host_id))
        return "runner_1"

    async def run_turn(
        self,
        session_id: str,
        text: str,
        *,
        workspace: str | None = None,
        host_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
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


class FakePool:
    """Returns the same FakeOmnigentClient for every server URL, recording URLs."""

    def __init__(self, client: FakeOmnigentClient) -> None:
        self._client = client
        self.requested: list[str] = []

    async def get(self, server_url: str, user_id: str = "") -> FakeOmnigentClient:
        self.requested.append(server_url)
        return self._client


class FakeSetup:
    """Records unconfigured-user prompts instead of opening real DMs/modals."""

    def __init__(self) -> None:
        self.prompted: list[dict[str, Any]] = []

    async def prompt_unconfigured(
        self,
        client: Any,
        user_id: str,
        *,
        channel: str,
        thread_ts: str | None,
        in_channel: bool,
    ) -> None:
        self.prompted.append(
            {
                "user_id": user_id,
                "channel": channel,
                "thread_ts": thread_ts,
                "in_channel": in_channel,
            }
        )


async def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()
    return store


def _service(
    store: SQLiteStore,
    omnigent: FakeOmnigentClient,
    *,
    setup: FakeSetup | None = None,
) -> tuple[SlackOmnigentService, FakePool, FakeSetup]:
    pool = FakePool(omnigent)
    setup = setup or FakeSetup()
    service = SlackOmnigentService(
        store=store,
        pool=pool,  # type: ignore[arg-type]
        setup=setup,  # type: ignore[arg-type]
        server_url="http://omnigent.test",
    )
    return service, pool, setup


async def _configure_user(
    store: SQLiteStore,
    team_id: str,
    user_id: str,
    *,
    agent_id: str = "ag_1",
    workspace: str = "/tmp/workspace",
    host_id: str | None = None,
) -> None:
    await store.upsert_user_config(
        team_id,
        user_id,
        UserConfig(
            agent_id=agent_id,
            agent_name="Helper",
            workspace=workspace,
            host_id=host_id,
        ),
    )


async def _wait_for_stream_stop(client: FakeSlackClient) -> FakeStream:
    """Wait until a turn has opened a stream and finalized it."""
    for _ in range(50):
        if client.streams and client.stream.stopped:
            return client.stream
        await asyncio.sleep(0.02)
    raise AssertionError("Timed out waiting for a stream to stop")


async def test_app_mention_creates_session_and_posts_response(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> hello"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    stream = await _wait_for_stream_stop(slack)
    await service.shutdown()

    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    record = await store.get_session(key)
    assert record is not None and record.session_id == "conv_1"
    assert omnigent.created[0][0] == "ag_1"
    assert omnigent.bound == ["conv_1"]
    assert omnigent.turns == [("conv_1", "hello")]
    # The stream replies in-thread and delivers the streamed answer.
    assert stream.start_kwargs["thread_ts"] == "100.1"
    assert stream.text == "hello final"
    # Deltas streamed live; the final item added no text beyond them.
    assert stream.appended == ["hel", "lo"]
    # An immediate "Working on it…" ack was posted, then deleted once content
    # started streaming — leaving no leftover placeholder.
    assert len(slack.acks) == 1
    assert slack.acks[0]["ts"] in slack.deleted_ts
    assert slack.posts == []
    # The placeholder stayed up until the streamed message was actually on
    # screen. This short answer buffers in the SDK and only becomes visible at
    # stop(); the ack was still live then and is deleted only afterwards, so the
    # thread is never empty while waiting for content.
    assert stream.ack_live_when_visible is True


async def test_ack_is_posted_and_cleared_on_host_unavailable(tmp_path: Path) -> None:
    # Even when the session can't start, the immediate ack is posted and then
    # deleted before the guidance reply, so no placeholder lingers.
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = HostUnavailableClient()
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> hi"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_posts(slack, 1)
    await service.shutdown()

    assert len(slack.acks) == 1
    assert slack.acks[0]["ts"] in slack.deleted_ts
    # The only durable post is the guidance, not the ack.
    assert len(slack.posts) == 1
    assert "omni host --server http://omnigent.test" in slack.posts[-1]["text"]


async def test_channel_stream_passes_recipient_ids(tmp_path: Path) -> None:
    # Streaming to a channel requires recipient_user_id + recipient_team_id; the
    # bot supplies them from the turn (owner + team).
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> hello"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    stream = await _wait_for_stream_stop(slack)
    await service.shutdown()

    assert stream.start_kwargs["channel"] == "C1"
    assert stream.start_kwargs["recipient_user_id"] == "U1"
    assert stream.start_kwargs["recipient_team_id"] == "T1"


class StreamingClient(FakeOmnigentClient):
    """Streams ``final_text`` as delta chunks, then reports it as the final item.

    Mirrors a real turn where the delta events accumulate into exactly the final
    message text.
    """

    async def run_turn(
        self,
        session_id: str,
        text: str,
        *,
        workspace: str | None = None,
        host_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        self.turns.append((session_id, text))
        for i in range(0, len(self.final_text), 500):
            yield {
                "type": "response.output_text.delta",
                "delta": self.final_text[i : i + 500],
            }
        yield {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": self.final_text}],
            },
        }
        yield {"type": "response.completed", "response": {"status": "completed"}}


async def test_long_answer_streams_in_full(tmp_path: Path) -> None:
    # A long answer is streamed and finalized without any splitting/msg_too_long
    # handling — Slack owns chunking for streams.
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    long_answer = "x" * 9000
    omnigent = StreamingClient(final_text=long_answer)
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> hello"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    stream = await _wait_for_stream_stop(slack)
    await service.shutdown()

    # The full answer is delivered (deltas + stop tail) with one stream, no
    # overflow chat.postMessage replies.
    assert stream.text == long_answer
    assert slack.posts == []


async def test_turn_error_posts_separate_reply_and_keeps_answer(tmp_path: Path) -> None:
    """An error after content streamed must not erase the delivered answer.

    The failure is reported as its own thread reply so the user keeps both the
    real answer and the failure notice.
    """
    store = await _store(tmp_path)
    slack = FakeSlackClient()

    class ErroringAfterAnswerClient(FakeOmnigentClient):
        async def run_turn(
            self,
            session_id: str,
            text: str,
            *,
            workspace: str | None = None,
            host_id: str | None = None,
        ) -> AsyncIterator[dict[str, Any]]:
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
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> hello"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    stream = await _wait_for_stream_stop(slack)
    for _ in range(50):
        if slack.posts:
            break
        await asyncio.sleep(0.02)
    await service.shutdown()

    # The stream delivered the real answer, not the error.
    assert stream.text == "the real answer"
    # The failure is a separate reply in the same thread.
    failure_posts = [p for p in slack.posts if "failed" in str(p.get("text", ""))]
    assert len(failure_posts) == 1
    assert "boom" in failure_posts[0]["text"]
    assert failure_posts[0]["thread_ts"] == "100.1"


async def test_turn_error_without_answer_finalizes_with_error(tmp_path: Path) -> None:
    """When nothing streamed, the error surfaces as the stream's final text."""
    store = await _store(tmp_path)
    slack = FakeSlackClient()

    class ErroringNoAnswerClient(FakeOmnigentClient):
        async def run_turn(
            self,
            session_id: str,
            text: str,
            *,
            workspace: str | None = None,
            host_id: str | None = None,
        ) -> AsyncIterator[dict[str, Any]]:
            self.turns.append((session_id, text))
            yield {
                "type": "response.failed",
                "response": {"error": {"message": "boom"}},
            }

        async def latest_assistant_text(self, session_id: str) -> str | None:
            return None

    omnigent = ErroringNoAnswerClient()
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> hello"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    stream = await _wait_for_stream_stop(slack)
    await service.shutdown()

    assert "boom" in (stream.stop_text or "")
    # No extra failure reply when there was no answer to preserve.
    assert slack.posts == []


async def test_stream_closed_mid_turn_continues_in_new_stream(tmp_path: Path) -> None:
    # A long-running turn can outlast Slack's streaming window; Slack finalizes
    # the message and the next append raises message_not_in_streaming_state. The
    # bot opens a fresh streaming reply and keeps streaming into it, so the full
    # answer is delivered live across two messages rather than a static catch-up.
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    slack.stream_close_after = 1
    omnigent = StreamingClient(final_text="chunk-a" + "y" * 600)
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> hello"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_stream_stop(slack)
    await service.shutdown()

    # The reply split into more than one streaming message when Slack closed the
    # first, and together they reconstruct the full answer with no lost text.
    assert len(slack.streams) >= 2
    assert slack.streamed_text == "chunk-a" + "y" * 600
    # The continuation streamed in the same thread; no static catch-up reply.
    assert slack.streams[-1].start_kwargs["thread_ts"] == "100.1"
    assert slack.posts == []


async def test_stream_closed_then_error_continues_and_posts_failure(tmp_path: Path) -> None:
    # When the stream closes AND the turn errors, the answer keeps streaming in a
    # fresh reply and the failure lands as its own clean notice — not a crash.
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    slack.stream_close_after = 1

    class ClosedThenErrorClient(FakeOmnigentClient):
        async def run_turn(
            self,
            session_id: str,
            text: str,
            *,
            workspace: str | None = None,
            host_id: str | None = None,
        ) -> AsyncIterator[dict[str, Any]]:
            self.turns.append((session_id, text))
            yield {"type": "response.output_text.delta", "delta": "part one "}
            yield {"type": "response.output_text.delta", "delta": "part two"}
            yield {"type": "response.failed", "response": {"error": {"message": "boom"}}}

    omnigent = ClosedThenErrorClient()
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> hello"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_posts(slack, 1)
    await service.shutdown()

    # Both deltas streamed live (across the reopened stream); nothing was lost.
    assert slack.streamed_text == "part one part two"
    # The failure is its own clean reply, not the raw stream-closed error.
    failure_posts = [p for p in slack.posts if "failed" in str(p.get("text", ""))]
    assert len(failure_posts) == 1
    assert "boom" in failure_posts[0]["text"]


async def test_empty_app_mention_prompts_without_creating_session(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    service, _pool, _setup = _service(store, omnigent)

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


async def test_channel_thread_reply_without_mention_is_ignored(tmp_path: Path) -> None:
    # A channel thread that already has a session is human discussion until the
    # bot is @-mentioned again; plain replies must not reach the session.
    store = await _store(tmp_path)
    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    await store.upsert_session(key, "conv_existing", "title")
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    service, _pool, _setup = _service(store, omnigent)

    await service.handle_message(
        body={"team_id": "T1", "event_id": "Ev2"},
        event={
            "channel": "C1",
            "channel_type": "channel",
            "thread_ts": "100.1",
            "ts": "101.1",
            "user": "U1",
            "text": "just chatting with a teammate",
        },
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await service.shutdown()

    assert omnigent.created == []
    assert omnigent.bound == []
    assert omnigent.turns == []
    assert slack.posts == []
    assert slack.streams == []


async def test_direct_message_creates_session(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_message(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={
            "channel": "D1",
            "channel_type": "im",
            "ts": "100.1",
            "user": "U1",
            "text": "hello there",
        },
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_stream_stop(slack)
    await service.shutdown()

    assert len(omnigent.created) == 1
    assert omnigent.created[0][0] == "ag_1"
    assert omnigent.bound == ["conv_1"]
    assert omnigent.turns == [("conv_1", "hello there")]
    record = await store.get_session(ThreadKey("T1", "D1", "100.1"))
    assert record is not None and record.session_id == "conv_1"


async def test_direct_message_reply_reuses_existing_session(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    key = ThreadKey(team_id="T1", channel_id="D1", thread_ts="100.1")
    await store.upsert_session(
        key,
        "conv_existing",
        "title",
        owner_user_id="U1",
    )
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    service, _pool, _setup = _service(store, omnigent)

    await service.handle_message(
        body={"team_id": "T1", "event_id": "Ev2"},
        event={
            "channel": "D1",
            "channel_type": "im",
            "thread_ts": "100.1",
            "ts": "101.1",
            "user": "U1",
            "text": "follow up",
        },
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_stream_stop(slack)
    await service.shutdown()

    assert omnigent.created == []
    assert omnigent.bound == []
    assert omnigent.turns == [("conv_existing", "follow up")]


async def test_direct_message_with_bot_mention_is_handled(tmp_path: Path) -> None:
    # DMs do not fire app_mention, so a "<@bot>" in a DM is the only event we
    # get — it must be handled (mention stripped), not dropped as a duplicate.
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_message(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={
            "channel": "D1",
            "channel_type": "im",
            "ts": "100.1",
            "user": "U1",
            "text": "<@B1> hello there",
        },
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_stream_stop(slack)
    await service.shutdown()

    assert len(omnigent.created) == 1
    assert omnigent.turns == [("conv_1", "hello there")]


async def test_channel_message_without_session_is_ignored(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    service, _pool, _setup = _service(store, omnigent)

    await service.handle_message(
        body={"team_id": "T1", "event_id": "Ev3"},
        event={
            "channel": "C1",
            "channel_type": "channel",
            "ts": "100.1",
            "user": "U1",
            "text": "hello there",
        },
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await service.shutdown()

    assert omnigent.created == []
    assert omnigent.turns == []
    assert slack.posts == []


async def test_duplicate_event_is_ignored(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")
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
    await _wait_for_stream_stop(slack)
    await service.shutdown()

    assert len(omnigent.turns) == 1


async def test_generic_message_with_bot_mention_is_ignored(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    await store.upsert_session(key, "conv_existing", "title")
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    service, _pool, _setup = _service(store, omnigent)

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


async def test_unconfigured_user_is_prompted_and_no_turn_runs(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    service, _pool, setup = _service(store, omnigent)

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> hello"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await service.shutdown()

    # No session created; the user is nudged into setup instead.
    assert omnigent.created == []
    assert omnigent.turns == []
    assert len(setup.prompted) == 1
    assert setup.prompted[0]["user_id"] == "U1"
    assert setup.prompted[0]["in_channel"] is True


async def test_channel_followup_from_other_user_is_ignored(tmp_path: Path) -> None:
    # A thread's session belongs to its creator; a different user's @mention in
    # that thread is not added to the session for now.
    store = await _store(tmp_path)
    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    await store.upsert_session(
        key,
        "conv_existing",
        "title",
        owner_user_id="U1",
    )
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    service, _pool, setup = _service(store, omnigent)

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev2"},
        event={
            "channel": "C1",
            "thread_ts": "100.1",
            "ts": "101.1",
            "user": "U2",
            "text": "<@B1> jumping in",
        },
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await service.shutdown()

    assert omnigent.turns == []
    assert setup.prompted == []
    assert slack.posts == []


async def test_turn_runs_against_the_fixed_operator_server(tmp_path: Path) -> None:
    # The bot always routes to the operator-configured server; the user's saved
    # config only carries the agent/host/workspace choice.
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    service, pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1", agent_id="ag_custom")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> hi"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_stream_stop(slack)
    await service.shutdown()

    # Routed to the operator-fixed server (the only URL the pool is asked for).
    assert pool.requested == ["http://omnigent.test"]
    assert omnigent.created[0][0] == "ag_custom"
    record = await store.get_session(ThreadKey("T1", "C1", "100.1"))
    assert record is not None
    assert record.owner_user_id == "U1"


class ServerUnreachableClient(FakeOmnigentClient):
    async def create_session(self, agent_id: str, title: str) -> str:
        raise ServerUnreachableError("boom")


class HostUnavailableClient(FakeOmnigentClient):
    async def launch_runner(
        self, session_id: str, *, workspace: str, host_id: str | None = None
    ) -> str:
        raise HostUnavailableError("no host")


class AuthRequiredClient(FakeOmnigentClient):
    async def create_session(self, agent_id: str, title: str) -> str:
        raise AuthRequiredError("401")


class ServerErrorClient(FakeOmnigentClient):
    async def create_session(self, agent_id: str, title: str) -> str:
        # Mirrors a 500 from POST /v1/sessions: a bare OmnigentError, NOT one of
        # the specifically-handled subclasses.
        raise OmnigentError("Omnigent request failed with 500: internal_error")


async def _wait_for_posts(client: FakeSlackClient, count: int) -> None:
    for _ in range(50):
        if len(client.posts) >= count:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"Timed out waiting for {count} posts")


async def test_unreachable_server_prompts_config_command(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = ServerUnreachableClient()
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> hi"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_posts(slack, 1)
    await service.shutdown()

    # No session persisted; the user is told to reconfigure.
    assert await store.get_session(ThreadKey("T1", "C1", "100.1")) is None
    text = slack.posts[-1]["text"]
    assert "/omnigent" in text
    assert "couldn't reach" in text.lower()


async def test_auth_required_clears_ack_and_prompts_relogin(tmp_path: Path) -> None:
    # A user with saved config but no valid token (e.g. bot restarted, in-memory
    # tokens lost) must NOT be left with a lingering "Working on it…" — the ack
    # is cleared and a re-login prompt is posted instead.
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = AuthRequiredClient()
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> hi"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_posts(slack, 1)
    await service.shutdown()

    # The placeholder was posted and then deleted — nothing lingers.
    assert len(slack.acks) == 1
    assert slack.acks[0]["ts"] in slack.deleted_ts
    # No session persisted; the user is told to log in again.
    assert await store.get_session(ThreadKey("T1", "C1", "100.1")) is None
    text = slack.posts[-1]["text"]
    assert "/omnigent" in text
    assert "log in" in text.lower() or "login" in text.lower()


async def test_server_error_creating_session_clears_ack_and_reports(tmp_path: Path) -> None:
    # A 500 from create_session raises a bare OmnigentError (not one of the
    # specifically-handled subclasses). It must still clear the "Working on
    # it…" placeholder and post a failure — never strand the thread.
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = ServerErrorClient()
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> hi"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_posts(slack, 1)
    await service.shutdown()

    # Placeholder posted then deleted — nothing lingers on "Working on it…".
    assert len(slack.acks) == 1
    assert slack.acks[0]["ts"] in slack.deleted_ts
    # A failure reply was posted, and no session was persisted.
    assert await store.get_session(ThreadKey("T1", "C1", "100.1")) is None
    text = slack.posts[-1]["text"]
    assert "failed" in text.lower()


async def test_no_online_host_prompts_omni_host_command(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = HostUnavailableClient()
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> hi"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_posts(slack, 1)
    await service.shutdown()

    assert await store.get_session(ThreadKey("T1", "C1", "100.1")) is None
    text = slack.posts[-1]["text"]
    assert "omni host --server http://omnigent.test" in text
    assert "/omnigent" in text
