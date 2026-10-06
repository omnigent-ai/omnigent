"""Transport tests for Claude-native forwarding."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx
import pytest

import omnigent.harnesses.claude_native.forwarder as forwarder
from omnigent.harnesses.claude_native.bridge import (
    ClaudeTranscriptItem,
    record_hook_event,
)
from tests.harnesses.claude_native.forwarder._support import (
    _get_recorded_request,
    _start_recording_server,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate", [False, True])
async def test_handback_provenance_is_transported_outside_message_content(candidate: bool) -> None:
    captured: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(202)

    item = ClaudeTranscriptItem(
        source_id="native-handback",
        item_type="message",
        data={
            "role": "user",
            **({"is_meta": True} if not candidate else {}),
            "content": [{"type": "input_text", "text": "Done."}],
        },
        response_id="parent-turn",
        subagent_return_id=None if candidate else "native-agent-1",
        agent_message_candidate=candidate,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test"
    ) as client:
        await forwarder._post_external_conversation_item(client, session_id="parent", item=item)
    assert captured[0]["data"].get("subagent_return_id") == (
        None if candidate else "native-agent-1"
    )
    assert captured[0]["data"].get("agent_message_candidate", False) == candidate
    assert captured[0]["data"]["item_data"] == item.data
    assert "subagent_return_id" not in captured[0]["data"]["item_data"]
    assert "agent_message_candidate" not in captured[0]["data"]["item_data"]
    assert forwarder._external_conversation_item_event(item) == captured[0]


class _CountingAuth(httpx.Auth):
    """
    Test httpx Auth that mints a unique bearer per request.

    Stamps ``Bearer token-<n>`` into ``Authorization`` where ``n`` is
    the one-based call count. The counter is the observable that
    proves the forwarder invokes the auth flow per outbound request
    instead of capturing a single Authorization header at client
    construction.
    """

    def __init__(self) -> None:
        """
        Initialize the auth with a zero call counter.

        :returns: None.
        """
        self.calls = 0

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        """
        Stamp a fresh ``Bearer token-<n>`` on every outgoing request.

        :param request: Outgoing httpx request.
        :yields: The request with a freshly minted ``Authorization``
            header.
        """
        self.calls += 1
        request.headers["Authorization"] = f"Bearer token-{self.calls}"
        yield request


@pytest.mark.asyncio
async def test_forwarder_uses_auth_to_refresh_token_per_request(tmp_path: Path) -> None:
    """
    Each outbound HTTP request carries a freshly minted bearer token.

    Regression test for the production bug where the forwarder
    captured the bearer at startup and never refreshed it. After the
    ~1h Databricks OAuth token TTL, the stale token caused the
    forwarder to spin in a permanent retry loop while the runner
    kept processing turns — results never reached the UI. The fix
    threads an ``httpx.Auth`` through the forwarder so the
    Authorization header is recomputed on every request.

    This test fails if the forwarder reverts to passing the bearer
    as a static header on the ``AsyncClient`` (httpx snapshots
    construction-time headers into ``client.headers`` and later
    dict mutation does not propagate to in-flight requests).
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    # Two assistant transcript items → two external_conversation_item
    # POSTs, which is all this test needs: distinct bearers on two
    # outbound requests. We use transcript items rather than hook status
    # because running/idle are no longer hook-derived (only
    # StopFailure→failed remains, a single edge).
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "a1",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "first"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "a2",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "second"}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # SessionStart sets transcript_path so the forwarder reads the
    # transcript above; it posts no status of its own.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    auth = _CountingAuth()
    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
            auth=auth,
        )
    )
    try:
        # Two external_conversation_item POSTs (one per assistant item).
        # The PATCH that mirrors the Claude session id is filtered out
        # by ``_get_recorded_request``'s default ``method="POST"``.
        first = await _get_recorded_request(server)
        second = await _get_recorded_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    # Each POST carried a non-empty bearer minted by the auth flow
    # (matches ``Bearer token-<n>`` for some ``n``). The pattern check
    # would fail with ``None`` if auth were not threaded into the
    # AsyncClient at all.
    assert first["authorization"] is not None and first["authorization"].startswith(
        "Bearer token-"
    ), (
        f"First POST must carry a bearer minted by the counting auth, "
        f"got {first['authorization']!r}. ``None`` means auth was not "
        f"threaded into httpx.AsyncClient."
    )
    assert second["authorization"] is not None and second["authorization"].startswith(
        "Bearer token-"
    ), (
        f"Second POST must carry a bearer minted by the counting auth, "
        f"got {second['authorization']!r}."
    )
    # The load-bearing assertion: the two POSTs carry DIFFERENT
    # bearers. If they were equal, httpx would be reusing a
    # construction-time header snapshot instead of consulting the
    # auth flow per request — that is exactly the production bug.
    assert first["authorization"] != second["authorization"], (
        f"Two consecutive POSTs share the same Authorization "
        f"({first['authorization']!r}). The AsyncClient is reusing a "
        f"snapshot of the original header instead of consulting auth "
        f"on each request — this is the production token-refresh bug."
    )
    # Auth.auth_flow ran at least twice (one per recorded POST).
    # The mirroring PATCH may add one more invocation; the lower
    # bound is what matters — anything less means a request bypassed
    # the auth path entirely.
    assert auth.calls >= 2, (
        f"Expected the counting auth to fire at least twice (one per item POST), got {auth.calls}."
    )


