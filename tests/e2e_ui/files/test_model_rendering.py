"""E2E: 3D model files render through the FileViewer's <ModelViewer>.

A file the FileViewer classifies as a model (STL / 3MF / OBJ, via the shared
``getModelFormat`` resolver) must render as an interactive WebGL preview — a
``<canvas>`` under the ``aria-label="3D preview of …"`` host — not as
syntax-highlighted source nor the binary placeholder.

We seed ASCII STL and OBJ fixtures because the filesystem PUT endpoint can only
seed text (``str.encode(encoding)`` — base64 is not a text codec), and both
ASCII formats are valid UTF-8 that round-trips through that path. STL is routed
by its MIME type (``mimetypes.guess_type('x.stl')`` →
``application/vnd.ms-pki.stl``, a recognized model type); OBJ has no model MIME
(``application/x-tgif``) so it exercises the extension fallback. Seeded via the
filesystem PUT endpoint (no agent run).
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Iterator

import httpx
import pytest
from PIL import Image
from playwright.sync_api import Locator, Page, expect

# ---------------------------------------------------------------------------
# Test fixtures — tiny, valid single-triangle models
# ---------------------------------------------------------------------------

# Minimal ASCII STL: one triangle, so the mesh has finite, non-degenerate
# bounds and clears the viewer's ``hasRenderableBounds`` guard.
_ASCII_STL = """\
solid tri
  facet normal 0 0 1
    outer loop
      vertex 0 0 0
      vertex 1 0 0
      vertex 0 1 0
    endloop
  endfacet
