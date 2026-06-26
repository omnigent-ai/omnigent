"""
Tests for the codex-native forwarder's model-change sync-back
(:mod:`omnigent.codex_native_forwarder`).

For codex-native, ``config.toml``'s ``model`` key is the cost-policy source
of truth (it is what an in-TUI ``/model`` writes). At subscription and at
each ``turn/started`` the forwarder reads it (``_refresh_model_from_config``,
which delegates to the shared ``read_codex_config_model`` in the bridge
module) onto ``_CodexForwarderState.model`` and mirrors it to the Omnigent server
as an ``external_model_change`` event (→ persisted ``conv.model_override``)
so the cost-budget policy resolves the selected model. The startup/spawn
model IS mirrored (so Omnigent learns the session's model even when unchanged);
only an already-mirrored value is not re-posted.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from omnigent import codex_native_forwarder as fwd
from omnigent.codex_native_bridge import (
    CodexNativeBridgeState,
    codex_home_for_bridge_dir,
    read_bridge_state,
    write_bridge_state,
)


class _RecordingClient:
    """
    Async ``httpx`` client stub that records POSTs and returns HTTP 200.

    Only ``post`` is exercised by ``_post_session_event``; each call is
    recorded so the test can assert exactly what was mirrored.
    """

    def __init__(self) -> None:
        """Initialize with an empty record of posts."""
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, *, json: dict) -> httpx.Response:
        """
        Record ``(url, json)`` and return a 200 response.

        :param url: Request URL, e.g. ``"/v1/sessions/conv_x/events"``.
        :param json: JSON body, e.g.
            ``{"type": "external_model_change", "data": {"model": "gpt-5.4"}}``.
        :returns: A real ``httpx.Response`` with status 200.
        """
        self.posts.append((url, json))
        return httpx.Response(200, request=httpx.Request("POST", url))


def _state(model: str | None, posted_model: str | None) -> fwd._CodexForwarderState:
    """
    Build a forwarder state with the given current + last-mirrored model.

    :param model: Current Codex model, e.g. ``"gpt-5.4"`` or ``None``.
    :param posted_model: Last-mirrored model baseline, e.g. ``"gpt-5.5"``.
    :returns: A ``_CodexForwarderState`` for the sync-back helper.
    """
    state = fwd._CodexForwarderState()
    state.model = model
    state.posted_model = posted_model
    return state


@pytest.mark.asyncio
async def test_sync_model_change_posts_on_change() -> None:
    """A model differing from the baseline posts external_model_change.

    The in-TUI ``/model`` switch (gpt-5.5 → gpt-5.4) must mirror to Omnigent as
    an ``external_model_change`` and advance the baseline so it isn't
    re-posted. A missing post here is exactly the bug a user hit: the
    terminal model changed but the cost policy kept seeing gpt-5.5.
    """
    client = _RecordingClient()
    state = _state(model="gpt-5.4", posted_model="gpt-5.5")

    await fwd._sync_model_change(client, session_id="conv_x", forwarder_state=state)

    # Exactly one mirror post, carrying the new raw codex model id.
    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {"type": "external_model_change", "data": {"model": "gpt-5.4"}},
        )
    ]
    # Baseline advanced → the same model won't re-post on the next update.
    assert state.posted_model == "gpt-5.4"


@pytest.mark.asyncio
async def test_sync_model_change_no_post_when_unchanged() -> None:
    """Model equal to the baseline (seeded spawn default) does not post.

    Prevents the spawn/startup model from being echoed back to Omnigent as a
    spurious "change" (which would also fire on every settings update).
    """
    client = _RecordingClient()
    state = _state(model="gpt-5.5", posted_model="gpt-5.5")

    await fwd._sync_model_change(client, session_id="conv_x", forwarder_state=state)

    assert client.posts == []


@pytest.mark.asyncio
async def test_sync_model_change_no_post_when_model_unknown() -> None:
    """No model observed yet (``None``) → nothing to mirror."""
    client = _RecordingClient()
    state = _state(model=None, posted_model="gpt-5.5")

    await fwd._sync_model_change(client, session_id="conv_x", forwarder_state=state)

    assert client.posts == []


def _write_codex_config(bridge_dir: Path, body: str) -> Path:
    """
    Write a ``config.toml`` into the session's per-session ``CODEX_HOME``.

    :param bridge_dir: The bridge dir whose ``codex-home/config.toml`` is
        written (the path the model reader reads).
    :param body: Raw TOML body, e.g. ``'model = "gpt-5.4"\\n'``.
    :returns: The written ``config.toml`` path.
    """
    home = codex_home_for_bridge_dir(bridge_dir)
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.toml"
    path.write_text(body)
    return path


def test_refresh_model_from_config_updates_state(tmp_path: Path) -> None:
    """``config.toml``'s model lands on the forwarder state for mirroring.

    This is the exact path the subscription and ``turn/started`` handlers use
    to learn the user's ``/model`` selection: read config.toml (via the
    shared ``read_codex_config_model``) → set ``forwarder_state.model`` →
    ``_sync_model_change`` mirrors it to AP. The config.toml parsing itself
    is covered in ``tests/test_codex_native_bridge.py``; this asserts the
    forwarder wires the read into its state.
    """
    _write_codex_config(tmp_path, 'model = "gpt-5.4"\n')
    state = _state(model="gpt-5.5", posted_model="gpt-5.5")

    fwd._refresh_model_from_config(tmp_path, state)

    # The selected model (gpt-5.4) replaces the prior value, ready to mirror.
    assert state.model == "gpt-5.4"


def test_note_resume_response_records_model_without_seeding_baseline() -> None:
    """The startup/resume model is recorded but the baseline stays unset.

    Omnigent must learn the session's ACTUAL model — including the spawn default —
    because the cost gate resolves ``conv.model_override or spec.llm.model``
    and for codex the spawn model is frequently NOT ``spec.llm.model``. So
    ``note_resume_response`` records ``model`` but leaves ``posted_model``
    ``None``, so the next ``_sync_model_change`` mirrors the real model. If
    this re-seeded the baseline, an unchanged cheap session would never post
    ``external_model_change`` and the gate would wrongly DENY it.
    """
    state = fwd._CodexForwarderState()

    state.note_resume_response({"result": {"model": "gpt-5.4-mini"}})

    assert state.model == "gpt-5.4-mini"
    # Baseline NOT seeded → the spawn model will be mirrored on the next sync.
    assert state.posted_model is None


@pytest.mark.asyncio
async def test_sync_after_resume_posts_spawn_model() -> None:
    """End-to-end: an unchanged spawn model is mirrored to AP.

    This is the regression for the wrongly-blocked cheap session: codex
    spawned on gpt-5.4-mini, the model never "changed", yet Omnigent must still
    receive it as ``model_override`` so the cost gate sees a cheap model
    instead of falling back to the spec model and DENYing.
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState()
    state.note_resume_response({"result": {"model": "gpt-5.4-mini"}})

    await fwd._sync_model_change(client, session_id="conv_x", forwarder_state=state)

    # The spawn model is mirrored (not suppressed as "unchanged").
    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {"type": "external_model_change", "data": {"model": "gpt-5.4-mini"}},
        )
    ]
    assert state.posted_model == "gpt-5.4-mini"


