r"""UI journey: a codex-native session's Session cost stays cache-aware.

Codex reports cumulative token usage (``tokenUsage.total``, cached portion in
``cachedInputTokens``) that the forwarder mirrors to Omnigent for pricing. A
request served with no prompt-cache hits leaves the cumulative cached count
unchanged; tokens cached earlier must still be billed at the cache-read rate.

Journey (real web SPA, live server + runner, real ``codex`` CLI against the
mock ``/v1/responses``): send a cache-heavy turn (100K input, 90K cached), then
a cache-miss turn (100K input, 0 cached), then open the agent-info popover and
check the Session cost and the per-model Input / Cache read split.

Pricing comes from the mock codex provider: ``temp_omnigent_mock_config`` writes
it for standalone runs and ``dev.repro_env`` for workflow-owned ones, both from
``_CODEX_MOCK_PRICING_PER_MILLION``. ``OMNIGENT_E2E_CODEX_PRICING_PER_MILLION``
(``input,output,cache_read``) overrides the expected rates for another provider.
"""

from __future__ import annotations

import os
import re
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm
from tests.helpers.ui_configuration import _CODEX_MOCK_PRICING_PER_MILLION

from ..messages.test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
    _send,
    _turn_prompt,
)
from ..messages.test_native_codex_render_parity import (
    _CODEX_MOCK_MODEL,
    _open_terminal_view,
    _wait_terminal_connected,
)

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None,
    reason="the codex CLI binary is required for the codex-native usage e2e",
)

_MOCK_TURN_TIMEOUT_MS = 60_000
_USAGE_TIMEOUT_S = 30.0

# Request 1 is cache-heavy; request 2 is served with no cache hits, so the
# cumulative cached count does not grow between the two usage reports.
_TURN_USAGE = (
    {
        "input_tokens": 100_000,
        "output_tokens": 200,
        "input_tokens_details": {"cached_tokens": 90_000},
    },
    {
        "input_tokens": 100_000,
        "output_tokens": 200,
        "input_tokens_details": {"cached_tokens": 0},
    },
)


def _pricing_per_million() -> tuple[float, float, float]:
    # Expected rates must match the mock codex provider's pricing; the env
    # override only changes test expectations, not the server's rates.
    default = ",".join(str(rate) for rate in _CODEX_MOCK_PRICING_PER_MILLION)
    raw = os.environ.get("OMNIGENT_E2E_CODEX_PRICING_PER_MILLION", default)
    parts = raw.split(",")
    if len(parts) != 3:
        raise ValueError(
            "OMNIGENT_E2E_CODEX_PRICING_PER_MILLION must be 'input,output,cache_read', "
            f"got {raw!r}"
        )
    input_rate, output_rate, cache_read_rate = (float(part) for part in parts)
    return input_rate, output_rate, cache_read_rate


def _session_snapshot(base_url: str, session_id: str) -> dict:
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}",
        params={"include_usage": "true"},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()


def _model_usage(snapshot: dict) -> dict:
    return (snapshot.get("usage_by_model") or {}).get(_CODEX_MOCK_MODEL) or {}


def _wait_for_usage(base_url: str, session_id: str, settled: Callable[[dict], bool]) -> dict:
    """Poll the session snapshot until *settled* accepts its per-model usage."""
    deadline = time.monotonic() + _USAGE_TIMEOUT_S
    snapshot = _session_snapshot(base_url, session_id)
    while not settled(_model_usage(snapshot)) and time.monotonic() < deadline:
        time.sleep(0.5)
        snapshot = _session_snapshot(base_url, session_id)
    if not settled(_model_usage(snapshot)):
        pytest.fail(
            f"usage never settled within {_USAGE_TIMEOUT_S}s; last usage: {_model_usage(snapshot)}"
        )
    return snapshot


def _codex_request_count(mock_llm_server_url: str) -> int:
    resp = httpx.get(f"{mock_llm_server_url}/mock/requests", timeout=10.0)
    resp.raise_for_status()
    return sum(1 for r in resp.json()["requests"] if r.get("model") == _CODEX_MOCK_MODEL)


def _open_agent_info(page: Page) -> Locator:
    trigger = page.get_by_test_id("agent-info-trigger")
    if trigger.get_attribute("aria-expanded") != "true":
        trigger.focus()
        trigger.press("Enter")
    panel = page.get_by_test_id("agent-info-panel")
    expect(panel).to_be_visible(timeout=30_000)
    return panel


