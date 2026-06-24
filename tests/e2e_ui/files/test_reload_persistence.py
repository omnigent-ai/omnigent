"""E2E: file-viewer, comment, and files-panel state survive a full browser reload.

These tests prove the durability path that the AppShell unit tests can
only approximate: ``AppShell.test.tsx`` mocks ``FileViewer`` and asserts
the ``?file=`` / ``?comment=`` params are wired into component state, but
it never re-fetches from a real server or re-hydrates a real viewer. Here
we seed state through the REST API, drive the real SPA, then call
``page.reload()`` and assert the same state re-renders from scratch —
which can only pass if (a) the server persisted it and (b) the SPA
rebuilds its view from the URL on a cold load.

No LLM is involved: files are seeded via the filesystem PUT endpoint and
comments via ``POST /v1/sessions/{id}/comments``, so the tests are
deterministic.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

# The agent spec uses ``os_env.cwd: .`` (see ``_TEST_AGENT_YAML`` in
# conftest), so filesystem PUTs land in ``<repo-root>/<session_id>/``
# next to the spawned server. Clean that per-session dir up in teardown
# so the suite leaves no untracked files behind.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _cleanup_session_workdir(session_id: str) -> None:
    """Remove the per-session working dir created by filesystem PUTs."""
    shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


# A plain-text file whose unique body proves the viewer fetched and
# rendered real content (not an empty shell) before and after reload.
_TEXT_FILE_PATH = "reload_durability.txt"
_TEXT_FILE_CONTENT = "Durable file body that must survive a browser reload."

# A markdown file plus a single anchored comment. The comment body is a
# unique sentinel so its presence in the panel is unambiguous.
_MD_FILE_PATH = "reload_comment.md"
_MD_ANCHOR = "Anchor paragraph for the durable comment."
_MD_FILE_CONTENT = f"""\
# Reload Comment Durability

{_MD_ANCHOR}
"""
_COMMENT_BODY = "This comment must persist across a full browser reload."


def _seed_file(base_url: str, session_id: str, path: str, content: str) -> None:
    """PUT a file into the session workspace via the filesystem API."""
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem/{path}",
        json={"content": content, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()


@pytest.fixture
def seeded_text_file(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Seed ``_TEXT_FILE_PATH`` and yield ``(base_url, session_id)``."""
    base_url, session_id = seeded_session
    _seed_file(base_url, session_id, _TEXT_FILE_PATH, _TEXT_FILE_CONTENT)
    try:
        yield (base_url, session_id)
    finally:
        _cleanup_session_workdir(session_id)


@pytest.fixture
def seeded_comment(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str, str]]:
    """Seed a markdown file + one anchored comment via REST.

    Yields ``(base_url, session_id, comment_id)``. Offsets are computed
    from the raw file body so the comment classifies as ``open`` (a
    draft) and is therefore reachable via the ``?comment=`` deep link.
    """
    base_url, session_id = seeded_session
    _seed_file(base_url, session_id, _MD_FILE_PATH, _MD_FILE_CONTENT)

    start = _MD_FILE_CONTENT.index(_MD_ANCHOR)
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/comments",
        json={
            "path": _MD_FILE_PATH,
            "body": _COMMENT_BODY,
            "start_index": start,
            "end_index": start + len(_MD_ANCHOR),
            "anchor_content": _MD_ANCHOR,
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    comment_id = resp.json()["id"]
    try:
        yield (base_url, session_id, comment_id)
    finally:
        _cleanup_session_workdir(session_id)


def test_file_viewer_rehydrates_from_url_after_reload(
    page: Page,
    seeded_text_file: tuple[str, str],
) -> None:
    """Open a file via ``?file=``, reload, and assert it re-renders.

    A failure means either the SPA didn't restore ``selectedFilePath``
    from the URL on a cold load (AppShell hydration regression) or the
    server didn't persist the seeded file (filesystem store regression).
    The ``:visible`` filter targets whichever FileViewer instance is
    on-screen — the desktop inline aside renders one and the mobile
    push-panel renders another, both with the same testid.
    """
    base_url, session_id = seeded_text_file
    page.goto(f"{base_url}/c/{session_id}?file={_TEXT_FILE_PATH}")

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()
    # The open file is identified by its tab (the desktop viewer header no
    # longer repeats a top-level filename — it's redundant with the tab).
    # exact=True targets the close button, not the tab div whose accessible
    # name also contains "Close <name>".
    expect(
        page.get_by_role("button", name=f"Close {_TEXT_FILE_PATH}", exact=True).first
    ).to_be_visible()
    # Content proves the viewer fetched the real body, not just an
    # empty shell keyed off the path.
    expect(file_viewer.get_by_text(_TEXT_FILE_CONTENT).first).to_be_visible(timeout=20_000)

    page.reload()

    # After a cold reload the viewer must rebuild purely from the URL.
    expect(page).to_have_url(re.compile(rf"file={re.escape(_TEXT_FILE_PATH)}"))
    file_viewer_after = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer_after).to_be_visible()
    expect(
        page.get_by_role("button", name=f"Close {_TEXT_FILE_PATH}", exact=True).first
    ).to_be_visible()
    expect(file_viewer_after.get_by_text(_TEXT_FILE_CONTENT).first).to_be_visible(timeout=20_000)