def test_thread_settings_updated_records_effort_and_collaboration_mode() -> None:
    """
    ``thread/settings/updated`` records Codex's live thinking settings.

    App-server sends the public ``ThreadSettings`` shape with ``effort`` and
    ``collaborationMode``. If this parser regresses, the later sync helpers have
    no state to mirror, so Omnigent would keep stale ``reasoning_effort`` and
    mode metadata even though Codex changed them.
    """
    state = fwd._CodexForwarderState()

    state.note_thread_settings_updated(
        {
            "threadSettings": {
                "model": "gpt-5.4-codex",
                "effort": "medium",
                "collaborationMode": {
                    "mode": "plan",
                    "settings": {
                        "model": "gpt-5.4-codex",
                        "reasoning_effort": "medium",
                        "developer_instructions": None,
                    },
                },
            }
        }
    )

    assert state.model == "gpt-5.4-codex"
    assert state.effort == "medium"
    assert state.collaboration_mode == "plan"


@pytest.mark.asyncio
async def test_sync_reasoning_effort_change_posts_and_dedupes() -> None:
    """
    Codex effort changes mirror to Omnigent exactly once per observed value.

    The first sync must POST ``external_reasoning_effort_change`` so the server
    persists ``conversation.reasoning_effort``. The second sync with the same
    value must not re-post; otherwise every repeated settings notification would
    churn the session stream.
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState(effort="medium")

    await fwd._sync_reasoning_effort_change(
        client,
        session_id="conv_x",
        forwarder_state=state,
    )
    await fwd._sync_reasoning_effort_change(
        client,
        session_id="conv_x",
        forwarder_state=state,
    )

    # One post proves the new effort reached AP; no second post proves the
    # dedupe baseline advanced after a successful mirror.
    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {
                "type": "external_reasoning_effort_change",
                "data": {"reasoning_effort": "medium"},
            },
        )
    ]
    assert state.posted_effort == "medium"
    assert state.posted_effort_known is True


@pytest.mark.asyncio
async def test_sync_reasoning_effort_change_posts_clear() -> None:
    """
    Codex clearing effort mirrors JSON null to Omnigent.

    ``None`` is a meaningful observed value (model/default effort), so the
    forwarder must still post it after a prior explicit effort. If this returned
    early on falsey ``None``, Omnigent would keep a stale explicit effort.
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState(
        effort=None,
        posted_effort="high",
        posted_effort_known=True,
    )

    await fwd._sync_reasoning_effort_change(
        client,
        session_id="conv_x",
        forwarder_state=state,
    )

    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {
                "type": "external_reasoning_effort_change",
                "data": {"reasoning_effort": None},
            },
        )
    ]
    assert state.posted_effort is None
    assert state.posted_effort_known is True


