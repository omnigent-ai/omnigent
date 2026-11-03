"""Transport tests for Codex session."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder
from tests.harnesses.codex_native.session._support import (
    _forwarder_context,
)


def test_forwarder_skips_item_retry_on_ambiguous_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An ambiguous item-POST failure stops retries instead of re-posting.

    A lost response (e.g. read timeout) after the server already
    appended the item and published ``session.input.consumed`` is
    indistinguishable from a failed send. External items are not deduped
    server-side, so a retry would persist a second copy — the duplicate
    message bug in the web UI. The forwarder must give up after
    the first ambiguous attempt.

    A failure here (more than one POST attempt) is exactly the
    duplicate-item regression this guards against.
    """
    attempts: list[str] = []
    sleep_delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        """
        Record retry delays without slowing the test.

        :param seconds: Delay requested by the forwarder.
        :returns: None.
        """
        sleep_delays.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Fail every item POST with a read timeout (response lost).

        :param request: HTTP request sent by the forwarder.
        :returns: Never returns.
        :raises httpx.ReadTimeout: For every attempt, simulating the
            server committing the item but the response being lost.
        """
        attempts.append(json.loads(request.content)["type"])
        raise httpx.ReadTimeout("response lost", request=request)

    monkeypatch.setattr(codex_native_forwarder, "_sleep", fake_sleep)

    async def run() -> None:
        """
        Post one mirrored Codex item against the failing transport.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            await codex_native_forwarder._post_external_item(
                client,
                "conv_123",
                item_type="message",
                item_data={"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                response_id="codex_turn_123",
            )

    asyncio.run(run())

    # Exactly one POST attempt: the ambiguous failure must not be
    # retried. Two or three attempts would mean the forwarder re-posted
    # a possibly-committed item — the duplicate-bubble bug.
    assert attempts == ["external_conversation_item"]
    # Returned before any backoff: no retry was even scheduled.
    assert sleep_delays == []


def test_forwarder_retries_item_on_connect_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A provably-undelivered item POST keeps its full retry budget.

    A connection error proves no request bytes reached the server, so
    the item cannot have been committed — retrying is safe and dropping
    early would lose messages whenever the server is briefly
    unreachable. The complement to the ambiguous-skip test, guarding
    the duplicate fix from turning into a message-loss bug.
    """
    attempts: list[str] = []

    async def fake_sleep(seconds: float) -> None:
        """
        Skip retry backoff to keep the test fast.

        :param seconds: Delay requested by the forwarder.
        :returns: None.
        """

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Fail every item POST with a connection error (never delivered).

        :param request: HTTP request sent by the forwarder.
        :returns: Never returns.
        :raises httpx.ConnectError: For every attempt, simulating an
            unreachable server.
        """
        attempts.append(json.loads(request.content)["type"])
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(codex_native_forwarder, "_sleep", fake_sleep)

    async def run() -> None:
        """
        Post one mirrored Codex item against the unreachable transport.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            await codex_native_forwarder._post_external_item(
                client,
                "conv_123",
                item_type="message",
                item_data={"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                response_id="codex_turn_123",
            )

    asyncio.run(run())

    # The full retry budget was spent: connect errors are safe to retry,
    # so the forwarder must not give up after the first attempt. Fewer
    # attempts would mean the ambiguous-skip is over-broad (message loss).
    assert attempts == ["external_conversation_item"] * codex_native_forwarder._POST_MAX_ATTEMPTS


def test_forwarder_still_retries_status_on_ambiguous_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The ambiguous-failure skip applies only to conversation items.

    Status events are idempotent (last-write-wins), so re-posting one
    that may already have landed is harmless — and keeping the retries
    preserves delivery of the running/idle badge. A failure here means
    the skip leaked beyond ``external_conversation_item`` and transient
    events lost their retry budget.
    """
    attempts: list[str] = []

    async def fake_sleep(seconds: float) -> None:
        """
        Skip retry backoff to keep the test fast.

        :param seconds: Delay requested by the forwarder.
        :returns: None.
        """

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Fail every status POST with a read timeout.

        :param request: HTTP request sent by the forwarder.
        :returns: Never returns.
        :raises httpx.ReadTimeout: For every attempt.
        """
        attempts.append(json.loads(request.content)["type"])
        raise httpx.ReadTimeout("response lost", request=request)

    monkeypatch.setattr(codex_native_forwarder, "_sleep", fake_sleep)

    async def run() -> None:
        """
        Post one status edge against the failing transport.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            await codex_native_forwarder._post_status(client, "conv_123", "running")

    asyncio.run(run())

    # All attempts spent: status posts keep retrying through ambiguous
    # failures because re-delivery is harmless and dropping early would
    # strand the session badge.
    assert attempts == ["external_session_status"] * codex_native_forwarder._POST_MAX_ATTEMPTS


def test_forwarder_retries_transient_external_item_rejection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Transient Omnigent failures do not drop the mirrored Codex item.

    This test fails if ``_post_external_item`` gives up after the first
    retryable HTTP status instead of retrying the same item post.
    """
    posted: list[dict[str, Any]] = []
    sleep_delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        """
        Record retry delays without slowing the test.

        :param seconds: Delay requested by the forwarder.
        :returns: None.
        """
        sleep_delays.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Reject the first item post transiently, then accept the retry.

        :param request: HTTP request sent by the forwarder.
        :returns: HTTP response for this attempt.
        """
        posted.append(json.loads(request.content))
        if len(posted) == 1:
            return httpx.Response(503, text="starting")
        return httpx.Response(202, json={"queued": False})

    monkeypatch.setattr(codex_native_forwarder, "_sleep", fake_sleep)

    async def run() -> None:
        """
        Post one mirrored Codex item through the real handler.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            await codex_native_forwarder._handle_event(
                client,
                **_forwarder_context(client, tmp_path),
                event={
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "item": {
                            "type": "agentMessage",
                            "id": "item_agent",
                            "text": "retry me",
                        },
                    },
                },
            )

    asyncio.run(run())

    # Exactly two posts proves the first transient rejection was retried
    # once and then accepted; one would mean a dropped item, while more
    # would mean the forwarder retried after success.
    assert len(posted) == 2
    assert posted[0] == posted[1]
    assert posted[1]["data"]["item_data"]["content"][0]["text"] == "retry me"
    assert sleep_delays == [0.1]


def test_forwarder_logs_rejected_external_item(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """
    Omnigent 4xx responses are logged so mirror failures are diagnosable.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad payload")

    async def run() -> None:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            await codex_native_forwarder._handle_event(
                client,
                **_forwarder_context(client, tmp_path),
                event={
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "item": {
                            "type": "userMessage",
                            "id": "item_user",
                            "content": [{"type": "text", "text": "hello codex"}],
                        },
                    },
                },
            )

    asyncio.run(run())

    assert "failed to post Codex conversation item: status=400 body=bad payload" in caplog.text
