"""UI journey: the Agents rail floats active sub-agents above settled ones (#1410).

Critical user journey
---------------------
A supervisor fans work out to sub-agents and watches the right-rail Agents tab.
A sub-agent that's actively working must sort *above* a settled/idle sibling, so
the live one is never buried. This pins that, end to end, with no model call.

Why this is deliberately minimal
--------------------------------
Seeding child *state* over REST is constrained:

* A child is created with JSON ``POST /v1/sessions`` + ``parent_session_id``
  (see ``agents/test_add_subagent_dialog`` / ``mobile/test_mobile_workflow``).
* Its status is driven with ``external_session_status`` events — but for a
  ``sub_agent`` the server relays *terminal* statuses (``idle``/``failed``) to
  the child's runner inbox, which a REST-seeded child doesn't have, so those
  return 503. Only the non-terminal ``running`` is settable this way.
* A freshly-created child with no status already reads as ``idle``
  (``busy=false``, ``current_task_status=null`` on the snapshot builder).

So this test uses the two states it *can* deterministically produce —
``working`` (one ``running`` POST) and ``idle`` (an untouched child) — and
asserts the working row sorts above the idle one. The full ranking
(``awaiting``/``launching``/``done``/``failed`` and the ``created_at`` tiebreak)
is covered deterministically by the unit test
``web/src/shell/SubagentsPanel.test.tsx``; the snapshot read path can't render
``done``/``launching`` anyway (it only emits ``failed``/``null``).
"""

from __future__ import annotations

import re
import time

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

_SUBAGENT_ROW = '[data-testid="subagent-row"]'


def _agent_id(base_url: str, session_id: str) -> str:
    """Return the agent id the parent session is bound to."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json()["agent_id"]


def _create_child(base_url: str, parent_id: str, agent_id: str, name: str) -> str:
    """Create a sub-agent child session under *parent_id* (no LLM turn).

    Mirrors the JSON ``POST /v1/sessions`` contract used by
    ``mobile/test_mobile_workflow``. Retries briefly on 503 — a freshly-bound
    runner can be momentarily unavailable.

    :returns: The new child session id.
    """
    last_exc: Exception | None = None
    for _ in range(10):
        resp = httpx.post(
            f"{base_url}/v1/sessions",
            json={
                "agent_id": agent_id,
                "parent_session_id": parent_id,
                "sub_agent_name": name,
            },
            timeout=30.0,
        )
        if resp.status_code == 503:
            last_exc = httpx.HTTPStatusError(
                "runner unavailable", request=resp.request, response=resp
            )
            time.sleep(0.5)
            continue
        resp.raise_for_status()
        body = resp.json()
        return str(body.get("id") or body["session_id"])
    raise AssertionError(f"child create kept returning 503 for {name!r}") from last_exc


def _set_running(base_url: str, session_id: str) -> None:
    """Mark a child busy via the route native harnesses use (``running`` only).

    Terminal statuses (``idle``/``failed``) 503 on a REST-seeded child, so this
    helper is intentionally limited to the non-terminal ``running`` edge.
    """
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": {"status": "running"}},
        timeout=10.0,
    )
    resp.raise_for_status()


def _child_summaries(base_url: str, parent_id: str) -> dict[str, dict]:
    """Return the parent's child-session summaries keyed by child id."""
    resp = httpx.get(f"{base_url}/v1/sessions/{parent_id}/child_sessions", timeout=10.0)
    resp.raise_for_status()
    body = resp.json()
    data = body.get("data", body) if isinstance(body, dict) else body
    return {str(c["id"]): c for c in data}


def _wait_for(predicate, *, timeout_s: float = 30.0, interval_s: float = 0.5) -> None:
    """Poll *predicate* until truthy or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError("server did not reflect the seeded child states in time")


@pytest.mark.timeout(120)
def test_rail_sorts_working_above_idle(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A working sub-agent sorts above an idle one, inverting creation order."""
    base_url, parent_id = seeded_session
    agent_id = _agent_id(base_url, parent_id)

    # Create the working one FIRST (older) and the idle one SECOND (newer). The
    # rail's default order is newest-first, which would put idle on top — so a
    # working-above-idle result can only come from the activity sort.
    c_work = _create_child(base_url, parent_id, agent_id, "working-sub")
    c_idle = _create_child(base_url, parent_id, agent_id, "idle-sub")

    # working = one running POST; idle = leave the child untouched.
    _set_running(base_url, c_work)

    # Gate the UI assertion on the server reflecting both states.
    def _ready() -> bool:
        by = _child_summaries(base_url, parent_id)
        return (
            len(by) >= 2
            and by.get(c_work, {}).get("busy") is True
            and by.get(c_idle, {}).get("busy") is False
            and by.get(c_idle, {}).get("current_task_status") is None
        )

    _wait_for(_ready)

    # Open the Agents tab; scope to the desktop "Workspace" rail so lookups don't
    # match the hidden mobile drawer that mirrors the same testids.
    page.goto(f"{base_url}/c/{parent_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    rows = rail.locator(_SUBAGENT_ROW)
    expect(rows).to_have_count(2, timeout=30_000)

    # working floats above idle, inverting the default newest-first order.
    # Per-position expect() auto-retries, riding out the rail's poll/stream refresh.
    expect(rows.nth(0)).to_have_attribute("data-child-session-id", c_work, timeout=30_000)
    expect(rows.nth(1)).to_have_attribute("data-child-session-id", c_idle)
