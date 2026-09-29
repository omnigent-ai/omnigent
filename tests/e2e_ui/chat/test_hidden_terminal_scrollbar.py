"""Background xterm output must not paint a scrollbar over another surface.

The sessions and transcript are real, but their terminal inventory and attach
WebSockets are browser mocks. Binary output exercises the real xterm scrollbar
without launching a CLI or calling an LLM. Pixel comparisons cover the column
where a hidden terminal's thumb otherwise leaks across the chat and composer.
"""

from __future__ import annotations

import io
import json
import math
import re
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from PIL import Image, ImageChops
from playwright.sync_api import Browser, Locator, Page, Route, WebSocketRoute, expect

from tests.e2e_ui.conftest import _build_hello_world_bundle, fetch_with_retry

_TERMINAL_ID = "terminal_codex_main"
_BAR = ".xterm .scrollbar.vertical"
_SLIDER = f"{_BAR} .slider"
_RECT = """el => {
  const r = el.getBoundingClientRect();
  return {x: r.x, y: r.y, width: r.width, height: r.height};
}"""
_PAINT = """el => {
  let opacity = 1;
  let displayed = true;
  for (let node = el; node; node = node.parentElement) {
    const style = getComputedStyle(node);
    opacity *= Number(style.opacity);
    displayed &&= style.display !== 'none';
  }
  const rect = el.getBoundingClientRect();
  return {opacity, displayed, visibility: getComputedStyle(el).visibility,
    width: rect.width, height: rect.height};
}"""


@pytest.fixture
def terminal_scrollbar_session(live_server: str) -> Iterator[tuple[str, str]]:
    """Create an unbound terminal-first session with an overflowing transcript."""
    with httpx.Client(base_url=live_server, timeout=30) as client:
        response = client.post(
            "/v1/sessions",
            data={
                "metadata": json.dumps(
                    {
                        "labels": {
                            "omnigent.ui": "terminal",
                            "omnigent.wrapper": "codex-native-ui",
                        }
                    }
                )
            },
            files={"bundle": ("agent.tar.gz", _build_hello_world_bundle())},
        )
        response.raise_for_status()
        session_id = response.json()["session_id"]
        try:
            for role, text in (
                ("user", "Inspect the chat layout."),
                (
                    "assistant",
                    "\n\n".join(f"Transcript paragraph {n}." for n in range(32)),
                ),
            ):
                client.post(
                    f"/v1/sessions/{session_id}/events",
                    json={
                        "type": "external_conversation_item",
                        "data": {
                            "item_type": "message",
                            "response_id": "resp_scrollbar",
                            "item_data": {
                                "role": role,
                                "agent": "hello_world",
                                "content": [
                                    {
                                        "type": "input_text" if role == "user" else "output_text",
                                        "text": text,
                                    }
                                ],
                            },
                        },
                    },
                ).raise_for_status()
            yield live_server, session_id
        finally:
            client.delete(f"/v1/sessions/{session_id}").raise_for_status()


def _mock_terminal_attach(page: Page, session_ids: list[str]) -> dict[str, list[WebSocketRoute]]:
    sockets: dict[str, list[WebSocketRoute]] = {session_id: [] for session_id in session_ids}

    def session_snapshot(route: Route) -> None:
        response = fetch_with_retry(route)
        payload = response.json()
        rows = payload.get("data", [payload])
        for row in rows:
            if row.get("id") in session_ids:
                row.update(runner_id="runner_scrollbar_mock", runner_online=True)
        route.fulfill(response=response, json=payload)

    def terminal_inventory(route: Route) -> None:
        route.fulfill(
            json={
                "object": "list",
                "data": [
                    {
                        "id": _TERMINAL_ID,
                        "object": "terminal",
                        "name": "codex",
                        "metadata": {
                            "terminal_name": "codex",
                            "session_key": "main",
                            "running": True,
                        },
                    }
                ],
                "has_more": False,
            }
        )

    def attach(ws: WebSocketRoute) -> None:
        session_id = urlparse(ws.url).path.split("/")[3]
        sockets[session_id].append(ws)
        ws.on_message(lambda _message: None)

    ids = "|".join(re.escape(session_id) for session_id in session_ids)
    page.route(re.compile(rf"/v1/sessions(?:/(?:{ids}))?(?:\?|$)"), session_snapshot)
    page.route(
        re.compile(rf"/v1/sessions/(?:{ids})/resources/terminals(?:\?|$)"),
        terminal_inventory,
    )
    page.route(
        "**/health?session_ids=*",
        lambda route: route.fulfill(
            json={
                "sessions": {
                    session_id: {"runner_online": True, "host_online": None}
                    for session_id in session_ids
                }
            }
        ),
    )
    page.route_web_socket(
        re.compile(rf"/v1/sessions/(?:{ids})/resources/terminals/{_TERMINAL_ID}/attach"),
        attach,
    )
    # The fake online PTYs must not be contradicted by the unbound server rows.
    page.route_web_socket("**/v1/sessions/updates*", lambda ws: ws.on_message(lambda _msg: None))
    return sockets


def _select_view(page: Page, view: str) -> None:
    segment = page.get_by_test_id(f"view-mode-{view}")
    if segment.is_visible():
        segment.click()
    else:
        page.get_by_role("button", name=re.compile(r"^(Session|Conversation) actions$")).filter(
            visible=True
        ).click()
        page.get_by_test_id(f"view-mode-menu-{view}").click()
        expect(page.get_by_role("menu")).to_have_count(0)


