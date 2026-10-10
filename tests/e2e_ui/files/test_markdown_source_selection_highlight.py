"""E2E: the Source-view selection wash must reach a heading's leading ``#`` markers,
with the bundled Geist Mono and with a copy that ligates ``###`` into a glyph whose
ink hangs left of its cell (see ``fixtures/build_hash_ligature_font.py``)."""

from __future__ import annotations

import io
import json
import os
import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from PIL import Image
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import open_right_rail, switch_markdown_view_mode

_MARKDOWN_FILE_PATH = "rollback_plan.md"
_HEADING_LINE = 182
_HEADING = "### Rollback SOP"
_PARAGRAPH = (
    "Expected flag rollback is **under 10 minutes**, plus normal pod-roll time for "
    "startup-evaluated flags. **TBD: validate this estimate in staging.**"
)

_FIXTURES = Path(__file__).with_name("fixtures")
_LIGATURE_FONT = _FIXTURES / "geist-mono-latin-hash-ligature.woff2"
_GEOMETRY_JS = (_FIXTURES / "source_selection_geometry.js").read_text()
_BUNDLED_MONO_FONT = re.compile(r".*/geist-mono-latin-wght-normal-[^/]*\.woff2$")


def _markdown_content() -> str:
    filler = [
        f"Line {n} of the rollout plan with some filler prose." for n in range(1, _HEADING_LINE)
    ]
    filler[0] = "# Rollout plan"
    return (
        "\n".join([*filler, _HEADING, "", _PARAGRAPH, "", "## Next steps", "- Follow up"]) + "\n"
    )


