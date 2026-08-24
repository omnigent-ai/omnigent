"""E2E: /compact on a disconnected native session shows the reconnect hint.

A native-terminal (claude-native) session compacts only in its vendor TUI, so
when its runner is offline the server can't run server-side compaction. Rather
than the old confusing "agent declares no LLM model" error, ``/compact`` now
surfaces an actionable message telling the user to reconnect (send a message to
wake the runner) first. This drives that client contract end to end: on a
native-wrapper session, typing ``/compact`` and submitting posts the compact
control, and the composer renders the server's reconnect error inline.

The e2e harness binds a real *online* ``openai-agents`` runner, not a native
one with a sleeping sandbox, so the server-side auto-wake path itself is covered
by ``tests/server/integration/test_sessions_compact.py``. Here the browser's
view is patched into a native-wrapper session and the compact ``POST`` is
intercepted to return the exact 503 envelope the server change produces, so the
test exercises the real client ``/compact`` → error-render path.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

# Must match the OmnigentError raised in the compact branch of
# omnigent/server/routes/sessions/routes_events.py.
_RECONNECT_ERROR = (
    "Can't compact this session while its runner is offline. "
    "Reconnect the session (send a message to wake it), then run /compact again."
)
_WRAPPER_LABEL_KEY = "omnigent.wrapper"
_CLAUDE_NATIVE_WRAPPER = "claude-code-native-ui"


def _force_native_wrapper_and_stub_compact(page: Page, session_id: str) -> None:
    """Patch the browser's view into a native-wrapper session + stub /compact.

    Two route patches, registered before navigation:

    - ``GET /v1/sessions/{id}`` (snapshot) → stamp the claude-native wrapper
      label so ``isNativeWrapper`` is true and the composer treats ``/compact``
      as a supported native command (``showCompact``).
    - ``POST /v1/sessions/{id}/events`` for a ``compact`` body → return the
      server's 503 reconnect error envelope so the composer renders it inline.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch, e.g. ``"conv_abc123"``.
    """

    def _patch_snapshot(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        labels = dict(payload.get("labels") or {})
        labels[_WRAPPER_LABEL_KEY] = _CLAUDE_NATIVE_WRAPPER
        payload["labels"] = labels
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    def _patch_events(route: Route) -> None:
        request = route.request
        path = urlparse(request.url).path
        if request.method != "POST" or path != f"/v1/sessions/{session_id}/events":
            route.continue_()
            return
        # Only the compact control gets the offline-runner error; let other
        # events (e.g. messages) pass through to the real server.
        body: dict[str, object] | None = None
        try:
            body = json.loads(request.post_data or "")
        except (json.JSONDecodeError, TypeError):
            body = None
        if not (isinstance(body, dict) and body.get("type") == "compact"):
            route.continue_()
            return
        route.fulfill(
            status=503,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {"error": {"code": "runner_unavailable", "message": _RECONNECT_ERROR}}
            ),
        )

    page.route(re.compile(r"/v1/sessions/[^/]+/events$"), _patch_events)
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), _patch_snapshot)


def test_compact_offline_native_session_shows_reconnect_error(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Typing ``/compact`` on an offline native session shows the reconnect hint.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to a native-wrapper session and the
        compact POST is stubbed to the server's 503 reconnect error.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _force_native_wrapper_and_stub_compact(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=15_000)

    # Type /compact and press Enter. With the slash-command suggestions menu
    # open and "/compact" highlighted, Enter completes the menu selection,
    # which runs the builtin immediately (it takes no argument) → the compact
    # control POST.
    composer.click()
    composer.fill("/compact")
    composer.press("Enter")

    # The composer renders the server's reconnect error inline, NOT the old
    # "agent declares no LLM model" message.
    error = page.get_by_text(_RECONNECT_ERROR, exact=False)
    expect(error).to_be_visible(timeout=15_000)