@pytest.mark.asyncio
async def test_forwarder_drops_poison_item_after_bounded_permanent_retries(
    tmp_path: Path,
) -> None:
    """
    Permanent item rejections eventually advance the transcript cursor.

    A malformed transcript item that Omnigent rejects with a permanent 4xx
    should not be reposted forever at the poll interval. After the
    retry budget is exhausted, the forwarder emits a failed status,
    marks the source id handled, persists the new byte cursor, and
    dead-letters the dropped item to disk so it is recoverable (#1120).
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "poison-item",
                "message": {"role": "assistant", "content": "bad item"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    retry_tracker = forwarder._PostRetryTracker(
        max_permanent_attempts=2,
        base_delay_s=0.0,
        max_delay_s=0.0,
    )
    requests: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        """
        Reject conversation items but accept failure status posts.

        :param request: Outbound HTTP request from the forwarder.
        :returns: HTTP response for the mock Omnigent endpoint.
        """
        payload = json.loads(request.content.decode("utf-8"))
        assert isinstance(payload, dict)
        requests.append(payload)
        if payload["type"] == "external_conversation_item":
            return httpx.Response(422, json={"error": "bad item"})
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        dedupe = forwarder._ForwardDedupeState()
        first = await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=retry_tracker,
            dedupe=dedupe,
        )
        second = await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=first,
            retry_tracker=retry_tracker,
            dedupe=dedupe,
        )

    persisted = json.loads((bridge_dir / "transcript_forwarder.json").read_text("utf-8"))
    # The poison item is attempted twice, then the forwarder-failed status. No
    # status POST leads: the transcript path publishes none (Claude's status
    # file owns the badge).
    assert [request["type"] for request in requests] == [
        "external_conversation_item",
        "external_conversation_item",
        "external_session_status",
    ]
    # The failed edge carries BOTH the drop reason as ``output`` (#1113 — the
    # server surfaces it as the failure detail) and the turn's response id so
    # it closes the streaming turn instead of leaving its tool cards spinning.
    context = requests[-1]["data"].pop("failure_context")
    assert context["failure_source"] == "forwarder_delivery"
    assert context["detail_source"] == "forwarder_delivery_error"
    assert context["failure_id"]
    assert "poison-item:0:message" in context["forwarder_source_id"]
    assert requests[-1]["data"] == {
        "status": "failed",
        "output": "transcript item poison-item:0:message rejected",
        "response_id": requests[0]["data"]["response_id"],
    }
    assert first.byte_offset == 0
    assert second.byte_offset == transcript_path.stat().st_size
    assert second.line_cursor == 1
    assert second.seen_source_ids == ("poison-item:0:message",)
    assert persisted["byte_offset"] == transcript_path.stat().st_size
    assert persisted["seen_source_ids"] == ["poison-item:0:message"]
    # The dropped item is dead-lettered to disk so it is recoverable
    # instead of silently lost (#1120).
    dead_letter = (bridge_dir / "dead_letter.jsonl").read_text("utf-8").splitlines()
    assert len(dead_letter) == 1
    record = json.loads(dead_letter[0])
    assert record["session_id"] == "conv_abc"
    assert record["event_type"] == "external_conversation_item"
    assert record["reason"] == "permanent HTTP failure after retries"
    assert record["payload"]["item_type"] == "message"


@pytest.mark.asyncio
async def test_rejected_hook_status_keeps_delivery_failure_identity(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    record_hook_event(
        bridge_dir,
        {"hook_event_name": "Stop", "session_id": "native-session"},
    )
    requests: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(422 if payload["data"]["status"] == "idle" else 202)

    initial = forwarder.HookForwardState(event_cursor=0, byte_offset=0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test"
    ) as client:
        for _ in range(2):
            # Replaying the same hook after a restart retains the delivery failure ID.
            state = initial
            retry_tracker = forwarder._PostRetryTracker(
                max_permanent_attempts=2, base_delay_s=0.0, max_delay_s=0.0
            )
            for attempt in range(2):
                state = await forwarder._forward_available_status_events(
                    client=client,
                    session_id="conv_synthetic",
                    bridge_dir=bridge_dir,
                    state=state,
                    retry_tracker=retry_tracker,
                    dedupe=forwarder._ForwardDedupeState(),
                    task_subjects={},
                    task_statuses={},
                    task_order=[],
                    response_id="resp_synthetic",
                )
                assert state.event_cursor == attempt
            assert state.byte_offset == (bridge_dir / "hooks.jsonl").stat().st_size

    assert [request["type"] for request in requests] == ["external_session_status"] * 6
    assert [request["data"]["status"] for request in requests] == ["idle", "idle", "failed"] * 2
    first, second = [
        request["data"] for request in requests if request["data"]["status"] == "failed"
    ]
    assert first == second
    assert first["response_id"] == "resp_synthetic"
    assert first["output"] == "hook status idle rejected"
    context = first["failure_context"]
    assert context["failure_source"] == "forwarder_delivery"
    assert context["detail_source"] == "forwarder_delivery_error"
    assert context["failure_decision"] == "session_failed"
    assert context["failure_id"]
    assert context["forwarder_source_id"] == f"hook:1:{state.byte_offset}:idle"
    assert "native_error_category" not in context
    persisted = json.loads((bridge_dir / "hook_forwarder.json").read_text("utf-8"))
    assert persisted["event_cursor"] == 1
    assert persisted["byte_offset"] == state.byte_offset


@pytest.mark.asyncio
async def test_forwarder_retries_user_item_on_ambiguous_post_failure(tmp_path: Path) -> None:
    """
    An ambiguous POST failure holds the cursor and re-posts the item.

    A user message typed while Claude is busy round-trips through the
    transcript and is POSTed as an ``external_conversation_item``. If
    that POST's response is lost (e.g. a read timeout on a flaky
    forwarder->server hop), the forwarder cannot know whether the server
    committed the item. Skipping it would silently lose the message from
    the conversation store whenever the server had NOT committed it —
    the web view then misses a message the terminal still shows. The
    POST carries a ``source_id`` idempotency key the server dedupes on,
    so re-posting a committed item is a no-op: the forwarder must retry.

    A failure here (the item marked handled after one ambiguous failure,
    never re-posted) is exactly the lost-user-message regression this
    guards against.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "user-msg-1",
                "message": {"role": "user", "content": "hello while busy"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    retry_tracker = forwarder._PostRetryTracker(base_delay_s=0.0, max_delay_s=0.0)
    requests: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        """
        Fail the first item POST with a read timeout, then succeed.

        The timeout stands in for "request sent, response lost" — the
        ambiguous case where the server may or may not have committed
        the item.

        :param request: Outbound HTTP request from the forwarder.
        :returns: HTTP response for every POST after the first item POST.
        :raises httpx.ReadTimeout: For the first
            ``external_conversation_item`` POST, simulating a lost
            response.
        """
        payload = json.loads(request.content.decode("utf-8"))
        assert isinstance(payload, dict)
        requests.append(payload)
        first_item_post = payload["type"] == "external_conversation_item" and (
            sum(1 for r in requests if r["type"] == "external_conversation_item") == 1
        )
        if first_item_post:
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        dedupe = forwarder._ForwardDedupeState()
        first = await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=retry_tracker,
            dedupe=dedupe,
        )
        second = await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=first,
            retry_tracker=retry_tracker,
            dedupe=dedupe,
        )

    item_posts = [r for r in requests if r["type"] == "external_conversation_item"]
    # Re-POSTed on the second poll (2 attempts): the ambiguous failure
    # must not mark the item handled — skipping it would lose the user
    # message from the conversation store when the server had not
    # committed it.
    assert len(item_posts) == 2
    # Every attempt carries the same server-side idempotency key, so the
    # retry is a no-op when the first POST WAS committed — no duplicate
    # user bubble.
    source_ids = {post["data"]["source_id"] for post in item_posts}
    assert source_ids == {"user-msg-1:0:message"}
    # No "failed" status: unlike a permanent 4xx rejection, a transient
    # failure is retried, so we must not flag the turn failed.
    assert all(r["type"] != "external_session_status" for r in requests)
    # First poll held the cursor (nothing handled); the successful retry
    # advanced it past the item and recorded it as handled.
    assert first.byte_offset == 0
    assert first.seen_source_ids == ()
    assert second.byte_offset == transcript_path.stat().st_size
    assert second.seen_source_ids == ("user-msg-1:0:message",)


