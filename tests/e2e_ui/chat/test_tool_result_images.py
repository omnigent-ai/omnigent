"""E2E: the opt-in tool-result image previews across the server, runner and SPA.

A real ``sys_os_read`` of a workspace PNG shows the file under its row, read
back through the session filesystem API; a real MCP tool returning an image
block shows those bytes. With the setting off neither preview renders.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageDraw
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import (
    _create_bundled_session,
    configure_mock_llm,
    set_fallback_mock_llm,
)

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_TRIGGER = '[data-slot="collapsible-trigger"]'
_VIEWPORT = {"width": 1280, "height": 800}
_SCREENSHOT = "settings-dark.png"
_READ_REPLY = "The dark-mode settings page looks right: the cards keep their contrast."
_MCP_REPLY = "The preview shows the cards rendering in dark mode."
# Long enough for the live row to be seen before the turn settles and folds.
_REPLY_DELAY_S = 20.0

_AGENT_YAML = """\
spec_version: 1
name: {name}
prompt: |
  You are a deterministic test assistant. You inspect screenshots with the
  tools you are given, then reply with one short sentence.

executor:
  model: {model}
  config:
    harness: openai-agents

os_env:
  type: caller_process
  cwd: {cwd}
  sandbox:
    type: none
{tools}"""

_MCP_TOOLS_YAML = """
tools:
  design:
    type: mcp
    command: {python}
    args:
      - {server}
      - {image}
"""

# A stdio MCP server whose one tool returns a text block and a PNG image block.
_MCP_SERVER_CODE = '''\
import base64
import sys

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent

mcp = FastMCP("design")


@mcp.tool()
def capture_preview() -> list[TextContent | ImageContent]:
    """Capture the settings page preview."""
    with open(sys.argv[1], "rb") as image:
        data = base64.b64encode(image.read()).decode()
    return [
        TextContent(type="text", text="Captured the settings page in dark mode."),
        ImageContent(type="image", data=data, mimeType="image/png"),
    ]


mcp.run()
'''


def _draw_dark_mode_screenshot(path: Path) -> None:
    """Write a small PNG that looks like a dark-mode settings page."""
    image = Image.new("RGB", (640, 400), "#111827")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 640, 48), fill="#1f2937")
    draw.text((20, 18), "Settings  ·  Appearance", fill="#f9fafb")
    for row, label in enumerate(("Theme: Dark", "Font size: 14", "Chat defaults")):
        top = 80 + row * 100
        draw.rounded_rectangle((20, top, 620, top + 80), radius=10, fill="#374151")
        draw.text((40, top + 32), label, fill="#e5e7eb")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def _tool_call(call_id: str, name: str, arguments: dict[str, str]) -> dict[str, object]:
    return {"tool_calls": [{"call_id": call_id, "name": name, "arguments": json.dumps(arguments)}]}


SessionFactory = Callable[[str, dict[str, object], str, bool], tuple[str, Path]]


@pytest.fixture
def image_session(
    live_server: str,
    runner_id: str,
    mock_llm_server_url: str,
) -> Iterator[SessionFactory]:
    """Create runner-bound sessions whose turn calls one scripted tool.

    :returns: A factory ``(tool_name, arguments, reply, with_mcp) -> (session_id, workspace)``.
    """
    sessions: list[str] = []
    workspaces: list[Path] = []

    def create(
        tool_name: str, arguments: dict[str, object], reply: str, with_mcp: bool
    ) -> tuple[str, Path]:
        workspace = Path(tempfile.mkdtemp(prefix="omnigent-e2e-tool-images-"))
        workspaces.append(workspace)
        image = workspace / "shots" / _SCREENSHOT
        _draw_dark_mode_screenshot(image)
        tools = ""
        if with_mcp:
            server = workspace / "design_mcp.py"
            server.write_text(_MCP_SERVER_CODE)
            tools = _MCP_TOOLS_YAML.format(python=sys.executable, server=server, image=image)
        model = f"tool-images-probe-{uuid.uuid4().hex[:8]}"
        resolved = {key: str(value).format(image=image) for key, value in arguments.items()}
        configure_mock_llm(
            mock_llm_server_url,
            [
                _tool_call("call_inspect_screenshot", tool_name, resolved),
                {"text": reply, "delay": _REPLY_DELAY_S},
            ],
            key=model,
        )
        set_fallback_mock_llm(mock_llm_server_url, model, reply)
        yaml_text = _AGENT_YAML.format(
            name=f"tool_images_probe_{uuid.uuid4().hex[:8]}",
            model=model,
            cwd=str(workspace),
            tools=tools,
        )
        session_id = _create_bundled_session(
            live_server, runner_id, yaml_text, operator_authored=True
        )
        sessions.append(session_id)
        return session_id, workspace

    try:
        yield create
    finally:
        try:
            for session_id in sessions:
                with contextlib.suppress(httpx.HTTPError):
                    httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        finally:
            for workspace in workspaces:
                shutil.rmtree(workspace, ignore_errors=True)


def _hold_for_recording(page: Page, milliseconds: int = 1_500) -> None:
    """Leave a visible state on screen long enough to read in a demo recording."""
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(milliseconds)


def _set_tool_result_images(page: Page, *, enabled: bool) -> None:
    """Flip Settings → Appearance → "Show images from tool results" from the sidebar."""
    page.get_by_role("link", name="Settings", exact=True).click()
    page.get_by_test_id("settings-nav-appearance").click()
    toggle = page.get_by_test_id("show-tool-result-images-toggle")
    toggle.scroll_into_view_if_needed(timeout=20_000)
    expect(page.get_by_text("Show images from tool results", exact=True)).to_be_visible()
    expect(toggle).to_have_attribute("aria-checked", "false" if enabled else "true")
    _hold_for_recording(page)
    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "true" if enabled else "false")
    _hold_for_recording(page)
    page.get_by_role("link", name="Back", exact=True).click()


def _send(page: Page, text: str) -> None:
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _open_settled_work(page: Page) -> None:
    """Open the settled turn's Worked fold, and its tool group when the run folded."""
    worked = page.get_by_test_id("turn-worked-fold")
    expect(worked).to_be_visible(timeout=30_000)
    worked.locator(_TRIGGER).first.click()
    group = worked.get_by_role("button", name=re.compile(r"^(Read \d+ files?|Called \d+ tools?)$"))
    # The fold's first inner trigger is either the tool row or its group summary.
    expect(worked.locator(_TRIGGER).nth(1)).to_be_visible(timeout=10_000)
    if group.count() > 0 and group.first.get_attribute("aria-expanded") != "true":
        group.first.click()


