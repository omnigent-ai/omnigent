"""E2E: a chat Mermaid diagram's fullscreen view stays open while its reply settles."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

DIAGRAM_FENCE = "```mermaid\nflowchart LR\n  A[Client] --> B[Server]\n  B --> C[(Database)]\n```"
PROMPT = "Draw the request flow as a mermaid diagram and explain it slowly."
REPLY_DONE = "End of the diagram notes."
# At this pace the trailing words keep the reply streaming for seconds after the diagram.
STREAM_CHUNK_DELAY_S = 0.25
TRAILING_WORDS = 40

TRANSPARENT_BACKGROUNDS = {"", "transparent", "rgba(0, 0, 0, 0)"}

# Captures what a user would see: whether the overlay covers the window and is painted on top.
OVERLAY_PROBE = """() => {
  const exit = document.querySelector('button[aria-label="Exit fullscreen"]');
  const overlay = exit?.closest('.fixed');
  if (!overlay) return { open: false };
  const rect = overlay.getBoundingClientRect();
  const style = getComputedStyle(overlay);
  const svgRect = overlay.querySelector('svg[id^="mermaid-"]')?.getBoundingClientRect();
  const centre = document.elementFromPoint(innerWidth / 2, innerHeight / 2);
  return {
    open: true,
    rect: { width: rect.width, height: rect.height },
    viewport: { width: innerWidth, height: innerHeight },
    backgroundColor: style.backgroundColor,
    opacity: style.opacity,
    svgWidth: svgRect?.width ?? 0,
    centreInsideOverlay: overlay.contains(centre),
  };
}"""


def mermaid_reply() -> str:
    trailing = " ".join(f"note{index:02d}" for index in range(TRAILING_WORDS))
    return f"Here is the request flow.\n\n{DIAGRAM_FENCE}\n\n{trailing} {REPLY_DONE}"


def click_view_fullscreen(page: Page) -> bool:
    """Click the diagram's fullscreen control where a pointer lands; report if it was on top."""
    button = page.locator(".chat-mermaid").last.get_by_role("button", name="View fullscreen")
    box = None
    # A streaming reply can swap the block between locating the button and measuring it.
    for _ in range(20):
        expect(button).to_be_visible()
        box = button.bounding_box()
        if box is not None:
            break
    assert box is not None
    centre_x, centre_y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    on_top = page.evaluate(
        """([x, y]) => Boolean(
          document.elementFromPoint(x, y)?.closest('button[aria-label="View fullscreen"]')
        )""",
        [centre_x, centre_y],
    )
    page.mouse.click(centre_x, centre_y)
    return on_top


def assert_overlay_visible(page: Page) -> None:
    probe = page.evaluate(OVERLAY_PROBE)
    assert probe["open"], probe
    assert probe["rect"]["width"] >= probe["viewport"]["width"] - 1, probe
    assert probe["rect"]["height"] >= probe["viewport"]["height"] - 1, probe
    assert probe["backgroundColor"] not in TRANSPARENT_BACKGROUNDS, probe
    assert probe["opacity"] == "1", probe
    assert probe["centreInsideOverlay"], probe
    assert probe["svgWidth"] > 0, probe


def test_fullscreen_diagram_stays_open_when_the_reply_settles(
    page: Page, seeded_session: tuple[str, str], mock_llm_server_url: str
) -> None:
    base_url, session_id = seeded_session
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": mermaid_reply(), "stream": True, "chunk_delay": STREAM_CHUNK_DELAY_S}],
        key="mermaid-fullscreen-streaming",
        match=PROMPT,
    )
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(PROMPT)
    page.get_by_role("button", name="Send", exact=True).click()

    diagram = page.locator('[data-streamdown="mermaid-block"] svg[aria-roledescription]').last
    expect(diagram.filter(has_text="Database")).to_be_visible(timeout=60_000)
    interrupt = page.get_by_role("button", name="Interrupt", exact=True)
    expect(interrupt).to_be_visible()
    expect(page.get_by_text(REPLY_DONE)).to_have_count(0)

    assert click_view_fullscreen(page)
    exit_button = page.get_by_role("button", name="Exit fullscreen")
    expect(exit_button).to_be_visible(timeout=5_000)
    assert_overlay_visible(page)
    assert interrupt.is_visible(), "the reply finished before the click; nothing ran mid-stream"

    # Settling the reply remounts the message's blocks underneath the open view.
    expect(page.get_by_text(REPLY_DONE).first).to_be_attached(timeout=60_000)
    expect(interrupt).to_have_count(0, timeout=30_000)
    assert_overlay_visible(page)

    page.keyboard.press("Escape")
    expect(exit_button).to_have_count(0)

    assert click_view_fullscreen(page)
    expect(exit_button).to_be_visible(timeout=5_000)
    assert_overlay_visible(page)
    exit_button.click()
    expect(exit_button).to_have_count(0)
