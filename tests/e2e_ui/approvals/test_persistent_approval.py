r"""E2E: the persistent "don't ask again" approval button approves a scope once (mock).

A background thread POSTs directly to the server's
``POST /v1/sessions/{session_id}/hooks/permission-request`` endpoint with a
``WebFetch`` payload. The server parks the elicitation, stamps a
``remember_scope`` with host ``github.com``, and surfaces the persistent-
approval card. The test clicks the "Approve & don't ask again for github.com"
button and asserts the parked prompt drains.

This replaces the original ``native_claude_session`` approach (real Claude Code
+ real LLM to call WebFetch) with a ``seeded_session`` + synthetic hook POST —
no native CLI required, completes in seconds.

This is the persistent-allow-rule counterpart to ``test_ask_user_question.py``
(Claude's question tool) and ``test_exit_plan_mode.py`` (the plan card): all
three cover a claude-native ``PermissionRequest`` that surfaces a richer card
than the binary policy ASK.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx
import pytest
from playwright.sync_api import Page, expect

_log = logging.getLogger(__name__)

_APPROVAL_CARD = '[data-testid="approval-card"]'
_REMEMBER_BUTTON = '[data-testid="approval-card-remember"]'

_MOCK_ELICITATION_TIMEOUT_MS = 15_000

# WebFetch on a stable, well-known repo URL: the host the server scopes the
# domain rule to is ``github.com``.
_WEBFETCH_HOST = "github.com"


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    """Return the session snapshot's pending elicitation events (owner view)."""
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
def test_persistent_approval_remembers_webfetch_domain(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Mock WebFetch permission-request → domain remember button → click → drain."""
    base_url, session_id = seeded_session
    _log.info("seeded session ready: base_url=%s session_id=%s", base_url, session_id)

    result_holder: dict = {}

    def _post_hook() -> None:
        try:
            resp = httpx.post(
                f"{base_url}/v1/sessions/{session_id}/hooks/permission-request",
                json={
                    "tool_name": "WebFetch",
                    "tool_input": {
                        "url": "https://github.com/cli/cli",
                        "prompt": "summarize this page",
                    },
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            result_holder["response"] = resp.json()
        except Exception as exc:
            result_holder["error"] = exc

    hook_thread = threading.Thread(target=_post_hook, daemon=True)
    hook_thread.start()

    # Let the server park the elicitation before the SPA tries to render it.
    page.wait_for_timeout(500)

    page.goto(f"{base_url}/c/{session_id}")

    card = (
        page.locator(f'{_APPROVAL_CARD}[data-state="pending"]')
        .filter(has=page.locator(_REMEMBER_BUTTON))
        .first
    )
    expect(card).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
    # The server is genuinely parked on this prompt, not an optimistic UI.
    assert _pending_elicitations(base_url, session_id), "server has no parked elicitation"

    remember = card.locator(_REMEMBER_BUTTON)
    # Visible label: "Approve & don't ask again for github.com" (domain-scoped).
    expect(remember).to_contain_text(f"don't ask again for {_WEBFETCH_HOST}")
    # The tooltip spells out the (session-scoped) domain grant explicitly.
    assert (
        remember.get_attribute("title")
        == f"Won't ask again for {_WEBFETCH_HOST} for the rest of this session"
    )

    remember.click()

    # The card flips to its responded state with the persistent-approval label.
    responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').filter(
        has_text=f"won't ask again for {_WEBFETCH_HOST}"
    )
    expect(responded.first).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)

    hook_thread.join(timeout=30)
    if "error" in result_holder:
        raise AssertionError(f"hook thread failed: {result_holder['error']}") from result_holder[
            "error"
        ]

    # The parked prompt drains — the remember verdict reached the blocked call.
    _wait_for(lambda: not _pending_elicitations(base_url, session_id))
