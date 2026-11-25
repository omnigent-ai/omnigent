"""E2E: the ``floating_panes`` flag floats a session's panes as cards on the sidebar's color.

The flag is turned on by answering ``/v1/info`` with it added to the server's own features,
alongside ``workspace_sidebar_v2``; the server side, where it follows ``workspace_sidebar_v2``
until a deployment names it, is covered by ``tests/server/test_feature_flags.py``. Pixels are
read as painted: rows run from inside the sidebar across its edge to the chat's card, and one
surface has no step along them. Set ``OMNIGENT_E2E_CAPTURES`` to a directory to keep the
screenshots.
"""

from __future__ import annotations

import io
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest
from PIL import Image
from playwright.sync_api import Locator, Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry, open_right_rail
from tests.e2e_ui.desktop._mac_shell import MAC_SHELL_INIT_SCRIPT, WINDOW_CONTROLS_STRIP

_VIEWPORT = {"width": 1440, "height": 900}


def message_composer(page: Page) -> Locator:
    """The session's message box, there once its chat page shows."""
    return page.get_by_role("textbox", name="Message the agent")


# Neighbouring pixels of one surface differ by a gradient's dithering at most; an edge by more.
_MAX_STEP = 2
# Pixels averaged on each side of a point: gradients are dithered pixel by pixel, an edge isn't.
_SPAN = 2

_GEOMETRY_JS = """() => {
  const sidebar = document.querySelector(".conversations-sidebar");
  const chat = document.querySelector('[data-main-view="chat"] > main[data-session-id]');
  const box = chat.getBoundingClientRect();
  const style = getComputedStyle(chat);
  return {
    sidebarRight: sidebar.getBoundingClientRect().right,
    sidebarBorder: getComputedStyle(sidebar).borderRightWidth,
    chatLeft: box.left,
    chatTop: box.top,
    chatBottom: box.bottom,
    chatRadius: style.borderTopLeftRadius,
    chatBorder: style.borderLeftWidth,
  };
}"""


_SETTLE_JS = """async () => {
  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  const finite = document.getAnimations().filter(
    animation => animation.effect?.getComputedTiming().endTime !== Infinity
  );
  await Promise.all(finite.map(animation => animation.finished.catch(() => {})));
}"""


def _settle(page: Page) -> None:
    """Wait for the transitions under way to end, so the pixels read are the ones the user sees."""
    page.evaluate(_SETTLE_JS)


_HIDE_SIDEBAR_CONTENTS_JS = """() => {
  for (const child of document.querySelector(".conversations-sidebar").children) {
    child.style.visibility = "hidden";
  }
}"""


def _hide_sidebar_contents(page: Page) -> None:
    """Leave only the sidebar's surface: sessions other journeys made may fill its rows."""
    page.evaluate(_HIDE_SIDEBAR_CONTENTS_JS)


def _set_features(
    page: Page, floating: bool, *, tabs_sidebar: bool = True, extra: dict[str, bool] | None = None
) -> None:
    """Answer ``/v1/info`` with floating panes set to ``floating``, the tabs sidebar on unless
    ``tabs_sidebar`` turns it off for the classic one, and any ``extra`` features."""

    def answer(route: Route) -> None:
        response = route.fetch()
        info = response.json()
        info["features"] = {
            **(info.get("features") or {}),
            "workspace_sidebar_v2": tabs_sidebar,
            "floating_panes": floating,
            **(extra or {}),
        }
        route.fulfill(response=response, json=info)

    page.route("**/v1/info", answer)


def _list_only(page: Page, session_id: str) -> None:
    """Answer the sidebar's session list with this test's session alone.

    The shard shares one server, so sessions other tests created would otherwise fill the
    sidebar, and the pixel checks would compare rows a layout change moved, not its surface.
    """

    def answer(route: Route) -> None:
        # Only the list endpoint, not /v1/sessions/{id} or its sub-resources.
        if route.request.method != "GET" or urlparse(route.request.url).path != "/v1/sessions":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        payload["data"] = [
            session for session in payload.get("data", []) if session.get("id") == session_id
        ]
        route.fulfill(response=response, json=payload)

    page.route("**/v1/sessions?*", answer)


