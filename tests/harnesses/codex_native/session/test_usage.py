"""Usage tests for Codex session."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder
from tests.harnesses.codex_native.session._support import (
    _agent_message_delta_event,
    _completed_event,
    _elicitation_tracker,
    _expected_delta_data,
    _recording_forwarder_client,
    _usage_coalescer,
    _write_forwarder_bridge,
)


def _usage_event(input_tokens: int, context_window: int = 200_000) -> dict[str, Any]:
    """
    Build a Codex token-usage update event.

    :param input_tokens: Context token count, e.g. ``1234``.
    :param context_window: Context window size, e.g. ``200000``.
    :returns: App-server event payload.
    """
    return {
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": "thread_123",
            "tokenUsage": {
                "modelContextWindow": context_window,
                "total": {
                    "inputTokens": input_tokens,
                },
                "last": {"inputTokens": input_tokens},
            },
        },
    }


def test_forwarder_posts_codex_usage_live_per_frame(
    tmp_path: Path,
) -> None:
    """
    Codex usage posts live (per frame) so the web UI cost badge updates
    mid-turn, not only at the turn boundary.

    Codex emits ``thread/tokenUsage/updated`` every few seconds; the forwarder
    flushes the coalescer right after recording each frame so the server can
    price and broadcast cost immediately. (Previously usage was deferred to the
    turn boundary, leaving the cost badge stuck until the turn ended.) The
    sparse cadence means the per-frame post does not block the high-frequency
    text-delta path, which has its own coalescer.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    posted: list[dict[str, Any]] = []
    posts_after_usage_updates: list[dict[str, Any]] = []
    posts_after_text_flush: list[dict[str, Any]] = []

    async def run() -> None:
        """
        Replay usage updates, then text, then terminal completion.

        :returns: None.
        """
        async with _recording_forwarder_client(posted) as client:
            delta_coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=60.0,
                flush_char_threshold=1000,
            )
            usage_coalescer = codex_native_forwarder._SessionUsageCoalescer(
                client,
                "conv_123",
            )
            elicitation_tracker = _elicitation_tracker()
            for event in [_usage_event(100), _usage_event(150)]:
                await codex_native_forwarder._handle_event(
                    client,
                    session_id="conv_123",
                    bridge_dir=tmp_path,
                    event=event,
                    delta_coalescer=delta_coalescer,
                    usage_coalescer=usage_coalescer,
                    elicitation_tracker=elicitation_tracker,
                )
            posts_after_usage_updates.extend(posted)
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                event=_agent_message_delta_event("turn_123", "item_agent", "visible text"),
                delta_coalescer=delta_coalescer,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
            )
            await delta_coalescer.flush()
            posts_after_text_flush.extend(posted)
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                event=_completed_event("turn_123"),
                delta_coalescer=delta_coalescer,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
            )
            await delta_coalescer.close()
            await usage_coalescer.close()
            await elicitation_tracker.close()

    asyncio.run(run())

    # Usage now posts LIVE on each frame (so the cost badge moves mid-turn),
    # latest-only via the coalescer's dedup: 100 then 150. A value of ``[]``
    # here would mean usage was deferred again and the badge would stay stuck
    # until the turn boundary (the regression this guards).
    assert [p["type"] for p in posts_after_usage_updates] == [
        "external_session_usage",
        "external_session_usage",
    ]
    # The first frame posts every value (nothing posted yet); the second posts
    # only the CHANGED keys — context_window was unchanged, so the coalescer's
    # dedup drops it (proving latest-only diffing, not blind re-posting).
    assert posts_after_usage_updates[0]["data"] == {
        "context_tokens": 100,
        "context_window": 200_000,
        "cumulative_input_tokens": 100,
    }
    assert posts_after_usage_updates[1]["data"] == {
        "context_tokens": 150,
        "cumulative_input_tokens": 150,
    }
    # Text still streams via its own coalescer — the per-frame usage posts
    # neither swallowed nor blocked it.
    assert {
        "type": "external_output_text_delta",
        "data": _expected_delta_data("visible text", "turn_123", "item_agent"),
    } in posts_after_text_flush
    # Exactly two usage posts overall: the turn-boundary flush is a no-op
    # because 150 was already posted (dedup), so no duplicate lands at the end.
    assert [p["type"] for p in posted].count("external_session_usage") == 2


