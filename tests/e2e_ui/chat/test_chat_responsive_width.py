"""E2E coverage for responsive chat and prose widths."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import seed_committed_turn

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_PROSE_MARKER = "Responsive prose width marker"
_TABLE_MARKER = "Responsive table width marker"


@pytest.mark.parametrize(
    ("viewport_width", "expected_frame_width", "expected_prose_width"),
    [
        pytest.param(1920, 768, 736, id="desktop"),
        pytest.param(2400, 896, 768, id="wide-desktop"),
        pytest.param(3200, 1280, 960, id="fluid-ultrawide"),
        pytest.param(4096, 1600, 1024, id="4k"),
    ],
)
def test_chat_width_scales_while_prose_remains_readable(
    page: Page,
    seeded_session: tuple[str, str],
    viewport_width: int,
    expected_frame_width: float,
    expected_prose_width: float,
) -> None:
    """The chat frame scales on wide screens while ordinary prose stays capped."""
    base_url, session_id = seeded_session
    seed_committed_turn(
        session_id,
        prompt="Show a prose example.",
        reply=f"{_PROSE_MARKER}. " + "This sentence fills the available reading width. " * 30,
        response_id="resp_responsive_prose",
    )
    seed_committed_turn(
        session_id,
        prompt="Show a comparison table.",
        reply=(
            f"{_TABLE_MARKER}.\n\n"
            "| View | Intended behavior |\n"
            "| --- | --- |\n"
            "| Prose | Keeps a readable line length |\n"
            "| Tables | Uses the full conversation column |"
        ),
        response_id="resp_responsive_table",
    )

    page.set_viewport_size({"width": viewport_width, "height": 1080})
    page.goto(f"{base_url}/c/{session_id}")

    prose = page.locator(_ASSISTANT).filter(has_text=_PROSE_MARKER)
    table = page.locator(_ASSISTANT).filter(has_text=_TABLE_MARKER)
    expect(prose).to_be_visible(timeout=30_000)
    expect(table.locator("table")).to_be_visible(timeout=30_000)

    widths = page.evaluate(
        """({ proseMarker, tableMarker }) => {
          const frame = document.querySelector('.chat-conversation-content');
          const bubbles = [...document.querySelectorAll(
            '[data-testid="message-bubble"][data-role="assistant"]'
          )];
          const prose = bubbles.find((bubble) => bubble.textContent.includes(proseMarker));
          const table = bubbles.find((bubble) => bubble.textContent.includes(tableMarker));
          const frameStyle = getComputedStyle(frame);
          return {
            frame: frame.getBoundingClientRect().width,
            frameInner:
              frame.clientWidth -
              parseFloat(frameStyle.paddingLeft) -
              parseFloat(frameStyle.paddingRight),
            prose: prose.getBoundingClientRect().width,
            table: table.getBoundingClientRect().width,
          };
        }""",
        {"proseMarker": _PROSE_MARKER, "tableMarker": _TABLE_MARKER},
    )

    assert widths["frame"] == pytest.approx(expected_frame_width, abs=1)
    assert widths["prose"] == pytest.approx(expected_prose_width, abs=1)
    assert widths["table"] == pytest.approx(widths["frameInner"], abs=1)
