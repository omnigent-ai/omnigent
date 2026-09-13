"""E2E: an imported inline image must not blank the conversation.

A Codex rollout represents a pasted image as ``{"type": "input_image",
"image_url": "data:image/png;base64,…"}`` — inline bytes, no ``file_id`` — and
``omnigent/session_import/local.py`` preserves the block as-is. The web bubble
used to read ``file_id`` unconditionally, so that one block threw while the
user message rendered and took the whole transcript down: a blank page with
``Cannot read properties of undefined (reading 'startsWith')`` in the console.

A component test can prove the bubble renders, but not that the page survives:
the throw happened inside React's render, so what actually regressed is the
whole conversation route. This drives the real SPA and asserts both halves —
the inline image paints, and the messages around it are still there.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import seed_committed_items

_PROMPT = "Here is the pasted screenshot."
_REPLY = "Reviewing the screenshot."

# 1x1 PNG, inline exactly as a Codex rollout carries it.
_INLINE_PNG = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAA"
    "C0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _seed_imported_image_turn(session_id: str) -> None:
    """Commit a user turn holding an imported inline image block.

    :param session_id: Session to append the turn to.
    """
    from omnigent.entities import MessageData, NewConversationItem

    seed_committed_items(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id="codex:history",
                data=MessageData(
                    role="user",
                    content=[
                        {"type": "input_text", "text": _PROMPT},
                        {
                            "type": "input_image",
                            "image_url": _INLINE_PNG,
                            "detail": "auto",
                        },
                    ],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="codex:history",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": _REPLY}],
                    agent="codex-native-ui",
                ),
            ),
        ],
    )


def test_imported_inline_image_renders_without_blanking_the_conversation(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The inline image paints and its conversation stays readable."""
    base_url, session_id = seeded_session
    _seed_imported_image_turn(session_id)

    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(f"{base_url}/c/{session_id}")

    # Both messages present — the blanked render showed neither.
    expect(page.get_by_text(_PROMPT)).to_be_visible(timeout=30_000)
    expect(page.get_by_text(_REPLY)).to_be_visible(timeout=30_000)

    # The image itself decodes from the transcript's own bytes: no session
    # file resource exists for an imported block, so a fetch-backed preview
    # would stay empty here.
    image = page.get_by_alt_text("Attached image")
    expect(image).to_be_visible(timeout=30_000)
    page.wait_for_function(
        """() => {
            const img = document.querySelector('img[alt="Attached image"]');
            return img && img.complete && img.naturalWidth > 0;
        }""",
        timeout=30_000,
    )

    assert not errors, errors