@pytest.mark.asyncio
async def test_sync_codex_collaboration_mode_change_posts_and_dedupes() -> None:
    """
    Codex collaboration mode changes mirror to Omnigent labels once.

    The ``mode`` value is the durable "Plan vs Default" signal we can get from
    app-server. Missing this POST would leave the session snapshot without the
    current Codex mode.
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState(collaboration_mode="plan")

    await fwd._sync_codex_collaboration_mode_change(
        client,
        session_id="conv_x",
        forwarder_state=state,
    )
    await fwd._sync_codex_collaboration_mode_change(
        client,
        session_id="conv_x",
        forwarder_state=state,
    )

    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {
                "type": "external_codex_collaboration_mode_change",
                "data": {"mode": "plan"},
            },
        )
    ]
    assert state.posted_collaboration_mode == "plan"


@pytest.mark.parametrize(
    "content,expected",
    [
        ([{"type": "image", "url": "data:image/png;base64,AAAA"}], True),
        ([{"type": "input_file", "file_data": "data:application/pdf;base64,AAAA"}], True),
        ([{"type": "text", "text": "hi"}, {"type": "image", "url": "data:x"}], True),
        ([{"type": "text", "text": "only text"}], False),
        ([], False),
        ("not a list", False),
    ],
)
def test_user_message_has_file_content(content: object, expected: bool) -> None:
    """
    Detect a non-text (image/file) block in a Codex ``userMessage``.

    Drives the gate that decides whether a text-less ``userMessage`` is a
    real image-bearing message that must be persisted. ``True`` for any
    block whose ``type`` is not ``"text"``, else ``False``. A wrong result
    re-opens the image-only regression (text-less image skipped → dropped
    bubble + pending-FIFO bleed) or makes text-only messages post twice.
    """
    assert fwd._user_message_has_file_content({"content": content}) is expected


@pytest.mark.asyncio
async def test_post_user_message_image_only_posts_empty_content() -> None:
    """
    An image-only ``userMessage`` is posted with EMPTY Omnigent content.

    Regression guard for the image-only bleed/ordering bug: the forwarder
    must post the user item (so the server drains the pending-input FIFO
    entry and folds the image in by file_id). The posted content is empty
    — the base64 ``data:`` URL Codex echoes must NOT be written into text.
    A bail here would drop the user bubble and leak the pending entry into
    the next message.
    """
    client = _RecordingClient()
    item = {
        "type": "userMessage",
        "content": [{"type": "image", "url": "data:image/png;base64,AAAA"}],
    }

    await fwd._post_user_message(client, "conv_x", {"turnId": "t1"}, item)

    assert len(client.posts) == 1, "image-only userMessage must still be posted"
    _url, body = client.posts[0]
    item_data = body["data"]["item_data"]
    assert item_data["role"] == "user"
    # Empty content: the image is supplied server-side from the pending
    # entry; echoing Codex's base64 url here would re-introduce the freeze.
    assert item_data["content"] == []


@pytest.mark.asyncio
async def test_post_user_message_text_posts_input_text() -> None:
    """A text ``userMessage`` posts an ``input_text`` block (unchanged path)."""
    client = _RecordingClient()
    item = {"type": "userMessage", "content": [{"type": "text", "text": "hello"}]}

    await fwd._post_user_message(client, "conv_x", {"turnId": "t1"}, item)

    assert len(client.posts) == 1
    _url, body = client.posts[0]
    assert body["data"]["item_data"]["content"] == [{"type": "input_text", "text": "hello"}]


@pytest.mark.asyncio
async def test_post_user_message_truly_empty_is_skipped() -> None:
    """
    A ``userMessage`` with neither text nor a file block is not posted.

    Without this guard the forwarder would emit spurious empty user
    bubbles. A failure (a post recorded) means the empty-skip branch broke.
    """
    client = _RecordingClient()
    item = {"type": "userMessage", "content": []}

    await fwd._post_user_message(client, "conv_x", {"turnId": "t1"}, item)

    assert client.posts == []


# ── sub-agent usage pricing: seed the child coalescer's model ──────────


@pytest.mark.asyncio
async def test_usage_coalescer_seeded_model_rides_along_so_child_usage_prices() -> None:
    """A coalescer seeded with a model attaches it to its token post.

    Codex sub-agent (child-thread) usage is recorded on a coalescer created on
    the child-event path, where ``forwarder_state`` (the usual model source) is
    intentionally ``None`` — so ``record()`` receives no model. Without the
    constructor seed the token post carries no ``model``, the server leaves the
    child's ``total_cost_usd`` unpriced (``None``), and the sub-agent's spend
    drops out of the parent's subtree cost — letting it run past the budget.
    The seeded model must ride along on every token post so the server can
    price the cumulative tokens.
    """
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
    # The seeded model rides along — this is what lets the server price the
    # tokens into the child's total_cost_usd (the whole point of the fix).
    assert data["model"] == "gpt-5.5"
    # The cumulative token counts the server prices from are present.
    assert data["cumulative_input_tokens"] == 46003
    assert data["cumulative_output_tokens"] == 4141


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


class _FlakyElicitationClient:
    """
    Elicitation client stub: configurable failures, then HTTP 200.

    Real stub (not MagicMock) so unexpected extra calls surface in
    :attr:`posts`. Each call records ``(url, json)``. The first
    ``transport_failures`` calls raise ``httpx.ReadError`` (a severed
    long-poll) and the next ``gateway_failures`` calls return HTTP 502
    (a proxy gateway error); every later call returns 200 with a
    JSON-RPC result body.

    :param transport_failures: Calls to fail with ``httpx.ReadError``
        before succeeding, e.g. ``1``.
    :param gateway_failures: Calls to answer with HTTP 502 after the
        transport failures, e.g. ``0``.
    """

    def __init__(self, transport_failures: int = 0, gateway_failures: int = 0) -> None:
        self.posts: list[tuple[str, dict]] = []
        self._transport_failures = transport_failures
        self._gateway_failures = gateway_failures

    async def post(self, url: str, *, json: dict, timeout: httpx.Timeout) -> httpx.Response:
        """
        Record the call and fail/succeed per the configured schedule.

        :param url: Request URL, e.g.
            ``"/v1/sessions/conv_x/hooks/codex-elicitation-request"``.
        :param json: Codex JSON-RPC request envelope.
        :param timeout: Per-attempt budget (ignored by the stub).
        :returns: HTTP 502 during the gateway-failure window, else 200
            with a JSON-RPC result body.
        :raises httpx.ReadError: During the transport-failure window.
        """
        self.posts.append((url, json))
        attempt = len(self.posts)
        if attempt <= self._transport_failures:
            raise httpx.ReadError(
                "proxy severed the long-poll",
                request=httpx.Request("POST", url),
            )
        if attempt <= self._transport_failures + self._gateway_failures:
            return httpx.Response(502, request=httpx.Request("POST", url))
        return httpx.Response(
            200,
            json={"action": "accept", "content": {}, "_meta": None},
            request=httpx.Request("POST", url),
        )


async def _instant_retry_sleep(_seconds: float) -> None:
    """
    Drop-in for ``_elicitation_retry_sleep`` that returns at once.

    :param _seconds: Ignored backoff duration.
    :returns: None.
    """
    return


_ELICITATION_EVENT: dict = {
    "id": 7,
    "method": "mcpServer/elicitation/request",
    "params": {"mode": "form", "message": "Pick a date"},
}


@pytest.mark.asyncio
async def test_elicitation_post_reposts_after_transport_cut_with_same_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A severed elicitation long-poll is re-POSTed with the identical envelope.

    This is the invisible-stuck bug for codex sub-agents: one transport
    error used to abandon the prompt to the native-TUI path nobody is
    watching. The envelope must be byte-identical on the retry — the
    server derives the deterministic elicitation id from (session,
    method, rpc id), so an identical re-POST re-parks the SAME prompt
    and keeps the approval card alive.
    """
    monkeypatch.setattr(fwd, "_elicitation_retry_sleep", _instant_retry_sleep)
    client = _FlakyElicitationClient(transport_failures=1)

    response = await fwd._post_codex_elicitation_request(
        client,  # type: ignore[arg-type]  # stub implements the one used method
        "conv_x",
        event=_ELICITATION_EVENT,
    )

    assert response is not None
    assert response.status_code == 200
    # 2 = one severed attempt + one successful retry. 1 means the
    # transport error abandoned the prompt (the production bug).
    assert len(client.posts) == 2, f"expected 2 attempts, got {len(client.posts)}"
    # Identical (url, envelope) on the retry is the re-park contract.
    assert client.posts[0] == client.posts[1]
    assert client.posts[0][0] == "/v1/sessions/conv_x/hooks/codex-elicitation-request"