def test_session_usage_data_extracts_cumulative_tokens() -> None:
    """
    ``_session_usage_data_from_params`` surfaces Codex's cumulative input /
    output tokens as ``cumulative_*`` fields (for server-side cost pricing).

    Codex's ``tokenUsage.total`` is cumulative across the thread, so these are
    the session totals; without them codex-native ``session_usage.total_cost_usd``
    stays 0 (codex produces no ``response.completed`` for the Omnigent relay).
    """
    params = {
        "threadId": "thread_123",
        "tokenUsage": {
            "total": {
                "inputTokens": 1000,
                "outputTokens": 250,
                "contextWindow": 200000,
            },
        },
    }
    data = codex_native_forwarder._session_usage_data_from_params(params)
    assert data is not None
    assert data["cumulative_input_tokens"] == 1000
    assert data["cumulative_output_tokens"] == 250
    # Existing context-ring fields still flow.
    assert data["context_tokens"] == 1000
    assert data["context_window"] == 200000
    # No ``cachedInputTokens`` in the frame ⇒ no cache field forwarded. A
    # failure here would mean the server splits a phantom cache bucket out of
    # the input total, under-counting non-cached input.
    assert "cumulative_cache_read_input_tokens" not in data


def test_session_usage_data_forwards_cached_input_tokens() -> None:
    """
    ``cachedInputTokens`` is forwarded as ``cumulative_cache_read_input_tokens``
    while ``cumulative_input_tokens`` stays the FULL input total.

    Codex's ``inputTokens`` is inclusive of cached tokens (codex-rs
    ``non_cached_input = input_tokens - cached_input_tokens``). The forwarder
    must report both faithfully so the server can split the cheaper cache-read
    portion out before pricing — otherwise cached tokens are billed at the full
    input rate (the cost over-report this fix targets).
    """
    params = {
        "threadId": "thread_123",
        "tokenUsage": {
            "total": {
                "inputTokens": 1000,
                "cachedInputTokens": 800,
                "outputTokens": 250,
                "contextWindow": 200000,
            },
        },
    }
    data = codex_native_forwarder._session_usage_data_from_params(params)
    assert data is not None
    # Full input total preserved (NOT pre-subtracted) — the server owns the
    # split. If this were 200, the forwarder would have double-applied the
    # subtraction the server also does.
    assert data["cumulative_input_tokens"] == 1000
    # Cached count surfaced for the server to price at the cache-read rate.
    assert data["cumulative_cache_read_input_tokens"] == 800


def test_session_usage_data_without_output_tokens_omits_cumulative_output() -> None:
    """
    A usage notification lacking ``outputTokens`` yields no
    ``cumulative_output_tokens`` — the server then prices input only rather
    than treating a missing field as zero output.
    """
    params = {"tokenUsage": {"total": {"inputTokens": 500, "contextWindow": 200000}}}
    data = codex_native_forwarder._session_usage_data_from_params(params)
    assert data is not None
    assert data["cumulative_input_tokens"] == 500
    assert "cumulative_output_tokens" not in data


def test_session_usage_data_uses_effective_model_context_window() -> None:
    """The context ring uses Codex's effective model window when available."""
    params = {
        "tokenUsage": {
            "modelContextWindow": 258_400,
            "total": {
                "inputTokens": 200_000,
                "outputTokens": 10_000,
            },
        },
    }
    data = codex_native_forwarder._session_usage_data_from_params(params)
    assert data is not None
    assert data["context_window"] == 258_400


def test_session_usage_data_prefers_effective_context_window_over_legacy() -> None:
    """Codex's effective window takes precedence over the legacy total field."""
    params = {
        "tokenUsage": {
            "modelContextWindow": 258_400,
            "total": {
                "inputTokens": 200_000,
                "outputTokens": 10_000,
                "contextWindow": 1_050_000,
            },
        },
    }
    data = codex_native_forwarder._session_usage_data_from_params(params)
    assert data is not None
    assert data["context_window"] == 258_400


def test_session_usage_data_invalid_effective_window_falls_back_to_legacy() -> None:
    """Malformed effective windows do not suppress a valid legacy fallback."""
    params = {
        "tokenUsage": {
            "modelContextWindow": 0,
            "total": {
                "inputTokens": 200_000,
                "contextWindow": 1_050_000,
            },
        },
    }
    data = codex_native_forwarder._session_usage_data_from_params(params)
    assert data is not None
    assert data["context_window"] == 1_050_000


