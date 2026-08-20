"""E2E: a link-only markdown table column keeps its natural width.

Regression guard for the "PR # column is two characters wide" bug. Streamdown
styles links with ``wrap-anywhere``, which also drops the element's min-content
width to a single character. Its table is ``table-layout: auto``, so a column
holding only a link reported ~1ch and got squeezed to ~2ch — a short link like
``#3090`` stacked one or two characters per line while the prose columns took
the width. ``web/src/index.css`` narrows links inside table cells back to
``overflow-wrap: break-word``, which still wraps overlong URLs but keeps
min-content at the longest unbreakable run.

A deterministic assistant message (seeded via ``external_assistant_message`` —
no LLM run) carries the table shape that triggered the bug: a link-only ``#``
column, wide prose columns, and a full-URL column. Asserted:

  - The short link renders on **one line box** instead of stacking.
  - Its cell is at least as wide as the link, so the column was not squeezed
    below its content.
  - A long URL still **wraps** and does not overflow its cell — the narrowed
    rule must not regress long-URL handling into overflow.

Line-box counts and widths (rather than a computed ``overflow-wrap`` value)
keep the test tied to what the user actually sees.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

_AGENT_NAME = "hello_world"
_TABLE = '[data-streamdown="table"]'
_LINK = '[data-streamdown="link"]'

# The short link whose column used to collapse.
_SHORT_LINK_TEXT = "#3090"

# Rendered as its own link text, and too long to fit one line in a cell.
_LONG_URL = (
    "https://github.com/omnigent-ai/omnigent/pull/3350/files#diff-markdown-table-link-column"
)


def _row(num: str, title: str, author: str, waiting: str) -> str:
    """Build one markdown row: link-only `#`, prose title, then a full URL.

    :param num: PR number rendered as the whole `#` cell, e.g. ``#3090``.
    :param title: prose wide enough to win the auto-layout width fight.
    :param author: author cell.
    :param waiting: age cell.
    :returns: the pipe-delimited markdown row.
    """
    href = f"https://github.com/omnigent-ai/omnigent/pull/{num.lstrip('#')}"
    cells = (f"[{num}]({href})", title, author, waiting, f"[{_LONG_URL}]({_LONG_URL})")
    return f"| {' | '.join(cells)} |"


# The table that reproduced the bug: a link-only `#` column beside prose
# columns, plus a full-URL column that must still soft-wrap.
_MESSAGE_TEXT = "\n".join(
    [
        "Here are the pull requests still waiting on review:",
        "",
        "| # | PR | Author | Waiting | Link |",
        "| --- | --- | --- | --- | --- |",
        _row(
            _SHORT_LINK_TEXT,
            "Fix table link column collapsing to two characters",
            "alex",
            "3 days",
        ),
        _row("#3351", "Add e2e coverage for markdown table link wrapping", "robin", "1 day"),
        "",
    ]
)

# A collapsed column stacks the link across several line boxes.
_LINE_BOXES = "el => el.getClientRects().length"

# Widest line box vs. the cell's content box — catches a link painting outside
# its cell, the failure mode an over-aggressive fix would introduce.
_OVERFLOWS_CELL = """el => {
  const cell = el.closest('[data-streamdown="table-cell"]');
  if (!cell) return true;
  const style = getComputedStyle(cell);
  const inner =
    cell.clientWidth -
    parseFloat(style.paddingLeft) -
    parseFloat(style.paddingRight);
  const widest = Math.max(...[...el.getClientRects()].map((r) => r.width));
  return widest - inner > 1;  // 1px subpixel tolerance
}"""


@pytest.fixture
def table_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Seed a runner-bound session with an assistant reply containing the table.

    :param seeded_session: ``(base_url, session_id)`` for a runner-bound session.
    :returns: the same ``(base_url, session_id)`` after the reply is seeded.
    """
    base_url, session_id = seeded_session
    event_resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {"agent": _AGENT_NAME, "text": _MESSAGE_TEXT},
        },
        timeout=10.0,
    )
    event_resp.raise_for_status()
    yield (base_url, session_id)


def test_table_link_column_keeps_its_natural_width(
    page: Page,
    table_session: tuple[str, str],
) -> None:
    """A short link in a table cell stays on one line; long URLs still wrap."""
    base_url, session_id = table_session
    page.goto(f"{base_url}/c/{session_id}")

    table = page.locator(_TABLE).first
    expect(table).to_be_visible(timeout=30_000)

    short_link = page.locator(_LINK, has_text=_SHORT_LINK_TEXT).first
    expect(short_link).to_be_visible(timeout=30_000)

    # The bug: the column collapsed to ~2ch, so "#3090" stacked across boxes.
    boxes = short_link.evaluate(_LINE_BOXES)
    assert boxes == 1, f"short table link should occupy one line box, got {boxes}"

    # The column got at least its content's min-content width.
    cell_width = short_link.evaluate(
        'el => el.closest("[data-streamdown=\\"table-cell\\"]").clientWidth'
    )
    link_width = short_link.evaluate("el => el.getBoundingClientRect().width")
    assert cell_width >= link_width, (
        f"link column ({cell_width}px) is narrower than its link ({link_width}px)"
    )

    # The counterpart: a full URL soft-wraps inside its cell.
    long_link = page.locator(_LINK, has_text=_LONG_URL).first
    expect(long_link).to_be_visible(timeout=30_000)
    long_boxes = long_link.evaluate(_LINE_BOXES)
    assert long_boxes > 1, f"long URL should wrap across multiple lines, got {long_boxes}"
    assert not long_link.evaluate(_OVERFLOWS_CELL), "long URL overflows its table cell"