@pytest.fixture
def seeded_rollback_markdown_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    base_url, session_id = seeded_session
    file_url = (
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_MARKDOWN_FILE_PATH}"
    )
    resp = httpx.put(
        file_url,
        json={"content": _markdown_content(), "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    yield (base_url, session_id, _MARKDOWN_FILE_PATH)


def _serve_ligature_font(route: Route, served: list[str]) -> None:
    served.append(route.request.url)
    route.fulfill(body=_LIGATURE_FONT.read_bytes(), content_type="font/woff2")


def _is_dark(p: tuple[int, int, int]) -> bool:
    return 0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2] < 140


def _is_wash(p: tuple[int, int, int]) -> bool:
    # Light-theme ::selection wash is ~(252,234,244): pale pink with r>b>g and a
    # ~18-count spread. Anti-aliased selection-foreground ink on white fails one
    # of these, so halos around overhanging ink are not mistaken for wash.
    r, g, b = p
    return r >= 243 and b >= 232 and 222 <= g <= 242 and (r - g) >= 10 and r >= b >= g


@pytest.mark.browser_context_args(
    device_scale_factor=2, record_video_size={"width": 1600, "height": 900}
)
@pytest.mark.parametrize("font_mode", ["bundled", "ligature"])
def test_source_view_selection_wash_covers_heading_markers(
    request: pytest.FixtureRequest,
    seeded_rollback_markdown_session: tuple[str, str, str],
    tmp_path: Path,
    font_mode: str,
) -> None:
    base_url, session_id, _file_path = seeded_rollback_markdown_session
    # Open the (possibly recorded) page only once the session and file exist.
    page: Page = request.getfixturevalue("page")
    served_fonts: list[str] = []
    if font_mode == "ligature":
        page.route(_BUNDLED_MONO_FONT, lambda route: _serve_ligature_font(route, served_fonts))
    # The wash colour check below assumes the light palette.
    page.emulate_media(color_scheme="light")
    page.goto(f"{base_url}/c/{session_id}?view=explore")
    open_right_rail(page)

    file_button = page.get_by_role(
        "button", name=re.compile(rf"^{re.escape(_MARKDOWN_FILE_PATH)}\b")
    )
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()
    expect(file_viewer.locator("[contenteditable='true']")).to_be_visible(timeout=10_000)

    switch_markdown_view_mode(page, file_viewer, "Source")
    heading = file_viewer.locator(f'[data-line="{_HEADING_LINE}"]')
    expect(heading).to_contain_text(_HEADING, timeout=10_000)
    assert (
        page.evaluate("() => document.fonts.ready.then(() => document.fonts.status)") == "loaded"
    )
    if font_mode == "ligature":
        assert served_fonts, (
            "the bundled mono webfont was never requested; ligature copy not served"
        )
    paragraph = file_viewer.locator(f'[data-line="{_HEADING_LINE + 2}"]')
    expect(paragraph).to_contain_text("Expected flag rollback")
    heading.scroll_into_view_if_needed()
    expect(paragraph).to_be_in_viewport()

    # Drag from the first heading character to the end of the paragraph, as a
    # user highlighting the section would.
    start = heading.locator("span").first.bounding_box()
    end = paragraph.locator("span").last.bounding_box()
    assert start and end
    page.mouse.move(start["x"] + 1, start["y"] + start["height"] / 2)
    page.mouse.down()
    page.mouse.move(end["x"] + end["width"] - 1, end["y"] + end["height"] / 2, steps=12)
    page.mouse.up()
    page.wait_for_function(
        "heading => window.getSelection().toString().startsWith(heading)", arg=_HEADING
    )
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        # Hold the selected state on film long enough to read.
        page.wait_for_timeout(1_500)

    geometry = page.evaluate(_GEOMETRY_JS, [_HEADING_LINE, _HEADING_LINE + 2])
    geometry["sessionId"] = session_id
    assert geometry["selectedText"].startswith(_HEADING), geometry["selectedText"]

    viewer_box = file_viewer.bounding_box()
    assert viewer_box
    png = page.screenshot(clip=viewer_box)

    out_dir = tmp_path / font_mode
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "geometry.json").write_text(json.dumps(geometry, indent=2))
    (out_dir / "source-selection.png").write_bytes(png)

    # Pixel check on the heading row: the wash must start no later than the
    # first selected glyph.
    img = Image.open(io.BytesIO(png)).convert("RGB")
    dpr = geometry["devicePixelRatio"]
    hr = geometry["headingRect"]
    x0 = int((hr["x"] - viewer_box["x"]) * dpr)
    x1 = int((hr["right"] - viewer_box["x"]) * dpr)
    y0 = int((hr["y"] - viewer_box["y"]) * dpr)
    y1 = int((hr["y"] + hr["height"] - viewer_box["y"]) * dpr)
    px = img.load()
    # Measure by column: a genuine wash column spans most of the line height,
    # while the anti-aliased halo around overhanging ink is only a few pixels.
    wash_cols = [x for x in range(x0, x1) if sum(_is_wash(px[x, y]) for y in range(y0, y1)) >= 6]
    glyph_cols = [x for x in range(x0, x1) if sum(_is_dark(px[x, y]) for y in range(y0, y1)) >= 3]
    (out_dir / "pixels.json").write_text(
        json.dumps(
            {
                "heading_row_px": [x0, x1, y0, y1],
                "wash_x_min": min(wash_cols) if wash_cols else None,
                "wash_x_max": max(wash_cols) if wash_cols else None,
                "glyph_x_min": min(glyph_cols) if glyph_cols else None,
                "glyph_x_max": max(glyph_cols) if glyph_cols else None,
            },
            indent=2,
        )
    )
    assert wash_cols, "no selection wash painted on the heading row"
    assert glyph_cols, "no glyphs found on the heading row"
    assert min(wash_cols) <= min(glyph_cols) + 2, (
        f"selection wash starts at x={min(wash_cols)} but the first selected glyph ink is "
        f"at x={min(glyph_cols)} (device px): the highlight is clipped at the heading markers"
    )
    # Literal rendering: the three markers occupy three monospace cells.
    assert geometry["markerWidth"] >= 3 * geometry["cellWidth"] - 1, (
        f"'###' is laid out {geometry['markerWidth']:.1f}px wide against a "
        f"{geometry['cellWidth']:.1f}px cell: the markers were merged into fewer glyphs"
    )
