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

The Z-up print tests open a tall 3D-printing part (STL and 3MF, whose loaders
return geometry in the slicer's Z-up frame) and read the rendered canvas: the
part must stand upright, dragging vertically must keep orbiting past the
model's underside, and the faces turned toward the camera must stay lit while
the view swings under it. The 3MF container is a zip, so it is written into the
session workspace on disk rather than through the text-only PUT endpoint.
"""

from __future__ import annotations

import io
import math
import re
import subprocess
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageChops, ImageStat
from playwright.sync_api import Locator, Page, expect

from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import _ensure_runner_online, _server_state, open_right_rail

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
# Z-up print fixtures — a closed 2x2x10 box whose tall axis is +Z
# ---------------------------------------------------------------------------

_TOWER_STL_PATH = "tower.stl"
_TOWER_3MF_PATH = "tower.3mf"

_TOWER_VERTICES = [
    (0, 0, 0),
    (2, 0, 0),
    (2, 2, 0),
    (0, 2, 0),
    (0, 0, 10),
    (2, 0, 10),
    (2, 2, 10),
    (0, 2, 10),
]
# (outward normal, two counter-clockwise triangles) per face.
_TOWER_FACES = [
    ((0, 0, -1), [(0, 2, 1), (0, 3, 2)]),
    ((0, 0, 1), [(4, 5, 6), (4, 6, 7)]),
    ((0, -1, 0), [(0, 1, 5), (0, 5, 4)]),
    ((1, 0, 0), [(1, 2, 6), (1, 6, 5)]),
    ((0, 1, 0), [(2, 3, 7), (2, 7, 6)]),
    ((-1, 0, 0), [(3, 0, 4), (3, 4, 7)]),
]


def _tower_stl() -> str:
    lines = ["solid tower"]
    for normal, triangles in _TOWER_FACES:
        for triangle in triangles:
            lines.append(f"  facet normal {normal[0]} {normal[1]} {normal[2]}")
            lines.append("    outer loop")
            for index in triangle:
                x, y, z = _TOWER_VERTICES[index]
                lines.append(f"      vertex {x} {y} {z}")
            lines.append("    endloop")
            lines.append("  endfacet")
    lines.append("endsolid tower")
    return "\n".join(lines) + "\n"


def _tower_3mf() -> bytes:
    vertices = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in _TOWER_VERTICES)
    triangles = "".join(
        f'<triangle v1="{a}" v2="{b}" v3="{c}"/>'
        for _, face_triangles in _TOWER_FACES
        for a, b, c in face_triangles
    )
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" xml:lang="en-US"'
        ' xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        '<resources><object id="1" type="model"><mesh>'
        f"<vertices>{vertices}</vertices><triangles>{triangles}</triangles>"
        "</mesh></object></resources>"
        '<build><item objectid="1"/></build></model>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel0"'
        ' Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
        "</Relationships>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels"'
        ' ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="model"'
        ' ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
        "</Types>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in (
            ("[Content_Types].xml", content_types),
            ("_rels/.rels", rels),
            ("3D/3dmodel.model", model),
        ):
            # Fixed timestamps keep the archive bytes identical across runs.
            archive.writestr(zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0)), text)
    return buffer.getvalue()


_PRINT_AGENT_YAML = """\
name: print_project
prompt: You are a friendly assistant. Say hello and answer questions.

executor:
  model: gpt-4o-mini
  harness: openai-agents

os_env:
  type: caller_process
  cwd: {workspace}
  sandbox:
    type: none
"""


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


@pytest.fixture
def zup_print_session(
    live_server: str,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A runner-bound session whose workspace holds the Z-up tower as STL and 3MF."""
    workspace = tmp_path / "prints"
    workspace.mkdir()
    (workspace / _TOWER_3MF_PATH).write_bytes(_tower_3mf())

    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    bundle = bundle_files(
        {"print_project.yaml": _PRINT_AGENT_YAML.format(workspace=workspace).encode()}
    )
    create = post_session_bundle(httpx.post, f"{live_server}/v1/sessions", bundle, timeout=30.0)
    create.raise_for_status()
    session_id = create.json()["session_id"]
    bind_session_runner(httpx.patch, live_server, session_id, runner_id, timeout=10.0)
    try:
        httpx.put(
            f"{live_server}/v1/sessions/{session_id}"
            f"/resources/environments/default/filesystem/{_TOWER_STL_PATH}",
            json={"content": _tower_stl(), "encoding": "utf-8"},
            timeout=10.0,
        ).raise_for_status()
        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)


