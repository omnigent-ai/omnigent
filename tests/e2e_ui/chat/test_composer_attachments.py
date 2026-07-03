"""E2E: attaching and removing files in the chat composer.

The composer (``pages/ChatPage.tsx``) lets the user attach files via the
paperclip button (which clicks a hidden ``<input type="file">``), paste, or
drag-drop. Each attached file renders as a chip below the textarea with a
per-file remove button; on send the files are embedded inline in the message
(there is no separate upload endpoint), and ``removeFile`` drops a chip.

This flow has no coverage below the browser: no web vitest test exercises
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
# Composer accepts image/*,application/pdf,text/*,application/json (the hidden
# input's accept attr); a .txt file is in-scope and keeps the fixture trivial.
# ``set_input_files`` bypasses the accept filter, but ``addFiles`` now validates
# every file (type + size, via lib/attachments.ts) — a .txt passes both.
_ATTACH_NAME = "attach_sample.txt"
_ATTACH_BODY = "composer attachment e2e sample\n"

# An unsupported binary type: ``addFiles`` rejects it (no chip) and shows an
# inline error. Used by ``test_reject_unsupported_type``.
_PPTX_NAME = "deck.pptx"

# JSON is its own MIME (``application/json``), which is NOT covered by the
# ``text/*`` wildcard, so it has to be listed in the ``accept`` attr explicitly
# for the OS picker (and the drag-drop ``matchesAccept`` validator) to admit it.
_JSON_NAME = "attach_sample.json"
_JSON_BODY = '{"composer": "attachment", "e2e": true}\n'


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


def test_attach_json_file(page: Page, seeded_session: tuple[str, str], tmp_path: Path) -> None:
    """A ``.json`` file is admitted by the picker and attaches as a chip.

    Guards the change that added ``application/json`` to the composer's
    ``accept`` list. Two things are asserted:

    1. The hidden input advertises ``application/json`` in its ``accept`` attr.
       This is the part the OS file picker and the drag-drop ``matchesAccept``
       validator (``prompt-input.tsx``) actually read — and the part that would
       regress if the MIME were dropped from the list. ``set_input_files`` can't
       cover it because it bypasses the accept filter entirely.
    2. Driving a real ``.json`` file through the input still yields the chip +
       remove control, i.e. ``addFiles`` accepts the JSON end-to-end.
    """
    base_url, session_id = seeded_session
    sample = tmp_path / _JSON_NAME
    sample.write_text(_JSON_BODY)

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)

    file_input = page.locator('input[type="file"][accept*="image/"]')
    # The accept attr is what gates the picker/drag-drop; assert JSON is listed.
    accept = file_input.get_attribute("accept")
    assert accept is not None and "application/json" in accept, (
        f"composer file input should accept application/json; got {accept!r}"
    )

    file_input.set_input_files(str(sample))

    remove_button = page.get_by_role("button", name=f"Remove {_JSON_NAME}")
    expect(remove_button).to_be_visible(timeout=10_000)
    expect(page.get_by_text(_JSON_NAME, exact=True)).to_be_visible()


def test_reject_unsupported_type(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    """An unsupported type (pptx) is rejected client-side: no chip, inline error.

    Covers the validation ``addFiles`` gained (``validateAttachments`` in
    lib/attachments.ts): only images, PDF, and text/code files attach; office /
    binary formats are rejected before upload with a per-file message. Driving
    the hidden input with a ``.pptx`` (``set_input_files`` bypasses the accept
    filter, so the file reaches ``addFiles``) must yield NO chip and a visible
    rejection error.
    """
    base_url, session_id = seeded_session
    sample = tmp_path / _PPTX_NAME
    sample.write_bytes(b"PK\x03\x04 not a real pptx, just an unsupported binary")

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)

    file_input = page.locator('input[type="file"][accept*="image/"]')
    file_input.set_input_files(str(sample))

    # Rejected: no chip / remove control for the file.
    expect(page.get_by_role("button", name=f"Remove {_PPTX_NAME}")).to_have_count(0)
    # And the inline rejection error is shown.
    expect(page.get_by_text("can't be attached", exact=False)).to_be_visible(timeout=10_000)
