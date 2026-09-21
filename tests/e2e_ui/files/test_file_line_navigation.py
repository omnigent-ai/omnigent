"""Browser regressions for chat citations into Monaco source and diff views.

Keep real scrolling, collapsed diff regions, and editor transitions here;
navigation state and input/layout permutations belong in the frontend unit tests.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
from playwright.sync_api import Locator, Page, Route, expect

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


def _post_message(session: tuple[str, str], text: str) -> None:
    base_url, session_id = session
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {"agent": "hello_world", "text": text},
        },
        timeout=10,
    )
    response.raise_for_status()


def _open_viewer(
    page: Page,
    session: tuple[str, str],
    path: str,
    preferences: dict[str, str | bool],
    *,
    wide: bool = False,
) -> Locator:
    base_url, session_id = session
    page.set_viewport_size({"width": 3200 if wide else 1600, "height": 1000})
    encoded_preferences = json.dumps(json.dumps(preferences))
    page.add_init_script(
        f"localStorage.setItem('omnigent:file-view-preferences', {encoded_preferences});"
    )
    page.goto(f"{base_url}/c/{session_id}?file={path}")
    return page.locator('[data-testid="file-viewer"]:visible')


def _open_monaco(page: Page, session: tuple[str, str], layout: str | None = None) -> Locator:
    viewer = _open_viewer(
        page,
        session,
        _FILE_PATH,
        {"diffActive": layout is not None, "diffLayout": layout or "unified"},
        wide=layout is not None,
    )
    editor = viewer.locator(".monaco-diff-editor" if layout else ".monaco-editor")
    expect(editor).to_be_visible(timeout=30_000)
    if layout:
        separator = page.get_by_role("separator", name="Resize panel", exact=True)
        box = separator.bounding_box()
        assert box is not None
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(1600, box["y"] + box["height"] / 2)
        page.mouse.up()
        if layout == "split":
            expect(editor).to_have_class(re.compile(r"\bside-by-side\b"))
        else:
            expect(editor).not_to_have_class(re.compile(r"\bside-by-side\b"))
    return viewer


def _mock_markdown_files(page: Page, session: tuple[str, str], files: dict[str, str]) -> list[str]:
    base_url, session_id = session
    environment_url = f"{base_url}/v1/sessions/{session_id}/resources/environments/default"
    page.route(
        environment_url, lambda route: route.fulfill(json={"metadata": {"root": "/workspace"}})
    )
    page.route(
        f"{environment_url}/filesystem/src?*",
        lambda route: route.fulfill(
            json={
                "object": "list",
                "has_more": False,
                "data": [
                    {
                        "path": path,
                        "name": path.split("/")[-1],
                        "type": "file",
                        "bytes": len(content),
                    }
                    for path, content in files.items()
                ],
            }
        ),
    )
    writes: list[str] = []

    def serve_file(route: Route) -> None:
        if route.request.method != "GET":
            writes.append(route.request.url)
            route.fulfill(status=503, json={"error": "Workspace offline"})
            return
        path = route.request.url.split("/filesystem/", 1)[1]
        content = files[path]
        route.fulfill(
            json={
                "object": "session.environment.filesystem.file_content",
                "path": path,
                "content": content,
                "encoding": "utf-8",
                "content_type": "text/markdown",
                "bytes": len(content),
            }
        )

    for path in files:
        page.route(f"{environment_url}/filesystem/{path}", serve_file)
    return writes


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
    _post_message(
        seeded_session,
        f"[Line 100]({_FILE_PATH}:100) and [Line 200]({_FILE_PATH}:200) "
        f"and [Last line]({_FILE_PATH}:{len(_AFTER_LINES)}) "
        f"and [Beyond file]({_FILE_PATH}:5000) and [Plain file]({_FILE_PATH})",
    )


def test_markdown_diff_url_stays_stable_across_responsive_layouts(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Hidden responsive viewers must not undo the visible viewer's diff choice."""
    base_url, session_id = seeded_session
    path = "src/notes.md"
    before, after = "# Notes\nBefore\n", "# Notes\nAfter\n"
    _mock_markdown_files(page, seeded_session, {path: after})
    environment_url = f"{base_url}/v1/sessions/{session_id}/resources/environments/default"
    page.route(
        f"{environment_url}/changes",
        lambda route: route.fulfill(
            json={
                "object": "list",
                "has_more": False,
                "data": [
                    {"path": path, "name": "notes.md", "status": "modified", "bytes": len(after)}
                ],
            }
        ),
    )
    page.route(
        f"{environment_url}/diff/{path}",
        lambda route: route.fulfill(json={"path": path, "before": before, "after": after}),
    )
    viewer = _open_viewer(page, seeded_session, path, {"diffActive": False})
    expect(viewer.get_by_role("button", name="Show diff", exact=True)).to_be_visible()
    page.evaluate(
        """() => {
          window.diffUrlTransitions = [];
          let previous = new URLSearchParams(location.search).get('diff');
          const replaceState = history.replaceState;
          history.replaceState = function(...args) {
            replaceState.apply(this, args);
            const current = new URLSearchParams(location.search).get('diff');
            if (current !== previous) window.diffUrlTransitions.push(current);
            previous = current;
          };
        }"""
    )
    transitions: list[str | None] = []
    for width, enabled in ((1600, True), (600, False), (600, True), (1600, False)):
        page.set_viewport_size({"width": width, "height": 1000})
        label = "Show diff" if enabled else "Exit diff view"
        viewer.get_by_role("button", name=label, exact=True).click()
        diff_url = re.compile(r"[?&]diff=1(?:&|$)")
        if enabled:
            expect(viewer.locator(".monaco-diff-editor")).to_be_visible(timeout=30_000)
            expect(page).to_have_url(diff_url)
        else:
            expect(viewer.locator(".monaco-diff-editor")).to_have_count(0)
            expect(page).not_to_have_url(diff_url)
        # Observe subsequent paints to catch repeated URL writes after the click.
        page.evaluate(
            """async () => {
              for (let frame = 0; frame < 12; frame++) {
                await new Promise(requestAnimationFrame);
              }
            }"""
        )
        transitions.append("1" if enabled else None)
        assert page.evaluate("window.diffUrlTransitions") == transitions


