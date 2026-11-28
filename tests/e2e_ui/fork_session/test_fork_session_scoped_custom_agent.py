"""Browser e2e: fork can switch onto a session-scoped custom agent.

A custom agent launched from a spec file is stored as a session-scoped row
(its owning session references it). The fork dialog discovers those agents
from the user's own sessions and offers them as switch targets, and
``POST /v1/sessions`` accepts the same agent id after the owning-session
read check (``validate_session_agent``). ``POST /v1/sessions/{id}/fork``
must accept it the same way instead of rejecting the pick with
"Agent not found or not bindable".
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _CUSTOM_AGENT_NAME

# Unique marker so the copied-transcript assertion can't match UI chrome or
# another test's message.
_MARKER = "cobalt-custom-fork-marker"


def _session_agent(base_url: str, session_id: str) -> dict[str, Any]:
    """Fetch the agent bound to *session_id* via ``GET /v1/sessions/{id}/agent``.

    :param base_url: Live server base URL, e.g. ``"http://127.0.0.1:51234"``.
    :param session_id: The session whose bound agent to resolve.
    :returns: The AgentObject wire dict (``id``, ``name``, ``harness``, ...).
    """
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/agent", timeout=30.0)
    resp.raise_for_status()
    return resp.json()


def test_fork_switches_onto_session_scoped_custom_agent(
    page: Page,
    seeded_session: tuple[str, str],
    custom_agent_session: tuple[str, str],
) -> None:
    """Fork from a response onto a custom agent binds the fork to it.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for the runner-bound
        ``hello_world`` (openai-agents SDK) source session.
    :param custom_agent_session: ``(base_url, session_id)`` for a session on
        the custom ``echo_probe`` agent — its existence stores the
        session-scoped agent row the fork targets.
    """
    base_url, source_id = seeded_session
    _, custom_session_id = custom_agent_session
    custom_agent = _session_agent(base_url, custom_session_id)
    assert custom_agent["name"] == _CUSTOM_AGENT_NAME

    page.goto(f"{base_url}/c/{source_id}")

    # One marked turn so the fork has content AND an assistant bubble to
    # anchor the "Fork from here" action.
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible()
    composer.fill(f"Reply with one short word. Marker: {_MARKER}")
    page.get_by_role("button", name="Send", exact=True).click()
    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    expect(assistant).to_be_visible(timeout=60_000)

    assistant.hover()
    page.get_by_test_id("fork-from-response").first.click()
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()

    # The picker offering the custom agent at all is a precondition of the
    # bug: the dialog discovers it from the user's own sessions, so a pick
    # the server then rejects is a dead-end the UI itself proposed.
    page.get_by_test_id("fork-session-agent-select").click()
    option = page.get_by_test_id(f"fork-session-agent-option-{custom_agent['id']}")
    expect(option).to_be_visible(timeout=10_000)
    option.click()

    with page.expect_response(
        lambda r: r.url.endswith(f"/v1/sessions/{source_id}/fork") and r.request.method == "POST"
    ) as fork_response:
        page.get_by_test_id("fork-session-submit").click()
    response = fork_response.value
    if not response.ok:
        # Fail on the user-visible rejection so the observed symptom (the
        # dialog error, not just the HTTP status) is in the failure message.
        error = page.get_by_test_id("fork-session-error")
        expect(error).to_be_visible(timeout=10_000)
        page.wait_for_timeout(1_500)
        pytest.fail(
            f"fork rejected the session-scoped custom agent: HTTP {response.status}: "
            f"{response.text()} — dialog shows {error.text_content()!r}"
        )

    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(source_id)})[0-9a-f]{{32}}"),
        timeout=30_000,
    )
    expect(dialog).not_to_be_visible()
    fork_id = page.url.rsplit("/c/", 1)[1].split("?", 1)[0]
    assert fork_id != source_id

    # The copied transcript carries the source's marked user turn.
    copied_user = page.locator('[data-testid="message-bubble"][data-role="user"]').filter(
        has_text=_MARKER
    )
    expect(copied_user.first).to_be_visible(timeout=30_000)

    fork_agent = _session_agent(base_url, fork_id)
    assert fork_agent["name"].startswith(_CUSTOM_AGENT_NAME), (
        f"fork should bind the custom agent {_CUSTOM_AGENT_NAME!r}, got {fork_agent['name']!r}"
    )
