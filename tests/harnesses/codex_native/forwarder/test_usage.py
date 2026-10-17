"""Usage tests for Codex forwarder."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from tests.harnesses.codex_native.forwarder._support import (
    _RecordingClient,
)

# ── sub-agent usage pricing: child-local model attribution ──────────


def _child_state(
    codex_client: MagicMock | None = None,
    *,
    children: tuple[tuple[str, str], ...] = (("thread_child", "conv_child"),),
) -> fwd._CodexForwarderState:
    """Build parent state with known Codex-child session mappings."""
    state = fwd._CodexForwarderState(model="parent-model", posted_model="parent-model")
    state.parent_session_id = "conv_parent"
    state.codex_client = codex_client
    for thread_id, session_id in children:
        state.note_child_thread(thread_id, session_id)
    return state


def _resume_response(thread_id: str, model: str | None = None) -> dict:
    """Build the subset of a child ``thread/resume`` response under test."""
    result: dict = {"thread": {"id": thread_id, "turns": []}}
    if model is not None:
        result["model"] = model
    return {"result": result}


def _usage_event(
    thread_id: str,
    *,
    input_tokens: int = 100,
    output_tokens: int = 5,
    cached_input_tokens: int | None = None,
) -> dict:
    """Build a child cumulative-token notification."""
    total = {"inputTokens": input_tokens, "outputTokens": output_tokens}
    if cached_input_tokens is not None:
        total["cachedInputTokens"] = cached_input_tokens
    return {
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": thread_id,
            "turnId": f"turn_{thread_id}",
            "tokenUsage": {"total": total},
        },
    }


def _usage_posts(client: _RecordingClient) -> list[tuple[str, dict]]:
    """Return only recorded external usage posts."""
    return [(url, body) for url, body in client.posts if body["type"] == "external_session_usage"]


def _record_child_usage(
    client: _RecordingClient,
    state: fwd._CodexForwarderState,
    thread_id: str,
    session_id: str,
) -> None:
    """Seed one child's coalescer with pending cumulative usage."""
    coalescer = state.usage_coalescer_for_child_thread(
        client,
        thread_id=thread_id,
        session_id=session_id,
    )
    coalescer.record(_usage_event(thread_id)["params"])


async def _handle_child_event(
    client: _RecordingClient,
    state: fwd._CodexForwarderState,
    tracker: fwd._CodexElicitationTaskTracker,
    event: dict,
) -> None:
    """Route a child event through the production event dispatcher."""
    await fwd._handle_event(
        client,
        session_id="conv_parent",
        bridge_dir=Path(),
        event=event,
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_parent"),
        elicitation_tracker=tracker,
        expected_thread_id="thread_parent",
        forwarder_state=state,
    )


_GENEROUS_BUDGET_SECONDS = 60.0