def _open(
    page: Page,
    base_url: str,
    session_id: str,
    *,
    scheme: str,
    floating: bool,
    viewport: dict[str, int] = _VIEWPORT,
    tabs_sidebar: bool = True,
    extra_features: dict[str, bool] | None = None,
) -> None:
    page.set_viewport_size(viewport)
    page.emulate_media(color_scheme=scheme)
    _set_features(page, floating, tabs_sidebar=tabs_sidebar, extra=extra_features)
    page.goto(f"{base_url}/c/{session_id}")
    expect(message_composer(page)).to_be_visible(timeout=30_000)
    expect(page.locator(".app-shell[data-floating-panes]")).to_have_count(1 if floating else 0)
    _settle(page)


def _geometry(page: Page) -> dict[str, Any]:
    return page.evaluate(_GEOMETRY_JS)


def _capture(page: Page, name: str) -> Image.Image:
    shot = page.screenshot(animations="disabled")
    if target := os.environ.get("OMNIGENT_E2E_CAPTURES"):
        Path(target, f"{name}.png").write_bytes(shot)
    return Image.open(io.BytesIO(shot)).convert("RGB")


def _largest_step(image: Image.Image, y: int, x_from: int, x_to: int) -> tuple[int, int]:
    """The largest per-channel step along row ``y`` within ``x_from``..``x_to``, and where: at
    each x, the mean of the pixels just left of it against the mean of those from it on."""
    row = [image.getpixel((x, y))[:3] for x in range(x_from, x_to + 1)]

    def mean(pixels: list[tuple[int, int, int]]) -> list[float]:
        return [sum(channel) / len(pixels) for channel in zip(*pixels, strict=True)]

    steps = [(0, x_from)]
    for index in range(_SPAN, len(row) - _SPAN + 1):
        left, right = mean(row[index - _SPAN : index]), mean(row[index : index + _SPAN])
        steps.append((round(max(abs(a - b) for a, b in zip(left, right, strict=True))), index))
    return max(steps)


