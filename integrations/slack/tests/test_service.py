import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from omnigent_slack.approvals import Verdict, parse_action_value
from omnigent_slack.models import ThreadKey, UserConfig
from omnigent_slack.omnigent import (
    AuthRequiredError,
    HostUnavailableError,
    OmnigentError,
    ServerUnreachableError,
)
from omnigent_slack.service import _ACK_TEXT, SlackOmnigentService
from omnigent_slack.store import SQLiteStore
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_slack_response import AsyncSlackResponse


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
        # Monotonic rank of when this stream's message opened, relative to other
        # posts/streams on the same client. Slack orders by the timestamp fixed
        # at open time, so this models a segment's position in the thread.
        self.open_order = client._tick()
        self._close_after = close_after
        self.closed = False
        # Whether the placeholder ack was still live the moment this stream first
        # put content on screen (a mid-stream flush, or the finalizing stop for a
        # short answer that never filled the buffer).
        self.ack_live_when_visible: bool | None = None
        # Monotonic rank of when this stream's text first became visible (first
        # flush/stop). Lets a test assert content was revealed before a later
        # out-of-band post (e.g. an approval card), not coincident with it.
        self.first_visible_order: int | None = None
        # Rank of a FORCED flush (append with chunks — our _LiveReply.flush),
        # None if the buffer was only ever revealed by the finalizing stop.
        self.forced_flush_order: int | None = None
        self._buffer_size = buffer_size
        self._pending = 0

    def _record_ack_state(self) -> None:
        if self.first_visible_order is None:
            self.first_visible_order = self._client._tick()
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

    async def append(
        self, *, markdown_text: str | None = None, chunks: Any = None
    ) -> dict[str, Any] | None:
        if self.closed:
            self._raise_closed()
        if markdown_text is not None:
            self.appended.append(markdown_text)
            self._pending += len(markdown_text)
        if self._close_after is not None and len(self.appended) >= self._close_after:
            self.closed = True
        # The SDK flushes when the buffer crosses the threshold OR when called
        # with ``chunks`` set (a forced flush, even chunks=[]). Otherwise buffer.
        if chunks is None and self._pending < self._buffer_size:
            return None
        if chunks is not None and self._pending == 0:
            # Forced flush with nothing buffered → no-op (matches an empty flush).
            return None
        if chunks is not None:
            # A forced flush (our _LiveReply.flush) — record its position so a
            # test can assert buffered text was revealed via flush, before a
            # later out-of-band post, rather than only at the finalizing stop.
            self.forced_flush_order = self._client._tick()
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
        self.updates: list[dict[str, Any]] = []
        # Ephemeral ("Only visible to you") notices — private, not durable posts.
        self.ephemerals: list[dict[str, Any]] = []
        self.streams: list[FakeStream] = []
        self._next_ts = 0
        self._order = 0
        # When set, every stream this client opens auto-closes after this many
        # appended deltas — simulating Slack finalizing the message mid-turn.
        self.stream_close_after: int | None = None

    def _tick(self) -> int:
        # Monotonic rank stamped on each post/stream-open so tests can assert
        # the thread's chronological order (Slack sorts by creation timestamp).
        self._order += 1
        return self._order

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self._next_ts += 1
        ts = f"bot-{self._next_ts}"
        entry = {**kwargs, "ts": ts, "order": self._tick()}
        self.posts.append(entry)
        if kwargs.get("text") == _ACK_TEXT:
            self.acks.append(entry)
        return {"ok": True, "ts": ts}

    async def chat_postEphemeral(self, **kwargs: Any) -> dict[str, Any]:
        self.ephemerals.append({**kwargs})
        return {"ok": True, "message_ts": "ephemeral"}

    async def chat_delete(self, **kwargs: Any) -> dict[str, Any]:
        ts = kwargs.get("ts")
        self.deleted_ts.append(str(ts))
        self.posts = [p for p in self.posts if p.get("ts") != ts]
        return {"ok": True}

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        ts = kwargs.get("ts")
        self.updates.append({**kwargs})
        for post in self.posts:
            if post.get("ts") == ts:
                post.update(kwargs)
        return {"ok": True, "ts": ts}

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
        self.resolved: list[tuple[str, str, bool]] = []
        self.resolved_content: list[dict[str, Any] | None] = []
        self.next_session_id = "conv_1"
        self.final_text = final_text
        # Rolled-up status the grace window polls at a soft idle; default idle so
        # a turn ends promptly unless a test sets it to "running".
        self.status = "idle"
        # Newest assistant message the server would return, for the no-delta
        # fallback. ``latest_message_id`` pins the id (else each call gets a
        # fresh id, so the fallback treats it as new relative to the baseline).
        self.latest_message: str | None = None
        self.latest_message_id: str | None = None
        self._latest_calls = 0
        # Whether an outstanding elicitation is still pending server-side. Default
        # True so the Slack-click path is exercised; a test sets it False to
        # simulate the user answering elsewhere (web UI).
        self.elicitation_pending = True
        # Server activity reported at ROUTE time (before a turn) — the gate that
        # decides whether a new message runs or is deflected. Defaults to free
        # (idle, no pending) so a follow-up runs; a test sets these to simulate a
        # busy or awaiting-input session. Kept separate from ``status`` (which the
        # in-turn grace window polls) so the two don't collide.
        self.route_status: str | None = "idle"
        self.route_pending_elicitation = False

    async def get_session_status(self, session_id: str) -> str | None:
        return self.status

    async def get_session_activity(self, session_id: str) -> Any:
        from omnigent_slack.omnigent import SessionActivity

        return SessionActivity(
            status=self.route_status, pending_elicitation=self.route_pending_elicitation
        )

    async def is_elicitation_pending(self, session_id: str, elicitation_id: str) -> bool:
        return self.elicitation_pending

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

    async def latest_assistant_message(self, session_id: str) -> tuple[str, str] | None:
        # (item_id, text) of the newest assistant message, or None. Tests that
        # exercise the no-delta fallback set ``latest_message``; the id must
        # differ from the pre-turn baseline for the fallback to fire, so a
        # counter makes each call's id unique unless a test pins it.
        if self.latest_message is None:
            return None
        self._latest_calls += 1
        item_id = self.latest_message_id or f"msg-{self._latest_calls}"
        return (item_id, self.latest_message)

    async def resolve_elicitation(
        self,
        session_id: str,
        elicitation_id: str,
        *,
        accepted: bool,
        content: dict[str, Any] | None = None,
    ) -> None:
        self.resolved.append((session_id, elicitation_id, accepted))
        self.resolved_content.append(content)


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


