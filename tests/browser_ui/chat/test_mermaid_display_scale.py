"""Mermaid label wrapping on displays whose scale factor is not 1."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract
from tests.browser_ui.chat.test_transcript_rendering import (
    _CLIPPED_MERMAID_LABELS,
    _seed_assistant_message,
)

_MULTI_WORD_LABEL_MERMAID = """\
```mermaid
flowchart TD
  A["GET with include_browse=true on a table"]
  A --> B["UnityAccessDecorator: checks caller privileges"]
  B --> C{"ResultFilter: usual metadata access?"}
  C -- Yes --> D["Return full metadata"]
  C -- No --> E["Return PERMISSION_DENIED error"]
```"""

# Emulated scale factors do not change layout rounding; only a real window scale does.
pytestmark = pytest.mark.browser_context_args(no_viewport=True)


@pytest.fixture(scope="module", params=[0.9, 1.75], ids=["scale-0.9", "scale-1.75"])
def browser(
    request: pytest.FixtureRequest,
    launch_browser: Callable[..., Browser],
    browser_type_launch_args: dict[str, Any],
) -> Iterator[Browser]:
    scaled = launch_browser(
        args=[
            *browser_type_launch_args.get("args", []),
            f"--force-device-scale-factor={request.param}",
            "--window-size=1280,900",
        ]
    )
    yield scaled
    scaled.close()


def test_mermaid_multi_word_labels_wrap_inside_their_boxes(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    chat = chat_session_contract
    _seed_assistant_message(chat, _MULTI_WORD_LABEL_MERMAID)
    page.goto(chat.url)

    diagram = page.locator('[data-streamdown="mermaid-block"] svg[aria-roledescription]').first
    expect(diagram).to_be_visible(timeout=20_000)
    page.evaluate("() => document.fonts.ready")
    assert diagram.evaluate(_CLIPPED_MERMAID_LABELS) == []
