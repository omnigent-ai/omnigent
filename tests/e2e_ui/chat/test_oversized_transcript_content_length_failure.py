"""An oversized session's turn (and its fork's) fail with the byte-cap error.

Journey: a session's transcript grows large enough that a request carrying it
exceeds the deployment's content-length cap
(``RequestSize(bytes): 33967957, Limit(bytes): 33554432``). The turn fails with
a ``BAD_REQUEST`` "exceeds maximum allowed content length" error, every
subsequent turn re-hits the same cap, and forking inherits the failure because
the fork deep-copies the same oversized transcript. The failure must classify
as a context-window overflow — the error pill's headline reads "The
conversation grew past the model's context window." — not as a generic
unrecognized turn error, so the user is pointed at compaction instead of a
dead end.

Environment fidelity: the ``33554432``-byte (32 MiB) limit is the Databricks
Apps front-door content-length cap and is NOT present in the omnigent codebase
(the only omnigent analog is the 10 MiB ``_SessionEventBodyLimitRoute`` on
``POST /v1/sessions/{id}/events``). This test therefore stands in for the
Databricks host: it seeds a genuinely oversized (>33554432-byte) transcript so
the "large session" precondition and the fork's copied history are real, forks
it through the real UI fork action, and drives the failed turn through the real
native-forwarder failed-status path carrying the exact reported error string.
"""

from __future__ import annotations

import re

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import seed_committed_items

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'

# Exact reported failure detail (RequestSize just over the 33554432 cap).
_CONTENT_LENGTH_ERROR = (
    "Server received a request which exceeds maximum allowed content length. "
    "RequestSize(bytes): 33967957, Limit(bytes): 33554432"
)

# ~16KB of wrappable text per item; ~2200 items ~= 35MB of history, so the
# stored transcript genuinely exceeds the reported 33554432-byte cap.
_ITEM_TEXT = ("lorem ipsum dolor sit amet consectetur adipiscing elit " * 320)[:16000]
_N_ITEMS = 2200
_LIMIT_BYTES = 33_554_432


def _seed_oversized_transcript(session_id: str) -> None:
    """Seed a committed transcript larger than the reported 33554432-byte cap."""
    from omnigent.entities import MessageData, NewConversationItem

    items = []
    for i in range(_N_ITEMS):
        role = "user" if i % 2 == 0 else "assistant"
        items.append(
            NewConversationItem(
                type="message",
                response_id=f"resp_{i // 2}",
                data=MessageData(
                    role=role,
                    content=[
                        {
                            "type": "input_text" if role == "user" else "output_text",
                            "text": f"turn {i}: {_ITEM_TEXT}",
                        }
                    ],
                    agent="hello_world" if role == "assistant" else None,
                ),
            )
        )
    for start in range(0, len(items), 200):
        seed_committed_items(session_id, items[start : start + 200])


def _publish_native_status(
    base_url: str,
    session_id: str,
    status: str,
    *,
    response_id: str,
    output: str | None = None,
) -> None:
    """Publish the status a native harness forwarder reports for a turn."""
    data: dict[str, object] = {"status": status, "response_id": response_id}
    if output is not None:
        data["output"] = output
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
        timeout=10.0,
    )
    response.raise_for_status()


def _fail_turn_with_content_length_error(base_url: str, session_id: str, response_id: str) -> None:
    """Drive one turn that the server rejects for exceeding the content cap."""
    _publish_native_status(base_url, session_id, "running", response_id=response_id)
    _publish_native_status(
        base_url,
        session_id,
        "failed",
        response_id=response_id,
        output=_CONTENT_LENGTH_ERROR,
    )


def _expect_content_length_failure(page: Page, *, expected_pills: int) -> None:
    """Assert the content-length error surfaces classified as a context overflow."""
    pills = page.get_by_test_id("error-pill")
    expect(pills).to_have_count(expected_pills, timeout=30_000)
    last_pill = pills.nth(expected_pills - 1)
    # The byte-cap rejection classifies as a context-window overflow, so the
    # pill leads with the recoverable-context headline, not a generic error.
    expect(last_pill.get_by_test_id("error-headline")).to_have_text(
        "The conversation grew past the model's context window."
    )
    collapsed = last_pill.locator('button[aria-expanded="false"]')
    if collapsed.count() > 0:
        collapsed.first.click()
    message = last_pill.get_by_test_id("error-message-content")
    expect(message).to_contain_text("exceeds maximum allowed content length")
    expect(message).to_contain_text(str(_LIMIT_BYTES))


def test_oversized_session_turn_and_fork_fail_with_content_length_error(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The oversized session's fork fails, and the source turn cannot recover.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a runner-bound session.
    """
    base_url, session_id = seeded_session
    _seed_oversized_transcript(session_id)

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=90_000)
    last_assistant = page.locator(_ASSISTANT).last
    expect(last_assistant).to_be_visible(timeout=90_000)

    # Fork the oversized session from the last response: the fork deep-copies
    # the full over-cap transcript, so it inherits the same failing request.
    last_assistant.hover()
    last_assistant.get_by_test_id("fork-from-response").click()
    expect(page.get_by_test_id("fork-session-dialog")).to_be_visible()
    with page.expect_response(
        lambda r: r.url.endswith(f"/v1/sessions/{session_id}/fork"),
        timeout=300_000,
    ) as resp_info:
        page.get_by_test_id("fork-session-submit").click()
    fork_response = resp_info.value
    fork_response.finished()
    assert fork_response.status == 201, f"fork failed: HTTP {fork_response.status}"

    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(session_id)})(conv_)?[0-9a-f]+"),
        timeout=300_000,
    )
    fork_id = page.url.rstrip("/").rsplit("/c/", 1)[1]
    assert fork_id and fork_id != session_id

    # The fork's first turn carries the inherited oversized transcript and is
    # rejected by the same content-length cap.
    _fail_turn_with_content_length_error(base_url, fork_id, "fork_turn_1")
    _expect_content_length_failure(page, expected_pills=1)

    # Back on the source, the turn fails the same way and retrying re-hits
    # the cap; each failure must keep the context-overflow classification.
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=90_000)
    _fail_turn_with_content_length_error(base_url, session_id, "turn_1")
    _expect_content_length_failure(page, expected_pills=1)
    _fail_turn_with_content_length_error(base_url, session_id, "turn_2")
    _expect_content_length_failure(page, expected_pills=2)


def test_server_rejects_oversized_transcript_carrying_event(
    seeded_session: tuple[str, str],
) -> None:
    """The real server content-length guard rejects an over-cap ``/events`` body.

    Exercises omnigent's own ``_SessionEventBodyLimitRoute`` (10 MiB) — the
    in-product analog of the Databricks 32 MiB front-door cap the reported
    failure hits — so an oversized transcript-carrying request is refused
    rather than dispatched.
    """
    base_url, session_id = seeded_session
    oversized_text = "x" * (11 * 1024 * 1024)
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": oversized_text}],
                }
            },
        },
        timeout=30.0,
    )
    assert response.status_code == 400, response.status_code
    assert "exceeds" in response.text.lower() and "limit" in response.text.lower(), response.text
