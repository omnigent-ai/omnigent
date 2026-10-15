"""Delivery tests for Codex forwarder."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from tests.harnesses.codex_native.forwarder._support import (
    _RaisingPostClient,
    _RecordingClient,
)


@pytest.mark.asyncio
async def test_post_session_event_dead_letters_durable_event_on_permanent_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A permanently-failed durable event is dead-lettered to disk (#1120).

    Drives ``_post_session_event`` (the health-tracking wrapper) with a stubbed
    inner that returns an HTTP 500, and asserts the dropped
    ``external_conversation_item`` payload is appended to
    ``{bridge_dir}/dead_letter.jsonl``.

    :param tmp_path: Pytest temp dir standing in for the bridge dir.
    :param monkeypatch: Pytest patcher (auto-restores the stubbed inner).
    """
    import json as _json

    fwd._reset_forward_health()

    async def _failing_inner(client, session_id, *, event_type, data, max_attempts, timeout):
        return fwd._PostResult(
            response=httpx.Response(500, request=httpx.Request("POST", "http://test"))
        )

    monkeypatch.setattr(fwd, "_post_session_event_inner", _failing_inner)
    token = fwd._dead_letter_dir.set(tmp_path)
    try:
        data = {"item_type": "message", "item_data": {"role": "assistant"}}
        await fwd._post_session_event(
            MagicMock(),
            "conv_codex1",
            event_type="external_conversation_item",
            data=data,
        )
    finally:
        fwd._dead_letter_dir.reset(token)
        fwd._reset_forward_health()

    dl_path = tmp_path / "dead_letter.jsonl"
    lines = dl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = _json.loads(lines[0])
    assert record["session_id"] == "conv_codex1"
    assert record["event_type"] == "external_conversation_item"
    assert record["payload"] == data


@pytest.mark.asyncio
async def test_post_session_event_does_not_dead_letter_ephemeral_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An ephemeral (non-durable) event is NOT dead-lettered on failure (#1120).

    :param tmp_path: Pytest temp dir standing in for the bridge dir.
    :param monkeypatch: Pytest patcher (auto-restores the stubbed inner).
    """
    fwd._reset_forward_health()

    async def _failing_inner(client, session_id, *, event_type, data, max_attempts, timeout):
        return fwd._PostResult(
            response=httpx.Response(500, request=httpx.Request("POST", "http://test"))
        )

    monkeypatch.setattr(fwd, "_post_session_event_inner", _failing_inner)
    token = fwd._dead_letter_dir.set(tmp_path)
    try:
        await fwd._post_session_event(
            MagicMock(),
            "conv_codex1",
            event_type="external_output_text_delta",
            data={"delta": "hi"},
        )
    finally:
        fwd._dead_letter_dir.reset(token)
        fwd._reset_forward_health()

    assert not (tmp_path / "dead_letter.jsonl").exists()


@pytest.mark.asyncio
async def test_post_session_event_dead_letters_usage_on_permanent_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A permanently-failed ``external_session_usage`` event is dead-lettered (#1120).

    Usage is the other durable type alongside conversation items, so its
    transcript/usage data must also be recoverable on a sustained outage.

    :param tmp_path: Pytest temp dir standing in for the bridge dir.
    :param monkeypatch: Pytest patcher (auto-restores the stubbed inner).
    """
    import json as _json

    fwd._reset_forward_health()

    async def _failing_inner(client, session_id, *, event_type, data, max_attempts, timeout):
        return fwd._PostResult(
            response=httpx.Response(500, request=httpx.Request("POST", "http://test"))
        )

    monkeypatch.setattr(fwd, "_post_session_event_inner", _failing_inner)
    token = fwd._dead_letter_dir.set(tmp_path)
    try:
        data = {"context_tokens": 1234, "model": "databricks-claude-opus-4-7"}
        await fwd._post_session_event(
            MagicMock(),
            "conv_codex_usage",
            event_type="external_session_usage",
            data=data,
        )
    finally:
        fwd._dead_letter_dir.reset(token)
        fwd._reset_forward_health()

    dl_path = tmp_path / "dead_letter.jsonl"
    lines = dl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = _json.loads(lines[0])
    assert record["session_id"] == "conv_codex_usage"
    assert record["event_type"] == "external_session_usage"
    assert record["payload"] == data


class _SequencedPostClient:
    """Return or raise configured POST outcomes in order."""

    def __init__(self, outcomes: list[httpx.Response | httpx.HTTPError]) -> None:
        self._outcomes = outcomes
        self.calls: list[tuple[str, object, float | None]] = []

    async def post(
        self,
        url: str,
        *,
        json: object,
        timeout: float | None = None,
    ) -> httpx.Response:
        self.calls.append((url, json, timeout))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, httpx.HTTPError):
            raise outcome
        return outcome


