"""Browser e2e: a fork's image attachment loads from the fork's own copy.

``fork_conversation`` deep-copies conversation items (including a message's
``file_id`` image block); the fork must also own a copy of the underlying
session-scoped file resource that block references, so any consumer that
resolves it against the FORK's own session id — the web chat renderer here,
native harness executors elsewhere (``omnigent/inner/native_attachments.py``)
— can load it.

This drives the reported journey at the web surface: attach + send an image
in a plain SDK chat, fork it via the header "Fork" menu (a full clone, not
tied to any specific assistant response), and confirm the forked chat's copy
of that image loads. Same-agent (no native-CLI switch) is enough to prove the
file-copy behavior; the native-harness half of the same journey is covered at
the API level in ``tests/e2e/test_cross_family_fork_image_attachment_e2e.py``
(needs a real host + logged-in CLI, which this harness doesn't spawn).
"""

from __future__ import annotations

import base64
import re
import time
from pathlib import Path

from playwright.sync_api import Page, Response, expect

_COMPOSER_LABEL = "Message the agent"
_SCREENSHOT_NAME = "fork-attachment.png"
_USER = '[data-testid="message-bubble"][data-role="user"]'
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'

# 240x180 PNG (same fixture used by test_image_only_first_message_send.py).
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAPAAAAC0CAIAAAAl/ja/AAABaUlEQVR42u3SQREAMAjAsDFdyEEdKvHAk0sk"
    "9BpZ/eCKLwGGBkODocHQGBoMDYYGQ4OhMTQYGgwNhgZDY2gwNBgaDA2GxtBgaDA0GBoMjaHB0GBoMDQYGkOD"
    "ocHQYGgwNIYGQ4OhwdAYGgwNhgZDg6ExNBgaDA2GBkNjaDA0GBoMDYbG0GBoMDQYGgyNocHQYGgwNBgaQ4Oh"
    "wdBgaDA0hgZDg6HB0GBoDA2GBkODoTE0GBoMDYYGQ2NoMDQYGgwNhsbQYGgwNBgaDI2hwdBgaDA0GBpDg6HB"
    "0GBoMDSGBkODocHQYGgMDYYGQ4OhMTQYGgwNhgZDY2gwNBgaDA2GxtBgaDA0GBoMjaHB0GBoMDQYGkODocHQ"
    "YGgwNIYGQ4OhwdBgaAwNhgZDg6HB0BgaDA2GBkNjaDA0GBoMDYbG0GBoMDQYGgyNocHQYGgwNBgaQ4OhwdBg"
    "aDA0hgZDg6HB0GBoDA2GBkPD3gAlVgKygGocMwAAAABJRU5ErkJggg=="
)


def _write_png(tmp_path: Path) -> Path:
    sample = tmp_path / _SCREENSHOT_NAME
    sample.write_bytes(base64.b64decode(_PNG_B64))
    return sample


def test_fork_carries_image_reference_and_its_resource(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """Fork a chat that has an image attachment; the clone's copy loads.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound ``hello_world`` (openai-agents SDK) session.
    :param tmp_path: Per-test temp dir for the attached PNG.
    """
    base_url, session_id = seeded_session
    png = _write_png(tmp_path)

    file_responses: list[tuple[str, int]] = []

    def _track_file_response(response: Response) -> None:
        if "/resources/files/" in response.url:
            file_responses.append((response.url, response.status))

    page.on("response", _track_file_response)

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)

    # Attach + send the image in the SOURCE session; it loads fine there.
    page.locator('input[type="file"][accept*="image/"]').set_input_files(str(png))
    expect(page.get_by_role("button", name=f"Remove {_SCREENSHOT_NAME}")).to_be_visible(
        timeout=10_000
    )
    send = page.get_by_role("button", name="Send", exact=True)
    expect(send).to_be_enabled(timeout=10_000)
    send.click()

    source_user_bubble = page.locator(_USER)
    expect(source_user_bubble.locator("img").first).to_be_visible(timeout=30_000)
    # The turn's own reply content isn't asserted on: only that it settles.
    expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=60_000)
    expect(page.locator('[data-testid="working-indicator"]')).to_have_count(0, timeout=60_000)

    assert any(status == 200 for _, status in file_responses), (
        f"expected the source session's own image fetch to succeed, got {file_responses!r}"
    )

    # Fork via the header menu's "Fork" item — a full clone of the whole
    # session, not anchored to any one response, so it doesn't depend on
    # the assistant turn having produced renderable text.
    page.get_by_test_id("header-conversation-actions").click()
    page.get_by_role("menuitem", name="Fork", exact=True).click()
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()
    page.get_by_test_id("fork-session-submit").click()

    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(session_id)})[0-9a-f]{{32}}"),
        timeout=30_000,
    )
    expect(dialog).not_to_be_visible()
    fork_id = page.url.rsplit("/c/", 1)[1].split("?", 1)[0]
    assert fork_id != session_id

    # The forked transcript renders a user bubble with an <img> tag — the
    # copied message item kept its image content block, and the tag points
    # at the FORK's own session-scoped file path — which must load, because
    # the fork owns its own copy of the underlying resource.
    forked_user_bubble = page.locator(_USER)
    forked_img = forked_user_bubble.locator("img").first
    expect(forked_img).to_be_attached(timeout=30_000)
    src = forked_img.get_attribute("src")
    assert src is not None and f"/sessions/{fork_id}/resources/files/" in src, (
        f"expected the fork's copied image block to reference its OWN session id "
        f"{fork_id!r} (a fresh copy is expected to own its data), got src={src!r}"
    )
    # The preview renders with loading="lazy"; scroll it into view so the
    # browser actually issues the fetch instead of deferring it.
    forked_img.scroll_into_view_if_needed()

    def _fork_file_status() -> int | None:
        for url, status in file_responses:
            if fork_id in url:
                return status
        return None

    _wait_for(lambda: _fork_file_status() is not None, timeout_s=30.0)
    assert _fork_file_status() == 200, (
        f"expected the fork's own copy of the image to load, got {file_responses!r}"
    )


def _wait_for(predicate, *, timeout_s: float = 15.0, interval_s: float = 0.25) -> None:
    """Poll *predicate* until truthy or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError("condition not met within timeout")