def _compact(tokens: int) -> str:
    """Mirror the SPA's compact token formatting for the sub-1M counts used here (``110K``)."""
    if tokens < 1_000:
        return str(tokens)
    value = round(tokens / 1_000, 1)
    return f"{value:g}K"


@pytest.mark.timeout(600)
def test_codex_native_session_cost_keeps_cached_tokens_at_cache_rate(
    page: Page,
    native_codex_mock_session: tuple[str, str],
    mock_llm_server_url: str,
    pytestconfig: pytest.Config,
) -> None:
    """Session cost must price earlier cache hits at the cache-read rate after a cache miss."""
    base_url, session_id = native_codex_mock_session
    input_rate, output_rate, cache_read_rate = _pricing_per_million()

    reset_mock_llm(mock_llm_server_url)
    nonces = [uuid.uuid4().hex[:8] for _ in _TURN_USAGE]
    turns = [(f"usr-{i + 1}-{n}", f"ast-{i + 1}-{n}") for i, n in enumerate(nonces)]
    for (marker, token), usage in zip(turns, _TURN_USAGE, strict=True):
        configure_mock_llm(
            mock_llm_server_url, [{"text": token, "usage": usage}], key=marker, match=marker
        )
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    reported_input = reported_cached = reported_output = 0
    for index, ((marker, token), usage) in enumerate(zip(turns, _TURN_USAGE, strict=True), 1):
        _send(page, _turn_prompt(index, marker, token))
        expect(page.locator(_ASSISTANT, has_text=token).first).to_be_visible(
            timeout=_MOCK_TURN_TIMEOUT_MS
        )
        expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)
        reported_input += usage["input_tokens"]
        reported_cached += usage["input_tokens_details"]["cached_tokens"]
        reported_output += usage["output_tokens"]
        floor = reported_input
        snapshot = _wait_for_usage(
            base_url,
            session_id,
            lambda u, floor=floor: (
                (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) >= floor
            ),
        )
        if index == 1:
            # Preconditions: the session is priced and codex forwarded the
            # cached split, so a later mismatch is mis-accounting, not a
            # missing report.
            assert snapshot.get("total_cost_usd") is not None, (
                "session is unpriced; the codex provider needs pricing configured "
                f"(snapshot usage {_model_usage(snapshot)})"
            )
            assert _model_usage(snapshot).get("cache_read_input_tokens") == reported_cached, (
                f"after turn 1 the server holds {_model_usage(snapshot)}; expected "
                f"{reported_cached} cached tokens from the model's usage report"
            )

    # Codex forwards its own cumulative tokenUsage.total, which covers only the
    # two scripted turns; the mock's extra hits are the server's background
    # title inference, outside codex's thread, so they do not enter the total.
    assert _codex_request_count(mock_llm_server_url) >= len(_TURN_USAGE), (
        "the scripted turns did not reach the mock as codex requests"
    )
    expected_uncached = reported_input - reported_cached
    expected_output = reported_output
    expected_cost = (
        expected_uncached * input_rate
        + reported_cached * cache_read_rate
        + expected_output * output_rate
    ) / 1_000_000

    panel = _open_agent_info(page)
    breakdown = panel.get_by_test_id("agent-info-usage-by-model")
    expect(breakdown).to_be_visible(timeout=30_000)
    breakdown.locator("summary").press("Enter")
    model_row = breakdown.get_by_test_id(f"agent-info-model-{_CODEX_MOCK_MODEL}")
    expect(model_row).to_be_visible(timeout=30_000)
    page.screenshot(
        path=str(Path(pytestconfig.getoption("--output")) / "codex-native-agent-info.png")
    )
    # Mirror the SPA's formatSessionCostUsd: sub-cent spend renders as "<$0.01".
    expected_cost_text = "<$0.01" if 0 < expected_cost < 0.01 else f"${expected_cost:.2f}"
    expect(panel.get_by_test_id("agent-info-session-cost")).to_have_text(
        expected_cost_text, timeout=30_000
    )
    expect(model_row).to_contain_text(
        re.compile(rf"Cache read\s*{re.escape(_compact(reported_cached))}(?![\d.])")
    )
    expect(model_row).to_contain_text(
        re.compile(rf"Input\s*{re.escape(_compact(expected_uncached))}(?![\d.])")
    )

    usage = _model_usage(_session_snapshot(base_url, session_id))
    assert usage.get("cache_read_input_tokens") == reported_cached
    assert usage.get("input_tokens") == expected_uncached
    assert usage.get("output_tokens") == expected_output
    assert usage.get("total_cost_usd") == pytest.approx(expected_cost, rel=0.01)
