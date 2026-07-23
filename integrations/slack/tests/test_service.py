import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import omnigent_slack.service as service_module
import pytest
from omnigent_slack.approvals import Verdict, parse_action_value
from omnigent_slack.models import ThreadKey, UserConfig
from omnigent_slack.omnigent import (
    AuthRequiredError,
    HarnessNotConfiguredError,
    HostUnavailableError,
    OmnigentError,
    ServerUnreachableError,
    StreamInterruptedError,
)
from omnigent_slack.service import (
    _ACK_TEXT,
    _SERVER_UNREACHABLE_TEXT,
    _STREAM_INTERRUPTED_TEXT,
    SlackOmnigentService,
)
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
        close_error: str = "message_not_in_streaming_state",
    ) -> None:
        self._client = client
        self.start_kwargs = start_kwargs
        self.appended: list[str] = []
        self.stopped = False
        self.stop_text: str | None = None
        # Which Slack error code a closed stream raises. Slack uses
        # ``message_not_in_streaming_state`` for a finalized message and
        # ``message_not_found`` for one old enough to be gone — both must trigger
        # the reopen path.
        self._close_error = close_error
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
                data={"ok": False, "error": self._close_error},
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
        # DM channels opened via conversations_open (users= payloads).
        self.dm_opens: list[dict[str, Any]] = []
        self.streams: list[FakeStream] = []
        self._next_ts = 0
        self._order = 0
        # When set, every stream this client opens auto-closes after this many
        # appended deltas — simulating Slack finalizing the message mid-turn.
        self.stream_close_after: int | None = None
        # The Slack error code a closed stream raises (see FakeStream).
        self.stream_close_error: str = "message_not_in_streaming_state"

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

    async def conversations_open(self, **kwargs: Any) -> dict[str, Any]:
        # Record who we DM'd and hand back a stable DM channel id. A DM message is
        # then a normal chat_postMessage to that channel.
        self.dm_opens.append({**kwargs})
        user = kwargs.get("users")
        return {"ok": True, "channel": {"id": f"D-{user}"}}

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

    async def chat_getPermalink(self, **kwargs: Any) -> dict[str, Any]:
        channel = kwargs.get("channel")
        ts = kwargs.get("message_ts")
        return {"ok": True, "permalink": f"https://slack.test/archives/{channel}/p{ts}"}

    async def chat_stream(self, **kwargs: Any) -> FakeStream:
        # Only the first stream auto-closes (Slack finalizes the idle message);
        # the continuation the bot opens streams fresh, mirroring reality.
        close_after = self.stream_close_after if not self.streams else None
        stream = FakeStream(
            self, kwargs, close_after=close_after, close_error=self.stream_close_error
        )
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
        # Newest assistant message the server would return, for the no-delta
        # fallback. ``latest_message_id`` pins the id (else each call gets a
        # fresh id, so the fallback treats it as new relative to the baseline).
        self.latest_message: str | None = None
        self.latest_message_id: str | None = None
        self._latest_calls = 0
        # Fires when the bot POSTs a verdict via resolve_elicitation — lets a
        # fixture generator wait for the answer before emitting the server's
        # elicitation_resolved + continuation (the pure-push model).
        self.resolve_signal = asyncio.Event()
        # Server activity reported at ROUTE time (before a turn) — the gate that
        # decides whether a new message runs or is deflected. Defaults to free
        # (idle, no pending) so a follow-up runs; a test sets these to simulate a
        # busy or awaiting-input session. Kept separate from ``status`` (which the
        # in-turn grace window polls) so the two don't collide.
        self.route_status: str | None = "idle"
        self.route_pending_elicitation = False
        # Server-authoritative harness/agent for the first-message config summary.
        self.info_harness: str | None = "claude-native"
        self.info_agent_name: str | None = "debby"

    async def get_session_activity(self, session_id: str) -> Any:
        from omnigent_slack.omnigent import SessionActivity

        return SessionActivity(
            status=self.route_status, pending_elicitation=self.route_pending_elicitation
        )

    async def get_session_info(self, session_id: str) -> Any:
        from omnigent_slack.omnigent import SessionInfo

        return SessionInfo(harness=self.info_harness, agent_name=self.info_agent_name)

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
        self.resolve_signal.set()


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
        self.relogin_prompted: list[dict[str, Any]] = []

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

    async def prompt_relogin(
        self,
        client: Any,
        user_id: str,
        *,
        channel: str,
        thread_ts: str | None,
        in_channel: bool,
    ) -> bool:
        self.relogin_prompted.append(
            {
                "user_id": user_id,
                "channel": channel,
                "thread_ts": thread_ts,
                "in_channel": in_channel,
            }
        )
        return True


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
    # Session title is "Slack: <thread permalink>" (a clickable URL the web UI
    # linkifies), not the old opaque "Slack C…/ts" descriptor.
    assert omnigent.created[0][1] == "Slack: https://slack.test/archives/C1/p100.1"
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
    # A new session posts one durable config-summary message (agent / harness /
    # workspace + web-UI link) as the first thread message.
    assert len(slack.posts) == 1
    info_text = slack.posts[0]["text"]
    assert "debby" in info_text  # agent name
    assert "claude-native" in info_text  # harness
    assert "/c/conv_1|Open in Omnigent>" in info_text  # web-UI link
    # The config summary comes FIRST, then the "Working on it…" ack: the thread
    # reads metadata → ack → answer.
    assert slack.posts[0]["order"] < slack.acks[0]["order"]
    # The placeholder stayed up until the streamed message was actually on
    # screen. This short answer buffers in the SDK and only becomes visible at
    # stop(); the ack was still live then and is deleted only afterwards, so the
    # thread is never empty while waiting for content.
    assert stream.ack_live_when_visible is True