# ---------------------------------------------------------------------------
# Canvas helpers
# ---------------------------------------------------------------------------

# Channel distance beyond which a pixel differs from the canvas background or
# from the same pixel in an earlier frame (anti-aliasing noise stays below it).
_PIXEL_TOLERANCE = 24
# Fraction of changed canvas pixels that counts as the view having rotated.
_ROTATION_VISIBLE = 0.01
_SETTLE_TIMEOUT_S = 10.0
_CONTROL_DRAG_PX = 70
_EXTRA_DRAG_PX = 300
_UNDERSIDE_DRAGS = 6
_LIT_LUMINANCE_MIN = 60


def _open_model_preview(page: Page, base_url: str, session_id: str, file_path: str) -> Locator:
    """Open ``file_path`` from the Files tab and return its 3D preview canvas."""
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Files")).click()
    file_button = rail.get_by_role("button", name=re.compile(rf"^{re.escape(file_path)}\b"))
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    canvas = file_viewer.locator(f'[aria-label="3D preview of {file_path}"] canvas')
    expect(canvas).to_be_visible(timeout=15_000)
    expect(file_viewer.get_by_text("Unable to render 3D model")).to_have_count(0)
    return canvas


def _artifacts(output_path: str) -> Path:
    artifacts = Path(output_path)
    artifacts.mkdir(parents=True, exist_ok=True)
    return artifacts


def _capture(page: Page, canvas: Locator) -> Image.Image:
    box = canvas.bounding_box()
    assert box is not None
    clip = {
        "x": math.ceil(box["x"]),
        "y": math.ceil(box["y"]),
        "width": math.floor(box["width"]) - 1,
        "height": math.floor(box["height"]) - 1,
    }
    return Image.open(io.BytesIO(page.screenshot(clip=clip))).convert("RGB")


def _exceeds_tolerance(difference: Image.Image) -> Image.Image:
    red, green, blue = difference.split()
    furthest = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    return furthest.point(lambda value: 255 if value > _PIXEL_TOLERANCE else 0)


def _changed_fraction(before: Image.Image, after: Image.Image) -> float:
    changed = _exceeds_tolerance(ImageChops.difference(before, after))
    return changed.histogram()[255] / (changed.width * changed.height)


def _settled_frame(page: Page, canvas: Locator) -> Image.Image:
    """Capture the canvas once OrbitControls damping has stopped moving it."""
    previous = _capture(page, canvas)
    deadline = time.monotonic() + _SETTLE_TIMEOUT_S
    while time.monotonic() < deadline:
        page.wait_for_timeout(400)
        current = _capture(page, canvas)
        if _changed_fraction(previous, current) < 0.001:
            return current
        previous = current
    return previous


def _model_mask(frame: Image.Image) -> Image.Image:
    colors = frame.getcolors(frame.width * frame.height)
    assert colors is not None
    background = max(colors, key=lambda entry: entry[0])[1]
    return _exceeds_tolerance(
        ImageChops.difference(frame, Image.new("RGB", frame.size, background))
    )


def _silhouette_size(frame: Image.Image) -> tuple[int, int]:
    bbox = _model_mask(frame).getbbox()
    assert bbox is not None, "no model pixels on the canvas"
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _model_luminance(frame: Image.Image) -> float:
    return ImageStat.Stat(frame.convert("L"), _model_mask(frame)).mean[0]


