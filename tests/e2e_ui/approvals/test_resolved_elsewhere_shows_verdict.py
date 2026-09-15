r"""E2E regression — a Claude Code permission approval resolved
away from the watched web tab must show its verdict, not an ambiguous
"Resolved elsewhere" pill.

Reconstructed user journey (from the report):

1. Start a Claude Code session.
2. Claude invokes a tool that needs approval → the web UI parks an
   ``ApprovalCard`` (a ``PermissionRequest`` hook, surfaced here with the same
   synthetic-hook fast pattern as ``test_native_permission_card_names_the_harness``
   / ``test_persistent_approval`` — no native CLI required, seconds to run).
3. The user answers the prompt from *another surface* — the native Claude Code
   terminal popup, another tab, or the inbox. Modelled here by resolving the
   parked elicitation server-side with an ``accept`` verdict via
   ``POST /v1/sessions/{id}/elicitations/{eid}/resolve`` — the exact path the
   native-terminal fast-path and the inbox/other-tab verdict take.
4. In the watched web tab the card flips to its responded state.

Symptom on the buggy build (the report's "What happens"): the card shows the
neutral **"Resolved elsewhere · Claude Code"** pill and gives no indication the
tool was *approved* — the session looks silently, ambiguously idle, and the
user cannot tell whether the agent completed, died, or is waiting.

Root-cause lead (for the fix step, not asserted here): the server *does* publish
``response.elicitation_resolved`` with ``action: "accept"``
(``omnigent/server/routes/_sessions/helpers.py:_publish_elicitation_resolved``),
but the SPA drops the action — the SSE parser
(``web/src/lib/sse.ts``, ``response.elicitation_resolved`` branch) keeps only
``elicitationId``, and the chat-store handler
(``web/src/store/chatStore.ts``, ``elicitation_resolved`` case) hardcodes
``response: { action: "auto_resolved" }``. So an approve/decline answered
elsewhere is rendered identically to an unknown auto-resolution.

This test asserts the resolved card reflects the ``accept`` verdict (a clear
"Approved" state) rather than the ambiguous "Resolved elsewhere" pill, so it
FAILS on the buggy build and passes once the verdict is carried through.
"""

from __future__ import annotations

import threading
import time

import httpx
import pytest
from playwright.sync_api import Page, expect

_APPROVAL_CARD = '[data-testid="approval-card"]'
_MOCK_ELICITATION_TIMEOUT_MS = 15_000


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    """Return the session snapshot's parked elicitation events (owner view)."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def _wait_for(predicate, *, timeout_s: float = 30.0, interval_s: float = 0.5) -> None:
    """Poll *predicate* until truthy or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError("condition not met within timeout")


@pytest.mark.timeout(90)
def test_elsewhere_resolved_approval_shows_verdict_not_ambiguous(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Approve-from-elsewhere → card must read "Approved", not "Resolved elsewhere"."""
    base_url, session_id = seeded_session

    # Step 2: Claude requests permission for a tool. Park the claude-native
    # PermissionRequest on a background thread; it long-polls until the verdict
    # arrives (from anywhere) and then returns Claude's allow/deny decision.
    hook_result: dict = {}

    def _post_permission_hook() -> None:
        try:
            resp = httpx.post(
                f"{base_url}/v1/sessions/{session_id}/hooks/permission-request",
                json={"tool_name": "Bash", "tool_input": {"command": "ls -la"}},
                timeout=60.0,
            )
            resp.raise_for_status()
            hook_result["response"] = resp.json()
        except Exception as exc:
            hook_result["error"] = exc

    hook_thread = threading.Thread(target=_post_permission_hook, daemon=True)
    hook_thread.start()

    # Let the server park the elicitation before the SPA renders it.
    page.wait_for_timeout(500)
    page.goto(f"{base_url}/c/{session_id}")

    # The pending Claude Code approval card is what the user is watching.
    pending = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(pending).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
    expect(pending).to_contain_text("Claude Code")

    # The server is genuinely parked on this prompt, not an optimistic UI.
    parked = _pending_elicitations(base_url, session_id)
    assert parked, "server has no parked elicitation"
    elicitation_id = parked[0].get("elicitation_id")
    assert isinstance(elicitation_id, str) and elicitation_id

    # Step 3: the user answers the prompt from ANOTHER surface (native terminal
    # popup / another tab / inbox). Resolve it server-side with an accept
    # verdict — WITHOUT touching the card in this tab — exactly what those
    # surfaces do. The server publishes response.elicitation_resolved carrying
    # action="accept".
    resolve = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
        json={"action": "accept"},
        timeout=15.0,
    )
    # The resolve endpoint acks with 202 Accepted (verdict fanned out async).
    assert resolve.status_code in (200, 202), (
        f"resolve failed: {resolve.status_code} {resolve.text}"
    )

    # The parked prompt drains and the hook returns Claude's allow decision.
    _wait_for(lambda: not _pending_elicitations(base_url, session_id))

    # Step 4: the watched card flips to its responded state.
    responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
    expect(responded).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)

    # The bug: the accept verdict is known to the server, yet the card shows the
    # ambiguous neutral "Resolved elsewhere" pill with no verdict — the user
    # cannot tell the tool was approved. A resolved-elsewhere approval must
    # surface its outcome ("Approved"), never the ambiguous "Resolved elsewhere".
    expect(responded).to_contain_text("Approved")
    expect(responded).not_to_contain_text("Resolved elsewhere")