@pytest.mark.asyncio
async def test_elicitation_post_retries_gateway_5xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A 5xx (proxy gateway error on a severed long-poll) is retried.

    The Databricks Apps proxy answers a killed upstream long-poll with
    502/504 rather than a clean transport error; the verdict may still
    be pending server-side, so the forwarder must re-park rather than
    treat it as final.
    """
    monkeypatch.setattr(fwd, "_elicitation_retry_sleep", _instant_retry_sleep)
    client = _FlakyElicitationClient(gateway_failures=1)

    response = await fwd._post_codex_elicitation_request(
        client,  # type: ignore[arg-type]
        "conv_x",
        event=_ELICITATION_EVENT,
    )

    assert response is not None
    assert response.status_code == 200
    # 2 = the 502 attempt + the successful retry; 1 would mean 5xx was
    # treated as a final answer and the prompt abandoned.
    assert len(client.posts) == 2


@pytest.mark.asyncio
async def test_elicitation_post_4xx_is_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A 4xx is a deliberate server rejection — returned without retry.

    Retrying a rejection would hammer the server with a request it
    already refused; the caller logs it and leaves the native request
    unanswered.
    """

    class _RejectingClient:
        """Client stub answering every elicitation POST with HTTP 400."""

        def __init__(self) -> None:
            self.posts: list[tuple[str, dict]] = []

        async def post(self, url: str, *, json: dict, timeout: httpx.Timeout) -> httpx.Response:
            """
            Record the call and reject it.

            :param url: Request URL.
            :param json: Codex JSON-RPC request envelope.
            :param timeout: Per-attempt budget (ignored by the stub).
            :returns: HTTP 400.
            """
            self.posts.append((url, json))
            return httpx.Response(400, request=httpx.Request("POST", url))

    monkeypatch.setattr(fwd, "_elicitation_retry_sleep", _instant_retry_sleep)
    client = _RejectingClient()

    response = await fwd._post_codex_elicitation_request(
        client,  # type: ignore[arg-type]
        "conv_x",
        event=_ELICITATION_EVENT,
    )

    assert response is not None
    assert response.status_code == 400
    # 1 = the rejection was final; 2+ means 4xx is being retried.
    assert len(client.posts) == 1


