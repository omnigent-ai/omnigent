"""Visual-regression snapshot of a *populated* sidebar ("/").

The empty-landing baseline (``test_landing_snapshot.py``) stubs the session list
empty, so its sidebar only ever shows the top nav + "No active sessions" — the
row-alignment surface (section headers, flat session rows, project folders and
their nested chats) is never rendered, so a padding regression there sails
through the gate. This baseline fills that gap: a fixed session list that lays
out every sidebar row type on one capture — a Pinned row, a "Projects" group with
an expanded folder (a nested chat) and an empty folder, and a flat "Sessions"
list including the "needs response" and running state badges.

Same gate, renderer, and update flow as the other snapshots — see ``README.md``.

Determinism strategy — the sidebar is a pure function of the committed bundle
plus ``page.route`` stubs and two extra pins, mirroring the landing:

* The main session list (``GET /v1/sessions``) and the per-project list
  (``?project=``) return fixed fixtures; ``GET /v1/sessions/projects`` names the
  two folders. Every row carries ``comments_count: 0`` so no per-row comment
  fetch mounts.
* Row timestamps are relative ("2h ago") and would drift, so the clock is pinned
  with ``page.clock.set_fixed_time`` to a fixed "now" a known distance past each
  row's ``updated_at`` — the rendered pill text is then constant.
* The ``WS /v1/sessions/updates`` socket would stream per-row field patches
  (badge/timestamp churn), so it's routed to a no-op that accepts the connection
  and never sends a frame.
* Pinned membership and the expanded-folder set are localStorage, seeded before
  boot so the Pinned section and the open "Moonshot" folder render on first
  paint.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest
from playwright.sync_api import Page, expect

_HOST_ID = "host_e2e"

# Bare session list/scan — the flat sidebar list. Anchored so it matches
# `/v1/sessions` (+ query) but NOT `/v1/sessions/projects` or the per-session
# `/v1/sessions/{id}/...` sub-paths, so the routes never overlap.
_SESSIONS_RE = re.compile(r"/v1/sessions(\?(?!.*\bproject=)[^/]*)?$")
# Per-project list — `/v1/sessions?...&project=<name>` (folder contents).
_PROJECT_SESSIONS_RE = re.compile(r"/v1/sessions\?[^/]*\bproject=")
_PROJECTS_RE = re.compile(r"/v1/sessions/projects$")
_FILESYSTEM_RE = re.compile(r"/v1/hosts/[^/]+/filesystem")

_AGENTS_BODY = {
    "data": [
        {
            "id": "ag_claude_e2e",
            "name": "claude-native-ui",
            "display_name": "Claude Code",
            "description": "Anthropic's coding agent",
            "harness": None,
            "skills": [],
        }
    ]
}
_HOSTS_BODY = {
    "hosts": [{"host_id": _HOST_ID, "name": "e2e-host", "owner": "e2e", "status": "online"}]
}
_EMPTY_LIST_BODY = {"object": "list", "data": [], "has_more": False}

# A fixed "now" (2024-01-02 00:00:00 UTC, epoch seconds). Every row's
# `updated_at` sits a whole number of hours/days before this, so the relative
# "Xh"/"Xd" pill renders a constant string once the clock is pinned to it.
_NOW_S = 1_704_153_600
_HOUR = 3_600
_DAY = 86_400

_PINNED_ID = "conv_pinned"
_PROJECT_OPEN = "Moonshot"
_PROJECT_EMPTY = "Fixit"


def _row(
    conv_id: str,
    title: str,
    *,
    updated_at: int,
    labels: dict[str, str] | None = None,
    status: str = "idle",
    pending: int = 0,
) -> dict:
    """One session-list row. ``permission_level`` is left null (owner) so the
    row lands on the "My sessions" slice on the local (loopback) test server."""
    return {
        "id": conv_id,
        "object": "conversation",
        "title": title,
        "created_at": updated_at,
        "updated_at": updated_at,
        "labels": labels or {},
        "permission_level": None,
        "status": status,
        "pending_elicitations_count": pending,
        "comments_count": 0,
        "archived": False,
        "git_branch": None,
    }


# The flat list carries every non-project row: the pinned one (peeled into
# Pinned via localStorage), plus the unfiled "Sessions" rows — one plain, one
# awaiting approval (pink "needs response" badge), one running (spinner).
_SESSIONS_BODY = {
    "object": "list",
    "data": [
        _row(_PINNED_ID, "Prototype the agent orchestration layer", updated_at=_NOW_S - _HOUR),
        _row(
            "conv_flat_1",
            "Draft the enterprise onboarding checklist",
            updated_at=_NOW_S - 2 * _HOUR,
        ),
        _row(
            "conv_flat_2",
            "Reconcile failed payment retries from March",
            updated_at=_NOW_S - 3 * _HOUR,
            pending=2,
        ),
        _row(
            "conv_flat_3",
            "Rethink the welcome experience for new signups",
            updated_at=_NOW_S - 5 * _HOUR,
            status="running",
        ),
        _row(
            "conv_flat_4", "Organize customer feedback from last quarter", updated_at=_NOW_S - _DAY
        ),
    ],
    "first_id": _PINNED_ID,
    "last_id": "conv_flat_4",
    "has_more": False,
}

# The open "Moonshot" folder's contents (its own `?project=` fetch). One nested
# chat is enough to show the row indented under the folder name.
_PROJECT_SESSIONS_BODY = {
    "object": "list",
    "data": [
        _row(
            "conv_proj_1",
            "Spike on multi-model routing for the planner",
            updated_at=_NOW_S - 4 * _HOUR,
            labels={"omni_project": _PROJECT_OPEN},
        ),
    ],
    "first_id": "conv_proj_1",
    "last_id": "conv_proj_1",
    "has_more": False,
}

# Two project folders: one expanded (seeded below), one left collapsed/empty.
_PROJECTS_BODY = [_PROJECT_OPEN, _PROJECT_EMPTY]


@pytest.mark.visual
def test_populated_sidebar_matches_baseline(
    snapshot_page: Page,
    live_server: str,
    fulfill_json,
    settle_for_snapshot,
    assert_snapshot,
) -> None:
    """A populated sidebar renders pixel-identical to the committed baseline.

    Covers the row-alignment surface the empty-landing baseline can't: section
    headers (Pinned / Projects / Sessions), a flat session row, a project folder
    with a nested chat, an empty folder, and the state badges.

    :param snapshot_page: page pinned to a fixed viewport + light palette (see
        the suite ``conftest.py``).
    :param live_server: Base URL of the spawned ``omnigent server`` serving the
        built SPA. Every data call the sidebar makes is stubbed below.
    :param fulfill_json: 200-JSON route helper (suite ``conftest.py``).
    :param settle_for_snapshot: fonts + caret settle, run before capture.
    :param assert_snapshot: ``pytest-playwright-visual-snapshot`` fixture; writes
        the baseline under ``--update-snapshots`` and otherwise compares against
        it, failing (and emitting actual/expected/diff PNGs) on any mismatch.
    """
    page = snapshot_page

    # Pin the clock to a fixed "now" so the rows' relative-time pills ("2h",
    # "1d") render a constant string instead of drifting each second.
    page.clock.set_fixed_time(datetime.fromtimestamp(_NOW_S, tz=timezone.utc))

    # Silence the session-updates socket: accept the upgrade and never push a
    # frame, so no live patch mutates a row's badge or timestamp mid-capture.
    page.route_web_socket(
        re.compile(r"/v1/sessions/updates"),
        lambda ws: None,
    )

    # Stub the landing's data endpoints so the view is fully deterministic.
    page.route("**/v1/agents", lambda r: fulfill_json(r, _AGENTS_BODY))
    page.route("**/v1/hosts", lambda r: fulfill_json(r, _HOSTS_BODY))
    page.route(_FILESYSTEM_RE, lambda r: fulfill_json(r, _EMPTY_LIST_BODY))
    # Order matters: register the narrower project routes before the bare list.
    page.route(_PROJECTS_RE, lambda r: fulfill_json(r, _PROJECTS_BODY))
    page.route(_PROJECT_SESSIONS_RE, lambda r: fulfill_json(r, _PROJECT_SESSIONS_BODY))
    page.route(_SESSIONS_RE, lambda r: fulfill_json(r, _SESSIONS_BODY))

    # Seed the working-directory chip (fixed value) and the two sidebar prefs —
    # the pinned session and the expanded "Moonshot" folder — before the SPA
    # boots so both render on first paint.
    page.add_init_script(
        f'window.localStorage.setItem("omnigent:recent-workspaces",'
        f' JSON.stringify({{"{_HOST_ID}": ["/work/repo"]}}));'
        f'window.localStorage.setItem("omnigent:pinned-conversation-ids",'
        f" {json.dumps(json.dumps([_PINNED_ID]))});"
        f'window.localStorage.setItem("omnigent:expanded-project-sections",'
        f" {json.dumps(json.dumps([_PROJECT_OPEN]))});"
    )

    page.goto(f"{live_server}/")

    landing = page.get_by_test_id("new-chat-landing")
    # Generous timeout: the SPA runs a short boot probe before the landing paints.
    expect(landing).to_be_visible(timeout=30_000)
    # Wait for the sidebar's populated regions to settle: the pinned row, both
    # project folders, and the nested project chat (the last row to arrive, via
    # its own `?project=` fetch). Match row text — "Sessions" as a section-header
    # name collides with the "Select sessions" button, so key off content.
    expect(page.get_by_text("Prototype the agent orchestration")).to_be_visible(timeout=30_000)
    expect(page.get_by_role("button", name=_PROJECT_OPEN, exact=True)).to_be_visible(
        timeout=30_000
    )
    expect(page.get_by_role("button", name=_PROJECT_EMPTY, exact=True)).to_be_visible(
        timeout=30_000
    )
    expect(page.get_by_text("Spike on multi-model routing")).to_be_visible(timeout=30_000)
    expect(page.get_by_text("Organize customer feedback")).to_be_visible(timeout=30_000)

    # Settle web fonts + kill the blinking caret (both time-dependent).
    settle_for_snapshot(page)

    # Full viewport: the populated sidebar + the hero + the composer.
    assert_snapshot(page)