def test_the_step_reading_passes_a_gradient_and_finds_an_edge() -> None:
    """The pixel check the journeys rest on: a dithered gradient is one surface, a 3-step edge
    is not, and the edge is found where it is."""
    gradient = Image.new("RGB", (60, 1))
    for x in range(60):
        gradient.putpixel((x, 0), (100 + x // 6 + x % 2, 100, 100))
    assert _largest_step(gradient, 0, 0, 59)[0] <= _MAX_STEP

    edge = Image.new("RGB", (60, 1), (100, 100, 100))
    for x in range(30, 60):
        edge.putpixel((x, 0), (103, 100, 100))
    step, at = _largest_step(edge, 0, 0, 59)
    assert step > _MAX_STEP
    assert at == 30


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_sidebar_and_the_backdrop_beside_the_chat_are_one_surface(
    page: Page, seeded_session: tuple[str, str], scheme: str
) -> None:
    """The chat is a rounded card; from inside the sidebar to the card's edge is one surface."""
    base_url, session_id = seeded_session
    _open(page, base_url, session_id, scheme=scheme, floating=True)
    geometry = _geometry(page)
    assert geometry["sidebarBorder"] == "0px"
    assert geometry["chatRadius"] != "0px"
    assert geometry["chatBorder"] == "1px"

    image = _capture(page, f"floating-panes-{scheme}")
    x_from = round(geometry["sidebarRight"]) - 24
    x_to = round(geometry["chatLeft"]) - 1
    # Beside the card, low enough to miss the sidebar's rows, and in the top row above it.
    for y in (round(geometry["chatBottom"]) - 40, round(geometry["chatTop"]) - 2):
        step, at = _largest_step(image, y, x_from, x_to)
        assert step <= _MAX_STEP, f"step of {step} at x={x_from + at}, y={y}"


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_without_floating_panes_the_layout_stays_edge_to_edge_and_the_sidebar_paints_the_same(
    page: Page, seeded_session: tuple[str, str], scheme: str
) -> None:
    """With the flag off the chat has no card and the dark sidebar keeps its edge; the
    sidebar's own surface is the same either way, so its rows keep their contrast."""
    base_url, session_id = seeded_session
    # The region compared below must be the sidebar's empty surface in both layouts.
    _list_only(page, session_id)
    regions: list[Image.Image] = []
    for floating in (True, False):
        _open(page, base_url, session_id, scheme=scheme, floating=floating)
        geometry = _geometry(page)
        if not floating:
            assert geometry["chatRadius"] == "0px"
            assert geometry["chatBorder"] == "0px"
            assert geometry["sidebarBorder"] == ("1px" if scheme == "dark" else "0px")
        sidebar_right = round(geometry["sidebarRight"])
        _hide_sidebar_contents(page)
        image = _capture(
            page, f"sidebar-{scheme}-floating" if floating else f"edge-to-edge-{scheme}"
        )
        # The lower part of the sidebar's bare surface, clear of its edge.
        regions.append(image.crop((16, 760, sidebar_right - 16, 860)))
    floating_region, flat_region = regions
    worst = max(
        max(abs(a - b) for a, b in zip(left, right, strict=True))
        for left, right in zip(floating_region.getdata(), flat_region.getdata(), strict=True)
    )
    assert worst <= _MAX_STEP, f"the sidebar paints up to {worst} differently"


def _choose_pane_corners(page: Page, query: str, choice: str | None = None) -> None:
    """Open ⌘K, find Pane corners by ``query``, and accept ``choice`` (or the one it names)."""
    page.keyboard.press("ControlOrMeta+k")
    palette = page.get_by_test_id("command-palette-input")
    expect(palette).to_be_focused(timeout=10_000)
    palette.fill(query)
    # Enter acts on the active row, so wait for the search to settle on Pane corners.
    row = page.get_by_role("option", name=re.compile("^Pane corners"))
    expect(row).to_have_attribute("aria-selected", "true")
    page.keyboard.press("Enter")
    if choice is not None:
        page.get_by_role("option", name=re.compile(f"^{re.escape(choice)}")).click()
    else:
        page.keyboard.press("Enter")
    # A setting's choice applies and goes back to the palette's list, which stays open.
    expect(page.get_by_role("option", name=re.compile("^Pane corners"))).to_be_visible()
    page.keyboard.press("Escape")
    expect(palette).to_have_count(0)


def test_cmd_k_switches_to_sharp_corners_and_back_to_automatic_across_a_reload(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Typing "sharp corners" in ⌘K turns floating panes off on this device, a reload keeps the
    choice, and Automatic follows the server's floating panes again."""
    base_url, session_id = seeded_session
    # The Pane corners row is a setting the QuickPick ⌘K offers.
    _open(
        page,
        base_url,
        session_id,
        scheme="light",
        floating=True,
        extra_features={"quick_pick_palette": True},
    )
    floating_shell = page.locator(".app-shell[data-floating-panes]")

    _choose_pane_corners(page, "sharp corners", "Sharp corners")
    expect(floating_shell).to_have_count(0)
    page.reload()
    expect(message_composer(page)).to_be_visible(timeout=30_000)
    expect(floating_shell).to_have_count(0)

    _choose_pane_corners(page, "pane corners", "Automatic (Floating panes)")
    expect(floating_shell).to_have_count(1)
    page.reload()
    expect(message_composer(page)).to_be_visible(timeout=30_000)
    expect(floating_shell).to_have_count(1)


def test_the_chat_card_stays_rounded_after_switching_sessions(
    page: Page, seeded_session_pair: tuple[str, str, str]
) -> None:
    """Picking another session in the sidebar and back, in place, keeps the chat's card. The
    switch exercises no theme rule, so one scheme covers it."""
    base_url, first, second = seeded_session_pair
    page.add_init_script("window.__documentLoadMarker = crypto.randomUUID();")
    _open(page, base_url, first, scheme="light", floating=True)
    load_marker = page.evaluate("window.__documentLoadMarker")
    for session_id in (second, first):
        page.locator(f'a[href="/c/{session_id}"]').click()
        expect(page).to_have_url(f"{base_url}/c/{session_id}", timeout=30_000)
        expect(message_composer(page)).to_be_visible(timeout=30_000)
    # The same document throughout: the shell and its session column were reused.
    assert page.evaluate("window.__documentLoadMarker") == load_marker
    geometry = _geometry(page)
    assert geometry["chatRadius"] != "0px"
    assert geometry["chatBorder"] == "1px"


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_a_full_screen_workspace_sits_on_the_sidebars_backdrop(
    page: Page, seeded_session: tuple[str, str], scheme: str
) -> None:
    """Full screen, the workspace runs on from the sidebar's surface with no edge."""
    base_url, session_id = seeded_session
    _open(page, base_url, session_id, scheme=scheme, floating=True)
    open_right_rail(page)
    workspace = page.get_by_role("complementary", name="Workspace")
    _capture(page, f"floating-panes-{scheme}-workspace")
    workspace.get_by_role("button", name="Full screen").click()
    expect(workspace).to_have_attribute("data-maximized", "true")
    _settle(page)
    body = page.evaluate(
        """() => document
          .querySelector('aside[aria-label="Workspace"] [data-workspace-panel-content]')
          .getBoundingClientRect().left"""
    )
    # Full screen puts the left sidebar away, so the backdrop runs from the window's edge to the
    # content: a strip that must be wide enough to read, or the check would pass on nothing.
    sidebar_right = max(0, round(_geometry(page)["sidebarRight"]))
    image = _capture(page, f"floating-panes-{scheme}-full-screen")
    x_from = max(0, sidebar_right - 24)
    x_to = round(body) - 1
    assert x_to - x_from >= 2 * _SPAN, (x_from, x_to)
    step, at = _largest_step(image, 860, x_from, x_to)
    assert step <= _MAX_STEP, f"step of {step} at x={x_from + at}"


_CHAT_RADIUS_JS = """() => getComputedStyle(
  document.querySelector('[data-main-view="chat"] > main[data-session-id]')
).borderTopLeftRadius"""


# 48rem is 768px: one side of the gate, then the other.
@pytest.mark.parametrize(("width", "floats"), [(760, False), (776, True)])
def test_a_browser_floats_the_panes_from_48rem(
    page: Page, seeded_session: tuple[str, str], width: int, floats: bool
) -> None:
    """In a browser the chat becomes a card at 48rem, below the desktop layout's own breakpoint."""
    base_url, session_id = seeded_session
    _open(
        page,
        base_url,
        session_id,
        scheme="light",
        floating=True,
        viewport={"width": width, "height": 900},
    )
    expect(page.locator("html")).not_to_have_attribute("data-desktop-app", "true")
    assert (page.evaluate(_CHAT_RADIUS_JS) != "0px") is floats


@pytest.mark.parametrize(("width", "floats"), [(700, False), (800, True)])
def test_the_desktop_app_floats_the_panes_from_48rem_too(
    page: Page, seeded_session: tuple[str, str], width: int, floats: bool
) -> None:
    """The desktop app keeps its desktop layout at every width, but floats the panes from 48rem."""
    base_url, session_id = seeded_session
    page.add_init_script(MAC_SHELL_INIT_SCRIPT)
    _open(
        page,
        base_url,
        session_id,
        scheme="light",
        floating=True,
        viewport={"width": width, "height": 900},
    )
    expect(page.locator("html")).to_have_attribute("data-desktop-app", "true")
    assert (page.evaluate(_CHAT_RADIUS_JS) != "0px") is floats
    # The left sidebar's resize edge: its 4px bar edge to edge, a band across the gap where
    # the panes float. The hit box pads it with a 5px inward and an 11px outward sliver (fine
    # pointer), so a touch or a loose click still lands on it.
    edge = page.get_by_role("separator", name="Resize sidebar").bounding_box()
    assert edge is not None
    assert edge["width"] == (10 if floats else 4) + 16, edge


_GRID_WORKSPACE_STYLE_JS = """() => {
  const aside = document.querySelector(
    '[data-testid="grid-workspace-panel"] aside[aria-label="Workspace"]'
  );
  const read = (element, properties) => {
    const style = getComputedStyle(element);
    return Object.fromEntries(properties.map((name) => [name, style.getPropertyValue(name)]));
  };
  const box = ["margin-top", "margin-bottom", "border-top-width", "border-top-left-radius"];
  return {
    aside: read(aside, ["background-color", "background-image", "border-left-width"]),
    strip: read(aside.querySelector(".workspace-tab-strip"), ["height", "border-bottom-width"]),
    content: read(aside.querySelector("[data-workspace-panel-content]"), [
      ...box,
      "background-color",
    ]),
    tab: read(aside.querySelector("[data-tab-strip-entry]"), ["background-color", "box-shadow"]),
  };
}"""


def _grid_workspace_style(
    page: Page, base_url: str, sessions: tuple[str, str], *, floating: bool
) -> dict[str, Any]:
    _open(page, base_url, sessions[0], scheme="dark", floating=floating)
    # A saved grid of both sessions, under this viewer's key, as dragging one onto the other saves.
    page.evaluate(
        """async (layout) => {
          const me = await (await fetch("/v1/me")).json();
          const key = `omnigent:session-grids:${location.origin}:${me.user_id}`;
          localStorage.setItem(key, JSON.stringify([{ id: "grid-a", layout }]));
        }""",
        {"axis": "columns", "children": list(sessions)},
    )
    page.goto(f"{base_url}/grid/grid-a")
    expect(page.get_by_test_id("session-grid")).to_be_visible(timeout=30_000)
    # A grid has no standalone session column, which the floating rules are scoped to.
    expect(page.locator("[data-main-view]")).to_have_count(0)
    page.locator(f'[data-grid-session="{sessions[0]}"]').click()
    expand = page.get_by_role("button", name="Expand grid workspace")
    if expand.is_visible():
        expand.click()
    aside = page.get_by_test_id("grid-workspace-panel").get_by_role(
        "complementary", name="Workspace"
    )
    expect(aside).to_be_visible()
    _settle(page)
    return page.evaluate(_GRID_WORKSPACE_STYLE_JS)


def test_a_grid_workspace_is_unchanged_by_floating_panes(
    page: Page, seeded_session_pair: tuple[str, str, str]
) -> None:
    """A grid's workspace sidebar keeps its own strip, edges and tab fills with the flag on."""
    base_url, first, second = seeded_session_pair
    floating = _grid_workspace_style(page, base_url, (first, second), floating=True)
    expect(page.locator(".app-shell[data-floating-panes]")).to_have_count(1)
    flat = _grid_workspace_style(page, base_url, (first, second), floating=False)
    assert floating == flat


_WORKSPACE = 'aside[aria-label="Workspace"]'
_CHAT_PANE = '[data-main-view="chat"] > main[data-session-id]'
# Narrowing the sidebar: widening it far would hit the chat's minimum width.
_DRAG_PX = 80


def _workspace_width(page: Page) -> float:
    """The workspace sidebar's width once its slide open, or a resize, has finished."""
    _settle(page)
    return page.locator(_WORKSPACE).evaluate("(el) => el.getBoundingClientRect().width")


def _gutter(page: Page) -> dict[str, float]:
    """Where the chat pane ends, where the workspace sidebar starts, and the pane's middle."""
    return page.evaluate(
        """([sidebar, pane]) => {
      const rail = document.querySelector(sidebar).getBoundingClientRect();
      const chat = document.querySelector(pane).getBoundingClientRect();
      return { paneRight: chat.right, railLeft: rail.left, middleY: chat.top + chat.height / 2 };
    }""",
        [_WORKSPACE, _CHAT_PANE],
    )


def _cursor_at(page: Page, x: float, y: float) -> str:
    return page.evaluate(
        "([x, y]) => getComputedStyle(document.elementFromPoint(x, y)).cursor", [x, y]
    )


def _drag(page: Page, x: float, y: float, dx: float) -> None:
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + dx / 2, y, steps=4)
    page.mouse.move(x + dx, y, steps=4)
    page.mouse.up()


@pytest.mark.parametrize("tabs_sidebar", [True, False], ids=["tabs", "classic"])
def test_the_workspace_sidebar_resizes_from_the_gutter_beside_the_chat(
    page: Page, seeded_session: tuple[str, str], tabs_sidebar: bool
) -> None:
    """The whole gap beside the chat's card is the sidebar's edge; a press in the card is not."""
    base_url, session_id = seeded_session
    _open(page, base_url, session_id, scheme="light", floating=True, tabs_sidebar=tabs_sidebar)
    open_right_rail(page)
    before = _workspace_width(page)
    gutter = _gutter(page)
    assert gutter["railLeft"] - gutter["paneRight"] >= 3, gutter
    middle_x = (gutter["paneRight"] + gutter["railLeft"]) / 2
    y = gutter["middleY"]
    for x in (gutter["paneRight"] + 0.5, middle_x, gutter["railLeft"] - 0.5):
        assert _cursor_at(page, x, y) == "col-resize", (x, gutter)

    _drag(page, middle_x, y, _DRAG_PX)
    narrowed = _workspace_width(page)
    assert abs(narrowed - (before - _DRAG_PX)) <= 4, (before, narrowed)

    inside = _gutter(page)["paneRight"] - 4
    assert _cursor_at(page, inside, y) != "col-resize"
    _drag(page, inside, y, -_DRAG_PX)
    assert abs(_workspace_width(page) - narrowed) <= 1


def _open_terminal_first(page: Page, base_url: str, session_id: str, **open_args: Any) -> None:
    """Open the session as ``omnigent claude`` sessions are: terminal-first, with the agent's
    terminal listed (no live PTY is needed to place the switch)."""
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"labels": {"omnigent.ui": "terminal"}},
        timeout=10.0,
    ).raise_for_status()
    terminals = {
        "object": "list",
        "data": [
            {
                "id": "terminal_tui_main",
                "type": "terminal",
                "session_id": session_id,
                "name": "tui:main",
                "metadata": {"terminal_name": "tui", "session_key": "main", "running": True},
            }
        ],
        "first_id": "terminal_tui_main",
        "last_id": "terminal_tui_main",
        "has_more": False,
    }
    page.route(
        re.compile(rf"/v1/sessions/{re.escape(session_id)}/resources/terminals\?.*"),
        lambda route: route.fulfill(json=terminals),
    )
    _open(page, base_url, session_id, **open_args)