def test_session_usage_data_context_tokens_uses_last_turn_input() -> None:
    """
    ``context_tokens`` (the context-ring value) should reflect the LAST
    turn's input — how much of the window the latest request occupied —
    not the cumulative total across the whole thread.

    When ``tokenUsage.last`` is present, ``context_tokens`` comes from
    ``last.inputTokens``; ``cumulative_input_tokens`` still uses
    ``total.inputTokens`` for cost pricing.
    """
    params = {
        "tokenUsage": {
            "total": {
                "inputTokens": 4_800_000,
                "outputTokens": 200_000,
                "contextWindow": 1_178_000,
            },
            "last": {
                "inputTokens": 950_000,
                "outputTokens": 12_000,
            },
        },
    }
    data = codex_native_forwarder._session_usage_data_from_params(params)
    assert data is not None
    # Ring shows current context occupancy from the last turn.
    assert data["context_tokens"] == 950_000
    # Cost pricing uses cumulative totals.
    assert data["cumulative_input_tokens"] == 4_800_000
    assert data["cumulative_output_tokens"] == 200_000
    assert data["context_window"] == 1_178_000


def test_session_usage_data_context_tokens_falls_back_without_last() -> None:
    """
    When ``tokenUsage.last`` is absent (e.g. first frame before a turn
    completes), ``context_tokens`` falls back to ``total.inputTokens``.
    """
    params = {
        "tokenUsage": {
            "total": {
                "inputTokens": 1000,
                "outputTokens": 250,
                "contextWindow": 200_000,
            },
        },
    }
    data = codex_native_forwarder._session_usage_data_from_params(params)
    assert data is not None
    assert data["context_tokens"] == 1000
    assert data["cumulative_input_tokens"] == 1000


def test_session_usage_data_context_tokens_falls_back_when_last_missing_input() -> None:
    """
    When ``tokenUsage.last`` is present but lacks a usable ``inputTokens``,
    ``context_tokens`` falls back to ``total.inputTokens`` rather than being
    omitted (which would leave the UI ring stuck on a stale value from a
    previous coalescer frame).
    """
    params = {
        "tokenUsage": {
            "total": {
                "inputTokens": 3000,
                "outputTokens": 500,
                "contextWindow": 200_000,
            },
            "last": {
                "outputTokens": 100,
                # inputTokens intentionally absent
            },
        },
    }
    data = codex_native_forwarder._session_usage_data_from_params(params)
    assert data is not None
    assert data["context_tokens"] == 3000


def test_usage_coalescer_flush_attaches_model_to_every_post() -> None:
    """
    ``flush`` attaches the recorded model to each token-bearing post.

    The server prices cumulative codex tokens into ``total_cost_usd`` only
    when the post carries a ``model`` (codex-native sessions have no
    ``llm.model`` to fall back on). Codex sends settings (model) and usage
    in separate frames, so the coalescer must remember the model and stamp
    it on every flush — including the second turn's post, where only the
    token counts changed and the dedup would otherwise omit the model.
    """
    posted: list[dict[str, Any]] = []

    async def run() -> None:
        """
        Record two usage frames (model known) and flush each.

        :returns: None.
        """
        async with _recording_forwarder_client(posted) as client:
            coalescer = _usage_coalescer(client)
            coalescer.record(
                {"tokenUsage": {"total": {"inputTokens": 1000, "outputTokens": 250}}},
                model="gpt-5.1-codex",
            )
            await coalescer.flush()
            # A later turn: only token counts change, model arrives as None
            # on the usage frame, yet the post must still carry the model.
            coalescer.record(
                {"tokenUsage": {"total": {"inputTokens": 2000, "outputTokens": 600}}},
            )
            await coalescer.flush()

    asyncio.run(run())

    assert len(posted) == 2
    assert posted[0]["data"]["model"] == "gpt-5.1-codex"
    assert posted[0]["data"]["cumulative_input_tokens"] == 1000
    assert posted[0]["data"]["cumulative_output_tokens"] == 250
    # Second post still carries the model even though only tokens changed.
    assert posted[1]["data"]["model"] == "gpt-5.1-codex"
    assert posted[1]["data"]["cumulative_input_tokens"] == 2000
    assert posted[1]["data"]["cumulative_output_tokens"] == 600
