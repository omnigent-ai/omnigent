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

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import _server_state


def _seed_error_item(session_id: str, *, code: str, message: str) -> None:
    """Append a committed ``error`` transcript item to the session's store.

    Mirrors ``seed_committed_turn`` but writes an error banner item, so the
    chat hydrates and renders it through the same path a real
    ``response.error`` persists.

    :param session_id: Session to append to, e.g. ``"conv_abc123"``.
    :param code: Error classifier, e.g. ``"required_terminal_exited"``.
    :param message: Raw error message stored alongside the code.
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
                data=ErrorData(source="execution", code=code, message=message),
            ),
        ],
    )


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
