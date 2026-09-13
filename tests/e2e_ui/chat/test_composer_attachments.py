"""E2E: attaching and removing files in the chat composer.

The composer (``pages/ChatPage.tsx``) lets the user attach files via the
paperclip button (which clicks a hidden ``<input type="file">``), paste, or
drag-drop. Each attached file renders as a chip below the textarea with a
per-file remove button; on send each file is POSTed to
``/v1/sessions/{id}/resources/files`` and the message references the returned
``file_id``, and ``removeFile`` drops a chip.

The new-chat landing screen (``shell/NewChatDialog.tsx``) has its own composer
with the same affordances, and it validates attachments the same way — that
one matters most, because a file rejected only after the session is created
strands the user's first message in a session they never wanted.

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

Drag-drop covers the whole chat column (``hooks/useFileDropTarget.ts``, bound to
``[data-chat-surface]``), not just the composer box, and nothing outside it. It
needs a real browser: the drag is claimed only when ``dataTransfer.types`` carries
``"Files"``, and ``preventDefault`` on ``dragover`` is what makes a ``drop`` fire
at all — both of which jsdom approximates rather than implements.
"""

from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import Page, Route, expect

_COMPOSER = "Send a message…"
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

# A zip is the case users actually hit (dragging an iCloud Photos export).
_ZIP_NAME = "photos.zip"

# The server's real 415 body for an unsupported upload, from
# ``routes_resources.upload_session_file``. Used to drive the failed-send path.
_SERVER_415_DETAIL = (
    "Unsupported attachment type 'application/zip'. Only images, PDF, "
    "and text/code files can be attached."
)


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