@pytest.mark.asyncio
async def test_forwarder_retries_user_item_on_connect_error(tmp_path: Path) -> None:
    """
    A provably-undelivered POST failure is retried, not dropped.

    A connection-refused error proves the request never reached the
    server, so the item was not committed. Dropping it would silently
    lose a user message. The forwarder must hold the cursor and re-POST
    on the next poll.

    A failure here (item marked handled / cursor advanced after a
    connect error) would mean a user message is silently lost whenever
    the server is briefly unreachable.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "user-msg-2",
                "message": {"role": "user", "content": "server is down"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    retry_tracker = forwarder._PostRetryTracker(base_delay_s=0.0, max_delay_s=0.0)
    requests: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        """
        Fail every item POST with a connection error (never delivered).

        :param request: Outbound HTTP request from the forwarder.
        :returns: HTTP response (never reached for the item POST).
        :raises httpx.ConnectError: For every ``external_conversation_item``
            POST, simulating an unreachable server.
        """
        payload = json.loads(request.content.decode("utf-8"))
        assert isinstance(payload, dict)
        requests.append(payload)
        if payload["type"] == "external_conversation_item":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        dedupe = forwarder._ForwardDedupeState()
        first = await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=retry_tracker,
            dedupe=dedupe,
        )
        second = await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=first,
            retry_tracker=retry_tracker,
            dedupe=dedupe,
        )

    item_posts = [r for r in requests if r["type"] == "external_conversation_item"]
    # Re-POSTed on the second poll (2 attempts): a connect error proves
    # non-delivery, so the item must be retried, not skipped.
    assert len(item_posts) == 2
    # Cursor held at the start and the item never marked handled, so it
    # keeps being retried until it lands.
    assert first.byte_offset == 0
    assert first.seen_source_ids == ()
    assert second.byte_offset == 0


def test_parse_json_response_returns_value_on_valid_json() -> None:
    """
    A normal JSON body parses through ``_parse_json_response`` unchanged.

    :returns: None.
    """
    resp = httpx.Response(200, json={"id": "conv_abc123"})
    assert forwarder._parse_json_response(resp, context="session snapshot") == {
        "id": "conv_abc123"
    }


def test_parse_json_response_raises_diagnosable_error_on_html_body() -> None:
    """
    An HTML body (e.g. an expired Databricks Apps OAuth login page served
    with a 200) raises a ``RuntimeError`` naming the content type and a
    body snippet, not an opaque ``json.JSONDecodeError``. The original
    parser error is preserved as ``__cause__`` for debugging.

    :returns: None.
    """
    resp = httpx.Response(
        200,
        html="<!DOCTYPE html><html><body>Sign in to continue</body></html>",
    )
    with pytest.raises(RuntimeError) as excinfo:
        forwarder._parse_json_response(resp, context="session 'conv_abc123' snapshot")
    message = str(excinfo.value)
    assert "session 'conv_abc123' snapshot" in message
    assert "text/html" in message
    assert "<!DOCTYPE html>" in message
    assert isinstance(excinfo.value.__cause__, ValueError)


@pytest.mark.asyncio
async def test_fetch_session_snapshot_raises_diagnosable_error_on_html_body() -> None:
    """
    ``_fetch_session_snapshot`` surfaces a clear error when the Sessions
    API returns a 200 HTML body instead of JSON — the failure mode behind
    Claude Code's "Unrecognized token '<'" crash when an auth/proxy page is
    served in place of the API response. Without the guard this raised a
    bare ``json.JSONDecodeError`` that the forwarder supervisor turned into
    a silent restart loop.

    :returns: None.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            html="<!DOCTYPE html><html><body>Sign in to continue</body></html>",
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        with pytest.raises(RuntimeError) as excinfo:
            await forwarder._fetch_session_snapshot(client, "conv_abc123")
    message = str(excinfo.value)
    assert "conv_abc123" in message
    assert "text/html" in message
