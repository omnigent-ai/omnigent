"""E2E: a Mermaid fullscreen view opened mid-reply stays open when the reply finishes."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

DIAGRAM_FENCE = "```mermaid\nflowchart LR\n    Client --> Server\n    Server --> Database\n```"
PROMPT = "Draw the request flow as a mermaid diagram and explain it slowly."
REPLY_DONE = "End of the diagram notes."
STREAM_CHUNK_DELAY_S = 0.25
TRAILING_WORDS = 40

DIAGRAM_SVG = '[data-streamdown="mermaid-block"] svg[aria-roledescription]'

OVERLAY_PROBE = """
() => {
  const exit = document.querySelector('button[aria-label="Exit fullscreen"]');
  const overlay = exit?.closest('.fixed');
  if (!overlay) return null;
  const rect = overlay.getBoundingClientRect();
  const style = getComputedStyle(overlay);
  const svg = overlay.querySelector('svg[id^="mermaid-"]');
  const centre = document.elementFromPoint(window.innerWidth / 2, window.innerHeight / 2);
  return {
    covers: rect.left <= 1 && rect.top <= 1 && rect.width >= window.innerWidth - 2
      && rect.height >= window.innerHeight - 2,
    background: style.backgroundColor,
    opacity: Number(style.opacity),
    svgWidth: svg ? svg.getBoundingClientRect().width : 0,
    topmostAtCentre: Boolean(centre && overlay.contains(centre)),
  };
}
"""


def _streamed_diagram_reply() -> str:
    trailing = " ".join(f"note{n:02d}" for n in range(TRAILING_WORDS))
    return f"Here is the request flow.\n\n{DIAGRAM_FENCE}\n\n{trailing} {REPLY_DONE}"


def _send_diagram_request(page: Page, base_url: str, session_id: str) -> None:
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(PROMPT)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(DIAGRAM_SVG).first).to_contain_text("Database", timeout=60_000)


def _click_view_fullscreen(page: Page) -> None:
    button = page.get_by_role("button", name="View fullscreen").first
    expect(button).to_be_visible(timeout=10_000)
    box = button.bounding_box()
    assert box is not None, "View fullscreen button has no layout box"
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    on_top = page.evaluate(
        "([x, y]) => Boolean(document.elementFromPoint(x, y)?.closest("
        "'button[aria-label=\"View fullscreen\"]'))",
        [x, y],
    )
    assert on_top, "another element covers the View fullscreen button"
    page.mouse.click(x, y)


def _assert_overlay_visible(page: Page) -> None:
    expect(page.get_by_role("button", name="Exit fullscreen")).to_be_visible(timeout=5_000)
    probe = page.evaluate(OVERLAY_PROBE)
    assert probe is not None, "fullscreen overlay is not mounted"
    assert probe["covers"], probe
    assert probe["opacity"] == 1, probe
    assert probe["background"] not in ("rgba(0, 0, 0, 0)", "transparent"), probe
    assert probe["svgWidth"] > 100, probe
    assert probe["topmostAtCentre"], probe


def _wait_for_reply_to_finish(page: Page) -> None:
    expect(page.get_by_text(REPLY_DONE).first).to_be_attached(timeout=60_000)
    expect(page.get_by_role("button", name="Interrupt", exact=True)).to_have_count(
        0, timeout=30_000
    )


@pytest.fixture
def diagram_session(seeded_session: tuple[str, str], mock_llm_server_url: str) -> tuple[str, str]:
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "text": _streamed_diagram_reply(),
                "stream": True,
                "chunk_delay": STREAM_CHUNK_DELAY_S,
            }
        ],
        key="mermaid-fullscreen",
        match=PROMPT,
    )
    return seeded_session


def test_fullscreen_diagram_stays_open_when_the_reply_settles(
    request: pytest.FixtureRequest, diagram_session: tuple[str, str]
) -> None:
    base_url, session_id = diagram_session
    page: Page = request.getfixturevalue("page")
    _send_diagram_request(page, base_url, session_id)
    expect(page.get_by_role("button", name="Interrupt", exact=True)).to_be_visible()
    assert page.get_by_text(REPLY_DONE).count() == 0, "reply finished before the click"

    _click_view_fullscreen(page)
    _assert_overlay_visible(page)

    _wait_for_reply_to_finish(page)
    page.wait_for_timeout(2_000)
    _assert_overlay_visible(page)

    page.keyboard.press("Escape")
    expect(page.get_by_role("button", name="Exit fullscreen")).to_have_count(0)
