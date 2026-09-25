"""Dragging a pinned session into a project must not flash through Sessions.

Dropping a pinned sidebar row onto a project folder
runs two optimistic mutations — file into the project and unpin. The unpin
commits to the query cache immediately, but ``useMoveToProject`` only
overlays the new membership optimistically when the target folder has a
cached first-class project id. Dropping onto a legacy label-only folder
skips the overlay, so until the on-demand project creation + PATCH round
trip lands the row is unpinned with no project — and visibly regroups into
the flat "Sessions" (My sessions) section before settling in the folder.

Both tests detect the transit with an in-page MutationObserver +
requestAnimationFrame tracer recording which sidebar section holds the
row's link at every DOM change / rendered frame.
"""

from __future__ import annotations

import uuid

import httpx
from playwright.sync_api import Locator, Page, expect

# Records, for the given session link, the set of sidebar sections containing
# it on every DOM mutation and every rendered frame. Consecutive identical
# states are deduped; `sawInSessions` flips if the flat "Sessions" section
# ever holds the link, and `sessionsFrames` counts rendered frames where it
# did (states a user could actually see).
_SECTION_TRACE_SCRIPT = """
(arg) => {
  const { href, projectName } = arg;
  const classify = (text) => {
    if (text.startsWith("Pinned")) return "Pinned";
    if (text.startsWith("Sessions")) return "Sessions";
    if (text.includes(projectName)) return "Project";
    return text.slice(0, 40);
  };
  const state = {
    trace: [],
    sawInSessions: false,
    sessionsFrames: 0,
    rafId: 0,
    observer: null,
  };
  const record = (source) => {
    const sections = new Set();
    for (const a of document.querySelectorAll(`a[href="${href}"]`)) {
      const section = a.closest("section");
      if (!section) continue;
      const button = section.querySelector("h2 button");
      if (button) sections.add(classify(button.textContent || ""));
    }
    const now = [...sections].sort().join("+") || "(absent)";
    const last = state.trace[state.trace.length - 1];
    if (!last || last.state !== now) {
      state.trace.push({ state: now, t: Math.round(performance.now()), source });
    }
    if (sections.has("Sessions")) {
      state.sawInSessions = true;
      if (source === "frame") state.sessionsFrames += 1;
    }
  };
  state.observer = new MutationObserver(() => record("mutation"));
  state.observer.observe(document.body, {
    subtree: true,
    childList: true,
    attributes: true,
    characterData: true,
  });
  const loop = () => {
    record("frame");
    state.rafId = requestAnimationFrame(loop);
  };
  state.rafId = requestAnimationFrame(loop);
  record("init");
  window.__sectionTrace = state;
}
"""

_STOP_TRACE_SCRIPT = """
() => {
  const state = window.__sectionTrace;
  cancelAnimationFrame(state.rafId);
  state.observer.disconnect();
  return {
    trace: state.trace,
    sawInSessions: state.sawInSessions,
    sessionsFrames: state.sessionsFrames,
  };
}
"""


def _section(page: Page, title: str) -> Locator:
    """Locate the sidebar ``<section>`` whose header button reads *title*."""
    return page.locator("section").filter(has=page.get_by_role("button", name=title, exact=True))


def _pin_session(page: Page, session_id: str) -> Locator:
    """Pin *session_id* via its row's quick action; return its Pinned link."""
    row = page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))
    expect(row).to_be_visible()
    row.hover()
    pin_button = row.get_by_test_id("quick-pin-conversation")
    expect(pin_button).to_have_attribute("aria-label", "Pin conversation")
    pin_button.click()
    link_in_pinned = _section(page, "Pinned").locator(f'a[href="/c/{session_id}"]')
    expect(link_in_pinned).to_be_visible()
    return link_in_pinned


def _reveal_project_folder(page: Page, project_name: str) -> Locator:
    """Expand the Projects group and return *project_name*'s folder header."""
    projects_group = page.get_by_role("button", name="Projects", exact=True)
    if projects_group.get_attribute("aria-expanded") == "false":
        projects_group.click()
    folder_header = page.locator(f'button[data-project-order-name="{project_name}"]')
    expect(folder_header).to_be_visible()
    return folder_header


