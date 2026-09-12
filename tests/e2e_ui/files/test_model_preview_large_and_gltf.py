"""E2E: 3D preview works for large (>10 MiB) models and glTF files.

The workspace FileViewer routes 3D models through ``<ModelViewer>`` (see
``test_model_rendering.py``), but two common classes of models never reached
a rendered preview:

1. **Large models.** A valid ASCII STL larger than the filesystem JSON read
   cap (10 MiB — ``_MAX_READ_BYTES`` in
   ``omnigent/runner/environment_filesystem.py``) arrives with
   ``truncated: true``, and the viewer gives up with "Model is too large to
   preview (truncated by the server)." instead of recovering the complete
   bytes through the authenticated ``?download=true`` filesystem route.

2. **glTF.** ``getModelFormat`` recognizes only STL/3MF/OBJ, so a
   self-contained ``.gltf`` (embedded data-URI buffer, no external
   references) opens as plain JSON source in the editor instead of a 3D
   preview. Binary ``.glb`` shares the same resolver gap; it cannot be
   seeded through the text-only filesystem PUT endpoint (``str.encode`` —
   base64 is not a text codec), so the ASCII ``.gltf`` case stands in for
   both here.

Both fixtures are valid, fully self-contained models — after a fix they must
render as an interactive WebGL preview (a ``<canvas>`` under the
``aria-label="3D preview of …"`` host), like the models in
``test_model_rendering.py``. Seeded via the filesystem PUT endpoint (no
agent run).
"""

from __future__ import annotations

import base64
import json
import math
import re
import struct
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

# ---------------------------------------------------------------------------
# Test fixtures — valid, self-contained models
# ---------------------------------------------------------------------------

# The runner's JSON file-content envelope truncates past this many bytes.
_READ_CAP_BYTES = 10 * 1024 * 1024


def _large_ascii_stl() -> str:
    """Build a valid ASCII STL comfortably larger than the 10 MiB read cap.

    A strip of adjacent unit triangles along the X axis with a gentle sine
    wave in Z — finite, non-degenerate bounds, so the complete file parses
    into a renderable mesh. Only the *envelope* truncation makes it fail.

    :returns: ASCII STL text of ~11 MiB.
    """
    target = _READ_CAP_BYTES + 1024 * 1024  # ~1 MiB past the cap
    lines = ["solid bigpart"]
    size = len(lines[0]) + 1
    i = 0
    while size < target:
        x0, x1 = float(i), float(i + 1)
        z = math.sin(i * 0.01)
        facet = (
            "  facet normal 0 0 1\n"
            "    outer loop\n"
            f"      vertex {x0:.3f} 0.0 {z:.3f}\n"
            f"      vertex {x1:.3f} 0.0 {z:.3f}\n"
            f"      vertex {x0:.3f} 1.0 {z:.3f}\n"
            "    endloop\n"
            "  endfacet"
        )
        lines.append(facet)
        size += len(facet) + 1
        i += 1
    lines.append("endsolid bigpart\n")
    return "\n".join(lines)


def _self_contained_gltf() -> str:
    """Build a minimal valid glTF 2.0 asset with an embedded data-URI buffer.

    One triangle, no external ``.bin``/texture references — the class of
    glTF file the viewer is expected to support without resolving relative
    workspace dependencies.

    :returns: glTF JSON text.
    """
    positions = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
    gltf = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,
                "count": 3,
                "type": "VEC3",
                "min": [0, 0, 0],
                "max": [1, 1, 0],
            }
        ],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(positions)}],
        "buffers": [
            {
                "byteLength": len(positions),
                "uri": "data:application/octet-stream;base64,"
                + base64.b64encode(positions).decode(),
            }
        ],
    }
    return json.dumps(gltf, indent=1)


# Each case: (file_path, content_factory). Factories keep the ~11 MiB STL
# text out of collection time.
_CASES = {
    "large-stl": ("big-model.stl", _large_ascii_stl),
    "gltf": ("scene.gltf", _self_contained_gltf),
}


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_model_file(
    seeded_session: tuple[str, str],
    request: pytest.FixtureRequest,
) -> Iterator[tuple[str, str, str]]:
    """Seed one model file and yield ``(base_url, session_id, path)``.

    :param seeded_session: Runner-bound (base_url, session_id) pair.
    :param request: Carries the ``(path, content_factory)`` case via
        ``indirect``.
    :returns: ``(base_url, session_id, file_path)`` for the test body.
    """
    base_url, session_id = seeded_session
    file_path, content_factory = request.param
    file_url = (
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{file_path}"
    )
    # 60s: the large-STL case ships an ~11 MiB JSON body through the server
    # to the runner.
    resp = httpx.put(
        file_url,
        json={"content": content_factory(), "encoding": "utf-8"},
        timeout=60.0,
    )
    resp.raise_for_status()
    yield (base_url, session_id, file_path)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seeded_model_file",
    list(_CASES.values()),
    ids=list(_CASES),
    indirect=True,
)
def test_model_renders_full_3d_preview(
    page: Page,
    seeded_model_file: tuple[str, str, str],
) -> None:
    """A large STL and a self-contained glTF render as WebGL previews.

    Before the fix this failed in two ways:

    - ``large-stl``: the ModelViewer mounts but shows "Model is too large to
      preview (truncated by the server)." and never builds a canvas.
    - ``gltf``: the file never routes to the ModelViewer at all — it opens
      as JSON source in the editor, so the preview host never appears.
    """
    base_url, session_id, file_path = seeded_model_file
    page.goto(f"{base_url}/c/{session_id}?view=explore")

    file_button = page.get_by_role("button", name=re.compile(rf"^{re.escape(file_path)}\b"))
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    # Two FileViewer instances mount with the same test id (mobile push-panel,
    # md:hidden, and the desktop rail). Match the visible one directly.
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()

    # The file routed to the ModelViewer: its canvas host carries the
    # filename aria-label. (Before the fix the glTF case failed here — the
    # file opened as JSON source instead.)
    preview = file_viewer.locator(f'[aria-label="3D preview of {file_path}"]')
    expect(preview).to_be_visible(timeout=30_000)

    # The WebGL scene built from the COMPLETE model bytes — three.js appended
    # a <canvas>. (Before the fix the large-STL case failed here: the viewer
    # consumed the truncated 10 MiB envelope and surfaced the too-large error
    # instead of fetching the full file through ?download=true.) 60s: the
    # large case must download and parse ~11 MiB first.
    expect(preview.locator("canvas")).to_be_visible(timeout=60_000)

    # Neither of the reported failure modes is on screen…
    expect(file_viewer.get_by_text("Model is too large to preview")).to_have_count(0)
    expect(file_viewer.get_by_text("Unable to render 3D model")).to_have_count(0)

    # …and it did NOT fall through to the binary placeholder or an editor.
    expect(file_viewer.get_by_text("Preview not available for binary files")).to_have_count(0)
    expect(file_viewer.locator("[contenteditable='true']")).to_have_count(0)
