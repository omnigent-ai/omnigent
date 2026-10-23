"""E2E: the ``/clear`` slash command in the in-session composer.

Covers the user-facing behavior this feature adds: "start a new
conversation, keeping this terminal" is reachable from the Chat view's
composer instead of only from the Terminal tab.

Three properties are driven end-to-end in a real browser, because each is
owned by a different piece and a regression in any one is invisible to the
unit tests:

- **Offered on a harness the runner can rotate.** ``/clear`` is gated on
  ``CLEAR_CAPABLE_HARNESSES``, which mirrors the runner's
  ``body_type == "clear"`` dispatch. The gate reads the bound session's
  harness, so only a real snapshot bind proves the wiring.
- **Not offered where the runner has no handler.** Offering it on such a
  harness would surface the server's "not available for this session type"
  400 as a dead end, which is the failure the gate exists to prevent.
- **Routed as a control event, not as chat text.** This is the whole point
  of the change: before it, ``/clear`` fell through to the plaintext send
  path and was pasted into the vendor pane (which is wrong for Codex, whose
  command is ``/new``). Asserting the POSTed event type is what pins that.

The server fixture seeds a normal ``hello_world`` session so the page boots
against the real app/server; a route patch reshapes only
``GET /v1/sessions/{id}`` into the harness under test. No live vendor CLI is
needed — the runner's injection is covered by
``tests/runner/test_app_sessions_native_events_options.py`` and the server
dispatch by ``tests/server/integration/test_sessions_clear.py``. Mirrors
``_patch_session_as_claude_sdk`` in ``test_claude_sdk_compact_and_context``.

Selectors mirror the component: rows are
``data-testid="slash-menu-item-<name-sans-slash>"`` (see
``SlashCommandMenu.tsx``).
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry


def _patch_session_harness(
    page: Page,
    session_id: str,
    harness: str,
    *,
    posted_events: list[dict[str, object]] | None = None,
) -> None:
    """Reshape the browser's ``GET /v1/sessions/{id}`` onto *harness*.

    Patches only the exact snapshot path (sub-paths pass through) so the SPA
    boots against the real server but binds the harness under test. When
    *posted_events* is given, ``POST /v1/sessions/{id}/events`` bodies are
    recorded and a ``clear`` is answered locally with the server's
    handled-by-the-runner shape, so the test needs no live runner.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch, e.g. ``"conv_abc123"``.
    :param harness: Harness to report, e.g. ``"claude-native"``.
    :param posted_events: When given, collects each POSTed event body.
    """

    def _handle(route: Route) -> None:
        request = route.request
        path = urlparse(request.url).path
        if posted_events is not None and request.method == "POST" and path.endswith("/events"):
            try:
                body = request.post_data_json
            except ValueError:
                body = None
            if isinstance(body, dict):
                posted_events.append(body)
                if body.get("type") == "clear":
                    # The runner handled it (200) -> the server answers 202
                    # with queued=False. Short-circuited here because this
                    # fixture has no runner bound to rotate a vendor pane.
                    route.fulfill(
                        status=202,
                        headers={"content-type": "application/json"},
                        body=json.dumps({"queued": False}),
                    )
                    return
            route.continue_()
            return
        if path != f"/v1/sessions/{session_id}" or request.method != "GET":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        headers = {**response.headers, "content-type": "application/json"}
        payload["harness"] = harness
        route.fulfill(status=200, headers=headers, body=json.dumps(payload))

    page.route("**/v1/sessions/**", _handle)


def test_clear_command_offered_on_a_clear_capable_harness(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """``/clear`` is offered on a claude-native session.

    claude-native is in ``CLEAR_CAPABLE_HARNESSES`` because the runner maps
    it to Claude Code's own ``/clear``. A regression that dropped the command
    from the built-ins, or gated it on the wrong harness set, leaves this row
    absent.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a server-backed
        session; the browser snapshot is patched to claude-native.
    """
    base_url, session_id = seeded_session
    _patch_session_harness(page, session_id, "claude-native")

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    composer.fill("/clear")

    expect(page.get_by_test_id("slash-menu-item-clear")).to_be_visible(timeout=15_000)


def test_clear_command_not_offered_on_a_harness_without_a_handler(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """``/clear`` is absent for claude-sdk, which the runner cannot rotate.

    claude-sdk has no ``clear`` handler, so the server would answer "/clear
    is not available for this session type". Offering the row anyway would
    make the menu advertise a dead end. ``/compact`` IS offered on claude-sdk,
    so asserting it is visible in the same breath proves the menu rendered and
    this test isn't passing on a blank menu.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a server-backed
        session; the browser snapshot is patched to claude-sdk.
    """
    base_url, session_id = seeded_session
    _patch_session_harness(page, session_id, "claude-sdk")

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    # "/c" prefixes both, so one query renders whichever rows are gated on.
    composer.fill("/c")

    expect(page.get_by_test_id("slash-menu-item-compact")).to_be_visible(timeout=15_000)
    expect(page.get_by_test_id("slash-menu-item-clear")).to_have_count(0)


def test_selecting_clear_posts_a_control_event_not_chat_text(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Selecting ``/clear`` POSTs a ``clear`` event instead of sending text.

    This is the behavior change. Before it, ``/clear`` was not a known
    command, so it fell through to the plaintext send path and was pasted
    into the vendor pane — which happens to work on Claude Code and is wrong
    on Codex (``/new``). Asserting that a ``clear`` event was POSTed AND that
    no message item carrying the literal text was, pins both halves.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a server-backed
        session; the browser snapshot is patched to claude-native.
    """
    base_url, session_id = seeded_session
    posted: list[dict[str, object]] = []
    _patch_session_harness(page, session_id, "claude-native", posted_events=posted)

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    composer.fill("/clear")
    clear_row = page.get_by_test_id("slash-menu-item-clear")
    expect(clear_row).to_be_visible(timeout=15_000)
    clear_row.click()

    # The composer clears on a handled command; a plaintext fall-through
    # would leave the text in flight as a message instead.
    expect(composer).to_have_value("", timeout=15_000)

    # The menu closes once the command is executed, which also means the
    # POST above has been issued by the time the assertions below run.
    expect(clear_row).to_have_count(0)
    assert any(event.get("type") == "clear" for event in posted), (
        f"Selecting /clear must POST a clear control event; got {posted!r}. "
        "An empty list means it fell through to the plaintext send path."
    )
    literal_sends = [
        event
        for event in posted
        if event.get("type") != "clear" and "/clear" in json.dumps(event.get("data", {}))
    ]
    assert literal_sends == [], (
        f"/clear must not also reach the harness as chat text; got {literal_sends!r}."
    )