async def test_failed_handle_unclaims_event_so_it_can_retry(tmp_path: Path) -> None:
    # Regression: the event is claimed (dedup) and Bolt auto-acks before the turn
    # runs, so Slack won't redeliver. If handling then fails, the claim must be
    # released — otherwise the user's message is permanently swallowed and even a
    # re-send with the same id is deduped away.
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    boom = RuntimeError("DB hiccup mid-route")
    original_get_session = store.get_session
    fail_next = {"on": True}

    async def _flaky_get_session(key: ThreadKey):  # type: ignore[no-untyped-def]
        if fail_next["on"]:
            raise boom
        return await original_get_session(key)

    store.get_session = _flaky_get_session  # type: ignore[method-assign]

    args: dict[str, Any] = {
        "body": {"team_id": "T1", "event_id": "Ev1"},
        "event": {"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> hello"},
        "client": slack,
        "context": {"bot_user_id": "B1"},
    }
    # The handler propagates the failure (Bolt's error handler logs it)...
    with pytest.raises(RuntimeError):
        await service.handle_app_mention(**args)  # type: ignore[arg-type]

    # ...and the event was unclaimed, so the SAME id is processable again.
    fail_next["on"] = False
    store.get_session = original_get_session  # type: ignore[method-assign]
    await service.handle_app_mention(**args)  # type: ignore[arg-type]
    stream = await _wait_for_stream_stop(slack)
    await service.shutdown()

    # The retry actually ran the turn (it wasn't deduped away).
    assert omnigent.turns == [("conv_1", "hello")]
    assert stream.text == "hello final"