def _expect_decoded_width(image: Locator, width: int) -> None:
    """Wait for *image* to finish decoding, then check it shows the real picture."""
    assert image.evaluate("img => img.decode().then(() => img.naturalWidth)") == width


def _preview_for(page: Page, row_text: str) -> tuple[Locator, Locator]:
    """The tool row naming *row_text* and the preview strip beside it."""
    row = page.locator(_TRIGGER, has_text=row_text).first
    previews = row.locator("xpath=..").get_by_test_id("tool-result-images")
    return row, previews


@pytest.mark.timeout(300)
@pytest.mark.browser_context_args(viewport=_VIEWPORT, record_video_size=_VIEWPORT)
def test_read_image_previews_under_its_row(
    request: pytest.FixtureRequest,
    live_server: str,
    image_session: SessionFactory,
) -> None:
    """A read workspace image shows under its row, zooms, survives reload and hides when off."""
    session_id, workspace = image_session("sys_os_read", {"path": "{image}"}, _READ_REPLY, False)
    # Open the recorded page only after the session and runner are ready.
    page: Page = request.getfixturevalue("page")
    page.goto(f"{live_server}/c/{session_id}")
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)
    _set_tool_result_images(page, enabled=True)

    _send(page, "Check the dark-mode screenshot of the settings page.")
    # The live row shows the preview without being expanded.
    row, previews = _preview_for(page, _SCREENSHOT)
    image = previews.get_by_role("img", name=_SCREENSHOT)
    expect(image).to_be_visible(timeout=60_000)
    expect(row).to_have_attribute("aria-expanded", "false")
    expect(image).to_have_attribute("src", re.compile(r"^blob:"))
    _expect_decoded_width(image, 640)
    expect(page.locator(_ASSISTANT, has_text=_READ_REPLY)).to_have_count(0)
    _hold_for_recording(page, 2_500)

    previews.get_by_role("button", name=f"Zoom image: {_SCREENSHOT}").click()
    expect(page.get_by_role("dialog")).to_be_visible(timeout=10_000)
    _hold_for_recording(page, 2_000)
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0, timeout=10_000)

    expect(page.locator(_ASSISTANT, has_text=_READ_REPLY).first).to_be_visible(timeout=90_000)
    assert (workspace / "shots" / _SCREENSHOT).exists()

    # The stored output holds no image bytes; a reload reads the file again.
    page.reload()
    expect(page.get_by_test_id("turn-worked-fold")).to_be_visible(timeout=30_000)
    # The settled turn is folded, so its preview stays hidden until opened.
    expect(page.get_by_test_id("tool-result-images")).to_have_count(0)
    _open_settled_work(page)
    _row, previews = _preview_for(page, _SCREENSHOT)
    expect(previews.get_by_role("img", name=_SCREENSHOT)).to_be_visible(timeout=30_000)
    _hold_for_recording(page, 2_500)

    _set_tool_result_images(page, enabled=False)
    _open_settled_work(page)
    row, previews = _preview_for(page, _SCREENSHOT)
    expect(row).to_be_visible(timeout=30_000)
    expect(previews).to_have_count(0)
    _hold_for_recording(page, 2_500)


@pytest.mark.timeout(300)
@pytest.mark.browser_context_args(viewport=_VIEWPORT, record_video_size=_VIEWPORT)
def test_mcp_image_result_shows_inline(
    request: pytest.FixtureRequest,
    live_server: str,
    image_session: SessionFactory,
) -> None:
    """An MCP tool's returned image block renders under its row from the stored output."""
    session_id, _workspace = image_session("design__capture_preview", {}, _MCP_REPLY, True)
    page: Page = request.getfixturevalue("page")
    page.goto(f"{live_server}/c/{session_id}")
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)
    _set_tool_result_images(page, enabled=True)

    _send(page, "Capture a preview of the settings page and check it.")
    row, previews = _preview_for(page, "Capture preview")
    image = previews.get_by_role("img", name="Tool result image")
    expect(image).to_be_visible(timeout=60_000)
    expect(row).to_have_attribute("aria-expanded", "false")
    expect(image).to_have_attribute("src", re.compile(r"^data:image/png;base64,"))
    _expect_decoded_width(image, 640)
    _hold_for_recording(page, 2_500)

    expect(page.locator(_ASSISTANT, has_text=_MCP_REPLY).first).to_be_visible(timeout=90_000)

    # The image bytes are part of the stored tool output, so a reload still shows them.
    page.reload()
    _open_settled_work(page)
    _row, previews = _preview_for(page, "Capture preview")
    image = previews.get_by_role("img", name="Tool result image")
    expect(image).to_be_visible(timeout=30_000)
    expect(image).to_have_attribute("src", re.compile(r"^data:image/png;base64,"))
    _hold_for_recording(page, 3_000)
