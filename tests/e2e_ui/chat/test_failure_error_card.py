"""Failure error pills render as clear English, not a raw code + log blob.

A harness launch/turn failure persists an ``error`` transcript item. Before,
the chat rendered it as ``Error · <code>`` over the raw message. Now the
failure renders as a centered pill leading with a human headline: a
classified failure shows its friendly title, and an unclassified one still
maps its ``code`` to a plain-English sentence (mirror of
``describe_failure_code`` / ``FAILURE_CODE_DESCRIPTIONS``) rather than
exposing the enum. The pill expands in place to the message and diagnostics.

Seeds the error item straight into the store (like ``seed_committed_turn``)
so the assertion is on transcript hydration + rendering, deterministic and
independent of any live turn.
"""

from __future__ import annotations

import json

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import _server_state, seed_committed_turn


def _publish_native_status(
    base_url: str,
    session_id: str,
    status: str,
    *,
    response_id: str,
    output: str | None = None,
) -> None:
    """Publish the status payload used by native harness forwarders."""
    data: dict[str, object] = {"status": status, "response_id": response_id}
    if output is not None:
        data["output"] = output
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
        timeout=10.0,
    )
    response.raise_for_status()


def _seed_error_item(
    session_id: str, *, code: str, message: str, level: str | None = None
) -> None:
    """Append a committed ``error`` transcript item to the session's store.

    Mirrors ``seed_committed_turn`` but writes an error banner item, so the
    chat hydrates and renders it through the same path a real
    ``response.error`` persists.

    :param session_id: Session to append to, e.g. ``"conv_abc123"``.
    :param code: Error classifier, e.g. ``"required_terminal_exited"``.
    :param message: Raw error message stored alongside the code.
    :param level: ``"info"`` seeds a neutral notice instead of a failure.
    :raises RuntimeError: If the server under test isn't one we spawned.
    """
    from omnigent.entities import ErrorData, NewConversationItem
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    database_uri = _server_state.get("database_uri")
    if not database_uri:
        raise RuntimeError(
            "seeding an error item needs the spawned server's database; it is "
            "unavailable when running against --ui-base-url."
        )
    SqlAlchemyConversationStore(str(database_uri)).append(
        session_id,
        [
            NewConversationItem(
                type="error",
                response_id="resp_seeded_error",
                data=ErrorData(source="execution", code=code, message=message, level=level),  # type: ignore[arg-type]
            ),
        ],
    )


