"""File-line navigation contracts in real Chromium with a sealed mock backend."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests.browser_ui.conftest import BrowserContract

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


@dataclass
class BrowserSession:
    contract: BrowserContract
    session_id: str = "file-line-contract"
    items: list[dict[str, object]] = field(default_factory=list)
    comments: list[dict[str, object]] = field(default_factory=list)
    runner_online: bool = True
    host_online: bool = True

    def __iter__(self) -> Iterator[str]:
        yield self.contract.base_url
        yield self.session_id


@pytest.fixture
def seeded_session(browser_contract: BrowserContract) -> BrowserSession:
    session = BrowserSession(browser_contract)
    sid = session.session_id
    api = f"/v1/sessions/{sid}"
    empty = {"object": "list", "data": [], "first_id": None, "last_id": None, "has_more": False}
    row = {
        "id": sid,
        "object": "conversation",
        "title": "File-line browser contract",
        "agent_id": "file-line-agent",
        "agent_name": "hello_world",
        "status": "idle",
        "created_at": 1,
        "updated_at": 1,
        "labels": {},
        "permission_level": None,
    }
    browser_contract.json(
        "/v1/info",
        {
            "accounts_enabled": False,
            "single_user": True,
            "login_url": None,
            "needs_setup": False,
            "databricks_features": False,
            "managed_sandboxes_enabled": False,
            "sandbox_provider": None,
            "sandbox_providers": [],
            "sandbox_provider_capabilities": {},
            "enabled_connections": [],
            "sharing_mode": "off",
            "public_sharing_enabled": False,
            "server_version": "browser-contract",
            "smart_routing_enabled": False,
            "smart_routing_sources": {"external": False, "oss": False},
            "features": {},
            "harness_install_enabled": False,
            "installable_harnesses": [],
            "dictation_available": False,
            "branding": {
                "app_name": None,
                "heading": None,
                "logos": {"main": None, "loading": None, "favicon": None},
                "powered_by": True,
            },
        },
    )
    browser_contract.json("/v1/me", {"user_id": "local", "is_admin": True})
    for path in ("agents", "hosts", "projects", "extensions"):
        browser_contract.json(f"/v1/{path}", empty)
    browser_contract.json(
        "/v1/projects/order", {"ordered_project_ids": None, "sort_mode": "alphabetical"}
    )
    browser_contract.json("/v1/harnesses", {"data": [], "setup_steps": {}})
    browser_contract.json("/v1/sessions/projects", [])
    browser_contract.json(
        "/v1/sessions", {**empty, "data": [row], "first_id": sid, "last_id": sid}
    )
    browser_contract.json(api, row)
    browser_contract.json(
        f"{api}/items",
        lambda _request: {
            **empty,
            "data": list(reversed(session.items)),
            "first_id": session.items[-1]["id"] if session.items else None,
            "last_id": session.items[0]["id"] if session.items else None,
        },
    )
    browser_contract.json(
        f"{api}/agent",
        {
            "id": "file-line-agent",
            "object": "agent",
            "name": "hello_world",
            "description": "Browser contract",
            "harness": "openai-agents",
            "mcp_servers": [],
            "policies": [],
            "terminals": [],
        },
    )
    browser_contract.json(f"{api}/child_sessions", empty)
    browser_contract.json(f"{api}/policies", empty)
    browser_contract.json("/v1/policy-registry", empty)
    browser_contract.json(f"{api}/owner", {"owner": None})
    browser_contract.response(f"{api}/read-state", method="PUT")
    browser_contract.json(
        f"{api}/resources/environments/default", {"metadata": {"root": "/workspace"}}
    )
    browser_contract.json(f"{api}/resources/environments/default/changes", empty)
    browser_contract.json(f"{api}/resources/environments/default/filesystem", empty)
    browser_contract.json(f"{api}/resources/environments/default/filesystem/src", empty)
    browser_contract.json(
        f"{api}/resources/github", {"error": {"message": "No GitHub resource"}}, status=404
    )
    browser_contract.json(
        f"{api}/resources/gitlab",
        {
            "object": "session.gitlab.info",
            "available": False,
            "reason": "not_a_git_repo",
        },
    )
    browser_contract.json(f"{api}/resources/terminals", empty)
    browser_contract.json(f"{api}/comments", lambda _request: session.comments)
    browser_contract.sse(f"{api}/stream")
    browser_contract.json(
        "/health",
        lambda _request: {
            "sessions": {
                sid: {"runner_online": session.runner_online, "host_online": session.host_online}
            }
        },
    )
    browser_contract.websocket(
        "**/v1/sessions/updates*", lambda socket: socket.on_message(lambda _message: None)
    )
    return session


def _post_message(session: BrowserSession, text: str) -> None:
    index = len(session.items) + 1
    session.items.append(
        {
            "id": f"citation-{index}",
            "response_id": f"citation-response-{index}",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "model": "hello_world",
            "content": [{"type": "output_text", "text": text}],
        }
    )


def _open_viewer(
    page: Page,
    session: BrowserSession,
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


def _open_monaco(page: Page, session: BrowserSession, layout: str | None = None) -> Locator:
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


def _mock_markdown_files(page: Page, session: BrowserSession, files: dict[str, str]) -> list[str]:
    base_url, session_id = session
    environment_url = f"{base_url}/v1/sessions/{session_id}/resources/environments/default"
    session.contract.route(
        environment_url, lambda route: route.fulfill(json={"metadata": {"root": "/workspace"}})
    )
    session.contract.route(
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
        session.contract.route(f"{environment_url}/filesystem/{path}", serve_file)
    return writes


def _seed_citation_file(
    page: Page,
    seeded_session: BrowserSession,
    *,
    truncated: bool = False,
) -> None:
    base_url, session_id = seeded_session
    environment_url = f"{base_url}/v1/sessions/{session_id}/resources/environments/default"
    seeded_session.contract.route(
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
    seeded_session.contract.route(
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
    seeded_session.contract.route(
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


@pytest.mark.parametrize("layout", ["split", "unified"])
def test_chat_line_link_expands_and_centers_diff_context(
    page: Page,
    seeded_session: BrowserSession,
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
    seeded_session: BrowserSession,
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
    # The rail stays mounted at width 0 so its 300ms exit can finish without
    # remounting the editor. Pin the closed rail state instead of expecting
    # the viewer subtree to disappear.
    rail = page.locator('aside[aria-label="Workspace"]')
    expect(rail).to_have_attribute("data-state", "closed")
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
    page: Page, seeded_session: BrowserSession
) -> None:
    """An unchanged Markdown file still guards its draft when diff is preferred."""
    base_url, session_id = seeded_session
    path = "src/unchanged.md"
    destination = "src/destination.md"
    content = "\n".join(f"Original paragraph {line}." for line in range(1, 51))
    writes = _mock_markdown_files(page, seeded_session, {path: content, destination: content})
    seeded_session.contract.route(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/changes",
        lambda route: route.fulfill(json={"object": "list", "has_more": False, "data": []}),
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
    seeded_session.runner_online = False
    seeded_session.host_online = False
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
    page: Page, seeded_session: BrowserSession
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


def test_plain_open_does_not_replay_a_citation(page: Page, seeded_session: BrowserSession) -> None:
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
    page: Page, seeded_session: BrowserSession
) -> None:
    """Closing comments must not replay a citation after a comment jump."""
    _seed_citation_file(page, seeded_session)
    base_url, session_id = seeded_session
    # Keep the two targets far apart without collapsed context.
    seeded_session.contract.route(
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
    seeded_session.comments.append(
        {
            "id": "later-comment",
            "conversation_id": session_id,
            "path": _FILE_PATH,
            "body": "Navigate to this later comment",
            "start_index": start,
            "end_index": start + len(anchor),
            "anchor_content": anchor,
            "status": "draft",
            "created_at": 1,
            "updated_at": 1,
            "created_by": None,
        }
    )
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


def test_markdown_diff_url_stays_stable_across_responsive_layouts(
    page: Page, seeded_session: BrowserSession
) -> None:
    """Hidden responsive viewers must not undo the visible viewer's diff choice."""
    base_url, session_id = seeded_session
    path = "src/notes.md"
    before, after = "# Notes\nBefore\n", "# Notes\nAfter\n"
    _mock_markdown_files(page, seeded_session, {path: after})
    environment_url = f"{base_url}/v1/sessions/{session_id}/resources/environments/default"
    seeded_session.contract.route(
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
    seeded_session.contract.route(
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
    for width, enabled in ((1600, True), (600, True), (600, False), (1600, False)):
        page.set_viewport_size({"width": width, "height": 1000})
        label = "Show diff" if enabled else "Exit diff view"
        expect(viewer.get_by_role("button", name=label, exact=True)).to_be_visible()
        diff_url = re.compile(r"[?&]diff=1(?:&|$)")
        # Each viewer retains its own mode. On resize the URL follows the newly
        # visible viewer once, then each click makes exactly one transition.
        before_click = None if enabled else "1"
        if (transitions[-1] if transitions else None) != before_click:
            transitions.append(before_click)
        if enabled:
            expect(page).not_to_have_url(diff_url)
        else:
            expect(page).to_have_url(diff_url)
        assert page.evaluate("window.diffUrlTransitions") == transitions
        viewer.get_by_role("button", name=label, exact=True).click()
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
