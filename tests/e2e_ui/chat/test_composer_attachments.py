"""E2E: attaching and removing files in the chat composer.

The composer (``pages/ChatPage.tsx``) lets the user attach files via the
paperclip button (which clicks a hidden ``<input type="file">``), paste, or
drag-drop. Each attached file renders as a chip below the textarea with a
per-file remove button; on send the files are embedded inline in the message
(there is no separate upload endpoint), and ``removeFile`` drops a chip.

This flow has no coverage below the browser: no ap-web vitest test exercises
the ChatPage composer's ``addFiles`` / ``removeFile`` path, and the attach
mechanism (a real hidden file input populated by the OS file picker) is exactly
what a unit test can't drive. Playwright's ``set_input_files`` populates the
hidden input directly — the same change event the picker fires — so the
attach → chip → remove cycle is fully deterministic and needs no agent turn or
network: the chips are local component state.

The assertion pins to the chip's per-file remove control
(``aria-label="Remove {filename}"``, ChatPage.tsx) appearing after attach and
disappearing after the remove click.
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Page, expect

_COMPOSER = "Ask the agent anything…"
# Composer accepts image/*,application/pdf,text/* (the hidden input's accept
# attr); a .txt file is in-scope and keeps the fixture trivial. ``set_input_files``
# bypasses the accept filter anyway — ``addFiles`` does no client-side filtering.
_ATTACH_NAME = "attach_sample.txt"
_ATTACH_BODY = "composer attachment e2e sample\n"


def test_attach_then_remove_file(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    """Attach a file via the hidden input → chip + remove button appear → remove clears it."""
    base_url, session_id = seeded_session
    sample = tmp_path / _ATTACH_NAME
    sample.write_text(_ATTACH_BODY)

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)

    # The attach affordance is a paperclip button; its click target is the
    # hidden file input. Drive the input directly (the picker can't be scripted).
    file_input = page.locator('input[type="file"][accept*="image/"]')
    file_input.set_input_files(str(sample))

    # The chip renders below the textarea with a per-file remove button whose
    # accessible name carries the filename.
    remove_button = page.get_by_role("button", name=f"Remove {_ATTACH_NAME}")
    expect(remove_button).to_be_visible(timeout=10_000)
    expect(page.get_by_text(_ATTACH_NAME, exact=True)).to_be_visible()

    # Removing the chip drops it from composer state.
    remove_button.click()
    expect(remove_button).to_be_hidden(timeout=10_000)
    expect(page.get_by_text(_ATTACH_NAME, exact=True)).to_be_hidden()
