"""E2E: the Settings → Appearance font-family picker re-fonts the UI and persists.

The font-family control lives on the Settings page (``pages/SettingsPage.tsx``,
``UiFontFamilyControl``): a searchable combobox plus a ``Reset`` button under a
``role="group"`` labelled "Font family". The combobox lists a few common families
and offers whatever the user types as its own entry, so an unlisted font is still
reachable. Choosing a name writes it to ``localStorage["omnigent:ui-font-family"]``
and applies it as the ``--ui-font-family`` custom property on ``<html>`` (see
``lib/uiFontPreferences.ts``). The "System" entry is the default: the property is
removed and the ``html`` rule falls back to ``var(--font-sans)``.

Because the whole rem-based UI inherits its font from the root ``html`` rule
(``font-family: var(--ui-font-family, var(--font-sans))``), setting that one
variable re-fonts the entire chrome. The value is applied before first paint in
``main.tsx`` so a reload doesn't flash the default first.

No LLM turn is involved.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

STORAGE_KEY = "omnigent:ui-font-family"
# Substring match: the real placeholder ends in a non-ASCII ellipsis.
SEARCH_PLACEHOLDER = "Search or type a font"


def _ui_font_family(page: Page) -> str:
    """The ``--ui-font-family`` custom property applied to ``<html>``."""
    return page.evaluate(
        "() => getComputedStyle(document.documentElement)"
        ".getPropertyValue('--ui-font-family').trim()"
    )


def _stored_family(page: Page) -> str | None:
    """The persisted font-family preference, or None when unset (default)."""
    return page.evaluate(f"() => window.localStorage.getItem('{STORAGE_KEY}')")


def _open_appearance(page: Page, base_url: str) -> None:
    """Navigate to the Settings Appearance section and wait for the control."""
    page.goto(f"{base_url}/settings/appearance")
    expect(page.get_by_role("group", name="Font family", exact=True)).to_be_visible(timeout=30_000)


def _pick_typed_family(page: Page, family: str) -> None:
    """Open the combobox, type ``family``, and take the "use what I typed" entry."""
    page.get_by_test_id("ui-font-family-combobox").click()
    page.get_by_placeholder(SEARCH_PLACEHOLDER).fill(family)
    page.get_by_test_id("ui-font-family-combobox-use-typed").click()


def test_ui_font_family_applies_and_persists(page: Page, seeded_session: tuple[str, str]) -> None:
    """Choosing a family updates the applied property + trigger and survives reload.

    A fresh context has no stored preference → the combobox reads "System" and no
    ``--ui-font-family`` override is applied (the UI uses the system stack).
    Choosing a name applies the property and persists the choice; a page reload
    restores it (no reset, no flash to default).
    """
    base_url, _session_id = seeded_session
    _open_appearance(page, base_url)

    trigger = page.get_by_test_id("ui-font-family-combobox")

    # Fresh context → default entry selected, nothing stored, no override applied.
    expect(trigger).to_have_text("System")
    assert _stored_family(page) is None, "expected no persisted family on a fresh load"
    assert _ui_font_family(page) == "", "fresh load should apply no family override"

    # → "Georgia": unlisted, so it arrives through the typed entry. The trigger,
    # the applied property, and storage all move together. The applied value leads
    # with the chosen family and appends the system stack (so an uninstalled or
    # partial name degrades to the default sans, not serif), so the resolved
    # custom property starts with — rather than equals — "Georgia".
    _pick_typed_family(page, "Georgia")
    expect(trigger).to_have_text("Georgia")
    assert _stored_family(page) == '"Georgia"', "the chosen family was not persisted"
    assert _ui_font_family(page).startswith("Georgia"), "root family did not track the choice"

    # The choice survives a full reload (persisted + re-applied before paint).
    page.reload()
    expect(page.get_by_role("group", name="Font family", exact=True)).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("ui-font-family-combobox")).to_have_text("Georgia")
    assert _ui_font_family(page).startswith("Georgia"), "family was not restored after reload"


def test_ui_font_family_reset_restores_system_default(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """The Reset button clears the override and returns to the system default."""
    base_url, _session_id = seeded_session

    # Seed a family before the app boots so the override is applied on load.
    page.goto(base_url)
    page.evaluate(f"() => window.localStorage.setItem('{STORAGE_KEY}', '\"Georgia\"')")
    _open_appearance(page, base_url)

    trigger = page.get_by_test_id("ui-font-family-combobox")
    reset = page.get_by_test_id("ui-font-family-reset")

    # The seeded family renders and is applied to the root (leading the appended
    # system-stack fallback, so the resolved value starts with "Georgia").
    expect(trigger).to_have_text("Georgia")
    assert _ui_font_family(page).startswith("Georgia")

    # → Reset: the trigger returns to "System", the override is removed, and the
    # key is cleared.
    reset.click()
    expect(trigger).to_have_text("System")
    assert _ui_font_family(page) == "", "the family override was not removed on reset"
    assert _stored_family(page) is None, "reset did not clear the persisted family"
