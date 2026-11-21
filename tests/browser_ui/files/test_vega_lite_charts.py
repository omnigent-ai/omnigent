"""Vega-Lite chart contracts in real Chromium: the lazy renderer, tooltips and PNG export."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Page, Request, expect

from tests.browser_ui.files import test_file_line_navigation as session_contract
from tests.browser_ui.files.test_file_line_navigation import (
    BrowserSession,
    _mock_markdown_files,
    _open_viewer,
    _post_message,
)

seeded_session = session_contract.seeded_session

_REMOTE_IMAGE = "https://charts.example.test/pixel.png"
_SPEC = {
    "data": {"values": [{"region": "North", "sales": 28}, {"region": "South", "sales": 55}]},
    "layer": [
        {
            "mark": {"type": "bar", "tooltip": True},
            "encoding": {
                "x": {"field": "region", "type": "nominal"},
                "y": {"field": "sales", "type": "quantitative"},
            },
        },
        {
            # An image mark and a link name remote files; the chart must not load them.
            "mark": {"type": "image", "width": 10, "height": 10, "href": _REMOTE_IMAGE},
            "encoding": {
                "x": {"field": "region", "type": "nominal"},
                "y": {"field": "sales", "type": "quantitative"},
                "url": {"value": _REMOTE_IMAGE},
            },
        },
    ],
}


def _record_remote_requests(page: Page, session: BrowserSession) -> list[str]:
    """Every request the page makes to a host other than the app's."""
    app = urlparse(session.contract.base_url)
    requests: list[str] = []

    def record(request: Request) -> None:
        url = urlparse(request.url)
        if url.scheme in {"http", "https"} and url.netloc != app.netloc:
            requests.append(request.url)

    page.on("request", record)
    return requests


def test_chat_fence_draws_interactive_chart_and_exports_png(
    page: Page, seeded_session: BrowserSession
) -> None:
    """A vega-lite fence draws bars with tooltips, loads nothing remote and saves a PNG."""
    seeded_session.features["vega_lite_charts"] = True
    remote = _record_remote_requests(page, seeded_session)
    _post_message(seeded_session, f"Sales:\n\n```vega-lite\n{json.dumps(_SPEC)}\n```")
    page.set_viewport_size({"width": 1400, "height": 1000})

    page.goto(f"{seeded_session.contract.base_url}/c/{seeded_session.session_id}")

    chart = page.get_by_test_id("vega-lite-chart")
    bars = chart.locator("g.mark-rect path")
    expect(bars).to_have_count(2, timeout=30_000)
    bars.first.hover()
    tooltip = page.get_by_role("tooltip").filter(has_text="region")
    expect(tooltip).to_contain_text("North")
    expect(tooltip).to_contain_text("28")

    with page.expect_download() as download_info:
        chart.get_by_role("button", name="Download PNG").click()
    download = download_info.value
    assert download.suggested_filename == "chart.png"
    assert Path(download.path()).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert remote == []


def test_chart_file_previews_as_chart_with_source_toggle(
    page: Page, seeded_session: BrowserSession
) -> None:
    """A .vl.json file opens as a chart, and View source shows its JSON."""
    seeded_session.features["vega_lite_charts"] = True
    path = "src/sales.vl.json"
    _mock_markdown_files(page, seeded_session, {path: json.dumps(_SPEC, indent=2)})

    viewer = _open_viewer(page, seeded_session, path, {"previewableViewMode": "preview"})

    expect(viewer.get_by_test_id("vega-lite-chart").locator("g.mark-rect path")).to_have_count(
        2, timeout=30_000
    )
    viewer.get_by_role("button", name="View source").click()
    expect(viewer.locator(".monaco-editor")).to_be_visible(timeout=30_000)
    expect(viewer.get_by_test_id("vega-lite-chart")).to_have_count(0)
