"""E2E: a chat file link citing a position targets the cited line.

Regression guard for "file links do not target the exact change": a
conversation file link took the user to the file, but not the exact change.

Agents habitually cite the place they mean as ``path:line`` (the chat
linkifier explicitly recognises and strips this suffix — ``POSITION_SUFFIX``
in ``ChatMarkdown.tsx``). The journey under test:

  1. A session's workspace contains ``src/module.py`` (400 lines).
  2. A deterministic assistant message cites ``src/module.py:350`` in
     backticks (seeded via ``external_assistant_message`` — no LLM run).
  3. The citation renders as a clickable file link (existing behaviour).
  4. Clicking it opens the FileViewer on ``src/module.py`` (existing
     behaviour — the URL gains ``?file=src/module.py``).
  5. THE POINT: the viewer must land on the cited line. Line 350 carries a
     unique marker; Monaco virtualises rendering to (roughly) the visible
     viewport, so with a 400-line file the marker only enters the DOM once
     the viewer has actually scrolled to the citation. Pre-fix the position
     is stripped and dropped (``openFile`` carries only a path, ``?file=``
     has no line component, the viewer has no line-reveal), so the viewer
     parks at line 1 and the final assertion times out.

If this goes red, either the chat linkifier stopped forwarding the cited
position, or the FileViewer stopped revealing it — both put the user back to
hunting for the exact change by hand.
"""

from __future__ import annotations

import gzip
import io
import json
import re
import shutil
import tarfile
import tempfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

_AGENT_NAME = "cited_line_target_demo"
_FILE_REL = "src/module.py"
_TOTAL_LINES = 400
_TARGET_LINE = 350
# Unique to line 350 — only rendered by Monaco when the viewport reaches it.
_TARGET_MARKER = "CITED_LINE_TARGET_MARKER"
_CITATION = f"{_FILE_REL}:{_TARGET_LINE}"


def _module_source() -> str:
    """400 numbered filler lines with a unique marker on the cited line."""
    lines = [f"filler_{n} = {n}" for n in range(1, _TOTAL_LINES + 1)]
    lines[0] = '"""Top of file - the pre-fix viewer parks here."""'
    lines[_TARGET_LINE - 1] = f'{_TARGET_MARKER} = "the exact change the link cites"'
    return "\n".join(lines) + "\n"


def _agent_bundle(cwd: str) -> bytes:
    """Gzip-tar an agent YAML pinning ``os_env.cwd`` to ``cwd``.

    Mirrors the executor block of the conftest test agent so the strict
    validator accepts it; the model is never invoked (the assistant bubble is
    seeded via ``external_assistant_message``). The ``*.yaml`` arcname routes
    the bundle through the omnigent compat adapter, matching the conftest
    helpers.

    :param cwd: Absolute workspace directory the runner should use as root.
    :returns: ``.tar.gz`` bytes for multipart upload.
    """
    yaml_text = f"""\
name: {_AGENT_NAME}
prompt: You are a deterministic test assistant.

executor:
  model: gpt-4o-mini
  harness: openai-agents

os_env:
  type: caller_process
  cwd: {cwd}
  sandbox:
    type: none
"""
    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        data = yaml_text.encode()
        info = tarfile.TarInfo(name=f"{_AGENT_NAME}.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def cited_line_session(
    live_server: str,
    runner_id: str,
) -> Iterator[tuple[str, str]]:
    """Bind a session over a workspace with ``src/module.py`` and seed a reply.

    Creates a fresh workspace containing the 400-line module, binds a session
    to the spawned runner (the filesystem API must serve the existence check
    and the file content), then appends a deterministic assistant bubble
    citing ``src/module.py:350`` via ``external_assistant_message`` so no LLM
    turn runs.

    :param live_server: Spawned server base URL.
    :param runner_id: Token-bound runner id to bind the session to.
    :returns: ``(base_url, session_id)``.
    """
    ws = Path(tempfile.mkdtemp(prefix="omnigent-e2e-ui-cited-line-"))
    (ws / "src").mkdir()
    (ws / _FILE_REL).write_text(_module_source())

    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", _agent_bundle(str(ws)), "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    try:
        httpx.patch(
            f"{live_server}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=10.0,
        ).raise_for_status()

        message_text = f"I made the change you asked for — see `{_CITATION}`."
        httpx.post(
            f"{live_server}/v1/sessions/{session_id}/events",
            json={
                "type": "external_assistant_message",
                "data": {"agent": _AGENT_NAME, "text": message_text},
            },
            timeout=10.0,
        ).raise_for_status()

        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        shutil.rmtree(ws, ignore_errors=True)


def test_chat_file_link_opens_viewer_at_cited_line(
    page: Page,
    cited_line_session: tuple[str, str],
) -> None:
    """Clicking a ``path:line`` citation reveals the cited line, not the top."""
    base_url, session_id = cited_line_session
    page.goto(f"{base_url}/c/{session_id}")

    # The citation linkifies (existing behaviour): the span shows exactly what
    # the agent wrote, position suffix included.
    link = page.get_by_test_id("assistant-text-section").last.get_by_role("button", name=_CITATION)
    expect(link).to_be_visible(timeout=30_000)
    link.click()

    # Existing behaviour: the FileViewer opens on the resolved path (the
    # ``file`` param drops the position; react-router encodes "/" as %2F).
    page.wait_for_url(re.compile(r"[?&]file=src(?:%2F|/)module\.py(?:&|$)"), timeout=15_000)

    # The desktop viewer renders src files in Monaco. Wait for real content so
    # a slow file fetch can't masquerade as the bug.
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer.locator(".monaco-editor")).to_be_visible(timeout=20_000)
    expect(file_viewer.locator(".view-lines")).to_contain_text("filler_", timeout=20_000)

    # THE BUG: the viewer must land on the cited line. Monaco only
    # renders the (roughly) visible viewport, so the line-350 marker is in the
    # DOM iff the viewer scrolled to the citation. Pre-fix the position is
    # stripped from the click target and never forwarded, the viewer parks at
    # line 1, and this times out.
    expect(file_viewer.locator(".view-lines")).to_contain_text(_TARGET_MARKER, timeout=10_000)