@pytest.mark.asyncio
async def test_elicitation_post_returns_none_when_budget_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An exhausted retry budget returns ``None`` (caller leaves the
    native request unanswered, matching the old single-attempt outcome).
    """
    # Budget smaller than the first backoff → exactly one attempt.
    monkeypatch.setattr(fwd, "_CODEX_ELICITATION_REQUEST_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(fwd, "_elicitation_retry_sleep", _instant_retry_sleep)
    client = _FlakyElicitationClient(transport_failures=100)

    response = await fwd._post_codex_elicitation_request(
        client,  # type: ignore[arg-type]
        "conv_x",
        event=_ELICITATION_EVENT,
    )

    assert response is None
    # 1 = the deadline check stopped the loop before a second attempt
    # (backoff 1.0s > 0.5s budget); more means the budget is ignored.
    assert len(client.posts) == 1


class _StatusClient:
    """httpx client stub whose ``post`` returns a fixed status code."""

    def __init__(self, status_code: int) -> None:
        """:param status_code: Status to return from every post, e.g. ``400``."""
        self.status_code = status_code
        self.posts = 0

    async def post(self, url: str, *, json: dict) -> httpx.Response:
        """Return the configured status; never raises."""
        del json
        self.posts += 1
        return httpx.Response(self.status_code, request=httpx.Request("POST", url))


def test_forward_failures_escalate_to_degraded_once() -> None:
    """
    Sustained forward failures flip the degraded latch exactly once (#1120).

    Network drops previously surfaced only as scattered per-item warnings;
    the latch turns a real outage into a single loud signal and does not
    re-fire per dropped item.
    """
    fwd._reset_forward_health()

    for _ in range(fwd._FORWARD_DEGRADED_THRESHOLD - 1):
        fwd._note_forward_failure("external_output_text_delta")
    # Below threshold: not yet degraded.
    assert fwd._forward_health.degraded_logged is False

    fwd._note_forward_failure("external_output_text_delta")  # crosses threshold
    assert fwd._forward_health.degraded_logged is True
    assert fwd._forward_health.consecutive_failures == fwd._FORWARD_DEGRADED_THRESHOLD

    # The latch holds — further failures keep counting but don't re-escalate.
    fwd._note_forward_failure("external_output_text_delta")
    assert fwd._forward_health.degraded_logged is True
    assert fwd._forward_health.consecutive_failures == fwd._FORWARD_DEGRADED_THRESHOLD + 1


def test_forward_success_resets_degraded_state() -> None:
    """
    A successful forward clears the failure count and degraded latch.

    Recovery must re-arm the indicator so a later outage escalates again.
    """
    fwd._reset_forward_health()
    for _ in range(fwd._FORWARD_DEGRADED_THRESHOLD):
        fwd._note_forward_failure("external_session_usage")
    assert fwd._forward_health.degraded_logged is True

    fwd._note_forward_success()

    assert fwd._forward_health.consecutive_failures == 0
    assert fwd._forward_health.degraded_logged is False


@pytest.mark.asyncio
async def test_post_session_event_tracks_success_and_failure() -> None:
    """
    _post_session_event classifies each outcome into forward health (#1120).

    A 2xx clears the failure run; a permanent 4xx counts as a failure so a
    sustained outage can escalate.
    """
    fwd._reset_forward_health()

    # A permanent 4xx is a failure.
    await fwd._post_session_event(
        _StatusClient(400), "conv_x", event_type="external_session_status", data={"status": "idle"}
    )
    assert fwd._forward_health.consecutive_failures == 1

    # A 2xx resets the run.
    await fwd._post_session_event(
        _RecordingClient(), "conv_x", event_type="external_session_status", data={"status": "idle"}
    )
    assert fwd._forward_health.consecutive_failures == 0


# ── #1108: turn-error "silent success" → surfaced failed ──────────────
#
# A failed Codex turn arrives as ``turn/completed`` (a clean success boundary)
# with ``turn.status == "failed"`` and a ``turn.error`` object. These tests pin
# the surface-only fix: such turns are forced to ``failed``, the reason is
# surfaced as the status output, auth errors (codexErrorInfo / 401-403) carry a
# re-auth hint, the resume path reaches the same verdict, an empty turn is idle
# (+ WARN), and a genuinely clean turn still reports success.


def _seed_active_turn(bridge_dir: Path, turn_id: str) -> None:
    """
    Seed bridge state so a terminal turn edge clears the active turn.

    ``_terminal_turn_status_edge`` only produces an edge when the terminal
    event clears the recorded active turn id; without this seed it returns
    ``None`` as "stale".

    :param bridge_dir: Native Codex bridge directory (the test ``tmp_path``).
    :param turn_id: Active Codex turn id to record, e.g. ``"turn_123"``.
    :returns: None.
    """
    write_bridge_state(
        bridge_dir,
        CodexNativeBridgeState(
            session_id="conv_x",
            socket_path=str(bridge_dir / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(bridge_dir / "codex-home"),
            active_turn_id=turn_id,
        ),
    )


def test_classify_codex_error_auth_vs_generic() -> None:
    """The shared classifier flags auth errors and leaves the rest generic.

    This is the single classifier reused by both the live and resume paths;
    if it regresses, an expired-login failure would surface without the
    re-auth hint (or a disk-full error would wrongly demand re-auth). It
    prefers ``codexErrorInfo`` (variant / httpStatusCode) and falls back to
    the message text.
    """
    auth = fwd._CODEX_ERROR_KIND_AUTH
    generic = fwd._CODEX_ERROR_KIND_GENERIC
    # Structured codexErrorInfo: string variant, tagged object, http status.
    assert fwd._classify_codex_error({"codexErrorInfo": "Unauthorized"}, "nope") == auth
    assert fwd._classify_codex_error({"codexErrorInfo": {"type": "Unauthorized"}}, "nope") == auth
    assert fwd._classify_codex_error({"codexErrorInfo": {"httpStatusCode": 401}}, "nope") == auth
    # Message-text fallback when codexErrorInfo is absent.
    assert fwd._classify_codex_error({}, "Please run codex login") == auth
    assert fwd._classify_codex_error({}, "ChatGPT session expired") == auth
    assert fwd._classify_codex_error({"codexErrorInfo": "Other"}, "disk full") == generic


def test_terminal_error_from_turn_reads_and_classifies_turn_error() -> None:
    """``_terminal_error_from_turn`` returns the classified ``turn.error``.

    The helper is the single source of truth for "did this turn fail"; both
    edge builders depend on it, so it must read ``turn.error`` and classify it.
    """
    params = {
        "turn": {
            "id": "turn_123",
            "status": "failed",
            "error": {
                "message": "401 Unauthorized: login expired",
                "codexErrorInfo": "Unauthorized",
            },
        }
    }

    error = fwd._terminal_error_from_turn(params)

    assert error is not None
    assert error.message == "401 Unauthorized: login expired"
    assert error.kind == fwd._CODEX_ERROR_KIND_AUTH
    assert error.is_auth is True


def test_terminal_error_from_turn_falls_back_to_error_item() -> None:
    """With no ``turn.error``, an ``error`` ThreadItem in ``turn.items`` is used.

    Both shapes exist in the app-server type system; the fallback keeps the fix
    correct on the version/path that emits the error as an item rather than as a
    ``turn.error`` object.
    """
    params = {
        "turn": {
            "id": "turn_123",
            "status": "completed",
            "items": [
                {"type": "agentMessage", "id": "a", "text": "working"},
                {"type": "error", "message": "please run codex login"},
            ],
        }
    }

    error = fwd._terminal_error_from_turn(params)

    assert error is not None
    assert error.message == "please run codex login"
    assert error.is_auth is True


def test_terminal_error_from_turn_prefers_turn_error_over_item() -> None:
    """``turn.error`` wins when both it and an ``error`` item are present."""
    params = {
        "turn": {
            "id": "turn_123",
            "status": "failed",
            "error": {"message": "from turn.error"},
            "items": [{"type": "error", "message": "from item"}],
        }
    }

    error = fwd._terminal_error_from_turn(params)

    assert error is not None
    assert error.message == "from turn.error"


def test_terminal_error_from_turn_none_for_clean_turn() -> None:
    """A turn with no ``error`` object or item yields ``None`` (no false positives)."""
    params = {
        "turn": {
            "id": "turn_123",
            "status": "completed",
            "items": [{"type": "agentMessage", "id": "a", "text": "done"}],
        }
    }

    assert fwd._terminal_error_from_turn(params) is None


def test_terminal_turn_status_edge_error_item_forces_failed(tmp_path: Path) -> None:
    """A ``turn/completed`` carrying an ``error`` item (no ``turn.error``) fails.

    The item-fallback path must flip the live edge to ``failed`` just like the
    ``turn.error`` path does.
    """
    _seed_active_turn(tmp_path, "turn_123")
    params = {
        "turn": {
            "id": "turn_123",
            "status": "completed",
            "items": [{"type": "error", "message": "model stream broke"}],
        }
    }

    edge = fwd._terminal_turn_status_edge(tmp_path, "turn/completed", params)

    assert edge is not None
    assert edge.status == "failed"
    assert edge.error is not None
    assert edge.error.message == "model stream broke"
    assert edge.source == "turn/completed:turn-error"


def test_terminal_turn_status_edge_turn_error_forces_failed(tmp_path: Path) -> None:
    """A ``turn/completed`` carrying ``turn.error`` is forced to ``failed``.

    This is the core of #1108: Codex reported a *completed* boundary, but the
    turn actually failed. The edge must be ``failed`` (not the silent ``idle``
    the method alone implies) and carry the classified error.
    """
    _seed_active_turn(tmp_path, "turn_123")
    params = {
        "turn": {
            "id": "turn_123",
            "status": "failed",
            "error": {"message": "model stream broke"},
        }
    }

    edge = fwd._terminal_turn_status_edge(tmp_path, "turn/completed", params)

    assert edge is not None
    assert edge.status == "failed"
    assert edge.turn_id == "turn_123"
    assert edge.error is not None
    assert edge.error.message == "model stream broke"
    assert edge.error.kind == fwd._CODEX_ERROR_KIND_GENERIC
    assert edge.source == "turn/completed:turn-error"


def test_terminal_turn_status_edge_auth_turn_error_classified(tmp_path: Path) -> None:
    """An auth-classified ``turn.error`` rides the failed edge as ``auth``."""
    _seed_active_turn(tmp_path, "turn_123")
    params = {
        "turn": {
            "id": "turn_123",
            "status": "failed",
            "error": {
                "message": "Forbidden",
                "codexErrorInfo": {"type": "Unauthorized", "httpStatusCode": 403},
            },
        }
    }

    edge = fwd._terminal_turn_status_edge(tmp_path, "turn/completed", params)

    assert edge is not None
    assert edge.status == "failed"
    assert edge.error is not None
    assert edge.error.is_auth is True


def test_terminal_turn_status_edge_failed_status_without_error(tmp_path: Path) -> None:
    """A ``turn.status == "failed"`` with no ``error`` object still fails.

    Defends against an app-server version that records the failed status but
    omits the populated ``turn.error`` — the edge must not fall back to ``idle``.
    """
    _seed_active_turn(tmp_path, "turn_123")
    params = {"turn": {"id": "turn_123", "status": "failed"}}

    edge = fwd._terminal_turn_status_edge(tmp_path, "turn/completed", params)

    assert edge is not None
    assert edge.status == "failed"
    assert edge.error is None
    assert edge.source == "turn/completed:turn-failed"


def test_terminal_turn_status_edge_clean_turn_still_idle(tmp_path: Path) -> None:
    """A genuinely clean ``turn/completed`` still maps to ``idle`` (regression).

    The turn-error check must not break the happy path: no error → the edge
    stays ``idle`` with no attached error.
    """
    _seed_active_turn(tmp_path, "turn_123")
    params = {
        "turn": {
            "id": "turn_123",
            "status": "completed",
            "items": [{"type": "agentMessage", "id": "a", "text": "all good"}],
        }
    }

    edge = fwd._terminal_turn_status_edge(tmp_path, "turn/completed", params)

    assert edge is not None
    assert edge.status == "idle"
    assert edge.error is None
    assert edge.source == "turn/completed"


def test_terminal_turn_status_edge_empty_turn_idle_and_warns(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A zero-item ``turn/completed`` maps to ``idle`` and emits a WARN.

    An empty turn is not an error, but it is unusual enough to log: it maps to
    ``idle`` (so the session closes) while a WARN records the anomaly.
    """
    _seed_active_turn(tmp_path, "turn_123")
    params = {"turn": {"id": "turn_123", "status": "completed", "items": []}}

    with caplog.at_level("WARNING", logger="omnigent.codex_native_forwarder"):
        edge = fwd._terminal_turn_status_edge(tmp_path, "turn/completed", params)

    assert edge is not None
    assert edge.status == "idle"
    assert edge.error is None
    assert any(
        "empty turn" in record.getMessage() and record.levelname == "WARNING"
        for record in caplog.records
    ), "expected a WARN log for the empty (zero-item) turn"


def test_omnigent_status_from_resume_turn_error_parity() -> None:
    """Resume parity: a completed resume turn carrying ``turn.error`` → ``failed``.

    Without this, a reconnect that backfills from ``thread/resume`` would close
    the session as ``idle`` even though the turn had errored — the resume-path
    half of the silent-success bug.
    """
    turn_with_error = {
        "id": "turn_123",
        "status": "completed",
        "error": {"message": "rate limited"},
    }
    turn_clean = {
        "id": "turn_123",
        "status": "completed",
        "items": [{"type": "agentMessage", "id": "a", "text": "hi"}],
    }

    assert fwd._omnigent_status_from_resume_turn(turn_with_error) == "failed"
    # Parity check: the clean turn still resolves to idle.
    assert fwd._omnigent_status_from_resume_turn(turn_clean) == "idle"


def test_resume_terminal_status_edge_attaches_error(tmp_path: Path) -> None:
    """The resume edge carries the classified error like the live edge does."""
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_x",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_123",
        ),
    )
    turns = [
        {
            "id": "turn_123",
            "status": "failed",
            "error": {"message": "please sign in again"},
        }
    ]

    edge = fwd._resume_terminal_status_edge_for_latest_turn(tmp_path, "thread_123", turns)

    assert edge is not None
    assert edge.status == "failed"
    assert edge.error is not None
    assert edge.error.is_auth is True
    assert edge.source == "thread/resume:turn-error"
    # The active turn id is cleared once the terminal edge is derived.
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id is None