async def test_session_title_falls_back_when_permalink_unavailable(tmp_path: Path) -> None:
    # The title lookup is cosmetic and must never block session start: if
    # chat.getPermalink fails (e.g. a missing scope), fall back to a plain
    # channel/ts descriptor and still create the session.
    store = await _store(tmp_path)

    class NoPermalinkSlack(FakeSlackClient):
        async def chat_getPermalink(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("missing scope")

    slack = NoPermalinkSlack()
    omnigent = FakeOmnigentClient()
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

    assert len(omnigent.created) == 1
    assert omnigent.created[0][1] == "Slack thread C1/100.1"


async def test_session_info_omits_missing_fields(tmp_path: Path) -> None:
    # The config summary degrades gracefully when the snapshot omits harness /
    # agent (unreadable or older session) — no "None", no crash.
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    omnigent.info_harness = None
    omnigent.info_agent_name = None
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

    info_text = slack.posts[0]["text"]
    assert "None" not in info_text
    assert "/c/conv_1|Open in Omnigent>" in info_text  # link still present


async def test_no_ack_when_session_cannot_start_host_unavailable(tmp_path: Path) -> None:
    # The "Working on it…" placeholder is posted only after the session is
    # established, so a failed start shows just the guidance — no placeholder
    # flicker to clear.
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

    assert slack.acks == []
    # The only durable post is the guidance.
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
    ``session.status: idle`` (not ``response.completed``). The ack must stay live
    until the buffered answer is on screen.
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


class MultiMessageClient(FakeOmnigentClient):
    """Streams two assistant messages back to back, each tagged with its own
    ``message_id`` — the claude-native shape when the agent narrates between tool
    calls. The deltas arrive with no boundary between the two messages.
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
        yield {"type": "session.status", "status": "running", "response_id": "resp_1"}
        yield {
            "type": "response.output_text.delta",
            "delta": "Let me poll once more.",
            "message_id": "msg_a",
        }
        # A tool call runs between the two messages; the next delta belongs to a
        # NEW assistant message (distinct message_id).
        yield {
            "type": "response.output_text.delta",
            "delta": "The credentials agent is taking longer.",
            "message_id": "msg_b",
        }
        yield {"type": "session.status", "status": "idle", "response_id": "resp_1"}


async def test_back_to_back_messages_get_paragraph_break(tmp_path: Path) -> None:
    # Regression: consecutive assistant messages (distinct message_id) must not
    # run together ("…once more.The credentials…"). A paragraph break is inserted
    # at the id boundary so each message reads as its own block, mirroring the web
    # UI's separate bubbles.
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = MultiMessageClient(final_text="")
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> status?"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_stream_stop(slack)
    await service.shutdown()

    # The two messages are separated by a blank line, not concatenated.
    assert (
        slack.streamed_text == "Let me poll once more.\n\nThe credentials agent is taking longer."
    )


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
    # overflow chat.postMessage replies — the only durable post is the session
    # config summary, which never carries answer text.
    assert stream.text == long_answer
    assert all(long_answer not in str(p.get("text", "")) for p in slack.posts)


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
    # The failure is a separate reply in the same thread — a GENERIC message; the
    # raw in-band detail ("boom", which could be a stack trace) is NEVER echoed.
    failure_posts = [p for p in slack.posts if "went wrong" in str(p.get("text", ""))]
    assert len(failure_posts) == 1
    assert "boom" not in failure_posts[0]["text"]
    assert failure_posts[0]["thread_ts"] == "100.1"


async def test_turn_error_without_answer_finalizes_with_generic_message(tmp_path: Path) -> None:
    """When nothing streamed, a GENERIC failure surfaces as the stream's final text.

    The raw in-band error ("boom" — which could be a stack trace / internal path)
    is logged server-side but NEVER echoed to the channel (DESIGN.md).
    """
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
                "response": {"error": {"message": "boom /secret/internal/path"}},
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

    # The generic failure is shown; the raw detail is NOT leaked to the channel.
    assert "went wrong" in (stream.stop_text or "")
    assert "boom" not in (stream.stop_text or "")
    assert "/secret/internal/path" not in (stream.stop_text or "")


async def test_exhausted_reconnect_shows_non_alarming_text(tmp_path: Path) -> None:
    # A mid-stream drop whose reconnects are exhausted surfaces as
    # StreamInterruptedError. The server stayed reachable, so the user must NOT be
    # told to reconfigure — they get the "lost the live connection" notice, and
    # the turn's result may still land in the thread.
    store = await _store(tmp_path)
    slack = FakeSlackClient()

    class StreamInterruptedClient(FakeOmnigentClient):
        async def run_turn(
            self,
            session_id: str,
            text: str,
            *,
            workspace: str | None = None,
            host_id: str | None = None,
        ) -> AsyncIterator[dict[str, Any]]:
            self.turns.append((session_id, text))
            raise StreamInterruptedError("stream dropped mid-turn")
            yield  # pragma: no cover -- makes this an async generator

    omnigent = StreamInterruptedClient()
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

    # The notice is a public post (not ephemeral): the non-alarming stream-drop
    # text, never the "server unreachable / reconfigure" guidance.
    text = slack.posts[-1]["text"]
    assert text == _STREAM_INTERRUPTED_TEXT
    assert text != _SERVER_UNREACHABLE_TEXT
    assert "reconfigure" not in text


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
    # The continuation streamed in the same thread; no static catch-up reply
    # (the answer text never appears in a durable post — only the config summary).
    assert slack.streams[-1].start_kwargs["thread_ts"] == "100.1"
    assert all("chunk-a" not in str(p.get("text", "")) for p in slack.posts)


async def test_stream_message_not_found_also_reopens(tmp_path: Path) -> None:
    # Regression: a turn that outlives repeated proxy stream-drop reconnects can be
    # so old that Slack reports the streaming message as ``message_not_found`` (not
    # just ``message_not_in_streaming_state``). Both mean "the stream is dead —
    # reopen"; a mishandled ``message_not_found`` previously surfaced as a generic
    # turn failure instead of continuing in a fresh reply.
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    slack.stream_close_after = 1
    slack.stream_close_error = "message_not_found"
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

    # Reopened into a fresh stream and delivered the full answer — no generic
    # failure post.
    assert len(slack.streams) >= 2
    assert slack.streamed_text == "chunk-a" + "y" * 600
    assert all("went wrong" not in str(p.get("text", "")) for p in slack.posts)


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
    # The failure is its own clean, GENERIC reply — the raw in-band detail is not
    # echoed to the channel.
    failure_posts = [p for p in slack.posts if "went wrong" in str(p.get("text", ""))]
    assert len(failure_posts) == 1
    assert "boom" not in failure_posts[0]["text"]


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

    stream = await _run_dm_and_stop(service, slack)
    await service.shutdown()

    assert len(omnigent.created) == 1
    assert omnigent.created[0][0] == "ag_1"
    assert omnigent.bound == ["conv_1"]
    assert omnigent.turns == [("conv_1", "hello there")]
    # A DM keys its session PER THREAD (like a channel): this top-level message
    # keys on its own ts, starting a new thread/session.
    record = await store.get_session(ThreadKey("T1", "D1", "100.1"))
    assert record is not None and record.session_id == "conv_1"
    # The reply threads under the triggering message.
    assert stream.start_kwargs["thread_ts"] == "100.1"


async def _run_dm_and_stop(service: Any, slack: "FakeSlackClient") -> "FakeStream":
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
    return await _wait_for_stream_stop(slack)


async def test_direct_message_threaded_reply_reuses_existing_session(tmp_path: Path) -> None:
    # A DM maps one session PER THREAD (like a channel): a reply carrying the
    # thread's root ts reuses that thread's session rather than creating a new one.
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
            "thread_ts": "100.1",  # a reply under the existing thread root
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


async def test_direct_message_top_level_starts_new_session_per_thread(tmp_path: Path) -> None:
    # A DM is NOT one standing session per channel: a bare top-level DM (its own
    # ts, no thread_ts) starts a NEW thread/session, keyed on its ts.
    store = await _store(tmp_path)
    # An unrelated prior DM thread exists on the same channel.
    await store.upsert_session(
        ThreadKey("T1", "D1", "099.9"), "conv_old", "title", owner_user_id="U1"
    )
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_message(
        body={"team_id": "T1", "event_id": "Ev2"},
        event={
            "channel": "D1",
            "channel_type": "im",
            "ts": "101.1",  # top-level, no thread_ts
            "user": "U1",
            "text": "brand new topic",
        },
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_stream_stop(slack)
    await service.shutdown()

    # A new session was created for the new thread, keyed on the message ts.
    assert omnigent.turns == [("conv_1", "brand new topic")]
    record = await store.get_session(ThreadKey("T1", "D1", "101.1"))
    assert record is not None and record.session_id == "conv_1"


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


async def test_message_while_parked_in_process_points_to_pending_request(tmp_path: Path) -> None:
    # Regression: a turn parked on a pending elicitation is STILL streaming, so it
    # holds the in-process reservation — a new message hits the _active_threads
    # branch, not the server-activity one. That branch must still surface the
    # "respond to the pending request above" notice (needs_action=True), NOT the
    # generic "still working" one, by consulting the server's activity.
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = ApprovalClient()
    # While parked, the server reports the session needs user action.
    omnigent.route_pending_elicitation = True
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    # First mention parks on the approval card (the turn keeps streaming).
    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> edit"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_card(slack)

    # A second mention in the same thread WHILE the card is pending in-process.
    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev2"},
        event={
            "channel": "C1",
            "thread_ts": "100.1",
            "ts": "101.1",
            "user": "U1",
            "text": "<@B1> another",
        },
        client=slack,
        context={"bot_user_id": "B1"},
    )

    # It got the pending-request notice, not the generic "still working" one.
    notices = [e for e in slack.ephemerals if "waiting on your response" in e["text"].lower()]
    assert len(notices) == 1
    assert notices[0]["user"] == "U1"
    assert not any("still working" in e["text"].lower() for e in slack.ephemerals)

    # Tear down with the card still parked (shutdown cancels the resolver).
    await service.shutdown()


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


async def test_non_owner_reply_rejected_without_session_record(tmp_path: Path) -> None:
    # Security regression guard: even with NO stored session (e.g. the ephemeral
    # store was wiped by a restart), a reply into another user's thread must be
    # refused. Slack's ``parent_user_id`` (the thread root author) is authoritative
    # and survives restarts, so the non-owner is turned away rather than silently
    # granted a fresh session in someone else's thread.
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    service, _pool, setup = _service(store, omnigent)
    # U2 is configured, so the only thing stopping them is the ownership gate.
    await _configure_user(store, "T1", "U2")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={
            "channel": "C1",
            "thread_ts": "100.1",
            "ts": "101.1",
            "user": "U2",
            "parent_user_id": "U1",  # thread was started by U1
            "text": "<@B1> jumping into U1's thread",
        },
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await service.shutdown()

    # No session was created for U2, no setup prompt — just the private notice.
    assert omnigent.turns == []
    assert omnigent.created == []
    assert setup.prompted == []
    assert slack.posts == []
    assert len(slack.ephemerals) == 1
    assert slack.ephemerals[0]["user"] == "U2"
    assert "start a new thread" in slack.ephemerals[0]["text"].lower()


async def test_owner_reply_in_own_thread_is_allowed(tmp_path: Path) -> None:
    # The owner replying in their OWN thread (parent_user_id == requester) passes
    # the ownership gate and runs, even after a store wipe (no session record).
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = FakeOmnigentClient()
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={
            "channel": "C1",
            "thread_ts": "100.1",
            "ts": "101.1",
            "user": "U1",
            "parent_user_id": "U1",  # U1 owns this thread
            "text": "<@B1> continuing my own thread",
        },
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_stream_stop(slack)
    await service.shutdown()

    # The owner's turn ran (a fresh session, since the store had no record).
    assert len(omnigent.created) == 1
    assert slack.ephemerals == []


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


async def test_auth_required_prompts_relogin(tmp_path: Path) -> None:
    # A user with saved config but an expired/lost token is told to log in
    # again — via the setup flow's DM re-login prompt (a reliably-delivered,
    # actionable button), NOT a plain thread notice a user may never see. The ack
    # posts only after the session starts, so a failed start leaves no
    # "Working on it…" behind.
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = AuthRequiredClient()
    service, _pool, setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> hi"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    for _ in range(50):
        if setup.relogin_prompted:
            break
        await asyncio.sleep(0.02)
    await service.shutdown()

    # No placeholder was posted (session never started).
    assert slack.acks == []
    # No session persisted.
    assert await store.get_session(ThreadKey("T1", "C1", "100.1")) is None
    # The re-login DM prompt was triggered for the owner (not a plain thread post).
    assert slack.posts == []
    assert len(setup.relogin_prompted) == 1
    prompt = setup.relogin_prompted[-1]
    assert prompt["user_id"] == "U1"
    assert prompt["channel"] == "C1"
    assert prompt["in_channel"] is True


async def test_auth_required_mid_stream_prompts_relogin(tmp_path: Path) -> None:
    # The token expires DURING the turn (the stream raises AuthRequiredError), not
    # at startup. This must also route to the DM re-login prompt — not a raw error
    # or a stranded "Working on it…".
    store = await _store(tmp_path)
    slack = FakeSlackClient()

    class AuthMidStreamClient(FakeOmnigentClient):
        async def run_turn(
            self,
            session_id: str,
            text: str,
            *,
            workspace: str | None = None,
            host_id: str | None = None,
        ) -> AsyncIterator[dict[str, Any]]:
            self.turns.append((session_id, text))
            raise AuthRequiredError("401 mid-stream")
            yield  # pragma: no cover -- makes this an async generator

    omnigent = AuthMidStreamClient()
    service, _pool, setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> hi"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    for _ in range(50):
        if setup.relogin_prompted:
            break
        await asyncio.sleep(0.02)
    await service.shutdown()

    # The mid-stream auth failure routed to the re-login DM prompt.
    assert len(setup.relogin_prompted) == 1
    assert setup.relogin_prompted[-1]["user_id"] == "U1"
    # No raw error text leaked to the thread.
    assert all("401" not in str(p.get("text", "")) for p in slack.posts)


async def test_auth_required_in_dm_skips_in_channel_pointer(tmp_path: Path) -> None:
    # In a DM the re-login post lands in the same conversation, so the redundant
    # "check your DM" ephemeral pointer must NOT be posted (in_channel=False).
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = AuthRequiredClient()
    service, _pool, setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_message(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={
            "channel": "D1",
            "channel_type": "im",
            "ts": "100.1",
            "user": "U1",
            "text": "hi",
        },
        client=slack,
        context={"bot_user_id": "B1"},
    )
    for _ in range(50):
        if setup.relogin_prompted:
            break
        await asyncio.sleep(0.02)
    await service.shutdown()

    assert len(setup.relogin_prompted) == 1
    prompt = setup.relogin_prompted[-1]
    assert prompt["user_id"] == "U1"
    assert prompt["channel"] == "D1"
    # A DM already receives the re-login post directly — no channel pointer.
    assert prompt["in_channel"] is False


async def test_server_error_creating_session_reports(tmp_path: Path) -> None:
    # A 500 from create_session raises a bare OmnigentError (not one of the
    # specifically-handled subclasses). It must still post a failure and never
    # strand the thread. The ack posts only after the session starts, so a
    # failed start leaves no placeholder to clear.
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

    # No placeholder was posted (session never started).
    assert slack.acks == []
    # A failure reply was posted, and no session was persisted.
    assert await store.get_session(ThreadKey("T1", "C1", "100.1")) is None
    text = slack.posts[-1]["text"]
    # A GENERIC startup-failure message — the raw OmnigentError detail (which can
    # carry a server status / internal string) is NOT echoed to the channel.
    assert "went wrong" in text.lower()
    assert "internal_error" not in text
    assert "500" not in text
    # A non-auth error is PUBLIC (affects everyone on the thread), unlike the
    # expired-login notice which is DM'd to the owner — no ephemeral here.
    assert slack.ephemerals == []


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


class HarnessNotConfiguredClient(FakeOmnigentClient):
    async def launch_runner(
        self, session_id: str, *, workspace: str, host_id: str | None = None
    ) -> str:
        raise HarnessNotConfiguredError(
            "host failed to launch runner: claude CLI not found; run omnigent setup"
        )


async def test_harness_not_configured_412_surfaces_server_message(tmp_path: Path) -> None:
    # A 412 on runner launch (harness not set up on the host) is actionable — the
    # server's message must reach the user so they know to run `omnigent setup`,
    # not a generic "request failed".
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = HarnessNotConfiguredClient()
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

    # Session was not persisted (startup failed) and the actionable message shows.
    assert await store.get_session(ThreadKey("T1", "C1", "100.1")) is None
    text = slack.posts[-1]["text"]
    assert "omnigent setup" in text
    assert "status 412" not in text  # not the generic fallback


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


async def _wait_any(*events: asyncio.Event) -> None:
    """Wait until any of ``events`` is set (with a safety timeout)."""
    waiters = [asyncio.ensure_future(e.wait()) for e in events]
    try:
        await asyncio.wait(waiters, timeout=5.0, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waiters:
            if not w.done():
                w.cancel()


class ApprovalClient(FakeOmnigentClient):
    """A turn that streams, parks on an elicitation, then streams a tail.

    Pure-push model: the generator yields the elicitation event, then WAITS for
    the verdict to be resolved — either the bot POSTs it (``resolve_signal``, a
    Slack click) or the test resolves it externally (``resolve_externally``).
    It then emits the server's ``response.elicitation_resolved`` push, streams
    the continuation, and ends on an id-bearing idle. This mirrors the real
    server holding the continuation until the elicitation is answered.
    """

    def __init__(
        self, elicitation_id: str = "elicit_1", event: dict[str, Any] | None = None
    ) -> None:
        super().__init__(final_text="done")
        self._elicitation_id = elicitation_id
        self._event = event or _elicitation_event(elicitation_id)
        # When set, the fixture emits elicitation_resolved WITHOUT waiting for the
        # bot to POST a verdict — models an answer in the web UI / another client.
        self.resolve_externally = asyncio.Event()

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
        # Keep the stream "open": wait until the elicitation is answered — either
        # the bot POSTs a verdict (Slack click) or the test resolves it elsewhere.
        await _wait_any(self.resolve_signal, self.resolve_externally)
        yield {
            "type": "response.elicitation_resolved",
            "elicitation_id": self._elicitation_id,
        }
        yield {"type": "response.output_text.delta", "delta": "ing"}
        yield {"type": "session.status", "status": "idle", "response_id": "resp_1"}


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
        # Wait for the answer, then the server pushes elicitation_resolved.
        await _wait_any(self.resolve_signal)
        yield {"type": "response.elicitation_resolved", "elicitation_id": "elicit_form"}
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
        yield {"type": "session.status", "status": "idle", "response_id": "resp_1"}


async def _wait_for_card(client: FakeSlackClient) -> dict[str, Any]:
    """Wait for the approval card (a post carrying an actions block)."""
    for _ in range(100):
        for post in client.posts:
            blocks = post.get("blocks") or []
            if any(b.get("type") == "actions" for b in blocks):
                return post
        await asyncio.sleep(0.02)
    raise AssertionError("Timed out waiting for an approval card")


def _card_target(card: dict[str, Any]) -> Any:
    for block in card.get("blocks", []):
        if block.get("type") == "actions":
            target = parse_action_value(block["elements"][0]["value"])
            assert target is not None
            return target
    raise AssertionError("Card has no actions block")


def _card_elicitation_id(card: dict[str, Any]) -> str:
    return str(_card_target(card).elicitation_id)


def _card_session_id(card: dict[str, Any]) -> str:
    return str(_card_target(card).session_id)


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
    sid = _card_session_id(card)
    delivered = await service.handle_elicitation_action(
        session_id=sid, elicitation_id=eid, verdict=Verdict(accepted=True)
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
    sid = _card_session_id(card)
    await service.handle_elicitation_action(
        session_id=sid, elicitation_id=eid, verdict=Verdict(accepted=True)
    )
    await _wait_for_resolved(omnigent)
    await service.shutdown()

    # The first (pre-card) segment carried "work" and was FORCE-flushed to screen
    # (via _LiveReply.flush) — not left buffered until the finalizing stop.
    pre_card = slack.streams[0]
    assert pre_card.text == "work"
    assert pre_card.forced_flush_order is not None, "pre-card text was not force-flushed"
    # The forced flush happened strictly before the card message was posted.
    assert pre_card.forced_flush_order < card["order"]


async def test_idle_stream_flushes_buffered_text_before_turn_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A short delta (well under the SDK buffer) followed by an idle gap must be
    # revealed while the stream is quiet — not held invisible until the turn
    # ends. The read loop detects the idle window and force-flushes it.
    monkeypatch.setattr(service_module, "_IDLE_FLUSH_SECONDS", 0.05)
    store = await _store(tmp_path)
    slack = FakeSlackClient()

    release = asyncio.Event()

    class IdleThenEndClient(FakeOmnigentClient):
        async def run_turn(
            self,
            session_id: str,
            text: str,
            *,
            workspace: str | None = None,
            host_id: str | None = None,
        ) -> AsyncIterator[dict[str, Any]]:
            self.turns.append((session_id, text))
            # A short burst that won't fill the SDK buffer, then go quiet.
            yield {"type": "response.output_text.delta", "delta": "partial answer"}
            # Stay open (no further events) until the test has verified the
            # buffered text was flushed during the idle window.
            await _wait_any(release)
            yield {"type": "session.status", "status": "idle", "response_id": "resp_1"}

    omnigent = IdleThenEndClient(final_text="partial answer")
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> go"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    # Before the turn ends, the idle window should have force-flushed the buffered
    # "partial answer" onto the screen.
    for _ in range(100):
        if slack.streams and slack.streams[0].forced_flush_order is not None:
            break
        await asyncio.sleep(0.02)
    else:
        release.set()
        await service.shutdown()
        raise AssertionError("buffered text was not flushed during the idle window")

    # It was revealed WHILE the turn was still open (before we let it end).
    assert slack.streams[0].text == "partial answer"

    release.set()
    await _wait_for_stream_stop(slack)
    await service.shutdown()


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
    sid = _card_session_id(card)
    await service.handle_elicitation_action(
        session_id=sid, elicitation_id=eid, verdict=Verdict(accepted=False)
    )
    await _wait_for_resolved(omnigent)
    await service.shutdown()

    assert omnigent.resolved == [("conv_1", "elicit_1", False)]
    assert "Denied" in slack.updates[-1]["blocks"][0]["text"]["text"]


async def test_elicitation_resolved_externally_finalizes_without_posting(tmp_path: Path) -> None:
    # Pure-push: the user answers in the web UI (not the Slack card). The loop
    # keeps reading and sees response.elicitation_resolved; it must finalize the
    # card ("Answered elsewhere") WITHOUT posting its own verdict, and the
    # continuation must still stream.
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
    await _wait_for_card(slack)
    # No Slack click — resolve elsewhere; the fixture then emits the push.
    omnigent.resolve_externally.set()
    await _wait_for_turn_end(slack)
    await service.shutdown()

    # We posted NO verdict (answered elsewhere), the card shows the neutral
    # outcome, and the continuation ("ing") streamed after the card.
    assert omnigent.resolved == []
    assert "Answered elsewhere" in slack.updates[-1]["blocks"][0]["text"]["text"]
    assert any("ing" in s.text for s in slack.streams)


async def test_abandoned_elicitation_at_turn_end_is_declined(tmp_path: Path) -> None:
    # A card left open when the turn is torn down (here: shutdown mid-park) was
    # never answered and the server is still parked on it. finish_pending must
    # DECLINE it (so the server park releases) and label it "Not answered", NOT
    # mislabel it "Answered elsewhere" (nothing answered it).
    store = await _store(tmp_path)
    slack = FakeSlackClient()
    omnigent = ApprovalClient()
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    # Neither a Slack click nor an external resolve — the card just sits open.
    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> edit"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    await _wait_for_card(slack)
    # Tear the turn down with the card still parked (mirrors a process shutdown).
    await service.shutdown()

    # The abandoned request was declined server-side (accepted=False), and the
    # card shows the abandonment label with a retry hint — not "Answered elsewhere".
    assert omnigent.resolved == [("conv_1", "elicit_1", False)]
    assert slack.updates
    card_text = slack.updates[-1]["blocks"][0]["text"]["text"]
    assert "Not answered" in card_text
    assert "Answered elsewhere" not in card_text


async def test_elicitation_card_post_failure_declines_and_unregisters(tmp_path: Path) -> None:
    # If posting the approval card fails, the coordinator waiter must not be
    # orphaned and the server must not stay parked: the request is declined
    # server-side and no pending entry is left behind.
    store = await _store(tmp_path)

    class CardPostFailsClient(FakeSlackClient):
        async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
            # The elicitation card carries an actions block — fail only that post,
            # so acks/summaries still work and we isolate the card-post failure.
            if any(b.get("type") == "actions" for b in (kwargs.get("blocks") or [])):
                raise SlackApiError(
                    "card post failed", {"ok": False, "error": "channel_not_found"}
                )
            return await super().chat_postMessage(**kwargs)

    slack = CardPostFailsClient()
    omnigent = ApprovalClient()
    service, _pool, _setup = _service(store, omnigent)
    await _configure_user(store, "T1", "U1")

    await service.handle_app_mention(
        body={"team_id": "T1", "event_id": "Ev1"},
        event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> edit"},
        client=slack,
        context={"bot_user_id": "B1"},
    )
    # The turn's ApprovalClient waits for a resolve; the card-post failure declines
    # it, which unblocks the stream and ends the turn without the 600s backstop.
    await _wait_for_resolved(omnigent)
    await service.shutdown()

    # The request was declined server-side so the park releases (no orphaned wait).
    assert omnigent.resolved == [("conv_1", "elicit_1", False)]
    # No live coordinator waiter left behind.
    assert service.elicitations._pending == {}  # type: ignore[attr-defined]


async def test_verdict_post_failure_shows_delivery_failed_not_approved(tmp_path: Path) -> None:
    # A Slack click whose resolve_elicitation POST raises must NOT label the card
    # "Approved" — the server never received the verdict and is still parked. The
    # card shows the delivery-failure notice instead, and no verdict is recorded.
    store = await _store(tmp_path)
    slack = FakeSlackClient()

    class FailingResolveClient(ApprovalClient):
        async def resolve_elicitation(
            self,
            session_id: str,
            elicitation_id: str,
            *,
            accepted: bool,
            content: dict[str, Any] | None = None,
        ) -> None:
            # The click reached us, but delivering it to the server fails.
            raise ServerUnreachableError("verdict POST failed")

    omnigent = FailingResolveClient()
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
    sid = _card_session_id(card)
    await service.handle_elicitation_action(
        session_id=sid, elicitation_id=eid, verdict=Verdict(accepted=True)
    )
    # The turn never gets the push (the server never received the verdict); the
    # card is finalized at turn teardown with the delivery-failure outcome.
    await service.shutdown()

    assert slack.updates
    card_text = slack.updates[-1]["blocks"][0]["text"]["text"]
    assert "Couldn't be delivered" in card_text
    assert "Approved" not in card_text


async def test_delivery_failed_releases_server_park_at_turn_end(tmp_path: Path) -> None:
    # Regression: a failed verdict POST leaves the server parked on the
    # elicitation. finish_pending must still DECLINE it (release the park) so the
    # session isn't wedged, even though the card is labelled "delivery failed".
    store = await _store(tmp_path)
    slack = FakeSlackClient()

    class RecordingResolveClient(ApprovalClient):
        def __init__(self) -> None:
            super().__init__()
            self.resolve_calls: list[tuple[str, bool]] = []

        async def resolve_elicitation(
            self,
            session_id: str,
            elicitation_id: str,
            *,
            accepted: bool,
            content: dict[str, Any] | None = None,
        ) -> None:
            self.resolve_calls.append((elicitation_id, accepted))
            # The verdict delivery (accepted=True) fails; the later park-release
            # decline (accepted=False) is allowed to succeed.
            if accepted:
                raise ServerUnreachableError("verdict POST failed")

    omnigent = RecordingResolveClient()
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
    sid = _card_session_id(card)
    await service.handle_elicitation_action(
        session_id=sid, elicitation_id=eid, verdict=Verdict(accepted=True)
    )
    await service.shutdown()

    # Two calls: the failed verdict delivery (accepted=True), then the park-release
    # decline (accepted=False) at turn end so the server isn't left wedged.
    assert (eid, True) in omnigent.resolve_calls
    assert (eid, False) in omnigent.resolve_calls
    # Card still shows the delivery-failure label, not "Approved".
    card_text = slack.updates[-1]["blocks"][0]["text"]["text"]
    assert "Couldn't be delivered" in card_text


async def test_resolver_tasks_are_cancelled_on_shutdown(tmp_path: Path) -> None:
    # A resolver awaiting a click that never comes must be cancelled on shutdown,
    # not orphaned ("Task was destroyed but it is pending").
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
    await _wait_for_card(slack)
    # A resolver is live and parked on the (never-arriving) click.
    assert len(service._elicitation._resolvers) == 1  # type: ignore[attr-defined]
    resolver = next(iter(service._elicitation._resolvers))  # type: ignore[attr-defined]

    await service.shutdown()

    # The resolver was cancelled/finished and dropped from the tracking set.
    assert resolver.done()
    assert service._elicitation._resolvers == set()  # type: ignore[attr-defined]


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
            # Only a gated tool call, no answer text. Park until the deny is
            # posted, then the server resolves and ends the turn — no answer.
            yield _elicitation_event("elicit_rm")
            await _wait_any(self.resolve_signal)
            yield {"type": "response.elicitation_resolved", "elicitation_id": "elicit_rm"}
            yield {"type": "session.status", "status": "idle", "response_id": "resp_1"}

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
    sid = _card_session_id(card)
    await service.handle_elicitation_action(
        session_id=sid, elicitation_id=eid, verdict=Verdict(accepted=False)
    )
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
        session_id="sess_gone", elicitation_id="elicit_gone", verdict=Verdict(accepted=True)
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
    sid = _card_session_id(card)
    # Answers arrive as option indices ("Redis" is index 0); the service maps
    # them back to the full labels before forwarding to the server.
    await service.handle_elicitation_action(
        session_id=sid, elicitation_id=eid, verdict=Verdict(accepted=True, content={"store": "0"})
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
    sid = _card_session_id(card)
    await service.handle_elicitation_action(
        session_id=sid, elicitation_id=eid, verdict=Verdict(accepted=True)
    )
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
    sid = _card_session_id(card)
    await service.handle_elicitation_action(
        session_id=sid, elicitation_id=eid, verdict=Verdict(accepted=True, content={"store": "A"})
    )
    await _wait_for_resolved(omnigent)
    await service.shutdown()

    # The post-answer text was delivered (in the post-seal segment), not dropped.
    assert any("You picked A. Full summary here." in s.text for s in slack.streams)


class PreambleThenSilentAfterElicitationClient(FakeOmnigentClient):
    """The turn produces NO answer text on the stream at all — a preamble seals
    at the elicitation, and after resolution the answer never streams (it lives
    only in the server's committed message). Exercises the no-delta fallback
    safety net: the final answer is recovered from latest_assistant_message.
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
        # Park until the verdict is posted; the answer then never streams (no
        # delta, no committed item) — only the id-bearing terminal. The final
        # answer is recovered from the server snapshot (latest_message).
        await _wait_any(self.resolve_signal)
        yield {"type": "response.elicitation_resolved", "elicitation_id": "elicit_form"}
        yield {"type": "session.status", "status": "idle", "response_id": "resp_1"}


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
    sid = _card_session_id(card)
    await service.handle_elicitation_action(
        session_id=sid, elicitation_id=eid, verdict=Verdict(accepted=True, content={"store": "A"})
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
    sid = _card_session_id(card)
    await service.handle_elicitation_action(
        session_id=sid, elicitation_id=eid, verdict=Verdict(accepted=True)
    )
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
    # And the sealed-off answer must NOT trip the empty-segment fallback: no
    # "completed without returning response text" segment after the notice.
    all_stream_text = " ".join(s.text for s in slack.streams)
    assert "without returning" not in all_stream_text


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
