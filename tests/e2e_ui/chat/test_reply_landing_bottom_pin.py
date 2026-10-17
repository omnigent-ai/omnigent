"""A reply landing in a bottom-pinned transcript keeps the view on the newest text.

Set OMNIGENT_E2E_CPU_THROTTLE (e.g. 3.5 or 6) to emulate a slow runner over CDP.
"""

from __future__ import annotations

import json
import os
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.chat.test_transcript_scroll_stability import (
    _NEWEST_REPLY,
    _STREAM_CHUNK_DELAY_S,
    _STREAM_PROMPT,
    _STREAMED_REPLY_DONE,
    _STREAMED_REPLY_WORDS,
    _TAG_SCROLLER,
    _TRACK_BOTTOM,
    _VIEWPORT,
    _seed_turns,
)
from tests.e2e_ui.conftest import configure_mock_llm

_WORKING = '[data-testid="working-indicator"]'
# Same budget and off-bottom threshold as the stock bottom-pin test, so the two agree.
_BUDGET_MS = 100
_OFF_BOTTOM_PX = 8
# A superset of the stock bottom-pin prompt: the two tests share the
# session-scoped mock with no reset, and longest-match routing keeps each
# request on its own queue even once the other's is exhausted.
_LANDING_PROMPT = f"{_STREAM_PROMPT} (landing)"
_CPU_THROTTLE = float(os.environ.get("OMNIGENT_E2E_CPU_THROTTLE", "1"))
_LANDINGS = int(os.environ.get("OMNIGENT_E2E_REPLY_LANDINGS", "6"))
_DUMP_DIR = os.environ.get("OMNIGENT_E2E_BOTTOM_PIN_DUMP")


def _streamed_reply(n: int) -> str:
    words = " ".join(f"streamedword{i:03d}" for i in range(_STREAMED_REPLY_WORDS))
    return f"{words} {_STREAMED_REPLY_DONE} {n}"


def _visible_stall_ms(samples: list[list[int]]) -> int:
    """Longest time the view stayed painted off the bottom before a frame brought it back."""
    longest = 0
    started: int | None = None
    for t, off_bottom, _height in samples:
        if off_bottom > _OFF_BOTTOM_PX:
            started = t if started is None else started
        elif started is not None:
            longest, started = max(longest, t - started), None
    if started is not None and samples:
        longest = max(longest, samples[-1][0] - started)
    return longest


def _longest_behind_ms(samples: list[list[int]]) -> tuple[int, int]:
    """The stock bottom-pin test's metric (kept for comparison), and the farthest sample."""
    behind = longest = 0
    for (t0, d0, _h0), (t1, d1, _h1) in pairwise(samples):
        behind = behind + (t1 - t0) if d0 > _OFF_BOTTOM_PX and d1 > _OFF_BOTTOM_PX else 0
        longest = max(longest, behind)
    return longest, max((s[1] for s in samples), default=0)


def test_reply_landing_keeps_a_bottom_pinned_view_at_the_bottom(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = seeded_session
    _seed_turns(session_id)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": _streamed_reply(n), "stream": True, "chunk_delay": _STREAM_CHUNK_DELAY_S}
            for n in range(1, _LANDINGS + 1)
        ],
        key="scroll-pin-landing",
        match=_LANDING_PROMPT,
    )
    # Non-browser setup is done; the (possibly recorded) page starts here.
    page: Page = request.getfixturevalue("page")
    page.set_viewport_size(_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_text(_NEWEST_REPLY).first).to_be_visible(timeout=30_000)
    assert page.evaluate(_TAG_SCROLLER), "transcript did not overflow; seed more turns"
    page.wait_for_timeout(500)
    page.evaluate(_TRACK_BOTTOM)
    if _CPU_THROTTLE != 1:
        cdp = page.context.new_cdp_session(page)
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": _CPU_THROTTLE})

    composer = page.get_by_label("Message the agent")
    landings: list[dict[str, Any]] = []
    for n in range(1, _LANDINGS + 1):
        expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)
        expect(composer).to_be_enabled(timeout=30_000)
        height_before = page.evaluate(
            "() => document.querySelector('[data-pw-scroller]').scrollHeight"
        )
        sent_at = page.evaluate("() => performance.now()")
        composer.fill(_LANDING_PROMPT)
        page.get_by_role("button", name="Send", exact=True).click()
        expect(page.get_by_text(f"{_STREAMED_REPLY_DONE} {n}").first).to_be_visible(timeout=90_000)
        page.wait_for_timeout(1000)
        samples = [s for s in page.evaluate("() => window.__bottomSamples") if s[0] >= sent_at]
        grown = [s for s in samples if s[2] > height_before]
        assert grown and grown[-1][2] > height_before + 200, (height_before, samples[-1:])
        assert samples[-1][1] <= 2, samples[-1]
        longest, farthest = _longest_behind_ms(grown)
        landings.append(
            {
                "landing": n,
                "visible_stall_ms": _visible_stall_ms(grown),
                "longest_behind_ms": longest,
                "farthest_px": farthest,
                "height_before": height_before,
                "height_after": grown[-1][2],
                "off_bottom_samples": [s for s in grown if s[1] > _OFF_BOTTOM_PX][:12],
                "samples": samples if _DUMP_DIR else [],
            }
        )

    report = json.dumps(
        {"cpu_throttle": _CPU_THROTTLE, "session_id": session_id, "landings": landings}, indent=1
    )
    if _DUMP_DIR:
        Path(_DUMP_DIR).mkdir(parents=True, exist_ok=True)
        (Path(_DUMP_DIR) / f"landings-{session_id}.json").write_text(report)
    stalled = [landing for landing in landings if landing["visible_stall_ms"] > _BUDGET_MS]
    assert not stalled, report