@pytest.mark.asyncio
async def test_post_turn_status_edge_surfaces_generic_error_output() -> None:
    """A failed edge with a generic error surfaces the message as output.

    The reason must reach the server (as ``output``) rather than being dropped;
    a generic error carries no re-auth flag.
    """
    client = _RecordingClient()
    edge = fwd._CodexTurnStatusEdge(
        status="failed",
        turn_id="turn_123",
        source="turn/completed:turn-error",
        error=fwd._CodexTerminalError(
            message="model stream broke",
            kind=fwd._CODEX_ERROR_KIND_GENERIC,
        ),
    )

    await fwd._post_turn_status_edge(client, "conv_x", edge)

    assert len(client.posts) == 1
    _url, body = client.posts[0]
    assert body["type"] == "external_session_status"
    data = body["data"]
    assert data["status"] == "failed"
    assert data["output"] == "model stream broke"
    # Generic errors do not demand re-auth.
    assert "reauth_required" not in data


@pytest.mark.asyncio
async def test_post_turn_status_edge_auth_error_includes_reauth_hint() -> None:
    """A failed edge with an auth error flags re-auth and appends the hint."""
    client = _RecordingClient()
    edge = fwd._CodexTurnStatusEdge(
        status="failed",
        turn_id="turn_123",
        source="turn/completed:turn-error",
        error=fwd._CodexTerminalError(
            message="401 Unauthorized",
            kind=fwd._CODEX_ERROR_KIND_AUTH,
        ),
    )

    await fwd._post_turn_status_edge(client, "conv_x", edge)

    assert len(client.posts) == 1
    _url, body = client.posts[0]
    data = body["data"]
    assert data["status"] == "failed"
    assert data["reauth_required"] is True
    assert "401 Unauthorized" in data["output"]
    assert fwd._CODEX_REAUTH_HINT in data["output"]