def _drag_pinned_row_onto_folder(
    page: Page, session_id: str, link_in_pinned: Locator, folder_header: Locator
) -> None:
    """Drag the pinned row onto the folder and wait for the filing PATCH."""
    source_box = link_in_pinned.bounding_box()
    target_box = folder_header.bounding_box()
    assert source_box and target_box
    x = source_box["x"] + source_box["width"] / 2
    y = source_box["y"] + source_box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 10, y, steps=3)
    page.mouse.move(
        target_box["x"] + target_box["width"] / 2,
        target_box["y"] + target_box["height"] / 2,
        steps=10,
    )
    with page.expect_response(
        lambda r: f"/v1/sessions/{session_id}" in r.url and r.request.method == "PATCH"
    ) as filed:
        page.mouse.up()
    assert filed.value.ok


def _assert_settled_without_sessions_transit(
    page: Page, session_id: str, project_name: str
) -> None:
    """Assert the row settled in the folder and never transited Sessions."""
    expect(_section(page, project_name).locator(f'a[href="/c/{session_id}"]')).to_be_visible()
    expect(_section(page, "Pinned").locator(f'a[href="/c/{session_id}"]')).to_have_count(0)
    # Keep observing across the post-drop reconcile refetches: a lagging list
    # read racing the PATCH can reintroduce the flicker after the drop settles.
    page.wait_for_timeout(1500)

    result = page.evaluate(_STOP_TRACE_SCRIPT)
    assert not result["sawInSessions"], (
        "pinned session transited the flat Sessions section on its way into "
        f"the project ({result['sessionsFrames']} rendered frame(s)); "
        f"section trace: {result['trace']}"
    )


def test_pinned_session_drag_to_label_only_project_never_passes_through_sessions(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """A pinned row dropped on a legacy label-only folder moves there directly.

    The target folder exists only through another session's ``omni_project``
    label (no first-class project row), so the move has no cached project id
    to overlay optimistically — the window where the bug shows.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session_pair: ``(base_url, session_a, session_b)`` — two
        runner-bound sessions in the same server.
    """
    base_url, session_a, session_b = seeded_session_pair
    httpx.patch(
        f"{base_url}/v1/sessions/{session_a}",
        json={"title": f"e2e-pin-move-{uuid.uuid4().hex[:8]}"},
        timeout=10.0,
    ).raise_for_status()
    project_name = f"Legacy-{uuid.uuid4().hex[:6]}"
    httpx.patch(
        f"{base_url}/v1/sessions/{session_b}",
        json={"labels": {"omni_project": project_name}},
        timeout=10.0,
    ).raise_for_status()

    try:
        page.goto(f"{base_url}/c/{session_a}")
        link_in_pinned = _pin_session(page, session_a)
        folder_header = _reveal_project_folder(page, project_name)
        page.evaluate(
            _SECTION_TRACE_SCRIPT,
            {"href": f"/c/{session_a}", "projectName": project_name},
        )
        _drag_pinned_row_onto_folder(page, session_a, link_in_pinned, folder_header)
        _assert_settled_without_sessions_transit(page, session_a, project_name)
    finally:
        # Filing promotes the label-only folder to a first-class project row;
        # delete it so the shared server doesn't accumulate folders.
        resp = httpx.get(f"{base_url}/v1/sessions/projects", timeout=10.0)
        if resp.status_code == 200:
            for proj in resp.json():
                if proj["name"] == project_name and proj.get("id"):
                    httpx.delete(f"{base_url}/v1/projects/{proj['id']}", timeout=10.0)
        httpx.patch(
            f"{base_url}/v1/sessions/{session_b}",
            json={"labels": {"omni_project": ""}},
            timeout=10.0,
        )


def test_pinned_session_drag_to_first_class_project_never_passes_through_sessions(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Same journey onto a first-class project (cached id → optimistic overlay).

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": f"e2e-pin-move-{uuid.uuid4().hex[:8]}"},
        timeout=10.0,
    ).raise_for_status()
    project_name = f"Proj-{uuid.uuid4().hex[:6]}"
    created = httpx.post(f"{base_url}/v1/projects", json={"name": project_name}, timeout=10.0)
    created.raise_for_status()
    project_id = created.json()["id"]

    try:
        page.goto(f"{base_url}/c/{session_id}")
        link_in_pinned = _pin_session(page, session_id)
        folder_header = _reveal_project_folder(page, project_name)
        page.evaluate(
            _SECTION_TRACE_SCRIPT,
            {"href": f"/c/{session_id}", "projectName": project_name},
        )
        _drag_pinned_row_onto_folder(page, session_id, link_in_pinned, folder_header)
        _assert_settled_without_sessions_transit(page, session_id, project_name)
    finally:
        httpx.delete(f"{base_url}/v1/projects/{project_id}", timeout=10.0)
