"""E2E: Settings > Appearance font-family rows stay readable at phone widths.

At iPhone widths (390-430px) the "Font family" and "Code font family" rows
squeezed their label and helper text into a vertical sliver beside the Reset
button + input instead of wrapping the control onto its own line. On a desktop
width the control is designed to sit inline beside a readable label.

No LLM turn is involved.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, ViewportSize, expect

# Narrower than this cannot hold the two-word label on one line; the crushed
# state measured 14-58px.
MIN_READABLE_LABEL_COLUMN_PX = 96

_IPHONE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}

# Logical widths of current iPhones; 375 (SE) already wrapped correctly.
_PHONE_WIDTHS = [390, 393, 414, 430]

_DESKTOP_VIEWPORT: ViewportSize = {"width": 1280, "height": 800}

# (row label text, input test id) for the two label-beside-input rows.
_FONT_FAMILY_ROWS = [
    ("Font family", "ui-font-family-input"),
    ("Code font family", "code-font-family-input"),
]


def _open_appearance(page: Page, base_url: str) -> None:
    """Navigate to Settings > Appearance the way a phone user does.

    On mobile the settings nav is a full-screen overlay pinned open on entering
    ``/settings``; tapping the Appearance row closes it and reveals the section.
    """
    page.goto(f"{base_url}/settings/appearance")
    nav_item = page.get_by_test_id("settings-nav-appearance")
    expect(nav_item).to_be_visible(timeout=30_000)
    nav_item.click()
    expect(page.get_by_role("heading", name="Appearance", exact=True)).to_be_visible(
        timeout=30_000
    )


def _row_layout(page: Page, input_test_id: str, label_text: str) -> dict:
    """Measure a font-family row: the label column box and the control group box."""
    return page.evaluate(
        """
        ([inputTestId, labelText]) => {
          const input = document.querySelector(`[data-testid="${inputTestId}"]`);
          if (!input) return {error: `no input ${inputTestId}`};
          const group = input.closest('[role="group"]');
          if (!group) return {error: `no role=group ancestor for ${inputTestId}`};
          const row = group.parentElement;
          const label = Array.from(row.querySelectorAll('span')).find(
            (s) => s.textContent.trim() === labelText,
          );
          if (!label) return {error: `no label ${labelText}`};
          const column = label.parentElement;
          const rect = (el) => {
            const b = el.getBoundingClientRect();
            return {
              left: b.left, right: b.right, top: b.top, bottom: b.bottom,
              width: b.width, height: b.height,
            };
          };
          return {column: rect(column), group: rect(group), row: rect(row)};
        }
        """,
        [input_test_id, label_text],
    )


def _measure_row(page: Page, input_test_id: str, label_text: str) -> dict:
    """Scroll a font-family row into view and measure its layout boxes."""
    control = page.get_by_test_id(input_test_id)
    control.scroll_into_view_if_needed()
    expect(control).to_be_visible()
    page.wait_for_timeout(500)  # settle so measurements and recordings show the row as seen

    layout = _row_layout(page, input_test_id, label_text)
    assert "error" not in layout, layout
    return layout


@pytest.mark.parametrize("width", _PHONE_WIDTHS)
@pytest.mark.parametrize(("label_text", "input_test_id"), _FONT_FAMILY_ROWS)
def test_appearance_font_family_rows_not_crushed_on_iphone(
    page: Page, live_server: str, label_text: str, input_test_id: str, width: int
) -> None:
    """At an iPhone width the control wraps below the label, or the label column stays readable."""
    page.set_viewport_size({"width": width, "height": _IPHONE_VIEWPORT["height"]})
    _open_appearance(page, live_server)

    layout = _measure_row(page, input_test_id, label_text)
    column = layout["column"]
    group = layout["group"]

    wrapped_below = group["top"] >= column["bottom"] - 1
    side_by_side_readable = column["width"] >= MIN_READABLE_LABEL_COLUMN_PX

    assert wrapped_below or side_by_side_readable, (
        f"'{label_text}' row is crushed at {width}px: the label "
        f"column renders only {column['width']:.0f}px wide beside the "
        f"{group['width']:.0f}px control group (column box {column}, group box "
        f"{group}) - the label/helper text stacks one word per line instead of "
        f"the row wrapping or keeping a readable label column"
    )

    # The crushed state is a tall sliver (14px wide by 262px tall).
    assert column["height"] < column["width"] * 6, (
        f"'{label_text}' label column is a vertical sliver: "
        f"{column['width']:.0f}px wide by {column['height']:.0f}px tall"
    )


@pytest.mark.parametrize(("label_text", "input_test_id"), _FONT_FAMILY_ROWS)
def test_appearance_font_family_rows_inline_on_desktop(
    page: Page, live_server: str, label_text: str, input_test_id: str
) -> None:
    """Guards the fix direction: wide layouts keep the control inline beside a readable label."""
    page.set_viewport_size(_DESKTOP_VIEWPORT)
    _open_appearance(page, live_server)

    layout = _measure_row(page, input_test_id, label_text)
    column = layout["column"]
    group = layout["group"]

    inline = group["top"] < column["bottom"] - 1
    assert inline and column["width"] >= MIN_READABLE_LABEL_COLUMN_PX, (
        f"'{label_text}' row lost its desktop layout at "
        f"{_DESKTOP_VIEWPORT['width']}px: the control group must sit inline "
        f"beside a readable label column (column box {column}, group box {group})"
    )
