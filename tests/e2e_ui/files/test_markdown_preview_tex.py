"""E2E: TeX math in the markdown FileViewer Preview renders as formulas.

The FileViewer's Preview mode showed TeX source (``$$``, ``\\text``,
``\\frac``) as literal text while the chat renderer formatted the same
content as KaTeX math, so math rendering was inconsistent between the two
markdown surfaces. This drives both surfaces with the same document: the
chat transcript (seeded via ``external_assistant_message`` — no LLM run)
must render a KaTeX display block, and the file preview (seeded via the
filesystem PUT endpoint) must render the same block instead of the raw
delimiters. Regular markdown (the heading) must keep rendering either way.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import switch_markdown_view_mode

_AGENT_NAME = "hello_world"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_FILE_PATH = "amdahl.md"

# A display equation plus inline single-dollar spans. Only the `$$` block is
# asserted as math: single-dollar math is intentionally off in the chat
# renderer (prose dollars stay prose), so surface parity means the preview
# must render the display block, not that `$P$` becomes math anywhere.
_MARKDOWN_CONTENT = "\n".join(
    [
        "# Amdahl's Law",
        "",
        r"$$",
        r"\text{Speedup} = \frac{1}{(1-P) + \frac{P}{N}}",
        r"$$",
        "",
        r"- $P$: fraction that can run in parallel",
        r"- $N$: number of processors",
        r"- $1-P$: serial fraction",
        "",
    ]
)

_PROSE_MARKER = "fraction that can run in parallel"


@pytest.fixture
def tex_markdown_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Seed the TeX document as a workspace file and as a chat message.

    :param seeded_session: ``(base_url, session_id)`` for a runner-bound session.
    :returns: the same ``(base_url, session_id)`` after both are seeded.
    """
    base_url, session_id = seeded_session
    file_url = (
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_FILE_PATH}"
    )
    put_resp = httpx.put(
        file_url,
        json={"content": _MARKDOWN_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    put_resp.raise_for_status()
    msg_resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {"agent": _AGENT_NAME, "text": _MARKDOWN_CONTENT},
        },
        timeout=10.0,
    )
    msg_resp.raise_for_status()
    yield (base_url, session_id)


def test_markdown_preview_renders_tex_like_chat(
    page: Page,
    tex_markdown_session: tuple[str, str],
) -> None:
    """The file preview renders `$$` display math as KaTeX, matching chat."""
    base_url, session_id = tex_markdown_session

    # Chat surface: the same document renders its display equation as KaTeX.
    page.goto(f"{base_url}/c/{session_id}")
    bubble = page.locator(_ASSISTANT, has_text=_PROSE_MARKER).first
    expect(bubble).to_be_visible(timeout=30_000)
    expect(bubble.locator(".katex-display").first).to_be_visible(timeout=30_000)

    # File surface: open the seeded file and switch to Preview mode.
    page.goto(f"{base_url}/c/{session_id}?view=explore")
    file_button = page.get_by_role("button", name=re.compile(rf"^{re.escape(_FILE_PATH)}\b"))
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    # Two FileViewer instances mount with the same test id (mobile push-panel
    # and desktop rail); target the visible one.
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()
    switch_markdown_view_mode(page, file_viewer, "Preview")

    preview = file_viewer.locator(".markdown-preview")
    expect(preview).to_be_visible(timeout=10_000)

    # Regular markdown keeps rendering: the heading is parsed, not dumped.
    expect(preview.locator("h1")).to_contain_text("Amdahl's Law")

    # The bug: no KaTeX output — the preview showed the raw `$$ … \frac … $$`
    # source as literal text instead of one rendered display block.
    expect(preview.locator(".katex-display").first).to_be_visible(timeout=10_000)
    assert preview.locator(".katex-display").count() == 1, (
        "expected the $$-fenced equation to render as one KaTeX display block"
    )

    # With the equation rendered, its raw `$$` delimiters no longer appear as
    # literal preview text.
    expect(preview.get_by_text("$$")).to_have_count(0)
