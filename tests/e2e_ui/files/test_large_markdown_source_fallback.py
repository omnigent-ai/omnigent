"""E2E regression test — the file viewer must survive very large markdown.

Opening a large (>10 MiB, single-line) markdown file from the Files panel
must render the truncated read-only preview, like every other large file
type does. The rich-text markdown surface used to crash while lexing the
huge single-line content (``RangeError: Maximum call stack size exceeded``
in the markdown block tokenizer), so the viewer never rendered at all.

The file is created inside the session workspace via the environment shell
endpoint (an 11 MiB PUT body would be needlessly heavy), then opened
through the real Files-panel journey.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[2]

_FILE_NAME = "large_oneline.md"
# 11 MiB of a single line: past the runner's 10 MiB read cap, so the server
# returns a truncated single-line preview that the viewer must still render.
_CREATE_CMD = f"python3 -c \"open('{_FILE_NAME}','w').write('y'*11534336)\""


def _shell(base_url: str, session_id: str, command: str) -> dict:
    """Run ``command`` in the session's default environment shell."""
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/shell",
        json={"command": command, "timeout": 300},
        timeout=320.0,
    )
    resp.raise_for_status()
    return resp.json()


def test_large_markdown_file_renders_truncated_preview(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    base_url, session_id = seeded_session

    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    result = _shell(base_url, session_id, _CREATE_CMD)
    assert result.get("exit_code") == 0, f"large-file creation failed: {result}"

    try:
        page.goto(f"{base_url}/c/{session_id}?view=explore")
        file_button = page.get_by_role("button", name=re.compile(rf"^{re.escape(_FILE_NAME)}\b"))
        file_button.wait_for(state="visible", timeout=30_000)
        file_button.click()

        # The viewer must come up and show the shared truncation banner —
        # exactly what large .txt / .json files already do. When the bug is
        # live the markdown surface dies mid-parse instead: the viewer never
        # becomes visible and a stack-overflow page error fires.
        viewer = page.locator('[data-testid="file-viewer"]:visible')
        expect(viewer).to_contain_text("too large to load fully", timeout=30_000)

        stack_overflows = [e for e in page_errors if "Maximum call stack size exceeded" in e]
        assert not stack_overflows, (
            f"opening the large markdown file crashed the markdown parser: {stack_overflows[:3]}"
        )
    finally:
        _shell(base_url, session_id, f"rm -f {_FILE_NAME}")
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)