@pytest.mark.asyncio
async def test_post_turn_status_edge_clean_idle_has_no_output() -> None:
    """A normal idle edge (no error) posts status only — the success path."""
    client = _RecordingClient()
    edge = fwd._CodexTurnStatusEdge(status="idle", turn_id="turn_123", source="turn/completed")

    await fwd._post_turn_status_edge(client, "conv_x", edge)

    assert len(client.posts) == 1
    _url, body = client.posts[0]
    data = body["data"]
    assert data["status"] == "idle"
    assert "output" not in data
    assert "reauth_required" not in data


@pytest.mark.asyncio
async def test_compaction_status_posts_and_dedupes_consecutive() -> None:
    """
    Compaction status mirrors as external_compaction_status, deduped (#1255).

    Codex may signal completion via both a ``contextCompaction`` item and a
    ``thread/compacted`` notification; consecutive identical statuses must
    not double-post (the spinner would flicker).
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    await fwd._post_compaction_status(client, "conv_x", "in_progress", forwarder_state=state)
    await fwd._post_compaction_status(client, "conv_x", "completed", forwarder_state=state)
    # Duplicate completion (e.g. item then notification) is suppressed.
    await fwd._post_compaction_status(client, "conv_x", "completed", forwarder_state=state)

    assert [post[1] for post in client.posts] == [
        {"type": "external_compaction_status", "data": {"status": "in_progress"}},
        {"type": "external_compaction_status", "data": {"status": "completed"}},
    ]
    assert state.compaction_status_posted == "completed"


@pytest.mark.asyncio
async def test_completed_context_compaction_item_clears_spinner() -> None:
    """
    A completed ``contextCompaction`` item posts compaction-completed.

    It is a status edge, not transcript history, so it must clear the
    spinner without being appended as a conversation item.
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState()
    state.compaction_status_posted = "in_progress"

    await fwd._handle_completed_item(
        client,
        "conv_x",
        {
            "threadId": "thread_1",
            "turnId": "turn_1",
            "item": {"type": "contextCompaction", "id": "item_c"},
        },
        forwarder_state=state,
    )

    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {"type": "external_compaction_status", "data": {"status": "completed"}},
        )
    ]