def test_runner_disconnect_card_clears_when_the_runner_reports_a_live_status(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A ``runner_disconnected`` card disappears once the runner is live again.

    A server that closed a runner tunnel on its way down (a deploy) lit a
    "connection to the host dropped" card; the runner never died, and its
    next status edge on reconnect proves it is reachable. That card is an
    observation, not a turn result, so a live (non-``failed``) status edge
    must remove it — the fix in the ``session_status`` handler. Other
    failure cards (e.g. ``required_terminal_exited``) must NOT be cleared by
    a status edge, so a control card is seeded alongside and asserted to
    survive.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _seed_error_item(
        session_id,
        code="runner_disconnected",
        message="Runner disconnected unexpectedly.",
    )
    _seed_error_item(
        session_id,
        code="required_terminal_exited",
        message="Required terminal exited unexpectedly; the runtime is no longer available.",
    )

    page.goto(f"{base_url}/c/{session_id}")

    pills = page.get_by_test_id("error-pill")
    expect(pills).to_have_count(2, timeout=15_000)
    disconnect_pill = page.get_by_test_id("error-pill").filter(
        has_text="The connection to the host dropped unexpectedly"
    )
    expect(disconnect_pill).to_have_count(1)

    # The runner reconnects and reports a live turn. The disconnect card
    # clears; the genuine terminal-exit failure card stays.
    _publish_native_status(base_url, session_id, "running", response_id="codex_turn_recover")
    expect(disconnect_pill).to_have_count(0, timeout=15_000)
    surviving = page.get_by_test_id("error-pill")
    expect(surviving).to_have_count(1)
    expect(surviving).to_contain_text("The agent's terminal exited unexpectedly")


def test_unclassified_failure_renders_english_headline_not_raw_code(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A known failure code reads as a sentence, never the bare enum.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _seed_error_item(
        session_id,
        code="required_terminal_exited",
        message="Required terminal exited unexpectedly; the runtime is no longer available.",
    )

    page.goto(f"{base_url}/c/{session_id}")

    pill = page.get_by_test_id("error-pill")
    # The friendly, code-derived headline is shown...
    expect(pill).to_contain_text("The agent's terminal exited unexpectedly", timeout=15_000)
    # ...and the raw enum is not surfaced as the headline.
    expect(pill).not_to_contain_text("Error · required_terminal_exited", timeout=15_000)


def test_live_native_failure_status_surfaces_each_turn(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Each failed native response renders its status-carried error message."""
    base_url, session_id = seeded_session
    message = "You've hit your usage limit."

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=15_000)

    _publish_native_status(base_url, session_id, "running", response_id="codex_turn_1")
    _publish_native_status(
        base_url,
        session_id,
        "failed",
        response_id="codex_turn_1",
        output=message,
    )
    pills = page.get_by_test_id("error-pill")
    expect(pills).to_have_count(1, timeout=15_000)

    _publish_native_status(base_url, session_id, "running", response_id="codex_turn_2")
    _publish_native_status(
        base_url,
        session_id,
        "failed",
        response_id="codex_turn_2",
        output=message,
    )
    expect(pills).to_have_count(2, timeout=15_000)

    second_pill = pills.nth(1)
    second_pill.locator('button[aria-expanded="false"]').click()
    expect(second_pill.get_by_test_id("error-message-content")).to_contain_text(message)


def test_failed_turn_surfaces_error_as_pill_not_raw_text(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A failed turn shows its error through the pill, never a bare red "Error:".

    A native ``failed`` status leaves the assistant bubble at
    ``lifecycle == "failed"`` with a null free-form ``error`` — the message
    rides on the error pill. The bubble must not paint a separate raw
    ``Error:`` line beneath it (the empty-content red text a dropped host
    connection used to show); the failure reads through the pill alone.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server.
    :returns: None.
    """
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=15_000)

    _publish_native_status(base_url, session_id, "running", response_id="codex_turn_fail")
    _publish_native_status(
        base_url,
        session_id,
        "failed",
        response_id="codex_turn_fail",
        output="You've hit your usage limit.",
    )

    # The failure surfaces as the standard error pill...
    expect(page.get_by_test_id("error-pill")).to_have_count(1, timeout=15_000)
    # ...and never as a bare, raw-red "Error:" line beneath the bubble.
    expect(page.get_by_text("Error:", exact=True)).to_have_count(0)


def test_persisted_failure_expands_retries_and_dismisses_locally(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A persisted recoverable error exposes details and local controls.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _seed_error_item(
        session_id,
        code="required_terminal_exited",
        message=(
            "Required terminal exited unexpectedly; the runtime is no longer available.\n\n"
            "Terminal diagnostics:\nexit_code: 1\nsignal: SIGTERM\n\n"
            "Last captured output:\nfatal: runner unavailable"
        ),
    )
    retry_payloads: list[dict[str, object]] = []

    def _recover(route: Route) -> None:
        payload = route.request.post_data_json
        if payload.get("type") != "retry_session":
            route.continue_()
            return
        retry_payloads.append(payload)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "queued": False,
                    "recovered": True,
                    "recovery": "runner_relaunched",
                }
            ),
        )

    page.route(f"**/v1/sessions/{session_id}/events", _recover)
    page.goto(f"{base_url}/c/{session_id}")

    pill = page.get_by_test_id("error-pill")
    expect(pill).to_be_visible(timeout=15_000)
    headline = pill.get_by_role(
        "button", name="The agent's terminal exited unexpectedly", exact=False
    )
    expect(headline).to_have_attribute("aria-expanded", "false")
    expect(pill.get_by_test_id("error-message-content")).to_have_count(0)

    headline.focus()
    page.keyboard.press("Enter")
    expect(headline).to_have_attribute("aria-expanded", "true")
    expect(pill.get_by_role("heading", name="Message")).to_be_visible()
    expect(pill.get_by_test_id("error-message-content")).to_contain_text(
        "Required terminal exited unexpectedly"
    )

    # Clicking inside the expanded message body must not collapse the pill.
    pill.get_by_test_id("error-message-content").click()
    expect(headline).to_have_attribute("aria-expanded", "true")

    # Clicking pill padding (outside any button) toggles expansion.
    pill.click(position={"x": 3, "y": 3})
    expect(headline).to_have_attribute("aria-expanded", "false")
    pill.click(position={"x": 3, "y": 3})
    expect(headline).to_have_attribute("aria-expanded", "true")

    pill.get_by_role("button", name="View diagnostics").click()
    tabs = pill.get_by_role("tablist", name="Diagnostic sections")
    expect(tabs).to_be_visible()
    terminal_tab = tabs.get_by_role("tab", name="Terminal")
    expect(terminal_tab).to_have_attribute("aria-selected", "true")
    expect(pill.get_by_test_id("error-diagnostics-content")).to_contain_text("exit_code: 1")
    tabs.get_by_role("tab", name="Last captured output").click()
    expect(pill.get_by_test_id("error-diagnostics-content")).to_contain_text(
        "fatal: runner unavailable"
    )

    # Retry triggers recovery and removes the pill rather than expanding it.
    pill.get_by_role("button", name="Retry").click()
    expect(pill).to_have_count(0)
    assert retry_payloads == [{"type": "retry_session", "data": {}}]

    page.reload()
    pill = page.get_by_test_id("error-pill")
    expect(pill).to_be_visible(timeout=15_000)
    pill.get_by_role("button", name="Dismiss error message").click()
    expect(pill).to_have_count(0)