class NoDeltaIdleClient(FakeOmnigentClient):
    """Mirrors a real claude-native short answer: NO text deltas — the answer
    arrives only as a committed ``output_item.done`` — and the turn ends on
    ``session.status: idle`` (not ``response.completed``), exercising the grace
    window. The ack must stay live until the buffered answer is on screen.
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
        yield {"type": "session.status", "status": "running"}
        yield {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": self.final_text}],
            },
        }
        yield {"type": "session.status", "status": "idle"}


async def test_no_delta_idle_answer_keeps_ack_until_visible(tmp_path: Path) -> None:
    # Regression guard for the real claude-native shape: no deltas, answer only
    # in output_item.done, turn ends on session.status idle. The "Working on it…"
    # placeholder must remain live until the buffered answer is delivered at
    # stop() — never deleted early leaving the thread momentarily empty.
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = NoDeltaIdleClient(final_text="Here is the answer.")
    service, _pool, _setup = _service(store, omnigent)
    # Snapshot idle so the grace window ends promptly.
    omnigent.status = "idle"  # type: ignore[attr-defined]
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> hi"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    stream = await _wait_for_stream_stop(slack)
    await service.shutdown()

    assert stream.text == "Here is the answer."
    # The ack was live when the answer became visible, and cleared afterward —
    # so the thread never showed an empty gap.
    assert stream.ack_live_when_visible is True
    assert len(slack.acks) == 1
    assert slack.acks[0]["ts"] in slack.deleted_ts


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


async def test_message_while_server_busy_is_deflected(tmp_path: Path) -> None:
    # The decision to accept is the SERVER's: if the snapshot reports the session
    # running/waiting, a new message is NOT run and NOT queued — the user is
    # privately told to wait or interrupt in the web UI. (Local connection state
    # is not consulted, so a stale reservation can't wrongly report busy.)
    store = await _store(tmp_path)
    key = ThreadKey(team_id="T1", channel_id="D1", thread_ts="100.1")
    await store.upsert_session(key, "conv_existing", "title", owner_user_id="U1")
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    omnigent.route_status = "running"  # server is busy at route time
    service, _pool, _setup = _service(store, omnigent)

    await service.handle_message(
        body={"team_id": "T1", "event_id": "Ev2"},
        event={
            "channel": "D1",
            "channel_type": "im",
            "thread_ts": "100.1",
            "ts": "101.1",
            "user": "U1",
            "text": "second while busy",
        },
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await service.shutdown()

    # Deflected (not run) with a busy notice pointing at the web UI.
    assert omnigent.turns == []
    busy = [e for e in slack.ephemerals if "still working on your previous" in e["text"].lower()]
    assert len(busy) == 1
    assert busy[0]["user"] == "U1"
    # The web UI is a Slack mrkdwn hyperlink (<url|text>), not a bare URL.
    assert "/c/conv_existing|web UI>" in busy[0]["text"]


async def test_second_message_while_local_stream_active_is_deflected(tmp_path: Path) -> None:
    # Even when the SERVER snapshot momentarily reads idle (claude-native flips to
    # idle between streaming bursts), a turn already streaming IN THIS PROCESS
    # must block a second turn — a 2nd stream would render every event twice
    # (the duplicate-responses bug). The local reservation catches this before
    # the server-activity check.
    store = await _store(tmp_path)
    key = ThreadKey(team_id="T1", channel_id="D1", thread_ts="100.1")
    await store.upsert_session(key, "conv_existing", "title", owner_user_id="U1")
    slack = FakeSlackClient()

    release = asyncio.Event()

    class BlockingClient(FakeOmnigentClient):
        async def run_turn(
            self,
            session_id: str,
            text: str,
            *,
            workspace: str | None = None,
            host_id: str | None = None,
        ) -> AsyncIterator[dict[str, Any]]:
            self.turns.append((session_id, text))
            await release.wait()  # hold the first turn streaming locally
            yield {"type": "session.status", "status": "idle"}

    omnigent = BlockingClient()
    omnigent.route_status = "idle"  # server LOOKS idle (the race window)
    service, _pool, _setup = _service(store, omnigent)

    async def _send(text: str, ts: str, event_id: str) -> None:
        await service.handle_message(
            body={"team_id": "T1", "event_id": event_id},
            event={
                "channel": "D1",
                "channel_type": "im",
                "thread_ts": "100.1",
                "ts": ts,
                "user": "U1",
                "text": text,
            },
            client=slack,
            context={"bot_user_id": "B1"},
        )

    await _send("first", "101.1", "Ev1")
    for _ in range(100):  # wait until the first turn is actually streaming
        if omnigent.turns:
            break
        await asyncio.sleep(0.02)
    await _send("second", "102.1", "Ev2")

    # Only the first turn ran; the second was deflected despite the idle snapshot.
    assert omnigent.turns == [("conv_existing", "first")]
    busy = [e for e in slack.ephemerals if "still working on your previous" in e["text"].lower()]
    assert len(busy) == 1
    release.set()
    await service.shutdown()


async def test_message_while_awaiting_action_points_to_pending_request(tmp_path: Path) -> None:
    # A session parked on a pending elicitation: a new message can't proceed. The
    # user is told to answer the pending request (here or in the web UI), matching
    # the web UI's "action required" state — distinct from the "still working" one.
    store = await _store(tmp_path)
    key = ThreadKey(team_id="T1", channel_id="D1", thread_ts="100.1")
    await store.upsert_session(key, "conv_existing", "title", owner_user_id="U1")
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    omnigent.route_status = "waiting"
    omnigent.route_pending_elicitation = True
    service, _pool, _setup = _service(store, omnigent)

    await service.handle_message(
        body={"team_id": "T1", "event_id": "Ev2"},
        event={
            "channel": "D1",
            "channel_type": "im",
            "thread_ts": "100.1",
            "ts": "101.1",
            "user": "U1",
            "text": "another request",
        },
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await service.shutdown()

    assert omnigent.turns == []
    notices = [e for e in slack.ephemerals if "waiting on your response" in e["text"].lower()]
    assert len(notices) == 1
    assert notices[0]["user"] == "U1"


async def test_idle_follow_up_message_runs_in_thread(tmp_path: Path) -> None:
    # A follow-up to an existing thread that is NOT currently streaming runs
    # normally in Slack (run-when-idle) — Slack stays a full conversational
    # surface, not kickoff-only.
    store = await _store(tmp_path)
    key = ThreadKey(team_id="T1", channel_id="D1", thread_ts="100.1")
    await store.upsert_session(key, "conv_existing", "title", owner_user_id="U1")
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
            "text": "follow up while idle",
        },
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_stream_stop(slack)
    await service.shutdown()

    # The follow-up ran against the existing session (no new session created).
    assert omnigent.created == []
    assert omnigent.turns == [("conv_existing", "follow up while idle")]
    assert slack.ephemerals == []


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
    # that thread is not added to the session, but that user gets a private
    # ("Only visible to you") note explaining why and how to get their own.
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
    # No durable post clutters the thread — the notice is ephemeral, aimed at U2.
    assert slack.posts == []
    assert len(slack.ephemerals) == 1
    notice = slack.ephemerals[0]
    assert notice["user"] == "U2"
    assert notice["channel"] == "C1"
    assert notice["thread_ts"] == "100.1"
    assert "start a new thread" in notice["text"].lower()


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


# ── Tool-approval (elicitation) flow ─────────────────────────────────


def _elicitation_event(
    elicitation_id: str = "elicit_1",
    message: str = "Agent wants to call Edit(). Approve?",
    content_preview: str = '{"name": "Edit"}',
) -> dict[str, Any]:
    return {
        "type": "response.elicitation_request",
        "elicitation_id": elicitation_id,
        "method": "elicitation/create",
        "params": {
            "mode": "form",
            "message": message,
            "policy_name": "require_approval",
            "content_preview": content_preview,
        },
    }


def _form_elicitation_event(elicitation_id: str = "elicit_form") -> dict[str, Any]:
    return {
        "type": "response.elicitation_request",
        "elicitation_id": elicitation_id,
        "method": "elicitation/create",
        "params": {
            "mode": "form",
            "message": "Pick options",
            "ask_user_question": {
                "questions": [
                    {
                        "id": "store",
                        "question": "Where to store?",
                        "options": [{"label": "Redis"}, {"label": "Memory"}],
                        "multiSelect": False,
                    }
                ]
            },
        },
    }


class ApprovalClient(FakeOmnigentClient):
    """A turn that streams, parks on an elicitation, then streams a tail.

    The generator yields the elicitation event and then blocks until the worker
    resolves it (the worker awaits the verdict before pulling the next event).
    This mirrors the server keeping the stream open across the park.
    """

    def __init__(
        self, elicitation_id: str = "elicit_1", event: dict[str, Any] | None = None
    ) -> None:
        super().__init__(final_text="done")
        self._elicitation_id = elicitation_id
        self._event = event or _elicitation_event(elicitation_id)

    async def run_turn(
        self,
        session_id: str,
        text: str,
        *,
        workspace: str | None = None,
        host_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        self.turns.append((session_id, text))
        yield {"type": "response.output_text.delta", "delta": "work"}
        yield self._event
        # The worker resolves the elicitation before requesting more events;
        # by the time control returns here the verdict has been delivered.
        yield {"type": "response.output_text.delta", "delta": "ing"}
        yield {"type": "session.status", "status": "idle"}


class PreambleThenCommittedAnswerClient(FakeOmnigentClient):
    """Mirrors the real AskUserQuestion shape: a preamble message (delta +
    committed), the elicitation, then a post-answer message delivered ONLY as a
    committed ``output_item.done`` (no deltas) — the deltas-race-behind-commit
    case. Exercises the tail recovery across the seal boundary.
    """

    def __init__(self, event: dict[str, Any]) -> None:
        super().__init__(final_text="")
        self._event = event

    async def run_turn(
        self,
        session_id: str,
        text: str,
        *,
        workspace: str | None = None,
        host_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        self.turns.append((session_id, text))
        # Preamble: streamed as a delta AND committed as an item.
        yield {"type": "response.output_text.delta", "delta": "Here's a demo."}
        yield {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Here's a demo."}],
            },
        }
        yield self._event
        # Post-answer message arrives ONLY as a committed item (no deltas) — the
        # tail must be recovered and delivered, not dropped.
        yield {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "You picked A. Full summary here."}],
            },
        }
        yield {"type": "session.status", "status": "idle"}


async def _wait_for_card(client: FakeSlackClient) -> dict[str, Any]:
    """Wait for the approval card (a post carrying an actions block)."""
    for _ in range(100):
        for post in client.posts:
            blocks = post.get("blocks") or []
            if any(b.get("type") == "actions" for b in blocks):
                return post
        await asyncio.sleep(0.02)
    raise AssertionError("Timed out waiting for an approval card")


def _card_elicitation_id(card: dict[str, Any]) -> str:
    for block in card.get("blocks", []):
        if block.get("type") == "actions":
            target = parse_action_value(block["elements"][0]["value"])
            assert target is not None
            return target.elicitation_id
    raise AssertionError("Card has no actions block")


async def _wait_for_resolved(omnigent: "FakeOmnigentClient", count: int = 1) -> None:
    """Wait until the turn has forwarded ``count`` approval verdicts to the server.

    The answer is now split across stream segments by an approval seal, so
    "first stream stopped" no longer marks turn completion — wait on the
    server-visible verdict instead.
    """
    for _ in range(100):
        if len(omnigent.resolved) >= count:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"Timed out waiting for {count} resolved elicitation(s)")


async def test_tool_approval_approve_resumes_turn(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = ApprovalClient()
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> edit"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    card = await _wait_for_card(slack)
    eid = _card_elicitation_id(card)
    delivered = await service.handle_elicitation_action(
        elicitation_id=eid, verdict=Verdict(accepted=True)
    )
    await _wait_for_resolved(omnigent)
    await service.shutdown()

    assert delivered is True
    # Verdict forwarded to the server as accept, then the turn resumed.
    assert omnigent.resolved == [("conv_1", "elicit_1", True)]
    # The answer is split by the approval seal: "work" streamed before the card,
    # "ing" after it — two separate stream segments in chronological order,
    # with the card posted between them.
    assert len(slack.streams) == 2
    assert slack.streams[0].text == "work"
    assert slack.streams[1].text == "ing"
    # The card was updated in place to its outcome and lost its buttons.
    assert slack.updates, "expected the card to be updated after resolution"
    updated_blocks = slack.updates[-1]["blocks"]
    assert not any(b.get("type") == "actions" for b in updated_blocks)
    assert "Approved" in updated_blocks[0]["text"]["text"]


async def test_short_pre_card_text_is_flushed_before_the_card(tmp_path: Path) -> None:
    # The pre-card answer text ("work", well under the SDK buffer size) must be
    # revealed BEFORE the approval card is posted — not left buffered until the
    # seal, which would make it appear coincident with the card (the web UI shows
    # it live as it streams). We assert the stream's first-visible tick precedes
    # the card post's order tick.
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = ApprovalClient()
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> edit"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    card = await _wait_for_card(slack)
    eid = _card_elicitation_id(card)
    await service.handle_elicitation_action(elicitation_id=eid, verdict=Verdict(accepted=True))
    await _wait_for_resolved(omnigent)
    await service.shutdown()

    # The first (pre-card) segment carried "work" and was FORCE-flushed to screen
    # (via _LiveReply.flush) — not left buffered until the finalizing stop.
    pre_card = slack.streams[0]
    assert pre_card.text == "work"
    assert pre_card.forced_flush_order is not None, "pre-card text was not force-flushed"
    # The forced flush happened strictly before the card message was posted.
    assert pre_card.forced_flush_order < card["order"]


async def test_tool_approval_deny_forwards_decline(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = ApprovalClient()
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> edit"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    card = await _wait_for_card(slack)
    eid = _card_elicitation_id(card)
    await service.handle_elicitation_action(elicitation_id=eid, verdict=Verdict(accepted=False))
    await _wait_for_resolved(omnigent)
    await service.shutdown()

    assert omnigent.resolved == [("conv_1", "elicit_1", False)]
    assert "Denied" in slack.updates[-1]["blocks"][0]["text"]["text"]


async def test_elicitation_resolved_externally_unblocks_without_verdict(tmp_path: Path) -> None:
    # The user answers the request in the web UI instead of clicking the Slack
    # card. The worker must stop waiting (once the server shows it no longer
    # pending) and NOT post its own verdict — otherwise it blocks to the
    # coordinator timeout, holding the thread's turn open and deflecting its
    # follow-ups the whole time.
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = ApprovalClient()
    service, _pool, _setup = _service(store, omnigent)
    service._external_resolve_poll_seconds = 0.02  # type: ignore[attr-defined]
    await _configure_user(store, "T1", "U1")

    # User will answer elsewhere; the card click never comes.
    omnigent.elicitation_pending = False
    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> edit"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    # Wait for the card to be updated with the outcome (the external-resolve path).
    for _ in range(100):
        if slack.updates:
            break
        await asyncio.sleep(0.02)
    await service.shutdown()

    # The bot did not post its own verdict (the server already has it), and the
    # card was updated to reflect the external resolution.
    assert omnigent.resolved == []
    assert slack.updates
    assert "Answered elsewhere" in slack.updates[-1]["blocks"][0]["text"]["text"]


async def test_denied_approval_does_not_resurrect_prior_answer(tmp_path: Path) -> None:
    # Regression: a turn that produces no new answer (the only action was a
    # denied approval) must NOT deliver the previous turn's message via the
    # no-delta fallback. The fallback only fires for a message newer than the
    # pre-turn baseline.
    store = await _store(tmp_path)
    slack = FakeSlackClient()

    class DeniedNoAnswerClient(FakeOmnigentClient):
        async def run_turn(
            self,
            session_id: str,
            text: str,
            *,
            workspace: str | None = None,
            host_id: str | None = None,
        ) -> AsyncIterator[dict[str, Any]]:
            self.turns.append((session_id, text))
            # Only a gated tool call, no answer text; ends on idle.
            yield _elicitation_event("elicit_rm")
            yield {"type": "session.status", "status": "idle"}

    omnigent = DeniedNoAnswerClient()
    # A stale prior-turn answer exists on the server, pinned to a fixed id so it
    # equals the pre-turn baseline (i.e. it is NOT new this turn).
    omnigent.latest_message = "PRIOR TURN SUMMARY — should not be re-sent"
    omnigent.latest_message_id = "prior-msg"
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> rm file"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    card = await _wait_for_card(slack)
    eid = _card_elicitation_id(card)
    await service.handle_elicitation_action(elicitation_id=eid, verdict=Verdict(accepted=False))
    for _ in range(100):
        if slack.streams and all(s.stopped for s in slack.streams):
            break
        await asyncio.sleep(0.02)
    await service.shutdown()

    # The stale prior summary was NOT delivered anywhere.
    all_text = "".join(s.text for s in slack.streams) + "".join(
        str(p.get("text", "")) for p in slack.posts
    )
    assert "PRIOR TURN SUMMARY" not in all_text


async def test_tool_approval_timeout_declines(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = ApprovalClient()
    # Zero timeout: no click arrives, so the worker gives up and declines.
    service, _pool, _setup = _service(store, omnigent)
    service.elicitations._timeout = 0.05  # type: ignore[attr-defined]
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> edit"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_resolved(omnigent)
    await service.shutdown()

    # Timed out → declined to the server so the parked turn doesn't hang, and the
    # card tells the user it was dropped and how to retry.
    assert omnigent.resolved == [("conv_1", "elicit_1", False)]
    outcome_text = slack.updates[-1]["blocks"][0]["text"]["text"]
    assert "Timed out" in outcome_text
    assert "again to retry" in outcome_text


async def test_stale_approval_click_is_reported_as_not_delivered(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    service, _pool, _setup = _service(store, FakeOmnigentClient())

    # No turn is parked on this id, so the click finds no waiter.
    delivered = await service.handle_elicitation_action(
        elicitation_id="elicit_gone", verdict=Verdict(accepted=True)
    )
    await service.shutdown()
    assert delivered is False


async def test_form_elicitation_forwards_selections_as_content(tmp_path: Path) -> None:
    # An AskUserQuestion (form) elicitation renders a selectable card; the
    # submitted answers are forwarded to the server as `content`, not a bare
    # accept — so the agent actually receives the user's choice.
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = ApprovalClient(elicitation_id="elicit_form", event=_form_elicitation_event())
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> ask"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    card = await _wait_for_card(slack)
    eid = _card_elicitation_id(card)
    # Answers arrive as option indices ("Redis" is index 0); the service maps
    # them back to the full labels before forwarding to the server.
    await service.handle_elicitation_action(
        elicitation_id=eid, verdict=Verdict(accepted=True, content={"store": "0"})
    )
    await _wait_for_resolved(omnigent)
    await service.shutdown()

    assert omnigent.resolved == [("conv_1", "elicit_form", True)]
    assert omnigent.resolved_content == [{"store": "Redis"}]
    # Card outcome reads "Answered" for a form, not "Approved".
    assert "Answered" in slack.updates[-1]["blocks"][0]["text"]["text"]


def _typed_input_elicitation_event(elicitation_id: str = "elicit_typed") -> dict[str, Any]:
    # A request for free-form typed input (non-empty schema, not AskUserQuestion)
    # — genuinely uncollectable with Slack buttons.
    return {
        "type": "response.elicitation_request",
        "elicitation_id": elicitation_id,
        "method": "elicitation/create",
        "params": {
            "mode": "url",
            "message": "Enter your name to continue",
            "requestedSchema": {"type": "object", "properties": {"name": {"type": "string"}}},
            "url": "/approve/conv_1/elicit_typed",
        },
    }


def _url_binary_elicitation_event(elicitation_id: str = "elicit_url") -> dict[str, Any]:
    # A plain binary approval delivered in `url` mode (the default server mode).
    return {
        "type": "response.elicitation_request",
        "elicitation_id": elicitation_id,
        "method": "elicitation/create",
        "params": {
            "mode": "url",
            "message": "Agent wants to run a shell command. Approve?",
            "phase": "tool_call",
            "requestedSchema": {},
            "url": "/approve/conv_1/elicit_url",
        },
    }


async def test_unsupported_typed_input_links_to_web_ui(tmp_path: Path) -> None:
    # A request for free-form typed input can't be rendered in Slack: the bot
    # posts a link to resolve it in the web UI and does NOT block or auto-resolve.
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = ApprovalClient(
        elicitation_id="elicit_typed", event=_typed_input_elicitation_event()
    )
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> go"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_turn_end(slack)
    await service.shutdown()

    # A link to the approve page was posted; no approval card, no auto-resolve.
    links = [p for p in slack.posts if "/approve/conv_1/elicit_typed" in str(p.get("text"))]
    assert links, "expected a web-UI link for the unsupported elicitation"
    assert "http://omnigent.test/approve/conv_1/elicit_typed" in links[0]["text"]
    assert omnigent.resolved == []
    assert not any(
        any(b.get("type") == "actions" for b in (p.get("blocks") or [])) for p in slack.posts
    )


async def test_url_mode_binary_renders_approval_card(tmp_path: Path) -> None:
    # The default server elicitation mode is `url`, but a binary approval must
    # still render a native Approve/Deny card (not the web link) — the verdict
    # posts to the resolve endpoint regardless of mode.
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = ApprovalClient(elicitation_id="elicit_url", event=_url_binary_elicitation_event())
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> run"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    card = await _wait_for_card(slack)
    eid = _card_elicitation_id(card)
    await service.handle_elicitation_action(elicitation_id=eid, verdict=Verdict(accepted=True))
    await _wait_for_resolved(omnigent)
    await service.shutdown()

    # Rendered as an Approve/Deny card and resolved via the endpoint — no web link.
    assert omnigent.resolved == [("conv_1", "elicit_url", True)]
    assert not any("/approve/" in str(p.get("text")) for p in slack.posts)


async def test_post_answer_message_only_committed_is_not_dropped(tmp_path: Path) -> None:
    # Regression: after a form elicitation, the answer message arrived only as a
    # committed output_item.done (no deltas). The seal must reset the per-segment
    # streamed_text so the tail reconciliation delivers that post-answer text,
    # rather than the pre-seal preamble polluting streamed_text and suppressing
    # the recovery (which silently truncated the reply in the thread).
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = PreambleThenCommittedAnswerClient(_form_elicitation_event())
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> demo"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    card = await _wait_for_card(slack)
    eid = _card_elicitation_id(card)
    await service.handle_elicitation_action(
        elicitation_id=eid, verdict=Verdict(accepted=True, content={"store": "A"})
    )
    await _wait_for_resolved(omnigent)
    await service.shutdown()

    # The post-answer text was delivered (in the post-seal segment), not dropped.
    assert any("You picked A. Full summary here." in s.text for s in slack.streams)


class PreambleThenSilentAfterElicitationClient(FakeOmnigentClient):
    """Models the stale-connection incident: a preamble streams, the elicitation
    is handled, then the SSE connection goes SILENT — the post-answer message
    never arrives on the stream (only the terminal idle does). The final answer
    lives solely in the server's latest_assistant_message, recovered by the
    no-delta fallback. Regression for a turn that hung + dropped the answer.
    """

    def __init__(self, event: dict[str, Any]) -> None:
        super().__init__(final_text="")
        self._event = event

    async def run_turn(
        self,
        session_id: str,
        text: str,
        *,
        workspace: str | None = None,
        host_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        self.turns.append((session_id, text))
        yield {"type": "response.output_text.delta", "delta": "Before deleting, let me look."}
        yield self._event
        # After the verdict resolves, the connection is stale — no post-answer
        # event arrives, only the eventual terminal idle. The answer is recovered
        # from the server snapshot (latest_message), not the stream.
        yield {"type": "session.status", "status": "idle"}


async def test_post_elicitation_answer_recovered_when_stream_silent(tmp_path: Path) -> None:
    # Incident: after an AskUserQuestion resolved, the server produced a final
    # message but the stale SSE connection never delivered it, so the turn hung
    # and the answer was dropped. The turn must end (via the idle status poll)
    # and recover the committed final message from the snapshot — exactly once.
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = PreambleThenSilentAfterElicitationClient(_form_elicitation_event())
    # The server's newest assistant message is the answer that never streamed.
    # Leaving the id unpinned gives each snapshot a fresh id, so the post-turn
    # final message is correctly seen as newer than the pre-turn baseline.
    omnigent.latest_message = "Understood — leaving the file in place."
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> demo"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    card = await _wait_for_card(slack)
    eid = _card_elicitation_id(card)
    await service.handle_elicitation_action(
        elicitation_id=eid, verdict=Verdict(accepted=True, content={"store": "A"})
    )
    await _wait_for_turn_end(slack)
    await service.shutdown()

    # The final answer was recovered and delivered exactly once; the turn task
    # finished (no lingering in-flight turn), so follow-ups aren't wedged.
    delivered = [s for s in slack.streams if "Understood — leaving the file in place." in s.text]
    assert len(delivered) == 1
    assert service._turn_tasks == set()  # type: ignore[attr-defined]


async def test_elicitation_clears_working_placeholder(tmp_path: Path) -> None:
    # Parking on an elicitation must drop the "Working on it…" ack so it doesn't
    # sit stale above the card for the whole (possibly long) wait.
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    # No preamble text before the elicitation, so only the ack could be showing.
    omnigent = ApprovalClient(elicitation_id="elicit_1")
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> edit"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    card = await _wait_for_card(slack)
    # By the time the card is up, the ack has been deleted (not left dangling).
    assert slack.acks, "expected an ack to have been posted"
    assert all(a["ts"] in slack.deleted_ts for a in slack.acks)
    eid = _card_elicitation_id(card)
    await service.handle_elicitation_action(elicitation_id=eid, verdict=Verdict(accepted=True))
    await _wait_for_resolved(omnigent)
    await service.shutdown()


# ── Stream enhancements: reasoning, policy-deny, files, todos ─────────


class EventScriptClient(FakeOmnigentClient):
    """Streams a fixed list of events, then settles idle.

    Lets a test assert how the service surfaces reasoning / policy-deny /
    output-file / todo events without a real server.
    """

    def __init__(self, events: list[dict[str, Any]]) -> None:
        super().__init__(final_text="")
        self._events = events

    async def run_turn(
        self,
        session_id: str,
        text: str,
        *,
        workspace: str | None = None,
        host_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        self.turns.append((session_id, text))
        for event in self._events:
            yield event
        yield {"type": "session.status", "status": "idle"}


async def _wait_for_turn_end(slack: FakeSlackClient) -> None:
    """Wait until the turn finished: its final stream segment is stopped.

    An interruption seal splits the answer, so "any stream stopped" is not a
    completion signal. The turn ends only once its last-opened segment stops
    with no further append pending, which is stable once the loop settles.
    """
    for _ in range(100):
        if slack.streams and all(s.stopped for s in slack.streams):
            # Give the loop a beat to open a follow-on segment if more is coming.
            await asyncio.sleep(0.02)
            if slack.streams and all(s.stopped for s in slack.streams):
                return
        await asyncio.sleep(0.02)
    raise AssertionError("Timed out waiting for the turn to end")


async def _run_scripted_turn(tmp_path: Path, events: list[dict[str, Any]]) -> "FakeSlackClient":
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    service, _pool, _setup = _service(store, EventScriptClient(events))
    await _configure_user(store, "T1", "U1")
    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> go"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_turn_end(slack)
    await service.shutdown()
    return slack


async def test_policy_denied_is_posted_as_reply(tmp_path: Path) -> None:
    slack = await _run_scripted_turn(
        tmp_path,
        [
            {"type": "response.output_text.delta", "delta": "ok"},
            {"type": "response.policy_denied", "conversation_id": "conv_1", "reason": "No rm."},
        ],
    )
    denials = [p for p in slack.posts if "Blocked by policy" in str(p.get("text"))]
    assert denials and "No rm." in denials[0]["text"]


async def test_output_file_is_posted_as_reply(tmp_path: Path) -> None:
    slack = await _run_scripted_turn(
        tmp_path,
        [{"type": "response.output_file.done", "file_id": "file_1", "filename": "out.csv"}],
    )
    files = [p for p in slack.posts if "Produced a file" in str(p.get("text"))]
    assert files and "out.csv" in files[0]["text"]


async def test_answer_then_trailing_notice_is_not_duplicated(tmp_path: Path) -> None:
    # Regression: an answer streams, THEN a trailing out-of-band notice (a
    # produced file) seals the segment. The seal resets the per-segment text, so
    # the end-of-turn no-delta fallback would look "empty" and re-fetch the
    # server's latest message — re-posting the answer a second time. The
    # turn-level "delivered anything" guard must suppress that.
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    client = EventScriptClient(
        [
            {"type": "response.output_text.delta", "delta": "The full answer."},
            {"type": "response.output_file.done", "file_id": "f1", "filename": "out.csv"},
        ]
    )
    # The server committed the streamed answer as its newest assistant message —
    # exactly what the (buggy) fallback would resurrect.
    client.latest_message = "The full answer."
    service, _pool, _setup = _service(store, client)
    await _configure_user(store, "T1", "U1")
    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> go"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_turn_end(slack)
    await service.shutdown()

    # The answer appears exactly once across all stream segments — not duplicated
    # into a fresh post-notice segment by the fallback.
    answer_segments = [s for s in slack.streams if "The full answer." in s.text]
    assert len(answer_segments) == 1


async def test_todos_posted_once_then_updated_in_place(tmp_path: Path) -> None:
    slack = await _run_scripted_turn(
        tmp_path,
        [
            {
                "type": "session.todos",
                "conversation_id": "conv_1",
                "todos": [{"content": "Step 1", "status": "in_progress", "activeForm": "Doing 1"}],
            },
            {
                "type": "session.todos",
                "conversation_id": "conv_1",
                "todos": [{"content": "Step 1", "status": "completed", "activeForm": "Doing 1"}],
            },
        ],
    )
    plan_posts = [p for p in slack.posts if str(p.get("text", "")).startswith("*Plan*")]
    plan_updates = [u for u in slack.updates if str(u.get("text", "")).startswith("*Plan*")]
    # One message posted, then edited in place for the second update.
    assert len(plan_posts) == 1
    assert len(plan_updates) == 1
    assert ":white_check_mark: Step 1" in plan_updates[-1]["text"]


async def test_interruption_preserves_chronological_order(tmp_path: Path) -> None:
    # Text before an out-of-band notice, the notice, then text after it must
    # appear in that order in the thread. The bot seals the streaming segment at
    # the notice so the answer doesn't stay anchored to its open-time timestamp
    # and float above the notice it depends on.
    slack = await _run_scripted_turn(
        tmp_path,
        [
            {"type": "response.output_text.delta", "delta": "before"},
            {"type": "response.policy_denied", "conversation_id": "conv_1", "reason": "No rm."},
            {"type": "response.output_text.delta", "delta": "after"},
        ],
    )
    # Two answer segments straddling the deny post.
    assert len(slack.streams) == 2
    assert slack.streams[0].text == "before"
    assert slack.streams[1].text == "after"
    deny = next(p for p in slack.posts if "Blocked by policy" in str(p.get("text")))
    # Chronological: segment-1 opened, then the deny posted, then segment-2 opened.
    assert slack.streams[0].open_order < deny["order"] < slack.streams[1].open_order
