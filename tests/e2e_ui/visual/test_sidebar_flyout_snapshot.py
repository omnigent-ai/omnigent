"""Visual-regression snapshot of the sidebar's pinned-project hover flyout.

The populated-sidebar baseline (``test_sidebar_snapshot.py``) captures every
sidebar *row* type, but not the hover flyout that surfaces a pinned session's
originating project — the card is portalled and only mounts on hover, so a
restyle of it (surface, title clamp, folder + project-name line) sails through
that gate. This baseline fills the gap: hover a pinned, project-owned row and
capture the ``PinnedProjectFlyoutContent`` HoverCard.

Same gate, renderer, and update flow as the other snapshots — see ``README.md``.

Determinism strategy mirrors ``test_sidebar_snapshot.py``: the sidebar is a pure
function of the committed bundle plus ``page.route`` stubs, with the clock pinned
(relative-time pills), the updates socket silenced (no row churn), and the pin +
expanded-folder prefs seeded in localStorage before boot. The flyout opens on a
150 ms ``openDelay``; ``page.clock.set_fixed_time`` pins only ``Date.now`` (real
timers still fire), so a plain ``hover`` opens it without advancing a fake clock.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest
from playwright.sync_api import Page, expect

_HOST_ID = "host_e2e"

# Anchored so it matches `/v1/sessions` (+ query) but NOT `/v1/sessions/projects`,
# the per-session `/v1/sessions/{id}/...` sub-paths, or the `?project=` /
# `?pinned=` filtered variants — same split as the populated-sidebar baseline.
_SESSIONS_RE = re.compile(r"/v1/sessions(\?(?!.*\b(?:project|pinned)=)[^/]*)?$")
_PROJECT_SESSIONS_RE = re.compile(r"/v1/sessions\?[^/]*\bproject=")
# Pinned list — `?pinned=true` drives the server-authoritative Pinned section.
_PINNED_SESSIONS_RE = re.compile(r"/v1/sessions\?[^/]*\bpinned=")
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

# A fixed "now" (2024-01-02 00:00:00 UTC, epoch seconds); the pinned row's
# `updated_at` sits a whole hour before it so its relative pill is constant.
_NOW_S = 1_704_153_600
_HOUR = 3_600

_PINNED_ID = "conv_pinned"
_PROJECT = "Moonshot"
# Long enough to exercise the flyout's 3-line title clamp.
_PINNED_TITLE = "Prototype the multi-model agent orchestration and routing layer end to end"


def _row(
    conv_id: str, title: str, *, updated_at: int, labels: dict[str, str] | None = None
) -> dict:
    """One session-list row; ``permission_level`` null (owner) so it lands on the
    "My sessions" slice on the local (loopback) test server."""
    return {
        "id": conv_id,
        "object": "conversation",
        "title": title,
        "created_at": updated_at,
        "updated_at": updated_at,
        "labels": labels or {},
        "permission_level": None,
        "status": "idle",
        "pending_elicitations_count": 0,
        "comments_count": 0,
        "archived": False,
        "git_branch": None,
    }


# Canonical pin label the client reads (the server collapses each viewer's
# per-user key back to this bare key on the wire); value is the epoch-ms pin time.
_PINNED_LABEL_KEY = "omnigent.pinned"
_PINNED_AT_MS = str(_NOW_S * 1000)

# One pinned, project-owned row: it carries the pin label (so it peels into the
# always-expanded "Pinned" section and the `?pinned=true` query returns it) and
# the project label (so hovering it opens the project flyout). It also appears
# in its project's `?project=` list.
_PINNED_ROW = _row(
    _PINNED_ID,
    _PINNED_TITLE,
    updated_at=_NOW_S - _HOUR,
    labels={"omni_project": _PROJECT, _PINNED_LABEL_KEY: _PINNED_AT_MS},
)
_SESSIONS_BODY = {
    "object": "list",
    "data": [_PINNED_ROW],
    "first_id": _PINNED_ID,
    "last_id": _PINNED_ID,
    "has_more": False,
}
# Both the per-project `?project=` list and the `?pinned=true` list return the
# same single row.
_PROJECT_SESSIONS_BODY = {
    "object": "list",
    "data": [_PINNED_ROW],
    "first_id": _PINNED_ID,
    "last_id": _PINNED_ID,
    "has_more": False,
}
_PINNED_SESSIONS_BODY = _PROJECT_SESSIONS_BODY
# One label-only project folder holding the pinned session.
_PROJECTS_BODY = [{"id": None, "name": _PROJECT}]


@pytest.mark.visual
def test_pinned_project_flyout_matches_baseline(
    snapshot_page: Page,
    live_server: str,
    fulfill_json,
    settle_for_snapshot,
    assert_snapshot,
) -> None:
    """Hovering a pinned, project-owned row opens a flyout that renders
    pixel-identical to the committed baseline.

    Covers ``PinnedProjectFlyoutContent`` — the compact HoverCard (clamped title
    + folder icon + project name) that the populated-sidebar baseline can't reach
    because it only mounts on hover.

    :param snapshot_page: page pinned to a fixed viewport + light palette.
    :param live_server: base URL of the spawned server serving the built SPA.
    :param fulfill_json: 200-JSON route helper (suite ``conftest.py``).
    :param settle_for_snapshot: fonts + caret settle, run before capture.
    :param assert_snapshot: visual-snapshot fixture (writes under
        ``--update-snapshots``, else compares).
    """
    page = snapshot_page

    # Pin the clock so the row's relative-time pill renders a constant string;
    # only Date.now is frozen, so the flyout's openDelay timer still fires.
    page.clock.set_fixed_time(datetime.fromtimestamp(_NOW_S, tz=timezone.utc))

    # Silence the session-updates socket so no live patch churns the row.
    page.route_web_socket(re.compile(r"/v1/sessions/updates"), lambda ws: None)

    page.route("**/v1/agents", lambda r: fulfill_json(r, _AGENTS_BODY))
    page.route("**/v1/hosts", lambda r: fulfill_json(r, _HOSTS_BODY))
    page.route(_FILESYSTEM_RE, lambda r: fulfill_json(r, _EMPTY_LIST_BODY))
    # Order matters: register the narrower project/pinned routes before the bare
    # list.
    page.route(_PROJECTS_RE, lambda r: fulfill_json(r, _PROJECTS_BODY))
    page.route(_PROJECT_SESSIONS_RE, lambda r: fulfill_json(r, _PROJECT_SESSIONS_BODY))
    page.route(_PINNED_SESSIONS_RE, lambda r: fulfill_json(r, _PINNED_SESSIONS_BODY))
    page.route(_SESSIONS_RE, lambda r: fulfill_json(r, _SESSIONS_BODY))

    # Seed the working-dir chip before the SPA boots. Pins are no longer a
    # localStorage pref — the Pinned section is driven by the `?pinned=true`
    # route stubbed above.
    page.add_init_script(
        f'window.localStorage.setItem("omnigent:recent-workspaces",'
        f' JSON.stringify({{"{_HOST_ID}": ["/work/repo"]}}));'
    )

    page.goto(f"{live_server}/")

    landing = page.get_by_test_id("new-chat-landing")
    expect(landing).to_be_visible(timeout=30_000)
    # The pinned row must be painted before we can hover it.
    row = page.get_by_role("link", name=_PINNED_TITLE)
    expect(row).to_be_visible(timeout=30_000)

    # Hover the row to open the project flyout, then wait for the portalled card.
    row.hover()
    flyout = page.get_by_test_id("pinned-project-flyout")
    expect(flyout).to_be_visible(timeout=30_000)
    expect(flyout.get_by_text(_PROJECT, exact=True)).to_be_visible()

    # Settle web fonts + kill the blinking caret (both time-dependent).
    settle_for_snapshot(page)

    # Full viewport: the sidebar with the open flyout over the hero.
    assert_snapshot(page)
