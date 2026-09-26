"""Reloading the Canvas must not flash tiles in the default grid.

After a reload the persisted layout is ultimately restored, but the first
painted frames show the tile layer in the default-grid view (the unfitted
default viewport, where every unmoved card sits exactly at its reset grid
slot) before everything jumps into the restored, fitted view. The first
painted frame that shows a tile must already show it where the restored
view puts it — or the tile layer must stay hidden until then.
"""

from __future__ import annotations

import json
import os

from playwright.sync_api import Page, Route, expect

# Samples every React Flow tile's on-screen position (and the viewport
# transform) on every animation frame, from document start. rAF callbacks run
# right before paint, so each sample is what that frame shows the user. Tiles
# hidden via CSS (a fix may keep the layer hidden until the view is restored)
# are not recorded — the record holds only what is actually visible.
_FRAME_SAMPLER = """
(() => {
  window.__canvasFrames = [];
  const visible = (el) =>
    typeof el.checkVisibility === 'function'
      ? el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })
      : true;
  const sample = () => {
    const viewport = document.querySelector('.react-flow__viewport');
    const nodes = Array.from(document.querySelectorAll('.react-flow__node')).filter(visible);
    if (viewport || nodes.length > 0) {
      window.__canvasFrames.push({
        t: performance.now(),
        viewport: viewport ? viewport.style.transform : null,
        nodes: nodes.map((el) => {
          const rect = el.getBoundingClientRect();
          return {
            id: el.getAttribute('data-id'),
            transform: el.style.transform,
            screen: [Math.round(rect.x), Math.round(rect.y)],
          };
        }),
      });
    }
    requestAnimationFrame(sample);
  };
  requestAnimationFrame(sample);
})();
"""

# Read the persisted canvas layout (any viewer slot) from localStorage.
_READ_LAYOUT = (
    "() => { const key = Object.keys(localStorage)"
    ".find(k => k.startsWith('omnigent:canvas-layout:')); "
    "return key ? JSON.parse(localStorage.getItem(key)) : null; }"
)


def _stub_server_info(page: Page) -> None:
    """Advertise the canvas release feature deterministically."""
    body = json.dumps(
        {
            "accounts_enabled": False,
            "single_user": True,
            "login_url": None,
            "needs_setup": False,
            "features": {"canvas": True, "usage_page": False, "harness_install": False},
            "harness_install_enabled": False,
            "installable_harnesses": [],
        }
    )
    page.route(
        "**/v1/info",
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )


def _session(session_id: str, title: str, updated_at: int) -> dict[str, object]:
    return {
        "id": session_id,
        "object": "conversation",
        "title": title,
        "status": "idle",
        "created_at": 1,
        "updated_at": updated_at,
        "labels": {},
        "permission_level": None,
        "workspace": "/workspace/canvas",
        "git_branch": None,
        "project_id": None,
        "archived": False,
        "parent_session_id": None,
    }


def _serve_list(sessions: list[dict[str, object]]):
    def serve(route: Route) -> None:
        route.fulfill(
            json={
                "object": "list",
                "data": sessions,
                "first_id": sessions[0]["id"] if sessions else None,
                "last_id": None,
                "has_more": False,
            }
        )

    return serve


def _open_canvas_and_persist_a_move(
    page: Page,
    live_server: str,
    sessions: list[dict[str, object]],
    moved_id: str,
) -> str:
    """Open ``/canvas``, wait for every slot to persist, drag one card, and
    return its persisted transform."""
    _stub_server_info(page)
    page.route("**/v1/sessions?*", _serve_list(sessions))
    page.route("**/v1/sessions/projects", lambda route: route.fulfill(json=[]))

    # Filming aid (off by default): the flash lasts one or two display frames,
    # which slips between video-recorder samples. A CPU throttle stretches the
    # same race across enough frames to land on film without changing it.
    throttle = float(os.environ.get("CANVAS_RELOAD_CPU_THROTTLE", "0") or 0)
    if throttle > 1:
        page.context.new_cdp_session(page).send(
            "Emulation.setCPUThrottlingRate", {"rate": throttle}
        )

    page.goto(f"{live_server}/canvas")
    moved = page.locator(f'.react-flow__node[data-id="{moved_id}"]')
    expect(moved).to_be_visible()
    page.wait_for_function(
        f"() => Object.keys((({_READ_LAYOUT})() ?? {{}}).positions ?? {{}}).length"
        f" === {len(sessions)}"
    )

    before = moved.bounding_box()
    assert before is not None
    page.mouse.move(before["x"] + 10, before["y"] + 10)
    page.mouse.down()
    page.mouse.move(before["x"] + 140, before["y"] + 300, steps=8)
    page.mouse.up()
    page.wait_for_function(
        f"() => {{ const p = (({_READ_LAYOUT})() ?? {{}}).positions?.['{moved_id}']; "
        f"return p && JSON.stringify(p) !== '[0,0]'; }}"
    )
    persisted_transform = moved.evaluate("el => el.style.transform")
    assert persisted_transform != "translate(0px, 0px)"
    return persisted_transform


