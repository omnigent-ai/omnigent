"""The chat's "Reconnecting" state shows an animated spinner.

After a recoverable runner connection failure the chat renders an error pill
with a Retry button. Clicking Retry swaps the pill for a centered
"Reconnecting" badge for the duration of the reconnection attempt. That badge
must carry an animated spinner next to the text so the UI reads as actively
working rather than stalled, while the text stays the accessible status label.

Seeds a ``runner_disconnected`` error item straight into the store (like
``test_failure_error_card``) so the pill hydrates deterministically, then
holds the ``retry_session`` POST open so the reconnecting state persists long
enough to observe.
"""

from __future__ import annotations

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import _server_state


def _seed_error_item(session_id: str, *, code: str, message: str) -> None:
    """Append a committed ``error`` transcript item to the session's store.

    Mirrors the helper in ``test_failure_error_card`` so the chat hydrates
    and renders the failure through the same path a real ``response.error``
    persists.

    :param session_id: Session to append to, e.g. ``"conv_abc123"``.
    :param code: Error classifier, e.g. ``"runner_disconnected"``.
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


def test_reconnecting_state_shows_spinner(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The in-flight retry's "Reconnecting" badge carries an animated spinner.

    Journey: a runner connection failure leaves an error pill in the chat;
    the user clicks Retry; while the reconnection attempt is in flight the
    pill reads "Reconnecting". The badge must pair that text with a spinner
    (the app's ``animate-spin`` loader, frozen automatically under
    ``prefers-reduced-motion`` by the global CSS gate) so the state reads as
    active work, not a stall.

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

    # Hold the retry_session POST open (never fulfill) so the banner stays in
    # its "Reconnecting" state for the duration of the assertions — mirroring
    # a slow real reconnection attempt.
    def _hold_retry(route: Route) -> None:
        payload = route.request.post_data_json if route.request.method == "POST" else None
        if not isinstance(payload, dict) or payload.get("type") != "retry_session":
            route.continue_()
            return
        # Intentionally unanswered: the in-flight retry keeps the
        # reconnecting UI mounted.

    page.route(f"**/v1/sessions/{session_id}/events", _hold_retry)
    page.goto(f"{base_url}/c/{session_id}")

    # The seeded runner-connection failure renders the error pill.
    pill = page.get_by_test_id("error-pill")
    expect(pill).to_be_visible(timeout=15_000)
    expect(pill).to_contain_text("The connection to the host dropped unexpectedly")

    # The user retries; the pill swaps to the "Reconnecting" state.
    pill.get_by_role("button", name="Retry").click()
    reconnecting = page.get_by_test_id("error-reconnecting")
    expect(reconnecting).to_be_visible(timeout=15_000)

    # The text stays the accessible status label...
    status = reconnecting.get_by_role("status")
    expect(status).to_contain_text("Reconnecting")

    # ...and an animated spinner accompanies it for the full attempt, so the
    # in-flight retry reads as active work rather than static text.
    expect(reconnecting.locator(".animate-spin")).to_be_visible(timeout=5_000)