@pytest.mark.parametrize("tabs_sidebar", [True, False], ids=["tabs", "classic"])
@pytest.mark.parametrize("floating", [True, False], ids=["floating", "edge-to-edge"])
def test_a_terminal_first_chat_switches_views_from_its_own_row(
    page: Page, seeded_session: tuple[str, str], tabs_sidebar: bool, floating: bool
) -> None:
    """With floating panes the Chat/Terminal switch sits in a row at the top of the chat's card,
    over both views; with ``-floating_panes`` it stays in the header. The classic layout's cases
    run dark, so the switch's track is seen in both themes."""
    base_url, session_id = seeded_session
    scheme = "light" if tabs_sidebar else "dark"
    name = f"terminal-first-{'strip' if floating else 'header'}-{scheme}"
    _open_terminal_first(
        page, base_url, session_id, scheme=scheme, floating=floating, tabs_sidebar=tabs_sidebar
    )
    header = page.locator("header.chat-header")
    strip = page.get_by_role("group", name="Chat pane")
    switch_home = strip if floating else header
    view_switch = switch_home.get_by_role("group", name="Switch between chat and terminal")
    expect(view_switch).to_be_visible()
    if floating:
        expect(header.get_by_role("group", name="Switch between chat and terminal")).to_have_count(
            0
        )
    else:
        expect(strip).to_have_count(0)
    _capture(page, name)

    view_switch.get_by_role("button", name=re.compile("^Terminal ")).click()
    # With the tabs sidebar the terminal shows as the panes view.
    terminal_view = page.get_by_test_id(
        "split-panes-main-view" if tabs_sidebar else "main-terminal-view"
    )
    expect(terminal_view).to_be_visible()
    expect(message_composer(page)).to_have_count(0)
    if floating:
        # The row stays over the terminal, which starts below it.
        expect(strip).to_be_visible()
        strip_box, terminal_box = strip.bounding_box(), terminal_view.bounding_box()
        assert strip_box is not None and terminal_box is not None
        content_top = terminal_box["y"] + float(
            terminal_view.evaluate("(el) => parseFloat(getComputedStyle(el).paddingTop)")
        )
        # Right below it, with no band between the row and the terminal.
        strip_bottom = strip_box["y"] + strip_box["height"]
        assert strip_bottom - 0.5 <= content_top <= strip_bottom + 1.5, (strip_box, content_top)
    _capture(page, f"{name}-terminal")

    view_switch.get_by_role("button", name="Chat view").click()
    expect(message_composer(page)).to_be_visible()