@pytest.mark.asyncio
async def test_post_session_event_inner_classifies_ambiguous_skip() -> None:
    """
    An ambiguous conversation-item transport failure surfaces as ambiguous (#1579).

    The inner used to conflate this with a proven-undelivered failure (both
    returned ``None``); replay must be able to tell them apart.
    """
    client = _RaisingPostClient(
        httpx.ReadTimeout("response lost", request=httpx.Request("POST", "http://test"))
    )
    result = await fwd._post_session_event_inner(
        client,
        "conv_codex1",
        event_type="external_conversation_item",
        data={"item_type": "message"},
    )
    assert result.response is None
    assert result.delivered_ambiguous is True
    assert result.transport_error == "ReadTimeout"
    # Ambiguous items are abandoned immediately — no retries.
    assert client.calls == 1


@pytest.mark.asyncio
async def test_idempotent_conversation_item_retries_ambiguous_failure_until_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stable source id makes response-loss retries duplicate-safe."""
    monkeypatch.setattr(fwd, "_sleep", AsyncMock())
    request = httpx.Request("POST", "http://test")
    client = _SequencedPostClient(
        [
            httpx.ReadTimeout("response lost", request=request),
            httpx.Response(503, request=request),
            httpx.Response(202, request=request),
        ]
    )
    data = {
        "item_type": "message",
        "item_data": {"role": "assistant"},
        "source_id": "thread_1:turn_1:item_1",
    }

    result = await fwd._post_session_event_inner(
        client,  # type: ignore[arg-type]
        "conv_codex1",
        event_type="external_conversation_item",
        data=data,
        max_attempts=None,
        timeout=5.0,
    )

    assert result.response is not None
    assert result.response.status_code == 202
    assert [call[1] for call in client.calls] == [
        {"type": "external_conversation_item", "data": data},
    ] * 3
    assert [call[2] for call in client.calls] == [5.0, 5.0, 5.0]


@pytest.mark.asyncio
async def test_idempotent_conversation_item_bounds_long_source_id() -> None:
    """Long native ids are hashed into the server's source-id limit."""
    client = _RecordingClient()

    posted = await fwd._post_external_item(
        client,  # type: ignore[arg-type]
        "conv_x",
        item_type="message",
        item_data={"role": "assistant", "content": []},
        response_id="codex_turn_1",
        source_id="source:" + ("x" * 300),
    )

    assert posted is True
    source_id = client.posts[0][1]["data"]["source_id"]
    assert isinstance(source_id, str)
    assert source_id.startswith("codex:")
    assert len(source_id) <= 256


@pytest.mark.asyncio
async def test_idempotent_conversation_items_keep_order_while_older_post_is_slow() -> None:
    """Concurrent resume/live delivery cannot let a newer item overtake an older one."""

    class _BlockingFirstPostClient(_RecordingClient):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def post(
            self,
            url: str,
            *,
            json: dict,
            timeout: float | None = None,
        ) -> httpx.Response:
            if not self.posts:
                self.entered.set()
                await self.release.wait()
            return await super().post(url, json=json, timeout=timeout)

    client = _BlockingFirstPostClient()
    token = fwd._conversation_item_locks.set({})
    try:
        older = asyncio.create_task(
            fwd._post_external_item(
                client,  # type: ignore[arg-type]
                "conv_x",
                item_type="message",
                item_data={"role": "assistant", "content": []},
                response_id="codex_turn_1",
                source_id="thread_1:turn_1:item_1",
            )
        )
        await asyncio.wait_for(client.entered.wait(), timeout=5.0)
        newer = asyncio.create_task(
            fwd._post_external_item(
                client,  # type: ignore[arg-type]
                "conv_x",
                item_type="message",
                item_data={"role": "assistant", "content": []},
                response_id="codex_turn_2",
                source_id="thread_1:turn_2:item_2",
            )
        )
        await asyncio.sleep(0)
        assert client.posts == []

        client.release.set()
        assert await asyncio.gather(older, newer) == [True, True]
    finally:
        fwd._conversation_item_locks.reset(token)

    assert [body["data"]["source_id"] for _url, body in client.posts] == [
        "thread_1:turn_1:item_1",
        "thread_1:turn_2:item_2",
    ]