def _reload_and_assert_first_frames_restored(
    page: Page,
    moved_id: str,
    persisted_transform: str,
    total: int,
) -> None:
    """Reload with a frame sampler installed and assert every tile's first
    painted frame is already in the restored view."""
    page.add_init_script(_FRAME_SAMPLER)
    page.reload()
    # At the fitted view every card is in view, so all of them render even
    # with onlyRenderVisibleElements; waiting for the full count waits for
    # the restored steady state.
    expect(page.locator(".react-flow__node")).to_have_count(total, timeout=30_000)
    page.wait_for_timeout(500)

    frames = [frame for frame in page.evaluate("window.__canvasFrames") if frame["nodes"]]
    assert frames, "no frames with visible tiles were sampled after the reload"

    # Steady state after the reload: the restored layout in the fitted view.
    steady = {node["id"]: node for node in frames[-1]["nodes"]}
    assert len(steady) == total, f"expected {total} steady tiles, got {len(steady)}"
    assert steady[moved_id]["transform"] == persisted_transform, (
        f"reload did not restore the persisted layout: {steady[moved_id]}"
    )

    # The bug: the first painted frames show the tile layer in the default-grid
    # view (unfitted viewport, unmoved tiles at their reset grid slots on
    # screen) before jumping to the restored view. Each tile's first visible
    # frame must already paint it where the restored view puts it.
    first_seen: dict[str, dict] = {}
    first_viewport: dict[str, str | None] = {}
    for frame in frames:
        for node in frame["nodes"]:
            if node["id"] not in first_seen:
                first_seen[node["id"]] = node
                first_viewport[node["id"]] = frame["viewport"]
    mismatches = {
        node_id: {
            "first": node,
            "first_viewport": first_viewport[node_id],
            "steady": steady.get(node_id),
        }
        for node_id, node in first_seen.items()
        if node_id not in steady
        or any(abs(node["screen"][axis] - steady[node_id]["screen"][axis]) > 2 for axis in (0, 1))
    }
    assert not mismatches, (
        "tiles flashed outside the restored view after the reload "
        "(first painted frame vs steady state): "
        f"{json.dumps(dict(list(mismatches.items())[:5]), indent=2)}\n"
        f"({len(mismatches)} of {total} tiles flashed; "
        f"first sampled frame: {json.dumps(frames[0], indent=2)[:2000]})"
    )


def test_canvas_reload_first_frame_shows_the_restored_view(
    page: Page,
    live_server: str,
) -> None:
    """Each tile's first painted frame after a reload is already in the restored view."""
    sessions = [_session("moved", "Moved session", 2), _session("other", "Other session", 1)]
    persisted = _open_canvas_and_persist_a_move(page, live_server, sessions, "moved")
    _reload_and_assert_first_frames_restored(page, "moved", persisted, total=len(sessions))


def test_canvas_reload_does_not_flash_the_default_grid_at_scale(
    page: Page,
    live_server: str,
) -> None:
    """The same reload journey on a realistically large canvas: a wall of
    tiles must not paint as the default grid before jumping to the restored,
    fitted view."""
    sessions = [
        _session(f"s{index:03d}", f"Session {index:03d}", 1_000 - index) for index in range(120)
    ]
    persisted = _open_canvas_and_persist_a_move(page, live_server, sessions, "s000")
    _reload_and_assert_first_frames_restored(page, "s000", persisted, total=len(sessions))