def _emit_output(page: Page, surface: Locator, socket: WebSocketRoute) -> None:
    previous_height = surface.locator(_SLIDER).evaluate("el => el.getBoundingClientRect().height")
    socket.send("".join(f"Background terminal line {n:03d}\r\n" for n in range(96)).encode())
    expect(surface.locator(f"{_BAR}.visible")).to_be_attached(timeout=10_000)
    page.wait_for_function(
        "({el, previous}) => el.getBoundingClientRect().height > 0 && "
        "el.getBoundingClientRect().height !== previous && "
        "Number(getComputedStyle(el.parentElement).opacity) > 0.99",
        arg={"el": surface.locator(_SLIDER).element_handle(), "previous": previous_height},
        timeout=10_000,
    )


def _assert_hidden_output(
    page: Page, surface: Locator, socket: WebSocketRoute, artifacts: Path, phase: str
) -> None:
    """Compare actual pixels while xterm independently reveals its scrollbar."""
    expect(surface).to_have_attribute("data-visible", "false")
    expect(surface).to_have_attribute("inert", "")
    # Wait for previous output/hover fading and transcript scroll settling.
    page.wait_for_timeout(1500)
    rect = surface.locator(_BAR).evaluate(_RECT)
    clip = {
        "x": math.ceil(rect["x"]),
        "y": math.ceil(rect["y"]),
        "width": math.floor(rect["width"]) - 1,
        "height": math.floor(rect["height"]) - 1,
    }
    assert clip["width"] > 0 and clip["height"] > 0, rect
    before = page.screenshot(path=str(artifacts / f"{phase}-before.png"), clip=clip)
    _emit_output(page, surface, socket)
    after = page.screenshot(path=str(artifacts / f"{phase}-after.png"), clip=clip)
    diff = ImageChops.difference(
        Image.open(io.BytesIO(before)).convert("RGB"),
        Image.open(io.BytesIO(after)).convert("RGB"),
    )
    assert diff.getbbox() is None, (
        f"Hidden terminal output changed visible pixels during {phase}; "
        f"compare {artifacts / f'{phase}-before.png'} and {artifacts / f'{phase}-after.png'}"
    )
    paint = surface.locator(_SLIDER).evaluate(_PAINT)
    assert not paint["displayed"] or paint["visibility"] != "visible" or paint["opacity"] == 0, (
        f"Hidden terminal slider can still paint: {paint}"
    )


@pytest.mark.parametrize("mobile", [True, False], ids=["mobile", "desktop"])
def test_hidden_terminal_scrollbar_never_paints_over_foreground(
    browser: Browser,
    terminal_scrollbar_session: tuple[str, str],
    output_path: str,
    mobile: bool,
) -> None:
    """Chat stays clean while the preserved terminal remains usable on demand."""
    if mobile and browser.browser_type.name == "firefox":
        pytest.skip("Playwright Firefox does not support mobile contexts")
    artifacts = Path(output_path)
    artifacts.mkdir(parents=True, exist_ok=True)
    base_url, session_id = terminal_scrollbar_session
    context = browser.new_context(
        viewport={"width": 402, "height": 714} if mobile else {"width": 1400, "height": 900},
        is_mobile=mobile,
        has_touch=mobile,
        color_scheme="light",
    )
    page = context.new_page()
    sockets = _mock_terminal_attach(page, [session_id])[session_id]
    try:
        page.goto(f"{base_url}/c/{session_id}")
        composer = page.get_by_label("Message the agent")
        expect(composer).to_be_visible(timeout=30_000)
        surface = page.get_by_test_id("main-terminal-view")
        expect(surface.get_by_test_id("terminal-view")).to_have_attribute(
            "data-state", "connected", timeout=20_000
        )
        expect(page.get_by_test_id("transcript-scrollbar-thumb")).to_be_visible()
        page.evaluate("document.fonts.ready")
        screen_size = surface.locator(".xterm-screen").evaluate(_RECT)
        assert len(sockets) == 1
        _assert_hidden_output(page, surface, sockets[0], artifacts, "initial-chat")
        expect(page.get_by_test_id("transcript-scrollbar-thumb")).to_be_visible()

        _select_view(page, "terminal")
        expect(surface).to_have_attribute("data-visible", "true")
        expect(surface).not_to_have_attribute("inert", "")
        assert surface.locator(".xterm-screen").evaluate(_RECT) == screen_size
        _emit_output(page, surface, sockets[0])
        slider = surface.locator(_SLIDER)
        assert slider.evaluate(_PAINT)["opacity"] > 0.99
        before_drag = slider.bounding_box()
        assert before_drag is not None
        x = before_drag["x"] + before_drag["width"] / 2
        y = before_drag["y"] + before_drag["height"] / 2
        assert page.evaluate(
            "({x, y}) => !!document.elementFromPoint(x, y)?.closest('.scrollbar .slider')",
            {"x": x, "y": y},
        ), "The terminal scrollbar must have a reachable drag target"
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x, y - 100, steps=10)
        page.mouse.up()
        page.wait_for_function(
            "({el, previous}) => el.getBoundingClientRect().y < previous - 30",
            arg={"el": slider.element_handle(), "previous": before_drag["y"]},
        )
        _select_view(page, "chat")
        page.mouse.move(0, 0)
        expect(composer).to_be_visible()
        _assert_hidden_output(page, surface, sockets[0], artifacts, "returned-chat")
        expect(page.get_by_test_id("transcript-scrollbar-thumb")).to_be_visible()
        assert len(sockets) == 1
        expect(surface.get_by_test_id("terminal-view")).to_have_attribute(
            "data-state", "connected"
        )
    finally:
        context.close()
