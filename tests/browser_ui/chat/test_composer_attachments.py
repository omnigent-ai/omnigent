"""Browser contracts for attachment chips, rejection, recovery, and file drops.

These checks exercise real file inputs and DataTransfer events. Upload failure
is mocked; upload storage and model ingestion remain in tests/e2e.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, Route, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract

_COMPOSER = "Send a message…"
# A text file passes both the OS picker filter and client validation.
_ATTACH_NAME = "attach_sample.txt"
_ATTACH_BODY = "composer attachment e2e sample\n"

# Unsupported by the default server policy; an operator may explicitly allow it.
_MEDIA_NAME = "clip.mp4"

# JSON is its own MIME (``application/json``), which is NOT covered by the
# ``text/*`` wildcard, so it has to be listed in the ``accept`` attr explicitly
# for the OS picker (and the drag-drop ``matchesAccept`` validator) to admit it.
_JSON_NAME = "attach_sample.json"
_JSON_BODY = '{"composer": "attachment", "e2e": true}\n'

# A ZIP is a common input, such as an iCloud Photos export.
_ZIP_NAME = "photos.zip"

# The server's real 415 body for an unsupported upload, from
# ``routes_resources.upload_session_file``. Used to drive the failed-send path.
_SERVER_415_DETAIL = (
    "Unsupported attachment type 'video/mp4'. Attach images, PDF, or text/code files, "
    "or use Claude Code or Codex for archives, Office documents, and databases."
)


def test_attach_supported_files_and_remove(
    page: Page, chat_session_contract: ChatSessionContract, tmp_path: Path
) -> None:
    """Text removal, JSON MIME acceptance, and ZIP cards share one composer journey."""
    chat = chat_session_contract
    sample = tmp_path / _ATTACH_NAME
    sample.write_text(_ATTACH_BODY)

    page.goto(chat.url)
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

    sample = tmp_path / _JSON_NAME
    sample.write_text(_JSON_BODY)

    # The accept attr is what gates the picker/drag-drop; assert JSON is listed.
    accept = file_input.get_attribute("accept")
    assert accept is not None and "application/json" in accept, (
        f"composer file input should accept application/json; got {accept!r}"
    )

    file_input.set_input_files(str(sample))

    remove_button = page.get_by_role("button", name=f"Remove {_JSON_NAME}")
    expect(remove_button).to_be_visible(timeout=10_000)
    expect(page.get_by_text(_JSON_NAME, exact=True)).to_be_visible()

    remove_button.click()
    expect(remove_button).to_be_hidden()

    sample = tmp_path / _ZIP_NAME
    sample.write_bytes(b"PK\x03\x04 a small but real-enough zip payload")

    # Without .zip in the accept attr the OS picker hides the very files the
    # server now accepts, so the feature is unreachable from the UI.
    accept = file_input.get_attribute("accept")
    assert accept is not None and ".zip" in accept, (
        f"composer file input should accept .zip; got {accept!r}"
    )

    file_input.set_input_files(str(sample))

    # Accepted: the chip and its remove control exist.
    expect(page.get_by_role("button", name=f"Remove {_ZIP_NAME}")).to_be_visible(timeout=10_000)
    chip = page.get_by_text(_ZIP_NAME, exact=True).locator("xpath=..")
    expect(chip).to_contain_text("ZIP ·")
    expect(chip).not_to_contain_text("workspace")


def test_reject_unsupported_type(
    page: Page, chat_session_contract: ChatSessionContract, tmp_path: Path
) -> None:
    """An unsupported type (mp4) is rejected client-side: no chip, inline error.

    Covers the validation ``addFiles`` gained (``validateAttachments`` in
    lib/attachments.ts). Office documents and archives are no longer rejected
    here, so this pins the shape
    refused by the default server policy. Driving the
    hidden input directly (``set_input_files`` bypasses the accept filter, so
    the file reaches ``addFiles``) must yield NO chip and a visible error.
    """
    chat = chat_session_contract
    sample = tmp_path / _MEDIA_NAME
    sample.write_bytes(b"\x00\x00\x00 not a real mp4, just an unsupported binary")

    page.goto(chat.url)
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)

    file_input = page.locator('input[type="file"][accept*="image/"]')
    file_input.set_input_files(str(sample))

    # Rejected: no chip / remove control for the file.
    expect(page.get_by_role("button", name=f"Remove {_MEDIA_NAME}")).to_have_count(0)
    # And the inline rejection error is shown.
    expect(page.get_by_text("can't be attached", exact=False)).to_be_visible(timeout=10_000)


def test_landing_rejects_unsupported_type_and_keeps_message(
    page: Page, chat_session_contract: ChatSessionContract, tmp_path: Path
) -> None:
    """The landing composer rejects an unsupported file without losing the message.

    The landing screen is the case that actually bit users: it used to append
    incoming files unchecked, so a zip only failed after the session had been
    created and navigated into — and by then the typed message was gone (the
    landing draft is cleared on create and the pending prompt is consumed
    destructively), leaving an error and nothing to resend.

    Three things are pinned here, none of which a component test can reach,
    because they depend on the real hidden input and on no session being
    created:

    1. No chip appears — the file never enters composer state.
    2. The typed message survives the rejection.
    3. The rejection notice clears on the next keystroke. A rejected file is
       never attached, so there is no chip to remove and nothing else would
       ever clear it; left sticky it reads as a hard blocker.
    """
    chat = chat_session_contract
    base_url = chat.base_url
    chat.contract.json(f"/v1/hosts/{chat.host_id}/filesystem", {"path": "/", "entries": []})
    sample = tmp_path / _MEDIA_NAME
    sample.write_bytes(b"\x00\x00\x00 not a real mp4, just an unsupported binary")

    page.goto(base_url)
    composer = page.get_by_test_id("new-chat-landing-input")
    expect(composer).to_be_visible(timeout=30_000)

    composer.fill("summarize these photos")
    page.get_by_test_id("new-chat-landing-file-input").set_input_files(str(sample))

    # Rejected: no chip, and the reason names the file.
    expect(page.get_by_role("button", name=f"Remove {_MEDIA_NAME}")).to_have_count(0)
    error = page.get_by_test_id("new-chat-landing-attachment-error")
    expect(error).to_be_visible(timeout=10_000)
    expect(error).to_contain_text(_MEDIA_NAME)

    # The message the user typed is untouched, and no session was created —
    # still on the landing screen, not redirected into /c/<id>.
    expect(composer).to_have_value("summarize these photos")
    assert "/c/" not in page.url, f"a session was created despite the rejection: {page.url}"

    # Typing clears the notice so it can't read as a blocker.
    composer.fill("summarize these photos please")
    expect(error).to_have_count(0, timeout=10_000)


def test_failed_upload_restores_the_message(
    page: Page, chat_session_contract: ChatSessionContract, tmp_path: Path
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
    chat = chat_session_contract
    sample = tmp_path / _ATTACH_NAME
    sample.write_text(_ATTACH_BODY)

    chat.contract.json(
        f"/v1/sessions/{chat.session_id}/resources/files",
        {"detail": _SERVER_415_DETAIL},
        method="POST",
        status=415,
    )

    page.goto(chat.url)
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
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    """A file dropped on the transcript attaches to the composer.

    The target used to be the composer box alone, so a screenshot dropped on the
    transcript fell through to the browser, which navigated away from the session
    to render the file — losing the page.
    """
    chat = chat_session_contract

    page.goto(chat.url)
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
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    """A file dropped on the sidebar is not a composer attachment.

    The target is the chat column, not the window, so the shell around it keeps
    whatever drag behavior it has.
    """
    chat = chat_session_contract

    page.goto(chat.url)
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


@pytest.mark.parametrize("surface", ["chat", "landing"])
@pytest.mark.parametrize("allowed", [True, False], ids=["allowed", "disabled"])
def test_composer_uses_published_filesystem_policy(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
    surface: str,
    allowed: bool,
) -> None:
    """Both composers mirror server policy in the picker and preserve rejected drafts."""
    base_url, session_id = seeded_session

    def publish_policy(route: Route) -> None:
        response = route.fetch()
        info = response.json()
        info["filesystem_attachment_policy"] = {
            "allowed_extensions": [".mp4"] if allowed else [],
            "denied_extensions": [],
            "max_bytes": 50 * 1024 * 1024,
            "max_total_bytes": 100 * 1024 * 1024,
            "max_files": 10,
            "harnesses": ["claude-native", "codex-native"],
        }
        route.fulfill(response=response, json=info)

    page.route("**/v1/info", publish_policy)
    page.goto(f"{base_url}/c/{session_id}" if surface == "chat" else base_url)
    composer = (
        page.get_by_placeholder(_COMPOSER)
        if surface == "chat"
        else page.get_by_test_id("new-chat-landing-input")
    )
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Inspect this video using local tools")
    file_input = (
        page.locator('input[type="file"][accept*="image/"]')
        if surface == "chat"
        else page.get_by_test_id("new-chat-landing-file-input")
    )
    expect(file_input).to_have_attribute("accept", re.compile(r".+"))
    accept = file_input.get_attribute("accept") or ""
    assert (".mp4" in accept.split(",")) is allowed
    assert ".zip" not in accept.split(","), "configured allowlist replaces built-in types"
    sample = tmp_path / _MEDIA_NAME
    sample.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    file_input.set_input_files(str(sample))
    remove = page.get_by_role("button", name=f"Remove {_MEDIA_NAME}")
    if allowed:
        expect(remove).to_be_visible(timeout=10_000)
    else:
        expect(remove).to_have_count(0)
        expect(
            page.get_by_text("this file type is not allowed by server policy", exact=False)
        ).to_be_visible()
    expect(composer).to_have_value("Inspect this video using local tools")
