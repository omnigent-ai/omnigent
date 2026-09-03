"""File edits must render as a diff, not a full-file replacement.

User journey: the agent is asked to change ONE line of an existing workspace
file. It performs the edit with ``sys_os_write`` (a full-content replacement,
the shape the generic ``ToolCard`` renders for every harness's file edits).
The user expands the edit tool call in the transcript to see what changed.

Expected: the change renders as a **diff** — the removed line and the added
line are both visible, so the reader can tell what actually changed.

Broken behavior guarded against: the expanded panel dumps the tool call's
raw Parameters JSON — the ENTIRE new file content as one escaped string —
and the replaced (old) line appears nowhere on the page. The edit is
unreadable as a change.

The discriminating assertion is the last one: after expanding the edit tool
call, the line that was REPLACED must be visible somewhere in the assistant
transcript (any reasonable diff rendering shows removed lines). Without a
diff surface in ``web/src/components/blocks/ToolCard.tsx`` it fails. The
added-line assertion just before it is fix-safe (a diff shows added lines
too) and doubles as a sanity check that the edit rendered at all.
"""

from __future__ import annotations

import gzip
import io
import json
import shutil
import tarfile
import tempfile
import textwrap
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm, set_fallback_mock_llm

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'

# A custom openai-agents turn is a couple of LLM calls via the mock.
_TURN_TIMEOUT_MS = 90_000

_FILE = "config.py"

# The single line the edit changes. The OLD line is the discriminator: it
# exists only in the pre-edit file, so it can only appear on screen if the
# UI renders the change as a diff (removed side). It is deliberately absent
# from the user prompt and the assistant reply.
_OLD_LINE = "REQUEST_TIMEOUT_SECONDS = 30"
_NEW_LINE = "REQUEST_TIMEOUT_SECONDS = 60"

_ORIGINAL_CONTENT = textwrap.dedent(
    '''\
    """Service configuration for the demo web client."""

    SERVICE_NAME = "demo-web-client"
    API_BASE_URL = "https://api.example.test"

    # Network tuning.
    REQUEST_TIMEOUT_SECONDS = 30
    MAX_RETRIES = 4
    RETRY_BACKOFF_SECONDS = 1.5

    # Feature flags.
    ENABLE_CACHING = True
    ENABLE_TRACING = False
    '''
)
_NEW_CONTENT = _ORIGINAL_CONTENT.replace(_OLD_LINE, _NEW_LINE)

# The per-fixture model gives this test an isolated mock-LLM queue.
_AGENT_YAML = """\
name: {name}
prompt: |
  You are a deterministic test assistant. When asked to change a config
  value you rewrite the file with sys_os_write and then confirm briefly.

executor:
  model: {model}
  harness: openai-agents

os_env:
  type: caller_process
  cwd: {cwd}
  sandbox:
    type: none
"""


def _agent_bundle(name: str, model: str, cwd: str) -> bytes:
    """Gzip-tar the agent YAML for multipart upload."""
    yaml_text = _AGENT_YAML.format(name=name, model=model, cwd=cwd)
    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        data = yaml_text.encode()
        info = tarfile.TarInfo(name=f"{name}.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def edit_probe_session(
    live_server: str,
    runner_id: str,
    mock_llm_server_url: str,
) -> Iterator[tuple[str, str, str]]:
    """Runner-bound session whose workspace holds the pre-edit file.

    Seeds ``config.py`` with the original content BEFORE the turn so the
    journey is a genuine edit of an existing file (the diff's "before"
    side exists on disk for a fix to use).
    """
    ws = Path(tempfile.mkdtemp(prefix="omnigent-e2e-edit-diff-"))
    (ws / _FILE).write_text(_ORIGINAL_CONTENT, encoding="utf-8")
    name = f"edit_probe_{uuid.uuid4().hex[:8]}"
    model = f"edit-probe-{uuid.uuid4().hex[:8]}"

    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={
            "bundle": (
                "agent.tar.gz",
                _agent_bundle(name, model, str(ws)),
                "application/gzip",
            )
        },
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
        yield (live_server, session_id, model)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        shutil.rmtree(ws, ignore_errors=True)


@pytest.mark.timeout(300)
def test_file_edit_renders_as_diff(
    page: Page,
    edit_probe_session: tuple[str, str, str],
    mock_llm_server_url: str,
) -> None:
    """Expanding a file-edit tool call must show the change as a diff."""
    base_url, session_id, model = edit_probe_session

    # Configure both responses together because reconfiguring a queue
    # resets it: the edit tool call, then the wrap-up answer.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_edit_config",
                        "name": "sys_os_write",
                        "arguments": json.dumps({"path": _FILE, "content": _NEW_CONTENT}),
                    }
                ]
            },
            {"text": "Updated config.py: request timeout raised to 60 seconds."},
        ],
        key=model,
    )
    set_fallback_mock_llm(mock_llm_server_url, model, "Edit recorded.")

    # 1. Ask the agent to change one line of the existing file. The prompt
    #    intentionally never spells out the old assignment line.
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Raise the request timeout in config.py to 60 seconds.")
    page.get_by_role("button", name="Send", exact=True).click()

    # 2. The turn settles: the answer lands and the process trace folds.
    expect(page.locator(_ASSISTANT).last).to_contain_text(
        "Updated config.py", timeout=_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_TURN_TIMEOUT_MS)
    worked = page.get_by_test_id("turn-worked-fold")
    expect(worked).to_be_visible(timeout=_TURN_TIMEOUT_MS)

    # 3. Drill into the edit: expand the Worked fold, then the folded tool
    #    run ("Edited 1 file"), then the edit tool row itself. Click the
    #    bold verb span — clicking the path span would open the FileViewer
    #    instead of expanding the row.
    worked.locator('[data-slot="collapsible-trigger"]').first.click()
    fold = page.get_by_text("Edited 1 file", exact=True)
    expect(fold).to_be_visible()
    fold.click()
    row = page.get_by_text("Write", exact=True).first
    expect(row).to_be_visible()
    row.click()
    expect(page.get_by_text("Parameters", exact=True).first).to_be_visible()

    # Sanity (fix-safe): the edit's new line is on screen. Today it is
    # buried in the raw Parameters JSON dump; after the fix a diff view
    # shows it as the added line.
    expect(page.get_by_text(_NEW_LINE).first).to_be_visible(timeout=10_000)

    # Let the expanded edit panel paint so a recording shows its state
    # before the discriminating assertion below.
    page.wait_for_timeout(2500)

    # 4. THE DISCRIMINATOR: the change must be readable as a diff — the
    #    REPLACED line has to be visible (a diff's removed side). A card
    #    that renders the edit as a full-file replacement never shows the
    #    old line, and this fails.
    expect(page.get_by_text(_OLD_LINE).first).to_be_visible(timeout=5_000)
