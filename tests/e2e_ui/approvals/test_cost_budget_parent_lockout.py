"""E2E (web): a session cost budget hard-blocks the user in the chat, with
no in-session way forward — the over-budget lockout catch-22.

Reported journey (Codex-native, macOS): the user set a ``$100`` cost budget on
a parent session and dispatched a Codex sub-agent that looped through many LLM
calls, running tree-wide spend to ``$379.11``. Once the budget was blown, every
model call and tool call the session made was DENYed by the same policy — so
when the user tried to interact with the parent to stop the runaway child, that
interaction was blocked too. The user was locked out of their own session; the
only escape was clicking "Stop session", which kills the whole tree.

This drives the real UI end-to-end against a live server: attach the exact
``cost_budget`` builtin the user set (a hard ``$100`` cap), seed cumulative
spend to the reported ``$379.11``, open the session in the web app, then fire
the next gated call (the same ``POST /policies/evaluate`` a native ``PreToolUse``
hook posts). The server hard-DENYs it over budget and publishes the decision on
the session stream, which the SPA renders as a "Blocked by policy" banner
carrying the over-budget reason — the user-visible face of the lockout. No LLM
in the loop (the gate is fired directly, like the other synthetic policy-hook
UI tests), so it runs on every PR.

The browser is driven manually (``browser.new_context(record_video_dir=…)``)
rather than through the ``page`` fixture so the journey is filmed as a ``.webm``
when ``OMNIGENT_E2E_RECORD_DIR`` is set.
"""

from __future__ import annotations

import os
import threading

import httpx
import pytest
from playwright.sync_api import Browser, BrowserContext, Page, expect

_BANNER_TIMEOUT_MS = 20_000

# The exact hard $100 session cost budget the user set on the parent:
# ``cost_budget`` with only ``max_cost_usd`` (no ``ask_thresholds_usd``, no
# ``expensive_models``) is a block-all hard stop once spend crosses the cap.
_COST_BUDGET_PAYLOAD = {
    "name": "session_cost_budget",
    "type": "python",
    "handler": "omnigent.policies.builtins.cost.cost_budget",
    "factory_params": {"max_cost_usd": 100.0},
    "enabled": True,
}

# Reported runaway: the sub-agent looped to $379.11 against a $100 cap.
_OVER_BUDGET_SPEND_USD = 379.11


def _seed_session_usage(session_id: str, usage: dict) -> None:
    """Write cumulative usage straight into the spawned server's store.

    :param session_id: Session to seed, e.g. ``"conv_abc123"``.
    :param usage: Usage dict, e.g. ``{"total_cost_usd": 379.11}``.
    :raises RuntimeError: When running against ``--ui-base-url`` (no local
        database to seed).
    """
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from tests.e2e_ui.conftest import _server_state

    database_uri = _server_state.get("database_uri")
    if not database_uri:
        raise RuntimeError(
            "seeding needs the spawned server's database; it is "
            "unavailable when running against --ui-base-url."
        )
    SqlAlchemyConversationStore(str(database_uri)).set_session_usage(session_id, usage)


def _new_page(browser: Browser) -> tuple[BrowserContext, Page]:
    """Open a fresh context that films to ``OMNIGENT_E2E_RECORD_DIR`` when set.

    :param browser: The pytest-playwright ``browser`` fixture.
    :returns: ``(context, page)``; close the context to flush the video.
    """
    context = browser.new_context(
        record_video_dir=os.environ.get("OMNIGENT_E2E_RECORD_DIR") or None,
    )
    return context, context.new_page()


def _beat(page: Page) -> None:
    """Pause briefly between journey steps — only while filming a clip.

    :param page: The active page.
    """
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(900)


@pytest.mark.timeout(120)
def test_over_budget_locks_user_out_of_session(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """Over-budget → the chat hard-blocks every call → the user is locked out.

    The session accrued $379.11 of spend against a $100 hard cost budget. The
    next gated call is DENYed and the SPA renders a "Blocked by policy" banner
    naming the over-budget reason — the user has no in-session way to proceed,
    which is the reported lockout. (After the fix, the runaway sub-agent should
    have been interrupted proactively and the user left able to cancel it, so a
    normal call would not silently keep hitting this wall.)
    """
    base_url, session_id = seeded_session

    # The user worked normally; the sub-agent looped tree-wide spend to
    # $379.11 — far past the (later-attached) $100 hard cap.
    _seed_session_usage(session_id, {"total_cost_usd": _OVER_BUDGET_SPEND_USD})

    # The $100 hard cost budget the user set on the parent.
    attach = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/policies",
        json=_COST_BUDGET_PAYLOAD,
        timeout=10.0,
    )
    assert attach.status_code < 400, f"policy attach failed: {attach.status_code} {attach.text}"

    context, page = _new_page(browser)
    result_holder: dict = {}
    try:
        page.goto(f"{base_url}/c/{session_id}")
        expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)
        _beat(page)

        # Fire the next gated call (what a native PreToolUse hook posts). The
        # server hard-DENYs it over budget and publishes response.policy_denied,
        # which the SPA renders as a "Blocked by policy" banner.
        def _evaluate() -> None:
            try:
                resp = httpx.post(
                    f"{base_url}/v1/sessions/{session_id}/policies/evaluate",
                    json={
                        "event": {
                            "type": "PHASE_TOOL_CALL",
                            "target": "",
                            "data": {"name": "Bash", "arguments": {"command": "ls"}},
                            "context": {},
                        },
                    },
                    timeout=60.0,
                )
                resp.raise_for_status()
                result_holder["response"] = resp.json()
            except Exception as exc:  # surfaced after the assertions below
                result_holder["error"] = exc

        gate_thread = threading.Thread(target=_evaluate, daemon=True)
        gate_thread.start()

        # The user-visible lockout: a "Blocked by policy" banner naming the
        # cost budget and the over-budget spend. No approve option, no way
        # forward.
        banner = page.get_by_text("Blocked by policy", exact=False).first
        expect(banner).to_be_visible(timeout=_BANNER_TIMEOUT_MS)
        expect(page.get_by_text("$100.00", exact=False).first).to_be_visible(
            timeout=_BANNER_TIMEOUT_MS
        )
        expect(page.get_by_text("$379.11", exact=False).first).to_be_visible(
            timeout=_BANNER_TIMEOUT_MS
        )
        _beat(page)

        # The gate settled as a hard DENY (the user's call was blocked outright).
        gate_thread.join(timeout=60)
        assert not gate_thread.is_alive(), "policy gate never settled"
        if "error" in result_holder:
            raise AssertionError(f"gate thread failed: {result_holder['error']}")
        assert result_holder["response"]["result"] == "POLICY_ACTION_DENY", result_holder[
            "response"
        ]
        assert f"${_OVER_BUDGET_SPEND_USD:.2f}" in (
            result_holder["response"].get("reason") or ""
        ), result_holder["response"]
    finally:
        # Close the context even if the drive failed, so a failed take still
        # yields footage.
        context.close()
