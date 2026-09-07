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

import re
from collections.abc import Iterator

import httpx
import pytest
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
