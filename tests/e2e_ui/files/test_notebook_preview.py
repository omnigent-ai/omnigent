"""E2E: read-only .ipynb notebook preview and source toggle in the FileViewer.

Counterpart to ``test_markdown_rich_rendering.py`` for notebooks: a seeded
``.ipynb`` must open as a rendered notebook (markdown cells as HTML, code
cells with execution counts, mime-bundle outputs) rather than raw JSON, with
the security posture pinned — ``text/html`` outputs are never injected into
the DOM (a hostile ``<script>`` output must not execute or mount) and fall
back to ``text/plain`` with a note; only raster images render, as inert
data-URIs. The source toggle must flip to the raw-JSON view and back.
Seeded via the filesystem PUT endpoint (no agent run).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_NOTEBOOK_FILE_PATH = "analysis.ipynb"

# 1x1 red PNG, base64 — a real decodable image for the data-URI output.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR4nGP4z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
)

# nbformat-4 fixture covering the render matrix: a markdown cell, a code cell
# with a stream output, a code cell whose execute_result carries BOTH a
# hostile text/html payload and a text/plain fallback, a display_data image,
# and an error output with ANSI escape codes in the traceback.
_NOTEBOOK_CONTENT = json.dumps(
    {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {"language_info": {"name": "python"}},
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": ["# Quarterly Analysis\n", "\n", "Exploring **Q3 data**.\n"],
            },
            {
                "cell_type": "code",
                "execution_count": 1,
                "metadata": {},
                "source": ["print('rows: 1523')\n"],
                "outputs": [{"output_type": "stream", "name": "stdout", "text": ["rows: 1523\n"]}],
            },
            {
                "cell_type": "code",
                "execution_count": 2,
                "metadata": {},
                "source": ["df.describe()\n"],
                "outputs": [
                    {
                        "output_type": "execute_result",
                        "execution_count": 2,
                        "metadata": {},
                        "data": {
                            "text/html": [
                                '<script id="nb-xss">window.__nb_xss = 1</script>'
                                "<table><tr><td>injected</td></tr></table>"
                            ],
                            "text/plain": ["       amount\ncount  1523.0"],
                        },
                    }
                ],
            },
            {
                "cell_type": "code",
                "execution_count": 3,
                "metadata": {},
                "source": ["df.plot()\n"],
                "outputs": [
                    {
                        "output_type": "display_data",
                        "metadata": {},
                        "data": {
                            "image/png": _PNG_B64,
                            "text/plain": ["<Figure size 640x480 with 1 Axes>"],
                        },
                    }
                ],
            },
            {
                "cell_type": "code",
                "execution_count": 4,
                "metadata": {},
                "source": ["1/0\n"],
                "outputs": [
                    {
                        "output_type": "error",
                        "ename": "ZeroDivisionError",
                        "evalue": "division by zero",
                        "traceback": ["\x1b[0;31mZeroDivisionError\x1b[0m: division by zero"],
                    }
                ],
            },
        ],
    }
)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_notebook_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str, str]]:
    """Seed the notebook file and yield (base_url, session_id, path).

    :param seeded_session: Runner-bound (base_url, session_id) pair.
    :returns: ``(base_url, session_id, file_path)`` for the test body.
    """
    base_url, session_id = seeded_session
    file_url = (
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_NOTEBOOK_FILE_PATH}"
    )
    resp = httpx.put(
        file_url,
        json={"content": _NOTEBOOK_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    yield (base_url, session_id, _NOTEBOOK_FILE_PATH)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_notebook_renders_preview_and_source_toggle(
    page: Page,
    seeded_notebook_session: tuple[str, str, str],
) -> None:
    """Notebook cells render as a preview; source toggle shows the raw JSON."""
    base_url, session_id, _file_path = seeded_notebook_session
    page.goto(f"{base_url}/c/{session_id}?view=explore")

    file_button = page.get_by_role(
        "button", name=re.compile(rf"^{re.escape(_NOTEBOOK_FILE_PATH)}\b")
    )
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    # Match the visible FileViewer instance directly (mobile + desktop both
    # mount with the same test id; order is not guaranteed).
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()
    expect(
        page.get_by_role("button", name=f"Close {_NOTEBOOK_FILE_PATH}", exact=True).first
    ).to_be_visible()

    # Preview is the default for notebooks: the markdown cell renders as
    # semantic HTML (a heading, not "# Quarterly Analysis" verbatim).
    heading = file_viewer.locator("h1").filter(has_text="Quarterly Analysis")
    expect(heading).to_be_visible(timeout=10_000)
    expect(heading).not_to_contain_text("#")
    expect(file_viewer.locator("strong").filter(has_text="Q3 data")).to_be_visible()

    # Code cells carry their source and execution counts; stream output shows.
    expect(file_viewer.get_by_text("In [1]:", exact=False)).to_be_visible()
    expect(file_viewer.get_by_text("rows: 1523").last).to_be_visible()

    # Security: the hostile text/html output is never injected — no script or
    # table mounts, no side effect runs, and the text/plain fallback shows
    # with the suppression note.
    expect(file_viewer.locator("#nb-xss")).to_have_count(0)
    expect(file_viewer.locator("table")).to_have_count(0)
    assert page.evaluate("() => window.__nb_xss") is None
    expect(file_viewer.get_by_text("Rich HTML output hidden", exact=False)).to_be_visible()
    expect(file_viewer.get_by_text("count  1523.0", exact=False)).to_be_visible()

    # The image/png output renders as an inert data-URI image.
    image = file_viewer.locator('img[src^="data:image/png;base64,"]')
    expect(image).to_be_visible()

    # The error traceback renders with its ANSI escapes consumed, not verbatim.
    expect(file_viewer.get_by_text("division by zero", exact=False).first).to_be_visible()
    expect(file_viewer.get_by_text("[0;31m", exact=False)).to_have_count(0)

    # Source toggle: the raw notebook JSON becomes visible.
    file_viewer.get_by_role("button", name="View source").click()
    expect(file_viewer.get_by_text('"nbformat"', exact=False).first).to_be_visible(timeout=10_000)
    expect(file_viewer.get_by_text('"cell_type"', exact=False).first).to_be_visible()

    # Toggle back to the rendered preview.
    file_viewer.get_by_role("button", name="View preview").click()
    expect(file_viewer.locator("h1").filter(has_text="Quarterly Analysis")).to_be_visible(
        timeout=10_000
    )
