r"""E2E regression: the Claude-only permission hook must not brand non-Claude sessions.

``POST /v1/sessions/{id}/hooks/permission-request`` is Claude Code's
PermissionRequest webhook: the server stamps Claude's identity into the
approval card it publishes (message ``Claude wants to call **<tool>**``,
policy ``claude_native_permission``). The endpoint authorizes on session
access only and never checks that the target session's harness is
actually Claude, so a stale or hand-edited hook config for any other
harness that still posts here renders a Claude-branded approval card on
a non-Claude session — the user is asked to approve a tool call
attributed to the wrong agent.

This test drives that stale-hook journey against the seeded
``hello_world`` session (openai-agents harness — not Claude) and asserts
the correct behavior: no ``claude_native_permission`` elicitation is
published on the session, the SPA never renders a Claude approval card
on it, and the POST is answered promptly instead of being parked on a
Claude-contract long-poll. On a server without the harness check the
card renders and the first expectation fails.
"""

from __future__ import annotations

import threading

import httpx
import pytest
from playwright.sync_api import Page, expect

_APPROVAL_CARD = '[data-testid="approval-card"]'
_CLAUDE_POLICY_STAMP = "claude_native_permission"

# How long the SPA gets to (incorrectly) render a parked Claude card
# before the test concludes none is coming. A server that accepts the
# POST parks and publishes the elicitation within ~a second, so this
# grace window is generous.
_CARD_GRACE_MS = 8_000


def _pending_claude_elicitations(base_url: str, session_id: str) -> list[dict]:
    """Return parked elicitations carrying the Claude permission stamp."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    pending = resp.json().get("pending_elicitations") or []
    return [
        event
        for event in pending
        if (event.get("params") or {}).get("policy_name") == _CLAUDE_POLICY_STAMP
    ]


@pytest.mark.timeout(120)
def test_claude_permission_hook_rejects_non_claude_session(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A non-Claude session must never surface a Claude-branded approval card."""
    base_url, session_id = seeded_session

    # Precondition: the seeded session's harness is not Claude. If the
    # fixture ever moves to a Claude harness this test stops guarding
    # anything, so fail loudly on the precondition instead.
    agent_resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/agent", timeout=10.0)
    agent_resp.raise_for_status()
    harness = agent_resp.json().get("harness") or ""
    assert "claude" not in harness.lower(), (
        f"seeded session is no longer non-Claude (harness={harness!r}); "
        "this regression test needs a non-Claude session"
    )

    # A stale hook config for this non-Claude harness posts Claude Code's
    # PermissionRequest payload to the Claude-only endpoint.
    result_holder: dict = {}

    def _post_stale_hook() -> None:
        try:
            resp = httpx.post(
                f"{base_url}/v1/sessions/{session_id}/hooks/permission-request",
                json={"tool_name": "Bash", "tool_input": {"command": "printf OLD_ROUTE"}},
                timeout=60.0,
            )
            result_holder["status"] = resp.status_code
            result_holder["body"] = resp.text
        except Exception as exc:
            result_holder["error"] = exc

    hook_thread = threading.Thread(target=_post_stale_hook, daemon=True)
    hook_thread.start()

    page.goto(f"{base_url}/c/{session_id}")
    # The chat surface is up. Locate the composer by aria-label — its
    # placeholder mutates (and the textarea disables) while an
    # elicitation is pending, which is exactly the buggy state.
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)

    # Give a faulty server ample time to park the elicitation and the
    # SPA time to render it, then require that no Claude card appeared.
    page.wait_for_timeout(_CARD_GRACE_MS)
    claude_card = page.locator(_APPROVAL_CARD).filter(has_text="Claude wants to call")
    expect(claude_card).to_have_count(0)

    # The server must not have parked a Claude-stamped elicitation on
    # this session either.
    assert not _pending_claude_elicitations(base_url, session_id), (
        "server published a claude_native_permission elicitation for a non-Claude session"
    )

    # And the POST must have been answered promptly — a server that
    # accepted it would still be parked on the Claude-contract
    # long-poll (24h timeout) waiting for a verdict.
    hook_thread.join(timeout=10)
    assert not hook_thread.is_alive(), (
        "hook POST is still parked server-side — the Claude-only endpoint "
        "accepted a non-Claude session instead of rejecting it"
    )
    if "error" in result_holder:
        raise AssertionError(
            f"hook POST failed at the transport level: {result_holder['error']}"
        ) from result_holder["error"]
    assert "hookSpecificOutput" not in (result_holder.get("body") or ""), (
        "the Claude-only endpoint completed the Claude decision contract "
        f"for a non-Claude session: HTTP {result_holder.get('status')} "
        f"{result_holder.get('body')!r}"
    )
