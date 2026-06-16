"""UI journey: the header "Agent tools & policies" popover (AgentInfo).

The chat header carries an info button (``components/AgentInfo.tsx`` →
``AgentInfoButton``) that opens a popover summarizing the bound agent: its
tools / MCP servers, session cost, and the **session policies** the user can
add and remove on the fly. The policy surface is the interactive part —
``GET /v1/policy-registry`` lists attachable handlers, ``POST`` /
``DELETE /v1/sessions/<id>/policies`` mutate the session — so this suite drives
the full add→delete loop and pins each step to the REST state behind it.

No LLM turn is involved (the popover is rail/REST state, not a function of any
model output), so this stays a fast, deterministic check. It is the companion
to the approval-card suite: that one proves a *spec-declared* policy gates a
tool call; this one proves a user can attach and detach a policy through the UI.

The load-bearing assertions are pinned to ``GET /v1/sessions/<id>/policies``:
the added handler appears with ``source == "session"`` after the dialog submits,
and is gone after the pill's Remove — proof the popover mutates real
server-side session policy, not just optimistic local state.
"""

from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import Page, expect

_COMPOSER = "Ask the agent anything…"
_AGENT_INFO_TRIGGER = '[data-testid="agent-info-trigger"]'


def _callable_registry_policy(base_url: str) -> dict:
    """Return a no-parameter (``callable``) policy from ``GET /v1/policy-registry``.

    A ``callable`` handler takes no factory params, so the Add-Policy dialog
    needs only a selection + submit — keeping the UI flow deterministic. Skips
    the test if the server exposes no such handler (a registry-shape change),
    rather than guessing at factory params.

    :param base_url: Spawned server base URL.
    :returns: The chosen registry entry dict (``name``, ``handler``, ``kind``).
    """
    resp = httpx.get(f"{base_url}/v1/policy-registry", timeout=10.0)
    resp.raise_for_status()
    for entry in resp.json()["data"]:
        if entry.get("kind") == "callable":
            return entry
    pytest.skip("no parameter-free (callable) policy in the registry to exercise the dialog")


def _session_policies(base_url: str, session_id: str) -> list[dict]:
    """Return the session's policy rows (owner view) from the CRUD API."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/policies", timeout=10.0)
    resp.raise_for_status()
    return resp.json()["data"]


def _user_policy_names(base_url: str, session_id: str) -> set[str]:
    """Names of user-attached (``source == "session"``) policies on the session."""
    return {p["name"] for p in _session_policies(base_url, session_id) if p["source"] == "session"}


def _open_popover(page: Page) -> None:
    """Open the agent-info popover from a known-closed state, idempotently.

    Adding a policy opens a modal dialog and removing one opens a nested
    popover; both dismiss the outer popover on the interaction-outside that
    follows. Pressing Escape first guarantees we re-open from a closed state
    rather than toggling an already-open popover shut.

    :param page: Playwright page on a ``/c/<id>`` route.
    """
    page.keyboard.press("Escape")
    trigger = page.locator(_AGENT_INFO_TRIGGER)
    expect(trigger).to_be_visible(timeout=30_000)
    trigger.click()
    # "Policies" section label proves the popover content mounted.
    expect(page.get_by_text("Policies", exact=True)).to_be_visible(timeout=15_000)


def test_agent_info_policy_add_and_remove(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Popover → add a policy → pill + REST reflect it → Remove → both clear."""
    base_url, session_id = seeded_session
    entry = _callable_registry_policy(base_url)
    registry_name = entry["name"]
    # The dialog stores the policy under a slugified name (see AgentInfo's
    # AddPolicyDialog.handleAdd): lowercased, whitespace runs → underscores.
    stored_name = re.sub(r"\s+", "_", registry_name.lower())

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)
    assert not _user_policy_names(base_url, session_id), "session started with policies already"

    # Open the popover: the Policies section starts empty.
    _open_popover(page)
    expect(page.get_by_text("No policies added")).to_be_visible()

    # Add the registry policy through the dialog.
    page.get_by_role("button", name="Add policy").click()
    dialog = page.get_by_role("dialog").filter(has=page.get_by_text("Add Policy"))
    expect(dialog).to_be_visible(timeout=15_000)
    dialog.get_by_role("button").filter(has_text=registry_name).first.click()
    dialog.get_by_role("button", name="Add", exact=True).click()
    expect(dialog).to_be_hidden(timeout=15_000)

    # The server recorded the attach as a session-source policy.
    _wait_for(lambda: _user_policy_names(base_url, session_id) == {stored_name})

    # Re-open the popover: the policy now shows as a pill.
    _open_popover(page)
    pill = page.get_by_role("button", name=stored_name, exact=True)
    expect(pill).to_be_visible(timeout=15_000)

    # Open the pill's popover and remove the policy.
    pill.click()
    page.get_by_role("button", name="Remove").click()
    _wait_for(lambda: not _user_policy_names(base_url, session_id))

    # Re-open the popover: the section is empty again.
    _open_popover(page)
    expect(page.get_by_text("No policies added")).to_be_visible(timeout=15_000)


def _wait_for(predicate, *, timeout_s: float = 15.0, interval_s: float = 0.25) -> None:
    """Poll *predicate* until truthy or the deadline passes."""
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError("condition not met within timeout")
