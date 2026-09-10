"""E2E: a ``sys_session_create`` child's verbatim colon title survives listings.

``sys_session_create`` stores the caller's title verbatim on the child
conversation, but the listing paths treat any ``:`` in a child's title as
the framework's ``"<agent>:<title>"`` spawn convention and split on the
first colon. A legitimate verbatim title like ``"research:pricing"`` is
therefore misreported as a bogus agent handle (``"research"``) with a
truncated title (``"pricing"``):

* ``_child_session_summary_from_conversation`` (server) surfaces
  ``tool="research"`` / ``session_name="pricing"`` on
  ``GET /v1/sessions/{parent}/child_sessions``;
* the web Agents rail renders that truncated name as the child row's
  label, so the user sees ``pricing`` instead of ``research:pricing``;
* the runner's ``sys_session_list`` (``_child_rows_to_entries``) relays
  the same bogus agent/title pair to the orchestrating LLM.

The child here is created exactly the way the runner's
``sys_session_create`` does — ``POST /v1/sessions`` with ``agent_id`` +
``parent_session_id`` + verbatim ``title`` (the body built by
``_build_session_create_body`` in ``omnigent/runner/tool_dispatch.py``) —
so both tests exercise the same stored conversation row the tool
produces, with no LLM turn required.
"""

from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

_VERBATIM_TITLE = "research:pricing"
_SUBAGENT_ROW = '[data-testid="subagent-row"]'


def _create_colon_titled_child(base_url: str, parent_id: str) -> str:
    """Create a child session the way ``sys_session_create`` does.

    Binds the child to the parent's own agent row (fetched via
    ``GET /v1/sessions/{id}/agent``) and passes the caller's verbatim
    colon-bearing title — the same ``agent_id`` + ``parent_session_id``
    + ``title`` JSON body the runner's ``sys_session_create`` posts.

    :param base_url: Spawned server base URL, e.g. ``"http://127.0.0.1:51234"``.
    :param parent_id: The parent session id the child hangs under.
    :returns: The new child session id.
    """
    agent_resp = httpx.get(
        f"{base_url}/v1/sessions/{parent_id}/agent",
        timeout=10.0,
    )
    agent_resp.raise_for_status()
    agent_id = agent_resp.json()["id"]

    child_resp = httpx.post(
        f"{base_url}/v1/sessions",
        json={
            "agent_id": agent_id,
            "parent_session_id": parent_id,
            "title": _VERBATIM_TITLE,
        },
        timeout=10.0,
    )
    child_resp.raise_for_status()
    return str(child_resp.json()["id"])


@pytest.mark.timeout(300)
def test_child_summary_keeps_verbatim_colon_title(
    seeded_session: tuple[str, str],
) -> None:
    """The child-summary route must not split a verbatim title on ``:``.

    ``GET /v1/sessions/{parent}/child_sessions`` is the row the web
    Agents rail and the runner's ``sys_session_list`` both consume, so
    this pins the contract at its source: a verbatim colon-bearing title
    must not be reported as agent ``"research"`` / name ``"pricing"``.

    :param seeded_session: ``(base_url, parent_session_id)`` bound to the
        spawned server's runner.
    :returns: None.
    """
    base_url, parent_id = seeded_session
    child_id = _create_colon_titled_child(base_url, parent_id)

    resp = httpx.get(
        f"{base_url}/v1/sessions/{parent_id}/child_sessions",
        timeout=10.0,
    )
    resp.raise_for_status()
    rows = {row["id"]: row for row in resp.json()["data"]}
    assert child_id in rows, f"child {child_id} missing from child_sessions: {sorted(rows)}"
    row = rows[child_id]

    # Sanity (holds before and after the fix): the stored title itself is
    # the caller's verbatim string.
    assert row["title"] == _VERBATIM_TITLE, f"stored title mutated: {row['title']!r}"

    # The bug: the first-colon split reports the title's head as a bogus
    # agent handle and its tail as the child's name.
    assert row["tool"] != "research", (
        "child summary reports the bogus agent handle split from the "
        f"verbatim title: tool={row['tool']!r} (title={row['title']!r})"
    )
    assert row["session_name"] != "pricing", (
        "child summary truncates the verbatim title to its post-colon "
        f"tail: session_name={row['session_name']!r} (title={row['title']!r})"
    )


@pytest.mark.timeout(300)
def test_agents_rail_shows_full_verbatim_colon_title(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The Agents rail row shows the full verbatim title, not the tail.

    User journey: create a child via the ``sys_session_create`` shape with
    ``title="research:pricing"``, open the parent session, open the
    right-rail Agents tab. The child's row label must carry the full
    verbatim title — before the fix it shows only ``"pricing"``, the tail
    left behind by the first-colon split.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, parent_session_id)`` bound to the
        spawned server's runner.
    :returns: None.
    """
    base_url, parent_id = seeded_session
    child_id = _create_colon_titled_child(base_url, parent_id)

    page.goto(f"{base_url}/c/{parent_id}")

    # Scope every lookup to the desktop "Workspace" rail so it never
    # matches the hidden mobile drawer that mirrors the same testids.
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")

    agents_tab = rail.get_by_role("tab", name=re.compile("^Agents"))
    expect(agents_tab).to_be_visible(timeout=30_000)
    agents_tab.click()

    row = rail.locator(f'{_SUBAGENT_ROW}[data-child-session-id="{child_id}"]')
    expect(row).to_be_visible(timeout=30_000)
    # The row label must carry the FULL verbatim title; before the fix the
    # first-colon split leaves only the truncated tail "pricing".
    expect(row).to_contain_text(_VERBATIM_TITLE)