@pytest.fixture(autouse=True)
def _generous_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep wall-clock budgets from expiring on a loaded runner; bound tests shrink them."""
    monkeypatch.setattr(fwd, "_CHILD_MODEL_RECOVERY_TIMEOUT_SECONDS", _GENEROUS_BUDGET_SECONDS)
    monkeypatch.setattr(fwd, "_CHILD_USAGE_SHUTDOWN_TIMEOUT_SECONDS", _GENEROUS_BUDGET_SECONDS)


@pytest.mark.asyncio
async def test_usage_coalescer_seeded_model_rides_along_so_child_usage_prices() -> None:
    """A coalescer seeded with a model attaches it to its token post."""
    client = _RecordingClient()
    coalescer = fwd._SessionUsageCoalescer(client, "conv_child", model="gpt-5.5")
    # Mirror the child path exactly: a usage frame with NO model in record().
    coalescer.record({"tokenUsage": {"total": {"inputTokens": 46003, "outputTokens": 4141}}})
    await coalescer.flush()

    assert len(client.posts) == 1  # one external_session_usage post
    url, body = client.posts[0]
    assert url == "/v1/sessions/conv_child/events"
    assert body["type"] == "external_session_usage"
    data = body["data"]
    # The server uses this model to price the child's cumulative tokens.
    assert data["model"] == "gpt-5.5"
    # The cumulative token counts the server prices from are present.
    assert data["cumulative_input_tokens"] == 46003
    assert data["cumulative_output_tokens"] == 4141


@pytest.mark.asyncio
async def test_child_usage_uses_each_child_resume_model_not_the_parent_model() -> None:
    """Sibling usage is routed and attributed by each child resume model.

    Codex usage notifications carry a thread id and cumulative token counts but
    no model. Each child's ``thread/resume`` response is authoritative, even
    when it differs from both the parent and sibling models.
    """
    client = _RecordingClient()
    codex_client = MagicMock()
    codex_client.request = AsyncMock()
    children = (("thread_a", "conv_a"), ("thread_b", "conv_b"))
    state = _child_state(codex_client, children=children)
    tracker = fwd._CodexElicitationTaskTracker()

    for (thread_id, session_id), model in zip(children, ("model-a", "model-b"), strict=True):
        await fwd._apply_child_resume(
            client,
            parent_session_id="conv_parent",
            child_session_id=session_id,
            child_thread_id=thread_id,
            response=_resume_response(thread_id, model),
            forwarder_state=state,
        )
    client.posts.clear()

    await _handle_child_event(
        client,
        state,
        tracker,
        _usage_event(
            "thread_a",
            input_tokens=20_280,
            cached_input_tokens=9_984,
            output_tokens=23,
        ),
    )
    await _handle_child_event(client, state, tracker, _usage_event("thread_b"))

    usage_by_session = {url: body["data"] for url, body in _usage_posts(client)}
    assert {url: data["model"] for url, data in usage_by_session.items()} == {
        "/v1/sessions/conv_a/events": "model-a",
        "/v1/sessions/conv_b/events": "model-b",
    }
    assert usage_by_session["/v1/sessions/conv_a/events"]["cumulative_input_tokens"] == 20_280
    assert (
        usage_by_session["/v1/sessions/conv_a/events"]["cumulative_cache_read_input_tokens"]
        == 9_984
    )
    assert state.model == "parent-model"
    codex_client.request.assert_not_awaited()
    await tracker.close()


@pytest.mark.asyncio
async def test_child_usage_retries_model_discovery_after_initial_resume_race() -> None:
    """A live usage frame retries a child resume that initially raced its rollout."""
    client = _RecordingClient()
    codex_client = MagicMock()
    codex_client.request = AsyncMock(
        side_effect=[
            RuntimeError("no rollout found for thread id thread_child"),
            _resume_response("thread_child", "child-model"),
        ]
    )
    state = _child_state(codex_client)
    tracker = fwd._CodexElicitationTaskTracker()

    await fwd._backfill_child_thread(
        client,
        codex_client,
        parent_session_id="conv_parent",
        child_session_id="conv_child",
        child_thread_id="thread_child",
        forwarder_state=state,
    )
    assert state.model_for_child_thread("thread_child") is None

    await _handle_child_event(
        client,
        state,
        tracker,
        _usage_event("thread_child", cached_input_tokens=80),
    )

    assert codex_client.request.await_count == 2
    assert [(url, body["data"]["model"]) for url, body in _usage_posts(client)] == [
        ("/v1/sessions/conv_child/events", "child-model")
    ]
    await tracker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "live_failure",
    [
        pytest.param(RuntimeError("no rollout found for thread id thread_child"), id="not_ready"),
        pytest.param(ConnectionError("connection closed"), id="connection_error"),
    ],
)
async def test_child_usage_close_recovers_model_after_failed_live_discovery(
    live_failure: Exception,
) -> None:
    """Counts buffered through a failed live discovery post once shutdown recovers the model."""
    client = _RecordingClient()
    codex_client = MagicMock()
    codex_client.request = AsyncMock(
        side_effect=[live_failure, _resume_response("thread_child", "child-model")]
    )
    state = _child_state(codex_client)
    tracker = fwd._CodexElicitationTaskTracker()

    await _handle_child_event(
        client,
        state,
        tracker,
        _usage_event("thread_child", input_tokens=20_280, cached_input_tokens=9_984),
    )

    assert _usage_posts(client) == []
    assert state.child_usage_coalescers_by_thread["thread_child"].has_pending_usage

    await state.close_child_usage_coalescers(client)

    assert codex_client.request.await_count == 2
    codex_client.request.assert_awaited_with("thread/resume", {"threadId": "thread_child"})
    posted = _usage_posts(client)[0][1]["data"]
    assert (
        posted["model"],
        posted["cumulative_input_tokens"],
        posted["cumulative_cache_read_input_tokens"],
    ) == ("child-model", 20_280, 9_984)
    await tracker.close()


@pytest.mark.asyncio
async def test_child_usage_frame_bounds_hung_live_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung resume on the live path cannot stall event handling; the counts stay buffered."""
    monkeypatch.setattr(fwd, "_CHILD_MODEL_RECOVERY_TIMEOUT_SECONDS", 0.01)
    client = _RecordingClient()
    codex_client = MagicMock()

    async def hang(*_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    codex_client.request = AsyncMock(side_effect=hang)
    state = _child_state(codex_client)
    tracker = fwd._CodexElicitationTaskTracker()

    await asyncio.wait_for(
        _handle_child_event(
            client, state, tracker, _usage_event("thread_child", cached_input_tokens=80)
        ),
        timeout=1.0,
    )

    assert _usage_posts(client) == []
    assert state.child_usage_coalescers_by_thread["thread_child"].has_pending_usage
    assert state.needs_child_thread_backfill("thread_child")

    monkeypatch.setattr(fwd, "_CHILD_MODEL_RECOVERY_TIMEOUT_SECONDS", _GENEROUS_BUDGET_SECONDS)
    codex_client.request = AsyncMock(return_value=_resume_response("thread_child", "child-model"))
    await state.close_child_usage_coalescers(client)

    posted = _usage_posts(client)[0][1]["data"]
    assert (posted["model"], posted["cumulative_cache_read_input_tokens"]) == ("child-model", 80)
    await tracker.close()


@pytest.mark.asyncio
async def test_child_usage_frame_survives_cancelled_recovery() -> None:
    """Cancelling event handling mid-recovery propagates but keeps the counts buffered."""
    client = _RecordingClient()
    codex_client = MagicMock()
    resume_started = asyncio.Event()

    async def hang(*_args: object, **_kwargs: object) -> None:
        resume_started.set()
        await asyncio.Event().wait()

    codex_client.request = AsyncMock(side_effect=hang)
    state = _child_state(codex_client)
    tracker = fwd._CodexElicitationTaskTracker()

    handling = asyncio.create_task(
        _handle_child_event(
            client, state, tracker, _usage_event("thread_child", cached_input_tokens=80)
        )
    )
    await resume_started.wait()
    handling.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handling

    assert _usage_posts(client) == []
    assert state.child_usage_coalescers_by_thread["thread_child"].has_pending_usage

    codex_client.request = AsyncMock(return_value=_resume_response("thread_child", "child-model"))
    await state.close_child_usage_coalescers(client)

    posted = _usage_posts(client)[0][1]["data"]
    assert (posted["model"], posted["cumulative_cache_read_input_tokens"]) == ("child-model", 80)
    await tracker.close()


@pytest.mark.asyncio
async def test_child_usage_close_continues_after_recovery_exception() -> None:
    """One broken child recovery cannot prevent later child usage from flushing."""
    client = _RecordingClient()
    codex_client = MagicMock()
    codex_client.request = AsyncMock(
        side_effect=[
            ValueError("connection closed"),
            _resume_response("thread_b", "model-b"),
        ]
    )
    children = (("thread_a", "conv_a"), ("thread_b", "conv_b"))
    state = _child_state(codex_client, children=children)
    for thread_id, session_id in children:
        _record_child_usage(client, state, thread_id, session_id)

    await state.close_child_usage_coalescers(client)

    assert codex_client.request.await_count == 2
    assert [(url, body["data"]["model"]) for url, body in _usage_posts(client)] == [
        ("/v1/sessions/conv_b/events", "model-b")
    ]
    assert state.child_usage_coalescers_by_thread == {}


@pytest.mark.asyncio
async def test_child_usage_close_bounds_hung_model_recovery(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A hung app-server request cannot block child usage cleanup indefinitely."""

    async def wait_forever(*_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(fwd, "_CHILD_MODEL_RECOVERY_TIMEOUT_SECONDS", 0.01)
    client = _RecordingClient()
    codex_client = MagicMock()
    codex_client.request = AsyncMock(side_effect=wait_forever)
    children = (("thread_a", "conv_a"), ("thread_b", "conv_b"))
    state = _child_state(codex_client, children=children)
    for thread_id, session_id in children:
        _record_child_usage(client, state, thread_id, session_id)

    await asyncio.wait_for(state.close_child_usage_coalescers(client), timeout=0.25)

    codex_client.request.assert_awaited_once_with("thread/resume", {"threadId": "thread_a"})
    assert state.child_usage_coalescers_by_thread == {}
    assert client.posts == []
    unposted = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("Codex child usage remained unposted at shutdown")
    ]
    assert len(unposted) == 2


class _HangingPostClient:
    """Async client stub whose ``post`` never returns."""

    def __init__(self) -> None:
        self.calls = 0

    async def post(self, url: str, **_kwargs: object) -> httpx.Response:
        self.calls += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_child_usage_close_bounds_hung_usage_post(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A hung usage POST at shutdown is abandoned within the drain budget and reported."""
    monkeypatch.setattr(fwd, "_CHILD_USAGE_SHUTDOWN_TIMEOUT_SECONDS", 0.05)
    client = _HangingPostClient()
    state = _child_state()
    state.child_models_by_thread["thread_child"] = "child-model"
    _record_child_usage(client, state, "thread_child", "conv_child")  # type: ignore[arg-type]

    await asyncio.wait_for(state.close_child_usage_coalescers(client), timeout=1.0)  # type: ignore[arg-type]

    assert client.calls == 1
    assert state.child_usage_coalescers_by_thread == {}
    messages = [record.getMessage() for record in caplog.records]
    assert any(m.startswith("Codex child usage post timed out at shutdown") for m in messages)
    assert any(m.startswith("Codex child usage remained unposted at shutdown") for m in messages)


@pytest.mark.asyncio
async def test_child_usage_close_flushes_child_discovered_during_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nested child discovered by resume replay mid-shutdown still gets its usage posted."""
    client = _RecordingClient()
    state = _child_state()
    _record_child_usage(client, state, "thread_child", "conv_child")

    async def discover_nested_child(
        _client: httpx.AsyncClient,
        *,
        child_session_id: str,
        child_thread_id: str,
        forwarder_state: fwd._CodexForwarderState,
    ) -> None:
        if child_thread_id != "thread_child":
            return
        assert child_session_id == "conv_child"
        forwarder_state.note_child_thread("thread_nested", "conv_nested")
        forwarder_state.child_models_by_thread["thread_nested"] = "nested-model"
        _record_child_usage(client, forwarder_state, "thread_nested", "conv_nested")

    monkeypatch.setattr(fwd, "_recover_child_usage_model", discover_nested_child)

    await state.close_child_usage_coalescers(client)

    assert [(url, body["data"]["model"]) for url, body in _usage_posts(client)] == [
        ("/v1/sessions/conv_nested/events", "nested-model")
    ]
    assert state.child_usage_coalescers_by_thread == {}


@pytest.mark.asyncio
async def test_child_usage_model_less_resume_is_not_retried() -> None:
    """A model-less resume stops retries until child settings supply its model.

    The settings event updates later child attribution without mutating the
    parent's model.
    """
    client = _RecordingClient()
    codex_client = MagicMock()
    codex_client.request = AsyncMock(return_value=_resume_response("thread_child"))
    state = _child_state(codex_client)
    _record_child_usage(client, state, "thread_child", "conv_child")

    for _ in range(2):
        await fwd._recover_child_usage_model(
            client,
            child_session_id="conv_child",
            child_thread_id="thread_child",
            forwarder_state=state,
        )

    codex_client.request.assert_awaited_once_with("thread/resume", {"threadId": "thread_child"})
    assert state.model_for_child_thread("thread_child") is None
    assert client.posts == []

    tracker = fwd._CodexElicitationTaskTracker()
    await _handle_child_event(
        client,
        state,
        tracker,
        {
            "method": "thread/settings/updated",
            "params": {
                "threadId": "thread_child",
                "threadSettings": {"model": "settings-model"},
            },
        },
    )
    await _handle_child_event(
        client,
        state,
        tracker,
        _usage_event(
            "thread_child",
            input_tokens=200,
            cached_input_tokens=80,
            output_tokens=8,
        ),
    )

    assert state.model == "parent-model"
    usage_posts = [body for _url, body in _usage_posts(client)]
    assert [post["data"]["model"] for post in usage_posts] == [
        "settings-model",
        "settings-model",
    ]
    final_usage = usage_posts[-1]["data"]
    assert (
        final_usage["cumulative_input_tokens"],
        final_usage["cumulative_output_tokens"],
        final_usage["cumulative_cache_read_input_tokens"],
        final_usage["context_tokens"],
    ) == (200, 8, 80, 200)
    await tracker.close()


@pytest.mark.asyncio
async def test_child_usage_recovery_error_does_not_mark_failed_or_retry() -> None:
    """A refused usage-only recovery neither fails the child nor retries the resume.

    Backfill only mirrors history: a resume error outside the not-ready class is
    logged and marked done for this connection, and the child's usage stays
    buffered rather than being posted under the parent model.
    """
    client = _RecordingClient()
    codex_client = MagicMock()
    codex_client.request = AsyncMock(side_effect=RuntimeError("temporary resume failure"))
    state = _child_state(codex_client)
    _record_child_usage(client, state, "thread_child", "conv_child")

    for _ in range(2):
        await fwd._recover_child_usage_model(
            client,
            child_session_id="conv_child",
            child_thread_id="thread_child",
            forwarder_state=state,
        )

    assert codex_client.request.await_count == 1
    assert state.model_for_child_thread("thread_child") is None
    assert not state.needs_child_thread_backfill("thread_child")
    assert _usage_posts(client) == []
    assert not any(
        body["type"] == "external_session_status" and body["data"]["status"] == "failed"
        for _url, body in client.posts
    )


@pytest.mark.asyncio
async def test_usage_coalescer_unseeded_omits_model() -> None:
    """Without a seed (and no model via record), the post carries no model.

    This is the pre-fix behavior that left a sub-agent's cost unpriced; the
    test pins the contrast so a regression dropping the seed is caught (the
    post would silently go back to model-less and the budget gap would return).
    """
    client = _RecordingClient()
    coalescer = fwd._SessionUsageCoalescer(client, "conv_child")  # no model seed
    coalescer.record({"tokenUsage": {"total": {"inputTokens": 100, "outputTokens": 5}}})
    await coalescer.flush()

    assert len(client.posts) == 1
    _url, body = client.posts[0]
    assert "model" not in body["data"]