endsolid tri
"""

# Minimal OBJ: three vertices and a face, likewise finite and drawable.
_OBJ_CONTENT = """\
v 0 0 0
v 1 0 0
v 0 1 0
f 1 2 3
"""

# Each case: (file_path, content). STL routes by MIME, OBJ by extension.
_MODELS = {
    "stl": ("part.stl", _ASCII_STL),
    "obj": ("mesh.obj", _OBJ_CONTENT),
}


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_model_session(
    seeded_session: tuple[str, str],
    request: pytest.FixtureRequest,
) -> Iterator[tuple[str, str, str]]:
    """Seed a model file and yield ``(base_url, session_id, path)``.

    :param seeded_session: Runner-bound (base_url, session_id) pair.
    :param request: Carries the ``(path, content)`` case via ``indirect``.
    :returns: ``(base_url, session_id, file_path)`` for the test body.
    """
    base_url, session_id = seeded_session
    file_path, content = request.param
    file_url = (
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{file_path}"
    )
    resp = httpx.put(
        file_url,
        json={"content": content, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    yield (base_url, session_id, file_path)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seeded_model_session",
    list(_MODELS.values()),
    ids=list(_MODELS),
    indirect=True,
)
def test_model_file_renders_as_3d_preview(
    page: Page,
    seeded_model_session: tuple[str, str, str],
) -> None:
    """A model file renders as a WebGL preview, not source or the placeholder."""
    base_url, session_id, file_path = seeded_model_session
    page.goto(f"{base_url}/c/{session_id}?view=explore")

    file_button = page.get_by_role("button", name=re.compile(rf"^{re.escape(file_path)}\b"))
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    # Two FileViewer instances mount with the same test id (mobile push-panel,
    # md:hidden, and the desktop rail). Match the visible one directly.
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()

    # The ModelViewer mounted: its canvas host carries the filename aria-label.
    preview = file_viewer.locator(f'[aria-label="3D preview of {file_path}"]')
    expect(preview).to_be_visible(timeout=15_000)

    # The WebGL scene built successfully — three.js appended a <canvas> and the
    # error overlay never showed (a parse or WebGL failure would surface it).
    expect(preview.locator("canvas")).to_be_visible(timeout=15_000)
    expect(file_viewer.get_by_text("Unable to render 3D model")).to_have_count(0)

    # It did NOT fall through to the binary placeholder or a source/editor view.
    expect(file_viewer.get_by_text("Preview not available for binary files")).to_have_count(0)
    expect(file_viewer.locator("[contenteditable='true']")).to_have_count(0)


# ---------------------------------------------------------------------------
# Canvas background follows the active theme
# ---------------------------------------------------------------------------

# Each case seeds the persisted Appearance preferences the Settings controls
# write. The custom case uses a saturated tint: the Custom derivation blends the
# tint most of the way toward white, so a near-white tint gives a pure-white pane.
_CUSTOM_THEME = {
    "basePalette": "omni",
    "accent": "#11171c",
    "darkAccent": "#e8ecf0",
    "tint": "#d9a441",
    "darkTint": "#0e1013",
    "contrast": 50,
    "translucentSidebar": False,
}
_THEMES: dict[str, tuple[str, dict[str, str]]] = {
    "gruvbox-light": ("light", {"omnigent:ui-theme-palette": json.dumps("gruvbox")}),
    "gruvbox-dark": ("dark", {"omnigent:ui-theme-palette": json.dumps("gruvbox")}),
    "custom-light": (
        "light",
        {
            "omnigent:ui-theme-palette": json.dumps("custom"),
            "omnigent:custom-theme": json.dumps(_CUSTOM_THEME),
        },
    ),
    "default-dark": ("dark", {}),
}

# The default palette's panel colours per mode. A themed pane must differ from
# these, or a canvas that matches it proves nothing.
_UNTINTED_PANE = {"light": (255, 255, 255), "dark": (14, 16, 19)}


def _apply_theme_preferences(page: Page, mode: str, extra: dict[str, str]) -> None:
    """Seed the Appearance localStorage keys the Settings controls write before the
    SPA boots; ``omnigent:default-workspace-panel`` opens the files rail on load."""
    store = {"web-theme": mode, "omnigent:default-workspace-panel": "open", **extra}
    page.add_init_script(
        ";".join(
            f"localStorage.setItem({json.dumps(k)}, {json.dumps(v)})" for k, v in store.items()
        )
    )


def _open_model_preview(page: Page, base_url: str, session_id: str, file_path: str) -> Locator:
    """Open ``file_path`` in the Explore file viewer and return its rendered canvas."""
    page.goto(f"{base_url}/c/{session_id}?view=explore")
    file_button = page.get_by_role("button", name=re.compile(rf"^{re.escape(file_path)}\b"))
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    preview = file_viewer.locator(f'[aria-label="3D preview of {file_path}"]')
    canvas = preview.locator("canvas")
    expect(canvas).to_be_visible(timeout=15_000)
    expect(file_viewer.get_by_text("Unable to render 3D model")).to_have_count(0)
    # Two animation frames guarantee the render loop has cleared the canvas at
    # least once; before that a fresh WebGL canvas is transparent.
    page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
    page.wait_for_timeout(1_000)
    return canvas


def _pixels_across_top_edge(
    page: Page, canvas: Locator
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Return ``(pane, canvas)`` RGB sampled 2px above and 2px inside the canvas's top
    edge, clearing the pane's 1px toolbar border."""
    box = canvas.bounding_box()
    assert box is not None
    x = round(box["x"] + box["width"] / 2)
    top = round(box["y"])
    image = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
    return image.getpixel((x, top - 2)), image.getpixel((x, top + 2))


@pytest.mark.parametrize("seeded_model_session", [_MODELS["stl"]], ids=["stl"], indirect=True)
@pytest.mark.parametrize("mode, extra", list(_THEMES.values()), ids=list(_THEMES))
def test_model_preview_canvas_matches_pane_background(
    request: pytest.FixtureRequest,
    seeded_model_session: tuple[str, str, str],
    mode: str,
    extra: dict[str, str],
) -> None:
    """The 3D preview canvas blends with the themed file pane instead of showing a box."""
    base_url, session_id, file_path = seeded_model_session
    page: Page = request.getfixturevalue("page")

    _apply_theme_preferences(page, mode, extra)
    canvas = _open_model_preview(page, base_url, session_id, file_path)

    assert page.evaluate("document.documentElement.classList.contains('dark')") == (mode == "dark")
    pane, canvas_pixel = _pixels_across_top_edge(page, canvas)
    assert pane != _UNTINTED_PANE[mode], f"pane is not themed: {pane}"

    assert all(abs(p - c) <= 2 for p, c in zip(pane, canvas_pixel, strict=True)), (
        f"3D preview canvas rgb{canvas_pixel} does not match the pane rgb{pane} above it"
    )
