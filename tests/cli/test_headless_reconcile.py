"""Headless recovery through the real SDK's HTTP and SSE parsing paths."""

import json
from functools import partial

import httpx
import pytest
from omnigent_client import OmnigentClient, OmnigentError

from omnigent.chat import _query_sessions_once


@pytest.mark.parametrize("completion_stream", [2, 3], ids=["probe", "later-turn"])
@pytest.mark.parametrize("item_status", ["completed", "incomplete"])
async def test_headless_recovers_final_text_when_completion_event_is_missed(
    monkeypatch: pytest.MonkeyPatch, completion_stream: int, item_status: str
) -> None:
    session_id = "conv_headless"
    final_text = "<!-- POLLY_REVIEW_START -->\n## Summary\nReview complete."
    snapshot = {"id": session_id, "agent_id": "ag_polly", "status": "idle", "created_at": 1}
    user_item = {"type": "message", "role": "user", "content": []}
    final_item = {
        "type": "message",
        "role": "assistant",
        "status": item_status,
        "content": [{"type": "output_text", "text": final_text}],
    }
    items = [user_item]
    streams = 0
    transcript_reads: list[bool] = []

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal streams
        path = request.url.path
        if request.method == "POST" and path == "/v1/sessions":
            return httpx.Response(200, json={"session_id": session_id})
        if path == f"/v1/sessions/{session_id}" and request.method in {"GET", "PATCH"}:
            return httpx.Response(200, json=snapshot)
        if path.endswith("/agent"):
            return httpx.Response(200, json={"tools": []})
        if path.endswith("/events") and request.method == "POST":
            snapshot["status"] = "running"
            return httpx.Response(200, json={"queued": True})
        if path.endswith("/items"):
            assert request.url.params["order"] == "desc"
            transcript_reads.append(final_item in items)
            return httpx.Response(200, json={"data": list(reversed(items))})
        if path.endswith("/stream"):
            streams += 1
            if streams == 1:
                events = [
                    {"type": "session.heartbeat", "conversation_id": session_id},
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_dispatch",
                            "status": "completed",
                            "model": "test-model",
                            "created_at": 1,
                            "output": [],
                        },
                    },
                ]
            else:
                assert streams <= completion_stream
                if streams == completion_stream:
                    # The final message is durable, but this subscription missed it.
                    items.append(final_item)
                    snapshot["status"] = "idle"
                events = [
                    {
                        "type": "session.status",
                        "conversation_id": session_id,
                        "status": "idle" if streams == completion_stream else "waiting",
                    }
                ]
            wire = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
            return httpx.Response(200, text=wire, headers={"Content-Type": "text/event-stream"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    monkeypatch.setattr(
        "omnigent_client._client.httpx.AsyncClient",
        partial(httpx.AsyncClient, transport=httpx.MockTransport(handle)),
    )
    async with OmnigentClient(base_url="http://127.0.0.1") as client:
        result = await _query_sessions_once(
            client=client,
            agent_name="polly",
            tool_handler=None,
            prompt="Review this PR",
            session_bundle=b"bundle",
            session_bundle_filename="agent.tar.gz",
            runner_id="runner_test",
        )

    assert streams == completion_stream
    assert transcript_reads[0] is False
    assert result == (final_text if item_status == "completed" else None)


async def test_headless_raises_when_turn_fails_after_streaming_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``response.failed`` turn exits non-zero even if it streamed text first.

    The Claude CLI reports a failing ``apiKeyHelper`` as assistant text and then
    fails the turn. If this regresses, ``omnigent run -p`` prints that error text
    as the reply and exits 0, so scripts and CI read an auth failure as success.
    """
    session_id = "conv_headless_failed"
    cli_text = "Your apiKeyHelper script is failing · re-authenticate with your provider"
    failure = "inner executor error: Claude SDK provider authentication failed (status=401)"
    snapshot = {"id": session_id, "agent_id": "ag_claude", "status": "idle", "created_at": 1}
    # The transcript holds the streamed text as a completed assistant item, so a
    # reconcile against it would also resurrect the error as a reply.
    items = [
        {"type": "message", "role": "user", "content": []},
        {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": cli_text}],
        },
        {"type": "error", "status": "completed", "code": "RuntimeError", "message": failure},
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/v1/sessions":
            return httpx.Response(200, json={"session_id": session_id})
        if path == f"/v1/sessions/{session_id}" and request.method in {"GET", "PATCH"}:
            return httpx.Response(200, json=snapshot)
        if path.endswith("/agent"):
            return httpx.Response(200, json={"tools": []})
        if path.endswith("/events") and request.method == "POST":
            return httpx.Response(200, json={"queued": True})
        if path.endswith("/items"):
            return httpx.Response(200, json={"data": list(reversed(items))})
        if path.endswith("/stream"):
            events = [
                {"type": "session.heartbeat", "conversation_id": session_id},
                {"type": "response.output_text.delta", "delta": cli_text},
                {
                    "type": "response.failed",
                    "source": "harness",
                    "response": {
                        "id": "resp_failed",
                        "status": "failed",
                        "error": {"code": "RuntimeError", "message": failure},
                    },
                },
            ]
            wire = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
            return httpx.Response(200, text=wire, headers={"Content-Type": "text/event-stream"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    monkeypatch.setattr(
        "omnigent_client._client.httpx.AsyncClient",
        partial(httpx.AsyncClient, transport=httpx.MockTransport(handle)),
    )
    async with OmnigentClient(base_url="http://127.0.0.1") as client:
        with pytest.raises(OmnigentError) as excinfo:
            await _query_sessions_once(
                client=client,
                agent_name="claude-sdk",
                tool_handler=None,
                prompt="hi",
                session_bundle=b"bundle",
                session_bundle_filename="agent.tar.gz",
                runner_id="runner_test",
            )

    assert str(excinfo.value) == failure
    assert excinfo.value.code == "RuntimeError"