def test_comment_persists_across_reload(
    page: Page,
    seeded_comment: tuple[str, str, str],
) -> None:
    """Deep-link to a comment, reload, and assert it re-renders.

    The ``?comment=<id>`` param makes the FileViewer auto-open the
    comments panel and surface the card. A failure after reload means
    either the comment wasn't persisted server-side or the deep-link
    rehydration broke — both are real durability regressions the
    component test can't catch (it mocks the viewer).
    """
    base_url, session_id, comment_id = seeded_comment
    url = f"{base_url}/c/{session_id}?file={_MD_FILE_PATH}&comment={comment_id}"
    page.goto(url)

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_contain_text(_COMMENT_BODY, timeout=20_000)

    page.reload()

    file_viewer_after = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer_after).to_contain_text(_COMMENT_BODY, timeout=20_000)

    # The comment is still the single persisted record — reload must not
    # have duplicated or dropped it. A count != 1 would mean the reload
    # re-posted the comment (the deep-link rehydration wrote instead of
    # read) or the server lost it — both durability regressions.
    comments = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/comments?path={_MD_FILE_PATH}",
        timeout=10.0,
    ).json()
    assert len(comments) == 1, f"Expected exactly 1 persisted comment, got {comments}"
    assert comments[0]["body"] == _COMMENT_BODY


def test_files_panel_collapsed_state_persists_across_reload(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Collapse the Working-folder panel, reload, and assert it stays collapsed.

    Unlike the file-viewer/comment cases above (server-persisted, URL-
    rehydrated), the panel's collapsed state lives in a single app-global
    localStorage key with no per-conversation keying — the same preference
    the panel reads to carry the choice across *sessions*. A cold reload is
    the strongest single-session proxy for that: it re-mounts ``FilesPanel``,
    whose initial collapsed state is seeded purely from the stored
    preference (``readFilesPanelPreferences().collapsed``). A failure after
    reload means the choice wasn't persisted, the durability path the
    AppShell unit test can only approximate with a mocked store.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)

    # The Working-folder header doubles as the collapse toggle; ``aria-expanded``
    # tracks the panel's collapsed state and starts expanded by default.
    rail = page.get_by_role("complementary", name="Workspace")
    header = rail.get_by_role("button", name=re.compile("Working folder"))
    expect(header).to_have_attribute("aria-expanded", "true", timeout=30_000)

    header.click()
    expect(header).to_have_attribute("aria-expanded", "false")

    page.reload()
    open_right_rail(page)

    # A cold reload must restore the collapsed choice from localStorage.
    header_after = page.get_by_role("complementary", name="Workspace").get_by_role(
        "button", name=re.compile("Working folder")
    )
    expect(header_after).to_have_attribute("aria-expanded", "false", timeout=30_000)
