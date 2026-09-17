"""Browser regressions for chat citations into Monaco source and diff views.

Only the file API responses are fixtures; chat links, view selection, diff
calculation, collapsed regions, and scrolling run through the real UI.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
from playwright.sync_api import Page, expect

_FILE_PATH = "src/citation_target.py"
_BEFORE_LINES = [f"# original source line {line}" for line in range(1, 501)]
_AFTER_LINES = [f"# inserted line {line}" for line in range(1, 6)] + _BEFORE_LINES
_AFTER_LINES[350] = "# changed current line 351"
_BEFORE = "\n".join(_BEFORE_LINES)
_AFTER = "\n".join(_AFTER_LINES)

_CENTERED_LINE = """({text, diff = true}) => {
  for (const line of document.querySelectorAll(
    `[data-testid="file-viewer"] ${diff ? '.modified ' : ''}.view-line`
  )) {
    if (line.textContent.replace(/\u00a0/g, ' ') !== text) continue;
    const rect = line.getBoundingClientRect();
    const editor = line.closest('.monaco-editor').getBoundingClientRect();
    if (!rect.height || !editor.height) continue;
    if (Math.abs((rect.top + rect.bottom - editor.top - editor.bottom) / 2) < 25) return true;
  }
  return false;
}"""


def _seed_citation_file(
    page: Page,
    seeded_session: tuple[str, str],
    *,
    truncated: bool = False,
) -> None:
    base_url, session_id = seeded_session
    environment_url = f"{base_url}/v1/sessions/{session_id}/resources/environments/default"
    page.route(
        f"{environment_url}/changes",
        lambda route: route.fulfill(
            json={
                "object": "list",
                "has_more": False,
                "data": [
                    {
                        "path": _FILE_PATH,
                        "name": _FILE_PATH,
                        "status": "modified",
                        "bytes": len(_AFTER),
                        "modified_at": 1,
                    }
                ],
            }
        ),
    )
    page.route(
        f"{environment_url}/filesystem/{_FILE_PATH}",
        lambda route: route.fulfill(
            json={
                "object": "session.environment.filesystem.file_content",
                "path": _FILE_PATH,
                "content": _AFTER,
                "encoding": "utf-8",
                "content_type": "text/plain",
                "bytes": len(_AFTER),
                "truncated": truncated,
            }
        ),
    )
    page.route(
        f"{environment_url}/diff/{_FILE_PATH}",
        lambda route: route.fulfill(
            json={
                "object": "session.environment.filesystem.file_diff",
                "path": _FILE_PATH,
                "before": _BEFORE,
                "after": _AFTER,
            }
        ),
    )
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {
                "agent": "hello_world",
                "text": (
                    f"[Line 100]({_FILE_PATH}:100) and [Line 200]({_FILE_PATH}:200) "
                    f"and [Last line]({_FILE_PATH}:{len(_AFTER_LINES)}) "
                    f"and [Beyond file]({_FILE_PATH}:5000)"
                ),
            },
        },
        timeout=10,
    )
    response.raise_for_status()


@pytest.mark.parametrize("layout", ["split", "unified"])
def test_chat_line_link_expands_and_centers_diff_context(
    page: Page,
    seeded_session: tuple[str, str],
    layout: str,
) -> None:
    """Click hidden current-file lines without changing the selected diff view."""
    _seed_citation_file(page, seeded_session)
    base_url, session_id = seeded_session
    # Keep the rail wide enough for Monaco's actual side-by-side layout.
    page.set_viewport_size({"width": 3200, "height": 1000})
    preferences = json.dumps({"diffActive": True, "diffLayout": layout})
    page.add_init_script(
        f"localStorage.setItem('omnigent:file-view-preferences', {json.dumps(preferences)});"
    )
    page.goto(f"{base_url}/c/{session_id}?file={_FILE_PATH}&diff=1")

    viewer = page.locator('[data-testid="file-viewer"]:visible')
    diff = viewer.locator(".monaco-diff-editor")
    expect(diff).to_be_visible(timeout=30_000)
    expect(page).to_have_url(re.compile(r"[?&]diff=1(?:&|$)"))
    modified = diff.locator(".modified .view-lines")
    expect(diff.get_by_text("339 hidden lines", exact=True)).to_be_visible(timeout=20_000)
    expect(modified.get_by_text(_AFTER_LINES[350], exact=True)).to_be_visible()
    expect(modified.get_by_text(_AFTER_LINES[99], exact=True)).to_have_count(0)
    separator = page.get_by_role("separator", name="Resize panel", exact=True)
    box = separator.bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(1600, box["y"] + box["height"] / 2)
    page.mouse.up()
    if layout == "split":
        expect(diff).to_have_class(re.compile(r"\bside-by-side\b"))
    else:
        expect(diff).not_to_have_class(re.compile(r"\bside-by-side\b"))

    for line in (100, 200, 100):
        page.get_by_role("button", name=f"Line {line}", exact=True).click()
        # The five inserted lines ensure original/current line numbers differ.
        expect(modified.get_by_text(_AFTER_LINES[line - 1], exact=True)).to_be_visible()
        page.wait_for_function(
            _CENTERED_LINE, arg={"text": _AFTER_LINES[line - 1]}, timeout=10_000
        )
        expect(diff).to_be_visible()
        expect(page).to_have_url(re.compile(r"[?&]diff=1(?:&|$)"))


@pytest.mark.parametrize("truncated", [False, True])
def test_source_citation_centers_last_loaded_line(
    page: Page,
    seeded_session: tuple[str, str],
    truncated: bool,
) -> None:
    """Final-line and out-of-range citations center even in a truncated source buffer."""
    _seed_citation_file(page, seeded_session, truncated=truncated)
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1600, "height": 1000})
    page.add_init_script(
        "localStorage.setItem('omnigent:file-view-preferences', '{\"diffActive\":false}');"
    )
    page.goto(f"{base_url}/c/{session_id}?file={_FILE_PATH}")
    viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(viewer.locator(".monaco-editor")).to_be_visible(timeout=30_000)

    for label in ("Last line", "Beyond file"):
        page.get_by_role("button", name=label, exact=True).click()
        page.wait_for_function(
            _CENTERED_LINE, arg={"text": _AFTER_LINES[-1], "diff": False}, timeout=10_000
        )
        expect(viewer.locator(".monaco-diff-editor")).to_have_count(0)

    # Reader interaction consumes the request even when the workspace remounts.
    viewer.locator(".view-lines").click()
    page.keyboard.press("Home")
    page.keyboard.press("ArrowUp")
    page.keyboard.press("PageUp")
    page.keyboard.press("PageUp")
    page.wait_for_function(
        f"arg => !({_CENTERED_LINE})(arg)",
        arg={"text": _AFTER_LINES[-1], "diff": False},
    )
    # Remember the nearest rendered line to the viewport center.
    centered_text = viewer.locator(".monaco-editor").evaluate("""editor => {
      const rect = editor.getBoundingClientRect();
      const center = (rect.top + rect.bottom) / 2;
      return [...editor.querySelectorAll('.view-line')].sort((a, b) =>
        Math.abs(a.getBoundingClientRect().top - center) -
        Math.abs(b.getBoundingClientRect().top - center)
      )[0].textContent.replace(/\u00a0/g, ' ');
    }""")
    page.get_by_role("button", name="Collapse right panel").click()
    expect(viewer).to_have_count(0)
    page.get_by_role("button", name="Expand right panel").click()
    page.wait_for_function(
        _CENTERED_LINE, arg={"text": centered_text, "diff": False}, timeout=30_000
    )
    page.get_by_role("button", name="Beyond file", exact=True).click()
    page.wait_for_function(
        _CENTERED_LINE, arg={"text": _AFTER_LINES[-1], "diff": False}, timeout=10_000
    )
    page.reload()
    page.wait_for_function(
        _CENTERED_LINE, arg={"text": _AFTER_LINES[-1], "diff": False}, timeout=30_000
    )


def test_citation_preserves_offline_markdown_draft_with_diff_preference(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """An unchanged Markdown file still guards its draft when diff is preferred."""
    base_url, session_id = seeded_session
    path = "src/unchanged.md"
    content = "# Existing document\n\nOriginal paragraph.\n"
    environment_url = f"{base_url}/v1/sessions/{session_id}/resources/environments/default"
    page.route(
        environment_url,
        lambda route: route.fulfill(json={"metadata": {"root": "/workspace"}}),
    )
    page.route(
        f"{environment_url}/filesystem/src?*",
        lambda route: route.fulfill(
            json={
                "object": "list",
                "has_more": False,
                "data": [
                    {"path": path, "name": "unchanged.md", "type": "file", "bytes": len(content)}
                ],
            }
        ),
    )
    page.route(
        f"{environment_url}/changes",
        lambda route: route.fulfill(json={"object": "list", "has_more": False, "data": []}),
    )
    page.route(
        f"{environment_url}/filesystem/{path}",
        lambda route: route.fulfill(
            json={
                "object": "session.environment.filesystem.file_content",
                "path": path,
                "content": content,
                "encoding": "utf-8",
                "content_type": "text/markdown",
                "bytes": len(content),
            }
        ),
    )
    page.route(
        f"{base_url}/health?session_ids=*",
        lambda route: route.fulfill(
            json={"sessions": {session_id: {"runner_online": False, "host_online": True}}}
        ),
    )
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {"agent": "hello_world", "text": f"[Markdown line]({path}:2)"},
        },
        timeout=10,
    )
    response.raise_for_status()
    preferences = json.dumps({"diffActive": True, "previewableViewMode": "editor"})
    page.add_init_script(
        f"localStorage.setItem('omnigent:file-view-preferences', {json.dumps(preferences)});"
    )
    page.goto(f"{base_url}/c/{session_id}?file={path}")
    viewer = page.locator('[data-testid="file-viewer"]:visible')
    editor = viewer.locator('[contenteditable="true"]')
    expect(editor).to_be_visible(timeout=30_000)
    editor.fill("Unsaved offline Markdown draft")
    expect(viewer.get_by_text("Runner offline — changes save", exact=False)).to_be_visible()

    page.get_by_role("button", name="Markdown line", exact=True).click()
    dialog = page.get_by_role("dialog", name="Unsaved changes")
    expect(dialog).to_contain_text("Unsaved changes")
    dialog.get_by_role("button", name="Keep editing", exact=True).click()
    expect(editor).to_be_visible()
    expect(editor).to_have_text("Unsaved offline Markdown draft")
    expect(viewer.locator(".monaco-editor")).to_have_count(0)


def test_plain_markdown_open_restores_scroll_after_citing_another_file(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """A citation must not suppress another file's first-render scroll restore."""
    _seed_citation_file(page, seeded_session)
    base_url, session_id = seeded_session
    path = "src/saved.md"
    content = "\n".join(f"Markdown source line {line}" for line in range(1, 501))
    environment_url = f"{base_url}/v1/sessions/{session_id}/resources/environments/default"
    page.route(
        environment_url,
        lambda route: route.fulfill(json={"metadata": {"root": "/workspace"}}),
    )
    page.route(
        f"{environment_url}/filesystem/src?*",
        lambda route: route.fulfill(
            json={
                "object": "list",
                "has_more": False,
                "data": [
                    {"path": path, "name": "saved.md", "type": "file", "bytes": len(content)}
                ],
            }
        ),
    )
    page.route(
        f"{environment_url}/filesystem/{path}",
        lambda route: route.fulfill(
            json={
                "object": "session.environment.filesystem.file_content",
                "path": path,
                "content": content,
                "encoding": "utf-8",
                "content_type": "text/markdown",
                "bytes": len(content),
            }
        ),
    )
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {
                "agent": "hello_world",
                "text": f"[Open Markdown]({path})",
            },
        },
        timeout=10,
    )
    response.raise_for_status()
    page.set_viewport_size({"width": 1600, "height": 1000})
    preferences = json.dumps({"diffActive": False, "previewableViewMode": "source"})
    page.add_init_script(
        f"localStorage.setItem('omnigent:file-view-preferences', {json.dumps(preferences)});"
    )
    page.goto(f"{base_url}/c/{session_id}?file={path}")
    viewer = page.locator('[data-testid="file-viewer"]:visible')
    target = viewer.locator('[data-line="200"]')
    expect(target).to_be_attached(timeout=30_000)
    target.evaluate("el => el.scrollIntoView({block: 'center'})")
    expect(target).to_be_in_viewport()
    original_top = target.evaluate("el => el.getBoundingClientRect().top")
    page.get_by_role("button", name="Line 100", exact=True).click()
    page.wait_for_function(
        _CENTERED_LINE, arg={"text": _AFTER_LINES[99], "diff": False}, timeout=30_000
    )
    page.get_by_role("button", name="Open Markdown", exact=True).click()
    expect(target).to_be_in_viewport(timeout=30_000)
    page.wait_for_function(
        """top => Math.abs(
          document.querySelector('[data-line="200"]').getBoundingClientRect().top - top
        ) < 25""",
        arg=original_top,
    )
