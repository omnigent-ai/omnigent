"""E2E: 3D preview renders large models and glTF/GLB files.

The workspace FileViewer must show an interactive WebGL preview for:

1. a valid STL larger than the runner's 10 MiB JSON read cap — the capped
   envelope arrives ``truncated``, so the viewer has to recover the complete
   bytes through the authenticated ``?download=true`` filesystem route
   instead of failing with a too-large error;
2. a self-contained ``.gltf`` (JSON with an embedded data-URI buffer), which
   must route to a glTF-capable loader instead of the JSON source editor;
3. a ``.glb``, which must route to the same loader instead of the binary
   placeholder.

The filesystem PUT endpoint can only seed text (``str.encode("utf-8")``), so
every fixture is built to round-trip that path byte-for-byte: STL and glTF
are plain ASCII, and the GLB is crafted so every byte is < 0x80 (NUL and
control bytes are valid UTF-8). Seeded via the PUT endpoint (no agent run).
"""

from __future__ import annotations

import base64
import json
import re
import struct
from collections.abc import Callable, Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

# Mirrors the runner's filesystem JSON read cap (10 MiB).
_READ_CAP_BYTES = 10 * 1024 * 1024


def _large_ascii_stl() -> str:
    """A valid ASCII STL comfortably past the 10 MiB read cap."""
    header = "solid grid\n"
    footer = "endsolid grid\n"
    target = _READ_CAP_BYTES + 1024 * 1024
    facets: list[str] = []
    size = len(header) + len(footer)
    i = 0
    while size < target:
        x = float(i % 1000)
        y = float(i // 1000)
        facet = (
            "  facet normal 0 0 1\n"
            "    outer loop\n"
            f"      vertex {x} {y} 0\n"
            f"      vertex {x + 1.0} {y} 0\n"
            f"      vertex {x} {y + 1.0} 0\n"
            "    endloop\n"
            "  endfacet\n"
        )
        facets.append(facet)
        size += len(facet)
        i += 1
    return header + "".join(facets) + footer


def _triangle_gltf_json() -> str:
    """A minimal self-contained glTF 2.0 asset (embedded data-URI buffer)."""
    positions = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
    uri = "data:application/octet-stream;base64," + base64.b64encode(positions).decode("ascii")
    return json.dumps(
        {
            "asset": {"version": "2.0"},
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [{"mesh": 0}],
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
            "buffers": [{"uri": uri, "byteLength": len(positions)}],
            "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(positions)}],
            "accessors": [
                {
                    "bufferView": 0,
                    "byteOffset": 0,
                    "componentType": 5126,
                    "count": 3,
                    "type": "VEC3",
                    "max": [1.0, 1.0, 0.0],
                    "min": [0.0, 0.0, 0.0],
                }
            ],
        }
    )


def _triangle_glb_text() -> str:
    """The same asset as a GLB whose every byte is < 0x80 (UTF-8 safe)."""
    json_bytes = _triangle_gltf_json().encode("ascii")
    # Pad the JSON chunk (spaces are legal glTF padding) until both the chunk
    # length and the total length serialize with every little-endian byte
    # < 0x80, so the whole container survives the text-only PUT route.
    while len(json_bytes) % 4 or (len(json_bytes) & 0xFF) > 0x6B:
        json_bytes += b" "
    total = 12 + 8 + len(json_bytes)
    blob = (
        b"glTF"
        + struct.pack("<II", 2, total)
        + struct.pack("<I", len(json_bytes))
        + b"JSON"
        + json_bytes
    )
    return blob.decode("utf-8")


# Each case: (file_path, content builder). The builder keeps the 11 MiB STL
# out of memory for the params that don't use it.
_MODELS: dict[str, tuple[str, Callable[[], str]]] = {
    "stl-over-read-cap": ("big-model.stl", _large_ascii_stl),
    "gltf-self-contained": ("scene.gltf", _triangle_gltf_json),
    "glb": ("model.glb", _triangle_glb_text),
}


@pytest.fixture
def seeded_model_session(
    seeded_session: tuple[str, str],
    request: pytest.FixtureRequest,
) -> Iterator[tuple[str, str, str]]:
    """Seed a model file and yield ``(base_url, session_id, path)``.

    :param seeded_session: Runner-bound (base_url, session_id) pair.
    :param request: Carries the ``(path, builder)`` case via ``indirect``.
    :returns: ``(base_url, session_id, file_path)`` for the test body.
    """
    base_url, session_id = seeded_session
    file_path, builder = request.param
    file_url = (
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{file_path}"
    )
    resp = httpx.put(
        file_url,
        json={"content": builder(), "encoding": "utf-8"},
        timeout=120.0,
    )
    resp.raise_for_status()
    yield (base_url, session_id, file_path)


@pytest.mark.parametrize(
    "seeded_model_session",
    list(_MODELS.values()),
    ids=list(_MODELS),
    indirect=True,
)
def test_model_preview_large_and_gltf(
    page: Page,
    seeded_model_session: tuple[str, str, str],
) -> None:
    """Large STL and glTF/GLB files render as a WebGL preview, not errors."""
    base_url, session_id, file_path = seeded_model_session
    page.goto(f"{base_url}/c/{session_id}?view=explore")

    file_button = page.get_by_role("button", name=re.compile(rf"^{re.escape(file_path)}\b"))
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    # Two FileViewer instances mount with the same test id (mobile push-panel
    # and the desktop rail). Match the visible one directly.
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()

    # The ModelViewer mounted: its canvas host carries the filename aria-label.
    preview = file_viewer.locator(f'[aria-label="3D preview of {file_path}"]')
    expect(preview).to_be_visible(timeout=30_000)

    # The WebGL scene built successfully. Generous timeout: the over-cap STL
    # is fetched in full (11 MiB) and parsed before the canvas appears.
    expect(preview.locator("canvas")).to_be_visible(timeout=45_000)
    expect(file_viewer.get_by_text("Model is too large to preview")).to_have_count(0)
    expect(file_viewer.get_by_text("Unable to render 3D model")).to_have_count(0)

    # It did NOT fall through to the binary placeholder or a source/editor view.
    expect(file_viewer.get_by_text("Preview not available for binary files")).to_have_count(0)
    expect(file_viewer.locator(".monaco-editor")).to_have_count(0)