@pytest.mark.parametrize("viewport_width", [1440, 2400])
def test_error_row_divider_aligns_with_message_edges(
    page: Page,
    seeded_session: tuple[str, str],
    viewport_width: int,
) -> None:
    """The dashed rule runs from the assistant edge to the user edge.

    Regression net for the error-only shrink-wrap: ``MessageContent``
    defaults to ``w-fit``, so an error-only bubble once clipped the rule to
    the 560px pill. The full-width error turn must instead start where an
    assistant message starts and end where a right-aligned user message
    ends. Edges are measured from real messages in the same viewport, with
    no hardcoded pixel values. Both widths guard the responsive conversation
    column and the wider desktop layout.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server.
    :param viewport_width: Browser width in CSS pixels for this run.
    :returns: None.
    """
    base_url, session_id = seeded_session
    seed_committed_turn(
        session_id,
        prompt="Write something long.",
        reply=(
            "A long assistant answer that stretches the chat column to its full "
            "width regardless of the viewport size. " * 12
        ),
        response_id="resp_geometry_long",
    )
    _seed_error_item(
        session_id,
        code="required_terminal_exited",
        message="Required terminal exited unexpectedly; the runtime is no longer available.",
    )

    page.set_viewport_size({"width": viewport_width, "height": 1080})
    page.goto(f"{base_url}/c/{session_id}")
    pill = page.get_by_test_id("error-pill")
    expect(pill).to_be_visible(timeout=15_000)

    edges = page.evaluate(
        """() => {
          const pill = document.querySelector('[data-testid="error-pill"]');
          const row = pill.parentElement;
          const divider = row.querySelector('span[aria-hidden="true"]');
          const bubbles = [...document.querySelectorAll('[data-testid="message-bubble"]')];
          const assistantBubble = bubbles.find((bubble) =>
            bubble.dataset.role === 'assistant' &&
            bubble.textContent.includes('stretches the chat column'));
          const userBubble = bubbles.find((bubble) =>
            bubble.dataset.role === 'user' &&
            bubble.textContent.includes('Write something long.'));
          const rowRect = row.getBoundingClientRect();
          const dividerRect = divider.getBoundingClientRect();
          const assistantRect = assistantBubble.firstElementChild.getBoundingClientRect();
          const userRect = userBubble.firstElementChild.getBoundingClientRect();
          return {
            rowLeft: rowRect.left,
            rowRight: rowRect.right,
            dividerLeft: dividerRect.left,
            dividerRight: dividerRect.right,
            assistantLeft: assistantRect.left,
            userRight: userRect.right,
          };
        }"""
    )
    assert abs(edges["rowLeft"] - edges["assistantLeft"]) <= 2, (
        f"error row starts at {edges['rowLeft']:.0f}px but the assistant message starts at "
        f"{edges['assistantLeft']:.0f}px"
    )
    assert abs(edges["rowRight"] - edges["userRight"]) <= 2, (
        f"error row ends at {edges['rowRight']:.0f}px but the user message ends at "
        f"{edges['userRight']:.0f}px"
    )
    assert abs(edges["dividerLeft"] - edges["rowLeft"]) <= 1, (
        f"dashed rule starts at {edges['dividerLeft']:.0f}px but its row starts at "
        f"{edges['rowLeft']:.0f}px"
    )
    assert abs(edges["dividerRight"] - edges["rowRight"]) <= 1, (
        f"dashed rule ends at {edges['dividerRight']:.0f}px but its row ends at "
        f"{edges['rowRight']:.0f}px"
    )


def test_info_level_error_item_renders_as_notice_pill(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A persisted ``error`` item with ``level: "info"`` renders as a neutral notice.

    This is the codex fresh-thread fallback's notice: same pill, no failure tone,
    headline derived from the code, and it survives reload because it is an item.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _seed_error_item(
        session_id,
        code="codex_thread_reset",
        message="Codex could not load this session's saved transcript.",
        level="info",
    )

    page.goto(f"{base_url}/c/{session_id}")

    pill = page.locator('[data-testid="error-pill"][data-level="info"]')
    expect(pill).to_be_visible(timeout=15_000)
    expect(pill).to_contain_text("Codex hit an error reloading", timeout=15_000)
    expect(page.locator('[data-testid="error-pill"][data-level="error"]')).to_have_count(0)