@pytest.mark.asyncio
async def test_cancelled_idempotent_item_is_dead_lettered_for_safe_replay(tmp_path: Path) -> None:
    """Shutdown during an in-flight completion keeps a replayable disk record."""

    class _BlockingPostClient:
        def __init__(self) -> None:
            self.entered = asyncio.Event()

        async def post(
            self,
            url: str,
            *,
            json: dict,
            timeout: float | None = None,
        ) -> httpx.Response:
            self.entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    client = _BlockingPostClient()
    dead_letter_token = fwd._dead_letter_dir.set(tmp_path)
    locks_token = fwd._conversation_item_locks.set({})
    try:
        task = asyncio.create_task(
            fwd._post_external_item(
                client,  # type: ignore[arg-type]
                "conv_x",
                item_type="message",
                item_data={"role": "assistant", "content": []},
                response_id="codex_turn_1",
                source_id="thread_1:turn_1:item_1",
            )
        )
        await asyncio.wait_for(client.entered.wait(), timeout=5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        fwd._conversation_item_locks.reset(locks_token)
        fwd._dead_letter_dir.reset(dead_letter_token)

    import json as _json

    record = _json.loads((tmp_path / "dead_letter.jsonl").read_text().splitlines()[0])
    assert record["delivered_ambiguous"] is True
    assert record["payload"]["source_id"] == "thread_1:turn_1:item_1"


@pytest.mark.asyncio
async def test_post_session_event_inner_classifies_proven_undelivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A connect failure exhausted after retries is proven-undelivered, not ambiguous.
    """
    monkeypatch.setattr(fwd, "_sleep", AsyncMock())
    client = _RaisingPostClient(
        httpx.ConnectError("refused", request=httpx.Request("POST", "http://test"))
    )
    result = await fwd._post_session_event_inner(
        client,
        "conv_codex1",
        event_type="external_conversation_item",
        data={"item_type": "message"},
    )
    assert result.response is None
    assert result.delivered_ambiguous is False
    assert result.transport_error == "ConnectError"
    # Connect failures are safe to retry, so all attempts are spent.
    assert client.calls == fwd._POST_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_post_session_event_dead_letters_ambiguous_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An ambiguous-skip drop is dead-lettered with ``delivered_ambiguous=True`` (#1579).

    :param tmp_path: Pytest temp dir standing in for the bridge dir.
    :param monkeypatch: Pytest patcher (auto-restores the stubbed inner).
    """
    import json as _json

    fwd._reset_forward_health()

    async def _ambiguous_inner(client, session_id, *, event_type, data, max_attempts, timeout):
        return fwd._PostResult(
            response=None, delivered_ambiguous=True, transport_error="ReadTimeout"
        )

    monkeypatch.setattr(fwd, "_post_session_event_inner", _ambiguous_inner)
    token = fwd._dead_letter_dir.set(tmp_path)
    try:
        await fwd._post_session_event(
            MagicMock(),
            "conv_codex1",
            event_type="external_conversation_item",
            data={"item_type": "message"},
        )
    finally:
        fwd._dead_letter_dir.reset(token)
        fwd._reset_forward_health()

    record = _json.loads((tmp_path / "dead_letter.jsonl").read_text().splitlines()[0])
    assert record["delivered_ambiguous"] is True
    assert record["http_status"] is None
    assert record["transport_error"] == "ReadTimeout"


@pytest.mark.asyncio
async def test_post_session_event_dead_letters_records_http_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A status-bearing failure records ``http_status`` and is not ambiguous (#1579).

    :param tmp_path: Pytest temp dir standing in for the bridge dir.
    :param monkeypatch: Pytest patcher (auto-restores the stubbed inner).
    """
    import json as _json

    fwd._reset_forward_health()

    async def _failing_inner(client, session_id, *, event_type, data, max_attempts, timeout):
        return fwd._PostResult(
            response=httpx.Response(503, request=httpx.Request("POST", "http://test"))
        )

    monkeypatch.setattr(fwd, "_post_session_event_inner", _failing_inner)
    token = fwd._dead_letter_dir.set(tmp_path)
    try:
        await fwd._post_session_event(
            MagicMock(),
            "conv_codex1",
            event_type="external_conversation_item",
            data={"item_type": "message"},
        )
    finally:
        fwd._dead_letter_dir.reset(token)
        fwd._reset_forward_health()

    record = _json.loads((tmp_path / "dead_letter.jsonl").read_text().splitlines()[0])
    assert record["http_status"] == 503
    assert record["delivered_ambiguous"] is False
    assert record["transport_error"] is None


@pytest.mark.asyncio
async def test_replay_dead_letters_before_resume_reposts_proven_undelivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Before resume, a proven-undelivered record is re-POSTed and removed (#1579).

    :param tmp_path: Pytest temp dir standing in for the bridge dir.
    :param monkeypatch: Pytest patcher (auto-restores the stubbed inner).
    """
    fwd.append_dead_letter(
        tmp_path,
        session_id="conv_codex1",
        event_type="external_conversation_item",
        payload={"item_type": "message"},
        reason="proven-undelivered transport failure after retries",
        delivered_ambiguous=False,
        http_status=None,
        transport_error="ConnectError",
    )

    posted: list[dict] = []

    async def _ok_inner(client, session_id, *, event_type, data, max_attempts, timeout):
        posted.append(
            {
                "session_id": session_id,
                "event_type": event_type,
                "data": data,
                "max_attempts": max_attempts,
                "timeout": timeout,
            }
        )
        return fwd._PostResult(
            response=httpx.Response(200, request=httpx.Request("POST", "http://test"))
        )

    monkeypatch.setattr(fwd, "_post_session_event_inner", _ok_inner)
    await fwd._replay_dead_letters_before_resume(MagicMock(), tmp_path)

    assert len(posted) == 1
    assert posted[0]["session_id"] == "conv_codex1"
    assert posted[0]["event_type"] == "external_conversation_item"
    assert posted[0]["data"] == {"item_type": "message"}
    # Replay re-POSTs with a single attempt and a short timeout so a large file
    # or a hung server cannot stall startup.
    assert posted[0]["max_attempts"] == 1
    assert posted[0]["timeout"] == fwd._REPLAY_POST_TIMEOUT_SECONDS
    # Delivered → record removed.
    assert not (tmp_path / "dead_letter.jsonl").exists()


@pytest.mark.asyncio
async def test_replay_dead_letters_before_resume_skips_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Before resume, an ambiguous record is never re-POSTed and is retained (#1579).

    :param tmp_path: Pytest temp dir standing in for the bridge dir.
    :param monkeypatch: Pytest patcher (auto-restores the stubbed inner).
    """
    fwd.append_dead_letter(
        tmp_path,
        session_id="conv_codex1",
        event_type="external_conversation_item",
        payload={"item_type": "message"},
        reason="ambiguous transport failure (may already be committed)",
        delivered_ambiguous=True,
    )

    called = False

    async def _inner(client, session_id, *, event_type, data, **_kwargs):
        nonlocal called
        called = True
        return fwd._PostResult(
            response=httpx.Response(200, request=httpx.Request("POST", "http://test"))
        )

    monkeypatch.setattr(fwd, "_post_session_event_inner", _inner)
    await fwd._replay_dead_letters_before_resume(MagicMock(), tmp_path)

    assert called is False
    # Ambiguous record retained as a forensic record.
    assert (tmp_path / "dead_letter.jsonl").exists()


class _RecordingPostClient:
    """Async client stub that records each ``post`` call's kwargs."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.calls: list[dict] = []

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append(kwargs)
        return self._response


@pytest.mark.asyncio
async def test_post_session_event_inner_single_attempt_and_timeout() -> None:
    """
    ``max_attempts=1`` makes one POST (no retry) and ``timeout`` is threaded through.

    Replay relies on both so a hung server fails fast and startup is bounded (#1579).
    """
    client = _RecordingPostClient(
        httpx.Response(503, request=httpx.Request("POST", "http://test"))
    )
    result = await fwd._post_session_event_inner(
        client,
        "conv_codex1",
        event_type="external_conversation_item",
        data={"item_type": "message"},
        max_attempts=1,
        timeout=5.0,
    )
    # A single attempt even though 503 is normally retryable.
    assert len(client.calls) == 1
    assert client.calls[0]["timeout"] == 5.0
    assert result.response is not None
    assert result.response.status_code == 503


async def test_post_session_event_records_connectivity_failure_for_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex's exhausted-retry POST failure is recorded for the idle watchdog.

    Writer half of issue #1119 for the codex forwarder: when every attempt to
    POST a session event raises a connect error, ``_post_session_event`` must
    record the failure in ``_native_forwarder_health`` (via
    ``_log_post_transport_failure``) so the harness idle-turn watchdog can name
    the connectivity cause instead of a generic "wedged LLM" reason.
    """
    from omnigent.native import _native_forwarder_health as health

    class _AlwaysConnectError:
        """Stub client whose every POST fails to connect."""

        async def post(self, url: str, *, json: object) -> httpx.Response:
            """Raise a connect error mimicking an unreachable server."""
            del json
            raise httpx.ConnectError("No route to host", request=httpx.Request("POST", url))

    async def _no_sleep(_seconds: float) -> None:
        """No-op sleep so the retry loop doesn't add real delay."""

    monkeypatch.setattr(fwd, "_sleep", _no_sleep)

    health.clear()
    try:
        result = await fwd._post_session_event(
            _AlwaysConnectError(),  # type: ignore[arg-type]
            "conv_x",
            event_type="external_session_status",
            data={},
        )
        assert result is None
        detail = health.recent_post_failure(60.0)
        assert detail is not None
        assert "external_session_status" in detail
        assert "No route to host" in detail
    finally:
        health.clear()