@pytest.mark.asyncio
async def test_reasoning_delta_opens_block_then_continues() -> None:
    """
    Codex reasoning deltas mirror as external_output_reasoning_delta (#1254).

    The first delta of a reasoning item opens the block (``started=True``
    → ``response.reasoning.started``); subsequent deltas for the same item
    continue it (``started=False``). Reasoning was previously dropped — only
    the effort *level* synced, never the thinking text.
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    await fwd._handle_reasoning_delta(
        client,
        "conv_x",
        {"turnId": "turn_1", "itemId": "item_r", "delta": "Let me "},
        state,
    )
    await fwd._handle_reasoning_delta(
        client,
        "conv_x",
        {"turnId": "turn_1", "itemId": "item_r", "delta": "think."},
        state,
    )

    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {
                "type": "external_output_reasoning_delta",
                "data": {"delta": "Let me ", "started": True},
            },
        ),
        (
            "/v1/sessions/conv_x/events",
            {
                "type": "external_output_reasoning_delta",
                "data": {"delta": "think.", "started": False},
            },
        ),
    ]


@pytest.mark.asyncio
async def test_reasoning_delta_new_item_reopens_block() -> None:
    """
    A reasoning delta for a new item id opens a fresh block.

    Multi-step turns (reason → tool → reason) emit a second reasoning item;
    its first delta must re-open the block so the web UI starts a new
    "thinking" section rather than appending to the prior one.
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    await fwd._handle_reasoning_delta(
        client, "conv_x", {"itemId": "item_a", "delta": "first"}, state
    )
    await fwd._handle_reasoning_delta(
        client, "conv_x", {"itemId": "item_b", "delta": "second"}, state
    )

    started_flags = [post[1]["data"]["started"] for post in client.posts]
    assert started_flags == [True, True]


@pytest.mark.asyncio
async def test_reasoning_delta_skips_empty_non_opening_delta() -> None:
    """
    An empty delta that does not open a block is dropped (no noise post).

    The block-opening delta is always posted (even empty, to emit
    ``response.reasoning.started``); a later empty continuation carries
    nothing to render and must not POST.
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    # Opening delta (empty) still posts to open the block.
    await fwd._handle_reasoning_delta(client, "conv_x", {"itemId": "item_r", "delta": ""}, state)
    # Empty continuation for the same item is dropped.
    await fwd._handle_reasoning_delta(client, "conv_x", {"itemId": "item_r", "delta": ""}, state)

    assert len(client.posts) == 1
    assert client.posts[0][1]["data"] == {"delta": "", "started": True}
