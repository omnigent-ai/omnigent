"""E2E: Appearance settings font-family rows must not crush their labels on iPhone.

On a phone-sized viewport (390-430px wide) the Settings > Appearance page
rendered the "Font family" and "Code font family" rows with broken formatting:
the label + helper column was squeezed to a ~14-54px sliver, so the text
stacked vertically one word (often one letter) per line beside the input.

Mechanism: in ``UiFontFamilyControl`` / ``UiCodeFontFamilyControl``
(``web/src/pages/SettingsPage.tsx``) the label column had a zero flex-basis
while sitting next to a ``shrink-0`` control group (ghost Reset button + input,
~290px). The row's ``flex-wrap`` therefore only fired when the control group
*alone* no longer fit (viewport <= ~375px). In the 376-767px band - every
current iPhone: 390, 393, 414, 428/430 - the group fit beside a near-zero-width
label column and the text was crushed instead of the row wrapping. The sibling
font-size stepper rows were immune because their label column had no flex-grow
with a zero basis, letting ``flex-wrap`` drop the control onto its own line.

The invariant asserted here is layout-strategy agnostic: on a phone viewport
each font-family row must either wrap its control group onto its own line
below the label, or keep a readably wide label column beside it - never a
letter-per-line sliver. On a desktop-wide viewport the control must stay
inline beside a readable label (the layout the rows were designed for).

No LLM turn is involved.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, ViewportSize, expect

# The label + helper column must never render narrower than this when the
# control sits beside it. The crushed layout measures 14px at 390 / 54px at
# 430; anything below ~96px cannot fit even the two-word label on a line.
MIN_READABLE_LABEL_COLUMN_PX = 96

# iPhone 12/13/14 logical viewport - squarely inside the crush band, and the
# form factor the bug was reported on.
_IPHONE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}

# The full crushed band measured live: iPhone 12/13/14 (390), 15/16 (393),
# 11/XR/Plus (414), and Pro Max (430). 375 (SE) already wrapped correctly.
_PHONE_WIDTHS = [390, 393, 414, 430]

# A comfortably wide desktop viewport where the control group is designed to
# sit inline beside the label, right-aligned with the stepper rows above.
_DESKTOP_VIEWPORT: ViewportSize = {"width": 1280, "height": 800}

# The two Appearance rows built from the label-beside-input pattern that
# crushed: (row label text, input test id).
_FONT_FAMILY_ROWS = [
    ("Font family", "ui-font-family-input"),
    ("Code font family", "code-font-family-input"),
]


def _open_appearance(page: Page, base_url: str) -> None:
    """Navigate to Settings > Appearance the way a phone user does.

    On mobile the settings nav is a full-screen sidebar overlay pinned open on
    entering ``/settings``, so the journey must tap the Appearance nav row
    (which closes the overlay) before the section content is reachable.
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
    # Let the scroll settle so the measured boxes (and any recording) show the
    # row the user actually sees.
    page.wait_for_timeout(500)

    layout = _row_layout(page, input_test_id, label_text)
    assert "error" not in layout, layout
    return layout


@pytest.mark.parametrize("width", _PHONE_WIDTHS)
@pytest.mark.parametrize(("label_text", "input_test_id"), _FONT_FAMILY_ROWS)
def test_appearance_font_family_rows_not_crushed_on_iphone(
    page: Page,
    seeded_session: tuple[str, str],
    label_text: str,
    input_test_id: str,
    width: int,
) -> None:
    """At an iPhone viewport, the font-family rows must stay readable.

    Reporter's journey: open the app on an iPhone, open Settings > Appearance,
    scroll down - the Font family / Code font family rows render with the
    label text crushed into a vertical one-word-per-line sliver beside the
    input. A correct layout either wraps the input onto its own line below the
    label (as the <=375px band already does) or keeps a readably wide label
    column beside it.
    """
    base_url, _session_id = seeded_session
    page.set_viewport_size({"width": width, "height": _IPHONE_VIEWPORT["height"]})
    _open_appearance(page, base_url)

    layout = _measure_row(page, input_test_id, label_text)
    column = layout["column"]
    group = layout["group"]

    # The layout is acceptable in either of two shapes:
    #   1. the control group wrapped below the label column (flex-wrap fired), or
    #   2. the two sit side by side AND the label column is readably wide.
    wrapped_below = group["top"] >= column["bottom"] - 1
    side_by_side_readable = column["width"] >= MIN_READABLE_LABEL_COLUMN_PX

    assert wrapped_below or side_by_side_readable, (
        f"'{label_text}' row is crushed at {width}px: the label "
        f"column renders only {column['width']:.0f}px wide beside the "
        f"{group['width']:.0f}px control group (column box {column}, group box "
        f"{group}) - the label/helper text stacks one word per line instead of "
        f"the row wrapping or keeping a readable label column"
    )

    # Guard the sliver symptom directly too: the helper paragraph inside the
    # label column must not be taller than it is wide (the crushed state is
    # 14px wide by 262px tall).
    assert column["height"] < column["width"] * 6, (
        f"'{label_text}' label column is a vertical sliver: "
        f"{column['width']:.0f}px wide by {column['height']:.0f}px tall"
    )


@pytest.mark.parametrize(("label_text", "input_test_id"), _FONT_FAMILY_ROWS)
def test_appearance_font_family_rows_inline_on_desktop(
    page: Page,
    seeded_session: tuple[str, str],
    label_text: str,
    input_test_id: str,
) -> None:
    """On a desktop viewport the control stays inline beside a readable label.

    Regression guard for the fix direction: making the rows wrap on phones must
    not make the control drop onto its own line on wide layouts, where it is
    designed to sit right-aligned beside the label like the stepper rows above.
    """
    base_url, _session_id = seeded_session
    page.set_viewport_size(_DESKTOP_VIEWPORT)
    _open_appearance(page, base_url)

    layout = _measure_row(page, input_test_id, label_text)
    column = layout["column"]
    group = layout["group"]

    inline = group["top"] < column["bottom"] - 1
    assert inline and column["width"] >= MIN_READABLE_LABEL_COLUMN_PX, (
        f"'{label_text}' row lost its desktop layout at "
        f"{_DESKTOP_VIEWPORT['width']}px: the control group must sit inline "
        f"beside a readable label column (column box {column}, group box {group})"
    )
