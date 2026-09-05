"""E2E coverage for links to files created on an out-of-process runner."""

from __future__ import annotations

import gzip
import io
import json
import re
import shutil
import tarfile
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm, set_fallback_mock_llm

_FILE = "report.md"
_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'

# The per-fixture model gives each test an isolated mock-LLM queue.
_AGENT_YAML = """\
name: {name}
prompt: |
  You are a deterministic test assistant. When asked to create a report you
  write the file with a shell command and then reply with a link to it.

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
def created_file_session(
    live_server: str,
    runner_id: str,
    mock_llm_server_url: str,
) -> Iterator[tuple[str, str, str, str]]:
    """Create an isolated runner-bound session for a file-writing turn."""
    ws = Path(tempfile.mkdtemp(prefix="omnigent-e2e-created-file-links-"))
    name = f"created_file_probe_{uuid.uuid4().hex[:8]}"
    model = f"created-file-probe-{uuid.uuid4().hex[:8]}"

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

        env_resp = httpx.get(
            f"{live_server}/v1/sessions/{session_id}/resources/environments/default",
            timeout=10.0,
        )
        env_resp.raise_for_status()
        root = env_resp.json().get("metadata", {}).get("root")
        assert root, "environment must report metadata.root"

        yield (live_server, session_id, root, model)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        shutil.rmtree(ws, ignore_errors=True)


def _drive_create_turn(
    page: Page,
    base_url: str,
    session_id: str,
    mock_llm_server_url: str,
    model: str,
    reply_text: str,
) -> None:
    """Have the agent create ``report.md`` and reply with the supplied link."""
    # Configure both responses together because reconfiguring a queue resets it.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_write_report",
                        "name": "sys_os_shell",
                        "arguments": json.dumps(
                            {"command": f'printf "# Report\\n\\nDone.\\n" > {_FILE}'}
                        ),
                    }
                ]
            },
            {"text": reply_text},
        ],
        key=model,
    )
    set_fallback_mock_llm(mock_llm_server_url, model, "Report created.")

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Create report.md and give me a link to it.")
    page.get_by_role("button", name="Send", exact=True).click()

    expect(page.locator(_ASSISTANT).last).to_contain_text(_FILE, timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)


def _assert_link_opens_file_viewer(page: Page) -> None:
    """Assert that the created-file link opens the FileViewer."""
    link = page.get_by_test_id("assistant-text-section").last.get_by_role("button", name=_FILE)
    # Linkification waits for the changed-files or existence query to settle.
    expect(link).to_be_visible(timeout=30_000)
    link.click()
    page.wait_for_url(re.compile(r"[?&]file=report\.md(?:&|$)"), timeout=15_000)
    expect(page.get_by_test_id("file-viewer").last).to_contain_text("Report", timeout=15_000)


def test_created_file_uri_link_opens_file(
    page: Page,
    created_file_session: tuple[str, str, str, str],
    mock_llm_server_url: str,
) -> None:
    """A ``file://`` link opens the created file instead of rendering blocked."""
    base_url, session_id, root, model = created_file_session
    _drive_create_turn(
        page,
        base_url,
        session_id,
        mock_llm_server_url,
        model,
        f"I created the report for you: [{_FILE}](file://{root}/{_FILE})",
    )

    expect(page.get_by_text("[blocked]")).to_have_count(0)
    _assert_link_opens_file_viewer(page)


def test_created_file_absolute_path_link_opens_file(
    page: Page,
    created_file_session: tuple[str, str, str, str],
    mock_llm_server_url: str,
) -> None:
    """An absolute-path link provides the remote-session baseline."""
    base_url, session_id, root, model = created_file_session
    _drive_create_turn(
        page,
        base_url,
        session_id,
        mock_llm_server_url,
        model,
        f"I created the report for you: [{_FILE}]({root}/{_FILE})",
    )
    _assert_link_opens_file_viewer(page)
