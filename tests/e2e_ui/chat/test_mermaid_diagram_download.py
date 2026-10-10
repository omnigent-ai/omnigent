"""E2E: a Mermaid diagram's Download menu saves usable SVG and PNG files.

Mermaid hands Streamdown an HTML-serialised SVG, so multi-line node labels carry
bare ``<br>`` (and ``&nbsp;``) that no XML parser accepts. The toolbar must still
save a ``diagram.svg`` the browser renders completely and a ``diagram.png``; the
PNG export rasterises the same markup through ``<img>``, so it fails with the
SVG. Seeded assistant message, no LLM turn.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

_AGENT_NAME = "hello_world"

_NODE_COUNT = 4
_MULTILINE_LABEL_FLOWCHART = (
    "Here is the ingestion flow:\n\n"
    "```mermaid\n"
    "flowchart LR\n"
    '  A["Databricks machines<br/>every process;<br/>file, network, DNS<br/>event"]'
    ' --> B["Collector<br/>normalize + enrich"]\n'
    '  B --> C["camera footage: raw<br/>telemetry (FDR)"]\n'
    '  B --> D["radio calls: EPP<br/>detections"]\n'
    "```\n"
)

_DIAGRAM_SVG = '[data-streamdown="mermaid-block"] svg[aria-roledescription]'
_XML_ERROR_BANNER = "This page contains the following errors"


def _seed_diagram_message(base_url: str, session_id: str) -> None:
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {"agent": _AGENT_NAME, "text": _MULTILINE_LABEL_FLOWCHART},
        },
        timeout=10.0,
    ).raise_for_status()


def _open_rendered_diagram(page: Page, base_url: str, session_id: str) -> None:
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.locator(_DIAGRAM_SVG)).to_be_visible(timeout=30_000)


def _open_download_menu(page: Page) -> None:
    page.locator('[data-streamdown="mermaid-block-actions"]').get_by_title(
        "Download diagram"
    ).click()
    expect(page.get_by_title("Download diagram as SVG")).to_be_visible()


def _xml_parse_error(svg: str) -> str | None:
    try:
        ET.fromstring(svg)
    except ET.ParseError as exc:
        return str(exc)
    return None


def test_downloaded_svg_opens_as_a_complete_diagram(
    request: pytest.FixtureRequest, seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    """The saved SVG is well-formed and the browser draws every node of it."""
    base_url, session_id = seeded_session
    _seed_diagram_message(base_url, session_id)

    page: Page = request.getfixturevalue("page")
    _open_rendered_diagram(page, base_url, session_id)
    _open_download_menu(page)
    with page.expect_download(timeout=10_000) as download_info:
        page.get_by_title("Download diagram as SVG").click()
    svg_path = tmp_path / download_info.value.suggested_filename
    download_info.value.save_as(svg_path)
    parse_error = _xml_parse_error(svg_path.read_text(encoding="utf-8"))

    # Open the saved file the way a user would: as a page in the same browser.
    page.goto(svg_path.as_uri())
    page.wait_for_load_state("load")
    error_banner_shown = page.get_by_text(_XML_ERROR_BANNER).count() > 0
    rendered_labels = page.locator(".nodeLabel").count()
    assert parse_error is None and not error_banner_shown and rendered_labels == _NODE_COUNT, (
        f"downloaded SVG is not a usable diagram: XML parse error {parse_error!r}; "
        f"browser error page shown: {error_banner_shown}; "
        f"{rendered_labels} of {_NODE_COUNT} node labels drawn"
    )


def test_png_download_saves_a_file(
    request: pytest.FixtureRequest, seeded_session: tuple[str, str]
) -> None:
    """Choosing PNG starts a diagram.png download instead of silently doing nothing."""
    base_url, session_id = seeded_session
    _seed_diagram_message(base_url, session_id)

    page: Page = request.getfixturevalue("page")
    _open_rendered_diagram(page, base_url, session_id)
    _open_download_menu(page)
    try:
        with page.expect_download(timeout=10_000) as download_info:
            page.get_by_title("Download diagram as PNG").click()
    except PlaywrightTimeoutError:
        menu_still_open = page.get_by_title("Download diagram as PNG").is_visible()
        pytest.fail(
            "choosing PNG started no download within 10 s and showed no error "
            f"(format menu still open: {menu_still_open})"
        )
    assert download_info.value.suggested_filename.endswith(".png")