_TOP_ROW_JS = """() => {
  const box = (selector) => {
    const rect = document.querySelector(selector).getBoundingClientRect();
    return { top: rect.top, height: rect.height, centre: rect.top + rect.height / 2 };
  };
  return {
    header: box('[data-main-view] > header.chat-header'),
    tabs: box('aside[aria-label="Workspace"] .workspace-tab-strip'),
    cluster: box('.electron-sidebar-header-actions'),
  };
}"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("zoom", [None, 1.0, 1.25], ids=["older-preload", "zoom-100", "zoom-125"])
def test_the_mac_top_row_is_the_title_bar_beside_the_window_buttons(
    page: Page, seeded_session: tuple[str, str], zoom: float | None, scheme: str
) -> None:
    """In the macOS shell the top row is the title bar: 48 window px, on the window buttons'
    centre line with the title-bar cluster, at any page zoom and with the sidebar open or
    closed. A preload that doesn't report the zoom gets the 48px row."""
    base_url, session_id = seeded_session
    page.add_init_script(MAC_SHELL_INIT_SCRIPT)
    if zoom is not None:
        # As the preload does: the page zoom, which keeps native controls in window pixels.
        page.add_init_script(
            "document.addEventListener('DOMContentLoaded', () => document.documentElement"
            f".style.setProperty('--omnigent-electron-zoom', '{zoom}'));"
        )
    _open(page, base_url, session_id, scheme=scheme, floating=True)
    expect(page.locator("html")).to_have_attribute("data-electron-mac", "true")
    open_right_rail(page)
    title_bar = 48 / (zoom or 1)
    cluster = page.locator(".electron-sidebar-header-actions")

    for sidebar in ("open", "closed"):
        if sidebar == "closed":
            cluster.get_by_role("button", name="Close sidebar", exact=True).click()
            expect(page.locator('aside[aria-label="Conversations"]')).to_have_attribute(
                "data-collapsed", "true"
            )
            _settle(page)
        zoom_name = "older-preload" if zoom is None else f"zoom-{round(zoom * 100)}"
        _capture(page, f"mac-top-row-{zoom_name}-{scheme}-sidebar-{sidebar}")
        row = page.evaluate(_TOP_ROW_JS)
        for part in ("header", "tabs"):
            assert abs(row[part]["height"] - title_bar) <= 0.5, (sidebar, part, row)
            assert abs(row[part]["centre"] - row["cluster"]["centre"]) <= 1, (sidebar, part, row)
        assert abs(row["cluster"]["centre"] - title_bar / 2) <= 1, (sidebar, row)

    # With the sidebar closed, nothing in the header sits under the window buttons, whose strip
    # is fixed in window pixels.
    strip = {key: value / (zoom or 1) for key, value in WINDOW_CONTROLS_STRIP.items()}
    for control in page.locator("header.chat-header button:visible").all():
        box = control.bounding_box()
        assert box is not None
        overlaps = (
            box["x"] < strip["x"] + strip["width"]
            and strip["x"] < box["x"] + box["width"]
            and box["y"] < strip["y"] + strip["height"]
            and strip["y"] < box["y"] + box["height"]
        )
        assert not overlaps, (box, strip)
