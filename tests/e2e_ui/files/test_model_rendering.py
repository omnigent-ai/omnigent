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

import math
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


def _large_ascii_stl(min_bytes: int = 9 * 1024 * 1024 + 512 * 1024) -> str:
    """An ASCII STL height field just under the 10 MiB read cap, large enough
    that the viewer's decode, parse and scene build take a visible while."""
    side = max(4, math.isqrt(min_bytes // 150) + 1)

    def height(a: float, b: float) -> float:
        return math.sin(a * 0.4) * math.cos(b * 0.4) * 6.0

    parts = ["solid plate\n"]
    size = len(parts[0])
    i = 0
    while size < min_bytes:
        gx, gy = i % side, i // side
        facet = (
            "  facet normal 0 0 1\n    outer loop\n"
            f"      vertex {gx:.3f} {gy:.3f} {height(gx, gy):.3f}\n"
            f"      vertex {gx + 1:.3f} {gy:.3f} {height(gx + 1, gy):.3f}\n"
            f"      vertex {gx:.3f} {gy + 1:.3f} {height(gx, gy + 1):.3f}\n"
            "    endloop\n  endfacet\n"
        )
        parts.append(facet)
        size += len(facet)
        i += 1
    parts.append("endsolid plate\n")
    return "".join(parts)


# Installed before the SPA boots. Records whether the loading status was in the
# DOM when the preview host first mounted, catching that transient state without
# external polling.
_LOAD_PROBE_JS = """
(() => {
  window.__modelLoadProbe = { statusAtHostMount: undefined };
  const check = () => {
    const host = document.querySelector('[aria-label^="3D preview of"]');
    if (!host || window.__modelLoadProbe.statusAtHostMount !== undefined) return;
    const status = host.parentElement && host.parentElement.querySelector('[role="status"]');
    window.__modelLoadProbe.statusAtHostMount = status ? status.textContent : null;
  };
  new MutationObserver(check).observe(document, { childList: true, subtree: true });
  check();
})();
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
    :param request: Carries the ``(path, content)`` case via ``indirect``;
        ``content`` may be a callable so large fixtures are built on demand.
    :returns: ``(base_url, session_id, file_path)`` for the test body.
    """
    base_url, session_id = seeded_session
    file_path, content = request.param
    if callable(content):
        content = content()
    file_url = (
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{file_path}"
    )
    resp = httpx.put(
        file_url,
        json={"content": content, "encoding": "utf-8"},
        timeout=60.0,
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


@pytest.mark.parametrize(
    "seeded_model_session",
    [("big.stl", _large_ascii_stl)],
    ids=["large-stl"],
    indirect=True,
)
def test_large_model_shows_loading_status_until_it_renders(
    page: Page,
    seeded_model_session: tuple[str, str, str],
) -> None:
    """The pane shows a loading status from mount until the model is drawn."""
    base_url, session_id, file_path = seeded_model_session
    page.add_init_script(_LOAD_PROBE_JS)
    page.goto(f"{base_url}/c/{session_id}?view=explore")

    file_button = page.get_by_role("button", name=re.compile(rf"^{re.escape(file_path)}\b"))
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    preview = file_viewer.locator(f'[aria-label="3D preview of {file_path}"]')
    expect(preview).to_be_visible(timeout=30_000)
    expect(preview.locator("canvas")).to_be_visible(timeout=120_000)

    # The probe records status presence at host mount, before the canvas was
    # built — DOM presence and timing, not painted visibility.
    probe = page.evaluate("() => window.__modelLoadProbe")
    assert probe["statusAtHostMount"] == "Preparing model…", probe

    # And it is gone once the model is on screen.
    expect(file_viewer.get_by_text("Preparing model…")).to_have_count(0)
    expect(file_viewer.get_by_text("Unable to render 3D model")).to_have_count(0)