def _drag_up(page: Page, canvas: Locator, distance: float) -> None:
    box = canvas.bounding_box()
    assert box is not None
    x = box["x"] + box["width"] / 2
    start_y = min(box["y"] + box["height"] / 2 + distance / 2, box["y"] + box["height"] - 2)
    page.mouse.move(x, start_y)
    page.mouse.down()
    page.mouse.move(x, start_y - distance, steps=12)
    page.mouse.up()


def _long_drag_up(page: Page, canvas: Locator) -> None:
    box = canvas.bounding_box()
    assert box is not None
    _drag_up(page, canvas, box["height"] * 0.8)


def _drag_to_underside(page: Page, canvas: Locator) -> None:
    """Drag upward several canvas heights so the view swings under the model."""
    for _ in range(_UNDERSIDE_DRAGS):
        _long_drag_up(page, canvas)


# ---------------------------------------------------------------------------
# Tests
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


@pytest.mark.parametrize("file_path", [_TOWER_STL_PATH, _TOWER_3MF_PATH], ids=["stl", "3mf"])
def test_zup_print_opens_upright(
    request: pytest.FixtureRequest,
    zup_print_session: tuple[str, str],
    output_path: str,
    file_path: str,
) -> None:
    """A Z-up print opens standing on end rather than lying on its side."""
    base_url, session_id = zup_print_session
    page: Page = request.getfixturevalue("page")
    canvas = _open_model_preview(page, base_url, session_id, file_path)

    frame = _settled_frame(page, canvas)
    frame.save(_artifacts(output_path) / f"{file_path}-opened.png")
    width, height = _silhouette_size(frame)
    assert height > width, (
        f"{file_path} renders as a {width}x{height} px silhouette: the print lies on its side"
    )


def test_vertical_drag_keeps_rotating_past_the_pole(
    request: pytest.FixtureRequest,
    zup_print_session: tuple[str, str],
    output_path: str,
) -> None:
    """Dragging upward keeps orbiting under the model instead of stopping at a pole."""
    base_url, session_id = zup_print_session
    page: Page = request.getfixturevalue("page")
    canvas = _open_model_preview(page, base_url, session_id, _TOWER_STL_PATH)
    initial = _settled_frame(page, canvas)

    _drag_up(page, canvas, _CONTROL_DRAG_PX)
    after_control = _settled_frame(page, canvas)
    assert _changed_fraction(initial, after_control) > _ROTATION_VISIBLE, (
        "the control drag did not rotate the view"
    )

    _drag_to_underside(page, canvas)
    at_pole = _settled_frame(page, canvas)
    _drag_up(page, canvas, _EXTRA_DRAG_PX)
    after_extra = _settled_frame(page, canvas)
    artifacts = _artifacts(output_path)
    at_pole.save(artifacts / "before-extra-drag.png")
    after_extra.save(artifacts / "after-extra-drag.png")
    assert _changed_fraction(at_pole, after_extra) > _ROTATION_VISIBLE, (
        "a further upward drag left the view unchanged: rotation stopped at the pole"
    )


def test_camera_facing_surfaces_stay_lit_under_the_model(
    request: pytest.FixtureRequest,
    zup_print_session: tuple[str, str],
    output_path: str,
) -> None:
    """Faces turned toward the camera stay lit as the view swings under the model."""
    base_url, session_id = zup_print_session
    page: Page = request.getfixturevalue("page")
    canvas = _open_model_preview(page, base_url, session_id, _TOWER_STL_PATH)
    initial = _settled_frame(page, canvas)
    lit = _model_luminance(initial)
    assert lit > _LIT_LUMINANCE_MIN, f"initial view is not lit ({lit:.0f}/255)"

    artifacts = _artifacts(output_path)
    initial.save(artifacts / "lighting-initial.png")
    darkest = lit
    for index in range(_UNDERSIDE_DRAGS):
        _long_drag_up(page, canvas)
        frame = _settled_frame(page, canvas)
        frame.save(artifacts / f"lighting-after-drag-{index + 1}.png")
        darkest = min(darkest, _model_luminance(frame))
    assert darkest > lit / 2, (
        f"model luminance fell from {lit:.0f} to {darkest:.0f}/255 while swinging under the model"
    )