def test_landing_rejects_unsupported_type_and_keeps_message(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    """The new-chat landing composer rejects a zip without losing the typed message.

    The landing screen is the case that actually bit users: it used to append
    incoming files unchecked, so a zip only failed after the session had been
    created and navigated into — and by then the typed message was gone (the
    landing draft is cleared on create and the pending prompt is consumed
    destructively), leaving an error and nothing to resend.

    Three things are pinned here, none of which a component test can reach,
    because they depend on the real hidden input and on no session being
    created:

    1. No chip appears — the zip never enters composer state.
    2. The typed message survives the rejection.
    3. The rejection notice clears on the next keystroke. A rejected file is
       never attached, so there is no chip to remove and nothing else would
       ever clear it; left sticky it reads as a hard blocker.
    """
    base_url, _session_id = seeded_session
    sample = tmp_path / _ZIP_NAME
    sample.write_bytes(b"PK\x03\x04 not a real zip, just an unsupported binary")

    page.goto(base_url)
    composer = page.get_by_test_id("new-chat-landing-input")
    expect(composer).to_be_visible(timeout=30_000)

    composer.fill("summarize these photos")
    page.get_by_test_id("new-chat-landing-file-input").set_input_files(str(sample))

    # Rejected: no chip, and the reason names the file.
    expect(page.get_by_role("button", name=f"Remove {_ZIP_NAME}")).to_have_count(0)
    error = page.get_by_test_id("new-chat-landing-attachment-error")
    expect(error).to_be_visible(timeout=10_000)
    expect(error).to_contain_text(_ZIP_NAME)

    # The message the user typed is untouched, and no session was created —
    # still on the landing screen, not redirected into /c/<id>.
    expect(composer).to_have_value("summarize these photos")
    assert "/c/" not in page.url, f"a session was created despite the rejection: {page.url}"

    # Typing clears the notice so it can't read as a blocker.
    composer.fill("summarize these photos please")
    expect(error).to_have_count(0, timeout=10_000)


def test_failed_upload_restores_the_message(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    """A send whose upload fails hands the message back to the composer.

    Before, a failed upload left the user with an error and an empty composer:
    ``submit`` clears the text optimistically, the optimistic bubble rolls
    back, and nothing else held the message. Now ``send`` stashes it in
    ``failedSendDraft`` and the composer restores it.

    The failure is injected at the network boundary (the upload route responds
    415 with the server's real body) rather than by attaching an unsupported
    file — client-side validation would reject that before any request, so it
    would never exercise this path. The 415 body also pins the second half of
    the fix: the banner must carry the server's reason, not a bare
    ``upload failed: 415`` built from an empty HTTP/2 ``statusText``.
    """
    base_url, session_id = seeded_session
    sample = tmp_path / _ATTACH_NAME
    sample.write_text(_ATTACH_BODY)

    def _reject_upload(route: Route) -> None:
        route.fulfill(
            status=415,
            content_type="application/json",
            body=json.dumps({"detail": _SERVER_415_DETAIL}),
        )

    page.route("**/resources/files", _reject_upload)

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)

    page.locator('input[type="file"][accept*="image/"]').set_input_files(str(sample))
    expect(page.get_by_role("button", name=f"Remove {_ATTACH_NAME}")).to_be_visible(timeout=10_000)

    composer.fill("look at this file")
    composer.press("Enter")

    # The compact pill keeps the reason one expansion away instead of
    # dropping it or replacing it with a bare status line.
    pill = page.get_by_test_id("error-pill")
    expect(pill).to_be_visible(timeout=30_000)
    headline = pill.get_by_role("button", name="Something went wrong", exact=False)
    expect(headline).to_have_attribute("aria-expanded", "false")
    headline.click()
    expect(page.get_by_text("Unsupported attachment type", exact=False)).to_be_visible()
    # And the message is back in the composer, ready to retry.
    expect(composer).to_have_value("look at this file", timeout=10_000)


# Synthesises an OS file drag: Playwright can't drive a real desktop-to-browser
# drag, but a page-built ``DataTransfer`` fires the same events.
_DISPATCH_FILE_DRAG = """
([selector, types, name, body]) => {
  const target = document.querySelector(selector);
  if (!target) throw new Error(`no drop target for ${selector}`);
  const transfer = new DataTransfer();
  transfer.items.add(new File([body], name, { type: "text/plain" }));
  const fire = (type) =>
    target.dispatchEvent(
      new DragEvent(type, { dataTransfer: transfer, bubbles: true, cancelable: true }),
    );
  let handled = null;
  for (const type of types) handled = fire(type);
  return handled;
}
"""


def test_file_dropped_on_the_transcript_attaches(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """A file dropped on the transcript attaches to the composer.

    The target used to be the composer box alone, so a screenshot dropped on the
    transcript fell through to the browser, which navigated away from the session
    to render the file — losing the page.
    """
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)

    # Guards the premise: the drop lands outside the composer box.
    assert page.evaluate(
        "() => !document.querySelector('[role=log]').closest('[data-composer-card]')"
    ), "the transcript resolved inside the composer box — the test proves nothing"

    page.evaluate(_DISPATCH_FILE_DRAG, ["[role=log]", ["dragenter"], "hover.txt", "x"])
    expect(page.get_by_test_id("file-drop-overlay")).to_be_visible(timeout=10_000)

    handled = page.evaluate(
        _DISPATCH_FILE_DRAG,
        ["[role=log]", ["dragover", "drop"], _ATTACH_NAME, _ATTACH_BODY],
    )
    # False = preventDefault, i.e. the app claimed the drop instead of letting
    # the browser open the file.
    assert handled is False, "the chat column did not claim the file drop"

    expect(page.get_by_role("button", name=f"Remove {_ATTACH_NAME}")).to_be_visible(timeout=10_000)
    expect(page.get_by_test_id("file-drop-overlay")).to_have_count(0)


def test_file_dropped_outside_the_chat_column_is_ignored(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """A file dropped on the sidebar is not a composer attachment.

    The target is the chat column, not the window, so the shell around it keeps
    whatever drag behavior it has.
    """
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)

    sidebar = "[data-testid=sidebar], nav, aside"
    assert page.evaluate(
        "([selector]) => {"
        "  const el = document.querySelector(selector);"
        "  return !!el && !el.closest('[data-chat-surface]');"
        "}",
        [sidebar],
    ), "no element outside the chat column to drop on"

    handled = page.evaluate(
        _DISPATCH_FILE_DRAG,
        [sidebar, ["dragenter", "dragover", "drop"], _ATTACH_NAME, _ATTACH_BODY],
    )
    assert handled is True, "a drop outside the chat column was claimed"
    expect(page.get_by_test_id("file-drop-overlay")).to_have_count(0)
    expect(page.get_by_role("button", name=f"Remove {_ATTACH_NAME}")).to_have_count(0)
