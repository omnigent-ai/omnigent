"""E2E: the Settings → Appearance font-size stepper scales the UI and persists.

The font-size control lives on the Settings page (``pages/SettingsPage.tsx``,
``UiFontSizeControl``): a segmented pill with a ``−`` button, a numeric value,
and a ``+`` button under a ``role="group"`` labelled "Interface font size". Stepping the
value writes the px choice to ``localStorage["omnigent:ui-font-size"]`` and
applies it as the ``--desktop-ui-font-size`` custom property on ``<html>`` (see
``lib/uiFontPreferences.ts``).

At desktop widths index.css maps that discrete value into typography tokens
while keeping the root rem grid fixed, so icons, controls, and spacing do not
resize. The default is 16px; the range is 11–18px, so the ``−``/``+`` buttons
disable at the bounds.

No LLM turn is involved.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

STORAGE_KEY = "omnigent:ui-font-size"
GROUP_NAME = "Interface font size"


def _desktop_ui_font_size(page: Page) -> str:
    """The ``--desktop-ui-font-size`` custom property applied to ``<html>``."""
    return page.evaluate(
        "() => getComputedStyle(document.documentElement)"
        ".getPropertyValue('--desktop-ui-font-size').trim()"
    )


def _stored_size(page: Page) -> str | None:
    """The persisted font-size preference, or None when unset (default 16)."""
    return page.evaluate(f"() => window.localStorage.getItem('{STORAGE_KEY}')")


def _open_appearance(page: Page, base_url: str) -> None:
    """Navigate to the Settings Appearance section and wait for the control."""
    page.goto(f"{base_url}/settings/appearance")
    expect(page.get_by_role("group", name=GROUP_NAME, exact=True)).to_be_visible(timeout=30_000)


def test_ui_font_size_scales_and_persists(page: Page, seeded_session: tuple[str, str]) -> None:
    """Stepping the size updates the token + value live and survives a reload.

    A fresh context has no stored preference → default 16px. Increasing the
    size updates ``--desktop-ui-font-size`` and persists the px value; a page
    reload restores it (no reset, no flash back to the default).
    """
    base_url, _session_id = seeded_session
    _open_appearance(page, base_url)

    value = page.get_by_test_id("ui-font-size-input")
    increase = page.get_by_test_id("ui-font-size-inc")

    # Fresh context → default 16px token, nothing stored.
    expect(value).to_have_value("16")
    assert _stored_size(page) is None, "expected no persisted size on a fresh load"
    assert _desktop_ui_font_size(page) == "16px", "fresh load should apply the default size"

    # → 18px: two steps up. The value, applied token, and storage all move.
    increase.click()
    increase.click()
    expect(value).to_have_value("18")
    assert _stored_size(page) == "18"
    assert _desktop_ui_font_size(page) == "18px", "font token did not track the stepped size"

    # The choice survives a full reload (persisted + re-applied before paint).
    page.reload()
    expect(page.get_by_role("group", name=GROUP_NAME, exact=True)).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("ui-font-size-input")).to_have_value("18")
    assert _desktop_ui_font_size(page) == "18px", "font size was not restored after reload"


def test_ui_font_size_steppers_clamp_at_bounds(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """The ``−``/``+`` buttons disable at the 11px min and 18px max."""
    base_url, _session_id = seeded_session

    # Seed the max before the app boots so the "+" button renders disabled.
    page.goto(base_url)
    page.evaluate(f"() => window.localStorage.setItem('{STORAGE_KEY}', '18')")
    _open_appearance(page, base_url)

    value = page.get_by_test_id("ui-font-size-input")
    decrease = page.get_by_test_id("ui-font-size-dec")
    increase = page.get_by_test_id("ui-font-size-inc")

    # At the 18px max, only "+" is disabled.
    expect(value).to_have_value("18")
    expect(increase).to_be_disabled()
    expect(decrease).to_be_enabled()

    # Hold "−" down to the 11px min; there it flips to "−" disabled, "+" enabled.
    for _ in range(8):
        if decrease.is_disabled():
            break
        decrease.click()
    expect(value).to_have_value("11")
    expect(decrease).to_be_disabled()
    expect(increase).to_be_enabled()


def test_ui_font_size_input_allows_free_editing(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Typing in the box doesn't clamp mid-edit; blur settles the final value.

    Regression guard: the box binds to a free-form draft, so backspacing "13"
    down to "1" (below the 11px min) must SHOW "1" without snapping to 11 or
    persisting the transient value. Retyping a valid size applies it live, and
    blurring a still-out-of-range draft clamps to the minimum.
    """
    base_url, _session_id = seeded_session

    # Seed a two-digit size so deleting a digit lands on a below-min "1".
    page.goto(base_url)
    page.evaluate(f"() => window.localStorage.setItem('{STORAGE_KEY}', '13')")
    _open_appearance(page, base_url)

    value = page.get_by_test_id("ui-font-size-input")
    expect(value).to_have_value("13")

    # Backspace to "1": the box holds the partial value; nothing clamps or
    # re-persists while the draft is out of range.
    value.click()
    value.press("End")
    value.press("Backspace")
    expect(value).to_have_value("1")
    assert _stored_size(page) == "13", "a mid-edit below-min draft must not persist"

    # Finish typing a valid size — it applies live and persists.
    value.press("8")
    expect(value).to_have_value("18")
    assert _stored_size(page) == "18"
    assert _desktop_ui_font_size(page) == "18px"

    # A still-out-of-range draft clamps to the minimum on blur.
    value.fill("1")
    value.blur()
    expect(value).to_have_value("11")
    assert _stored_size(page) == "11"
