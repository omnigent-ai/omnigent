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
The filesystem PUT is text-only: latin-1 is rejected because the writer
decodes as UTF-8. The binary 3MF is written to the workspace root returned
by the environment API, then checked byte-for-byte through the raw GET.
Coloured cases skip if that root is not local: binary seeding needs a same-host workspace.
"""

from __future__ import annotations

import io
import json
import re
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from PIL import Image
from playwright.sync_api import Page, expect

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


def _coloured_3mf() -> bytes:
    """Build five separated Bambu parts without committing binary fixtures."""
    palette = ["#F53B9D", "#4DC5A0", "#212329", "#FF7A18", "#FEFEFE"]
    namespace = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
    production = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
    triangles = [
        (0, 2, 1),
        (0, 3, 2),
        (4, 5, 6),
        (4, 6, 7),
        (0, 1, 5),
        (0, 5, 4),
        (1, 2, 6),
        (1, 6, 5),
        (2, 3, 7),
        (2, 7, 6),
        (3, 0, 4),
        (3, 4, 7),
    ]
    parts = []
    components = []
    for index in range(5):
        vertices = [
            (0, 0, 0),
            (22, 0, 0),
            (22, 22, 0),
            (0, 22, 0),
            (0, 0, 35),
            (22, 0, 35),
            (22, 22, 35),
            (0, 22, 35),
        ]
        mesh = (
            "<mesh><vertices>"
            + "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in vertices)
            + "</vertices><triangles>"
            + "".join(
                f'<triangle v1="{first}" v2="{second}" v3="{third}"/>'
                for first, second, third in triangles
            )
            + "</triangles></mesh>"
        )
        parts.append(f'<object id="{index + 1}" type="model">{mesh}</object>')
        components.append(
            f'<component objectid="{index + 1}" transform="1 0 0 0 1 0 0 0 1 {index * 34} 0 0"/>'
        )
    root = (
        f'<model xmlns="{namespace}" xmlns:p="{production}" unit="millimeter">'
        f'<resources>{"".join(parts)}<object id="100" type="model"><components>'
        f"{''.join(components)}</components></object></resources>"
        '<build><item objectid="100"/></build></model>'
    )
    settings = (
        '<config><object id="100">'
        + "".join(
            f'<part id="{index + 1}" subtype="normal_part">'
            f'<metadata key="extruder" value="{index + 1}"/></part>'
            for index in range(5)
        )
        + "</object></config>"
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="r" Target="/3D/3dmodel.model" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
            "</Relationships>",
        )
        archive.writestr("3D/3dmodel.model", root)
        archive.writestr("Metadata/model_settings.config", settings)
        archive.writestr(
            "Metadata/project_settings.config", json.dumps({"filament_colour": palette})
        )
    return output.getvalue()


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


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_coloured_3mf_keeps_filament_hues_across_themes(
    page: Page,
    seeded_session: tuple[str, str],
    theme: str,
) -> None:
    """Filament hues survive initial themes and a live Settings theme toggle."""
    base_url, session_id = seeded_session
    file_path = "bambu-five-colours.3mf"
    blob = _coloured_3mf()
    file_url = (
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{file_path}"
    )
    # A filesystem request initializes the session workspace before metadata is read.
    response = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem",
        timeout=10.0,
    )
    response.raise_for_status()
    response = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default", timeout=10.0
    )
    response.raise_for_status()
    workspace = Path(response.json()["metadata"]["root"])
    if not workspace.is_absolute() or not workspace.is_dir():
        pytest.skip("binary seeding needs a same-host workspace; filesystem PUT is text-only")
    (workspace / file_path).write_bytes(blob)
    response = httpx.get(file_url, params={"download": "true"}, timeout=10.0)
    response.raise_for_status()
    assert response.content == blob
    page.add_init_script(f"localStorage.setItem('web-theme', '{theme}')")
    page.goto(f"{base_url}/c/{session_id}?view=explore")
    file_button = page.get_by_role("button", name=re.compile(rf"^{re.escape(file_path)}\b"))
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()
    viewer = page.locator('[data-testid="file-viewer"]:visible')
    canvas = viewer.locator(f'[aria-label="3D preview of {file_path}"] canvas')
    expect(canvas).to_be_visible(timeout=15_000)

    def assert_hues() -> None:
        deadline = time.monotonic() + 10
        counts = {"pink": 0, "mint": 0, "orange": 0}
        while time.monotonic() < deadline:
            image = Image.open(io.BytesIO(canvas.screenshot())).convert("RGB")
            counts = {"pink": 0, "mint": 0, "orange": 0}
            for red, green, blue in image.get_flattened_data():
                if red > 100 and red > green * 1.35 and blue > green * 1.15:
                    counts["pink"] += 1
                if green > 75 and green > red * 1.2 and green > blue * 1.07:
                    counts["mint"] += 1
                if red > 100 and red > green * 1.35 and green > blue * 1.4:
                    counts["orange"] += 1
            if min(counts.values()) > 40:
                return
        pytest.fail(f"Missing filament hues in {theme} theme: {counts}")

    assert_hues()
    original_canvas = canvas.element_handle()
    settings = page.context.new_page()
    settings.goto(f"{base_url}/settings/appearance")
    modes = settings.get_by_role("radiogroup", name="Mode", exact=True)
    for selected in ["dark" if theme == "light" else "light", theme]:
        modes.get_by_role("radio", name=selected.title(), exact=True).click()
        expect(page.locator("html")).to_have_class(
            re.compile(r"\bdark\b") if selected == "dark" else re.compile(r"^(?!.*\bdark\b).*$")
        )
        assert canvas.evaluate("(element, original) => element === original", original_canvas)
        assert_hues()
    settings.close()
    expect(viewer.get_by_text("Unable to render 3D model")).to_have_count(0)
