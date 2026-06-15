"""E2E: GitHub alert callouts render in the markdown rich-text editor.

Counterpart to ``test_markdown_rich_rendering.py`` (which covers headings,
lists, code blocks, tables, links and plain blockquotes): this pins the
GitHub-flavored *alert* construct — a blockquote whose first line is a
``[!NOTE]`` / ``[!TIP]`` / ``[!IMPORTANT]`` / ``[!WARNING]`` / ``[!CAUTION]``
marker. The ``GitHubAlertBlockquote`` TipTap extension turns those into
typed callouts carrying ``data-alert-type`` / ``data-alert-label`` attrs
(see ``TipTapGitHubAlert.ts``), distinct from a plain blockquote.

Seeded via the filesystem PUT endpoint (no agent run).
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[2]

_MARKDOWN_FILE_PATH = "alerts.md"

# One blockquote per GitHub alert type, plus a plain blockquote that must
# stay un-typed (no data-alert-type), so the test proves the marker — not
# blockquotes in general — is what produces the callout.
_MARKDOWN_CONTENT = """\
# Alert Gallery

> [!NOTE]
> Highlights information that users should take into account.

> [!TIP]
> Optional information to help a user be more successful.

> [!IMPORTANT]
> Crucial information necessary for users to succeed.

> [!WARNING]
> Critical content demanding immediate user attention.

> [!CAUTION]
> Negative potential consequences of an action.

> A plain blockquote with no alert marker.
"""


@pytest.fixture
def seeded_alerts_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_MARKDOWN_FILE_PATH}",
        json={"content": _MARKDOWN_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    try:
        yield (base_url, session_id)
    finally:
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


def test_github_alerts_render_as_typed_callouts(
    page: Page,
    seeded_alerts_session: tuple[str, str],
) -> None:
    """Each ``[!TYPE]`` blockquote renders as a typed alert; plain quotes don't."""
    base_url, session_id = seeded_alerts_session
    page.goto(f"{base_url}/c/{session_id}?file={_MARKDOWN_FILE_PATH}")

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()
    editor = file_viewer.locator("[contenteditable='true']")
    expect(editor).to_be_visible(timeout=10_000)

    # Every alert type renders as a blockquote tagged with its lowercase
    # data-alert-type and human-readable data-alert-label.
    expected = {
        "note": "Note",
        "tip": "Tip",
        "important": "Important",
        "warning": "Warning",
        "caution": "Caution",
    }
    for alert_type, label in expected.items():
        callout = editor.locator(f'blockquote[data-alert-type="{alert_type}"]')
        expect(callout).to_have_count(1)
        expect(callout).to_have_attribute("data-alert-label", label)

    # The marker text itself is consumed — the rendered callout shows the body,
    # never the raw "[!NOTE]" syntax.
    note = editor.locator('blockquote[data-alert-type="note"]')
    expect(note).to_contain_text("Highlights information")
    expect(note).not_to_contain_text("[!NOTE]")

    # The plain blockquote stays an ordinary blockquote — no alert typing.
    plain = editor.locator("blockquote:not([data-alert-type])").filter(
        has_text="A plain blockquote"
    )
    expect(plain).to_have_count(1)

    # Source toggle: the raw markers are visible verbatim in the source view.
    file_viewer.get_by_role("button", name="Source view").click()
    expect(file_viewer.locator("[contenteditable='true']")).to_have_count(0)
    expect(file_viewer.get_by_text("[!WARNING]", exact=False)).to_be_visible(timeout=10_000)