@pytest.mark.parametrize("layout", ["split", "unified"])
def test_chat_line_link_expands_and_centers_diff_context(
    page: Page,
    seeded_session: tuple[str, str],
    layout: str,
) -> None:
    """Click hidden current-file lines without changing the selected diff view."""
    _seed_citation_file(page, seeded_session)
    viewer = _open_monaco(page, seeded_session, layout)
    diff = viewer.locator(".monaco-diff-editor")
    modified = diff.locator(".modified .view-lines:not(.line-delete)")
    expect(diff.locator(".modified").get_by_text("339 hidden lines", exact=True)).to_be_visible(
        timeout=20_000
    )
    expect(modified.get_by_text(_AFTER_LINES[99], exact=True)).to_have_count(0)

    for line in (100, 200, 100):
        page.get_by_role("button", name=f"Line {line}", exact=True).click()
        # The five inserted lines ensure original/current line numbers differ.
        expect(modified.get_by_text(_AFTER_LINES[line - 1], exact=True)).to_be_visible()
        page.wait_for_function(
            _CENTERED_LINE, arg={"text": _AFTER_LINES[line - 1]}, timeout=10_000
        )
        expect(diff).to_be_visible()
        expect(page).to_have_url(re.compile(r"[?&]diff=1(?:&|$)"))


