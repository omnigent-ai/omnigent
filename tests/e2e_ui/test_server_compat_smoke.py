"""UI↔server compatibility smoke test.

Guards the one meaningful cross-version deployment ordering for the UI:
the SPA (built from HEAD) running against an older server release.

There is no "old SPA / new server" direction to test: in production the
SPA is always served by the server binary itself, so they always share a
version. The case where the user's browser has a *cached* old SPA hitting
a freshly updated server is real, but it is addressed by the server's
cache-busting headers (``/v1/info`` forces a hard-refresh), not by a
separate compat matrix.

The test is intentionally minimal — it mirrors ``chat/test_smoke.py``
exactly:

  1. Open a pre-created session (``seeded_session`` fixture).
  2. Send a message.
  3. Assert an assistant bubble appears with non-empty text.

A failure here means the SPA's REST/SSE wiring no longer works against
the old server, which is the regression this guards.

Activation
----------
Normal ``pytest tests/e2e_ui/`` runs include this test automatically.
For compat CI (old server, new SPA/runner), set the env knob before
running::

  OMNIGENT_COMPAT_SERVER_PYTHON=/tmp/old-server-venv/bin/python  \\
  OMNIGENT_COMPAT_SERVER_VERSION=0.9.0                            \\
  pytest tests/e2e_ui/test_server_compat_smoke.py -v

See ``docs/SERVER_VERSION_COMPAT_CI.md`` for the full compat workflow.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.min_server_version("0.9.0")
def test_ui_turn_completes_against_old_server(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The SPA can send a message and render a reply on an older server.

    Opens a pre-created session, types a prompt, clicks Send, and waits
    for a non-empty assistant bubble.  The ``min_server_version("0.9.0")``
    marker skips this test when the server is older than 0.9.0 — below
    that baseline the ``/v1/info`` capabilities probe and the session-init
    envelope format may be absent.

    A failure here means one of:

    - ``GET /v1/info`` response shape changed between the old server and
      what the new SPA expects (capability keys added/removed/renamed).
    - The session or event REST API changed incompatibly.
    - The SSE stream event names the SPA listens for were renamed or
      dropped on the old server side.

    :param page: Playwright page fixture (function-scoped).
    :param seeded_session: ``(base_url, session_id)`` from the
        pre-created session fixture; the server is the compat-pinned
        (or HEAD) build.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder("Ask the agent anything…")
    expect(composer).to_be_visible()
    composer.fill("Say 'pong' in one word.")
    page.get_by_role("button", name="Send", exact=True).click()

    expect(page).to_have_url(re.compile(rf"/c/{re.escape(session_id)}"))

    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    expect(assistant).to_be_visible(timeout=60_000)
    expect(assistant).to_have_text(re.compile(r"\S"), timeout=60_000)
