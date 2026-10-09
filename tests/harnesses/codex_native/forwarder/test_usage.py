"""Usage tests for Codex forwarder."""

from __future__ import annotations

import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from tests.harnesses.codex_native.forwarder._support import (
    _RecordingClient,
)

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
