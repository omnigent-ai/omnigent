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
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from playwright.sync_api import Locator, Page, Route, expect

from tests.e2e_ui.conftest import open_right_rail
from tests.e2e_ui.desktop._mac_shell import MAC_SHELL_INIT_SCRIPT

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


def _set_features(page: Page, floating: bool) -> None:
    """Answer ``/v1/info`` with the tabs sidebar on and floating panes set to ``floating``."""

    def answer(route: Route) -> None:
        response = route.fetch()
        info = response.json()
        info["features"] = {
            **(info.get("features") or {}),
            "workspace_sidebar_v2": True,
            "floating_panes": floating,
        }
        route.fulfill(response=response, json=info)

    page.route("**/v1/info", answer)


def _open(
    page: Page,
    base_url: str,
    session_id: str,
    *,
    scheme: str,
    floating: bool,
    viewport: dict[str, int] = _VIEWPORT,
) -> None:
    page.set_viewport_size(viewport)
    page.emulate_media(color_scheme=scheme)
    _set_features(page, floating)
    page.goto(f"{base_url}/c/{session_id}")
    expect(message_composer(page)).to_be_visible(timeout=30_000)
    expect(page.locator(".app-shell[data-floating-panes]")).to_have_count(1 if floating else 0)
    # Motion settles, so the pixels read are the ones the user sees.
    page.wait_for_timeout(500)


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
def test_without_floating_panes_the_layout_stays_edge_to_edge(
    page: Page, seeded_session: tuple[str, str], scheme: str
) -> None:
    """With the flag off the chat has no card, and the dark sidebar keeps its edge."""
    base_url, session_id = seeded_session
    _open(page, base_url, session_id, scheme=scheme, floating=False)
    geometry = _geometry(page)
    assert geometry["chatRadius"] == "0px"
    assert geometry["chatBorder"] == "0px"
    assert geometry["sidebarBorder"] == ("1px" if scheme == "dark" else "0px")
    _capture(page, f"edge-to-edge-{scheme}")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_sidebar_paints_as_it_does_without_floating_panes(
    page: Page, seeded_session: tuple[str, str], scheme: str
) -> None:
    """The sidebar's own surface is the same either way, so its rows keep their contrast."""
    base_url, session_id = seeded_session
    regions: list[Image.Image] = []
    for floating in (True, False):
        _open(page, base_url, session_id, scheme=scheme, floating=floating)
        sidebar_right = round(_geometry(page)["sidebarRight"])
        image = _capture(page, f"sidebar-{scheme}-{'floating' if floating else 'flat'}")
        # The empty lower part of the sidebar, clear of its rows and its edge.
        regions.append(image.crop((16, 760, sidebar_right - 16, 860)))
    floating_region, flat_region = regions
    worst = max(
        max(abs(a - b) for a, b in zip(left, right, strict=True))
        for left, right in zip(floating_region.getdata(), flat_region.getdata(), strict=True)
    )
    assert worst <= _MAX_STEP, f"the sidebar paints up to {worst} differently"


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_chat_card_stays_rounded_after_switching_sessions(
    page: Page, seeded_session_pair: tuple[str, str, str], scheme: str
) -> None:
    """Moving to another session and back keeps the chat's card."""
    base_url, first, second = seeded_session_pair
    _open(page, base_url, first, scheme=scheme, floating=True)
    page.goto(f"{base_url}/c/{second}")
    expect(message_composer(page)).to_be_visible(timeout=30_000)
    page.go_back()
    expect(message_composer(page)).to_be_visible(timeout=30_000)
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
    page.wait_for_timeout(500)
    body = page.evaluate(
        """() => document
          .querySelector('aside[aria-label="Workspace"] [data-workspace-panel-content]')
          .getBoundingClientRect().left"""
    )
    # Full screen puts the left sidebar away, so the backdrop runs from the window's edge.
    sidebar_right = max(0, round(_geometry(page)["sidebarRight"]))
    image = _capture(page, f"floating-panes-{scheme}-full-screen")
    x_from = max(0, sidebar_right - 24)
    step, at = _largest_step(image, 860, x_from, round(body) - 1)
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
    page.wait_for_timeout(500)
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