def test_source_citation_centers_last_loaded_line(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Final-line and out-of-range citations center even in a truncated source buffer."""
    _seed_citation_file(page, seeded_session, truncated=True)
    viewer = _open_monaco(page, seeded_session)

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
    destination = "src/destination.md"
    content = "\n".join(f"Original paragraph {line}." for line in range(1, 51))
    writes = _mock_markdown_files(page, seeded_session, {path: content, destination: content})
    page.route(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/changes",
        lambda route: route.fulfill(json={"object": "list", "has_more": False, "data": []}),
    )
    liveness = {"runner_online": True, "host_online": True}
    page.route(
        f"{base_url}/health?session_ids=*",
        lambda route: route.fulfill(json={"sessions": {session_id: liveness}}),
    )
    _post_message(
        seeded_session,
        f"[Markdown line]({destination}:12) and [Plain Markdown]({destination}) "
        f"and [Original Markdown]({path})",
    )
    page.clock.install()
    viewer = _open_viewer(
        page,
        seeded_session,
        path,
        {"diffActive": True, "previewableViewMode": "editor"},
    )
    editor = viewer.locator('[contenteditable="true"]')
    expect(editor).to_be_visible(timeout=30_000)
    # Cache both files while reachable; an offline workspace cannot load a new file.
    for label, target_path in [("Plain Markdown", destination), ("Original Markdown", path)]:
        page.get_by_role("button", name=label, exact=True).click()
        page.wait_for_function(
            "path => new URL(location.href).searchParams.get('file') === path", arg=target_path
        )
        expect(editor).to_contain_text("Original paragraph 50.")
    # A live host can reconnect a sleeping runner and save, so both must be offline.
    liveness.update(runner_online=False, host_online=False)
    # Run the health poll without waiting ten seconds of wall time.
    page.clock.fast_forward(10_000)
    expect(
        viewer.get_by_role(
            "button", name="Runner offline — your changes will save when it reconnects", exact=True
        )
    ).to_be_visible(timeout=30_000)
    editor.fill("Unsaved offline Markdown draft")
    expect(viewer.get_by_text("Runner offline — changes save", exact=False)).to_be_visible()
    assert not writes, "An offline draft must not trigger a save"

    page.get_by_role("button", name="Markdown line", exact=True).click()
    dialog = page.get_by_role("dialog", name="Unsaved changes")
    expect(dialog).to_contain_text("Unsaved changes")
    page.wait_for_function(
        "path => new URL(location.href).searchParams.get('file') === path", arg=path
    )
    expect(page).not_to_have_url(re.compile(r"[?&]line="))
    dialog.get_by_role("button", name="Keep editing", exact=True).click()
    expect(editor).to_be_visible()
    expect(editor).to_have_text("Unsaved offline Markdown draft")
    expect(viewer.locator(".monaco-editor")).to_have_count(0)

    page.get_by_role("button", name="Markdown line", exact=True).click()
    dialog.get_by_role("button", name="Discard changes", exact=True).click()
    expect(dialog).not_to_be_visible()
    page.wait_for_function(
        "path => new URL(location.href).searchParams.get('file') === path", arg=destination
    )
    expect(page).to_have_url(re.compile(r"[?&]line=12(?:&|$)"))
    target = viewer.locator('[data-line="12"]')
    expect(target).to_have_text("Original paragraph 12.")
    expect(target).to_be_in_viewport()

    viewer.get_by_role("button", name=re.compile(r"^View mode")).click()
    page.get_by_role("menuitem", name="Edit", exact=True).click()
    editor.fill("Draft after following a citation")
    expect(viewer.get_by_text("Runner offline — changes save", exact=False)).to_be_visible()
    page.get_by_role("button", name="Plain Markdown", exact=True).click()
    expect(page).not_to_have_url(re.compile(r"[?&]line="))
    expect(dialog).not_to_be_visible()
    expect(editor).to_have_text("Draft after following a citation")
    assert not writes, "Navigation must not save an offline draft"


def test_plain_markdown_open_restores_scroll_after_citing_another_file(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """A citation must not suppress another file's first-render scroll restore."""
    _seed_citation_file(page, seeded_session)
    path = "src/saved.md"
    content = "\n".join(f"Markdown source line {line}" for line in range(1, 501))
    _mock_markdown_files(page, seeded_session, {path: content})
    _post_message(seeded_session, f"[Open Markdown]({path})")
    viewer = _open_viewer(
        page,
        seeded_session,
        path,
        {"diffActive": False, "previewableViewMode": "source"},
    )
    target = viewer.locator('[data-line="200"]')
    expect(target).to_be_attached(timeout=30_000)
    # A real reader gesture stops the initial saved-scroll restoration.
    viewer.locator('[data-line="1"]').click()
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


def test_plain_open_does_not_replay_a_citation(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """An explicit open without a line wins over a citation still in the URL."""
    _seed_citation_file(page, seeded_session)
    viewer = _open_monaco(page, seeded_session, "unified")
    page.get_by_role("button", name="Line 100", exact=True).click()
    page.wait_for_function(_CENTERED_LINE, arg={"text": _AFTER_LINES[99]})
    lines = viewer.locator(".modified .view-lines:not(.line-delete)")
    lines.get_by_text(_AFTER_LINES[99], exact=True).hover()
    page.mouse.wheel(0, -300)
    page.wait_for_function(f"arg => !({_CENTERED_LINE})(arg)", arg={"text": _AFTER_LINES[99]})
    saved = lines.evaluate(
        """async (lines, citedText) => {
      const el = lines.closest('.monaco-editor');
      const anchor = [...lines.querySelectorAll('.view-line')].find(line =>
        line.textContent.replace(/\u00a0/g, ' ') === citedText
      );
      if (!anchor) throw new Error('Cited line missing after reader scroll');
      let previous = anchor.getBoundingClientRect().top;
      let stable = 0;
      for (let frame = 0; frame < 60 && stable < 4; frame++) {
        await new Promise(requestAnimationFrame);
        const current = anchor.getBoundingClientRect().top;
        stable = current === previous ? stable + 1 : 0;
        previous = current;
      }
      if (stable < 4) throw new Error('Reader scroll did not settle');
      const top = el.getBoundingClientRect().top;
      const center = top + el.clientHeight / 2;
      const line = [...el.querySelectorAll('.view-line')].sort((a, b) =>
        Math.abs(a.getBoundingClientRect().top - center) -
        Math.abs(b.getBoundingClientRect().top - center)
      )[0];
      return {text: line.textContent, top: line.getBoundingClientRect().top - top};
    }""",
        _AFTER_LINES[99],
    )
    page.get_by_role("button", name="Plain file", exact=True).click()
    expect(page).not_to_have_url(re.compile(r"[?&](line|column)="))
    page.wait_for_function(
        """saved => {
      const editor = document.querySelector(
        '[data-testid="file-viewer"] .modified .view-lines:not(.line-delete)'
      )?.closest('.monaco-editor');
      if (!editor) return false;
      return [...editor.querySelectorAll('.view-line')].some(line =>
        line.textContent === saved.text && Math.abs(
          line.getBoundingClientRect().top - editor.getBoundingClientRect().top - saved.top
        ) < 3
      );
    }""",
        arg=saved,
    )
    expect(viewer.locator(".monaco-diff-editor")).to_have_count(1)
    # Plain opens do not disable a later explicit click on the same citation.
    page.get_by_role("button", name="Line 100", exact=True).click()
    page.wait_for_function(_CENTERED_LINE, arg={"text": _AFTER_LINES[99]})


def test_comment_navigation_supersedes_citation(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Closing comments must not replay a citation after a comment jump."""
    _seed_citation_file(page, seeded_session)
    base_url, session_id = seeded_session
    # Keep the two targets far apart without collapsed context.
    page.route(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/diff/{_FILE_PATH}",
        lambda route: route.fulfill(
            json={
                "object": "session.environment.filesystem.file_diff",
                "path": _FILE_PATH,
                "before": "\n".join(f"# previous content {i}" for i in range(500)),
                "after": _AFTER,
            }
        ),
    )
    anchor = _AFTER_LINES[299]
    start = _AFTER.index(anchor)
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/comments",
        json={
            "path": _FILE_PATH,
            "body": "Navigate to this later comment",
            "start_index": start,
            "end_index": start + len(anchor),
            "anchor_content": anchor,
        },
        timeout=10,
    )
    response.raise_for_status()
    viewer = _open_monaco(page, seeded_session, "split")
    page.get_by_role("button", name="Line 100", exact=True).click()
    page.wait_for_function(_CENTERED_LINE, arg={"text": _AFTER_LINES[99]})
    viewer.get_by_role("button", name="Show comments", exact=True).click()
    viewer.get_by_text("Navigate to this later comment", exact=True).click()
    lines = viewer.locator(".modified .view-lines:not(.line-delete)")
    target = lines.get_by_text(anchor, exact=True)
    expect(target).to_be_in_viewport()
    width = lines.evaluate("lines => lines.closest('.monaco-editor').clientWidth")
    viewer.get_by_role("button", name="Hide comments", exact=True).click()
    page.wait_for_function(
        """width => document.querySelector(
          '[data-testid="file-viewer"] .modified .view-lines:not(.line-delete)'
        )?.closest('.monaco-editor').clientWidth > width""",
        arg=width,
    )
    expect(target).to_be_in_viewport()
    # A fresh citation must still supersede the comment navigation.
    page.get_by_role("button", name="Line 100", exact=True).click()
    page.wait_for_function(_CENTERED_LINE, arg={"text": _AFTER_LINES[99]})
