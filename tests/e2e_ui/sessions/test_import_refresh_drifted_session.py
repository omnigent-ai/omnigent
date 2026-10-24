"""E2E: re-importing a grown Claude session by ID can replace its stale snapshot.

A real host daemon owns a transcript written by the real Claude Code CLI against
the mock model. The user batch-imports it from Settings › Import sessions, keeps
chatting in Claude Code on the host, then re-imports the session by id. A plain
re-import is skipped; Replace existing snapshot shows the new turn.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.host import Host, isolated_host
from tests.e2e_ui.conftest import configure_mock_llm

pytestmark = pytest.mark.min_server_version("0.20.0")

_CLAUDE_MODEL = "claude-sonnet-4-20250514"

# Ambient harness homes would redirect the host's transcript scan away from its HOME.
_HARNESS_HOME_OVERRIDES: dict[str, str | None] = {
    "CLAUDE_CONFIG_DIR": None,
    "CODEX_HOME": None,
    "QWEN_HOME": None,
    "PI_CODING_AGENT_DIR": None,
}


@dataclass
class _ClaudeHost:
    host: Host
    name: str
    session_id: str
    workdir: Path
    claude_env: dict[str, str]
    first_prompt: str
    follow_up_prompt: str


def _run_claude(claude_host: _ClaudeHost, *args: str) -> None:
    completed = subprocess.run(
        [shutil.which("claude") or "claude", "-p", "--model", _CLAUDE_MODEL, *args],
        cwd=claude_host.workdir,
        env=claude_host.claude_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]


def _transcript_text(claude_host: _ClaudeHost) -> str:
    projects = claude_host.host.home / ".claude" / "projects"
    (transcript,) = projects.glob(f"*/{claude_host.session_id}.jsonl")
    return transcript.read_text(encoding="utf-8")


@pytest.fixture
def claude_host(
    live_server: str, mock_llm_server_url: str, tmp_path: Path
) -> Iterator[_ClaudeHost]:
    """An online host whose HOME holds one Claude Code session made by the real CLI."""
    if shutil.which("claude") is None:
        pytest.skip("Claude Code CLI is not installed")
    name = f"drift-import-host-{uuid.uuid4().hex[:8]}"
    with (
        isolated_host(
            tmp_path / "host", live_server, env=_HARNESS_HOME_OVERRIDES, name=name
        ) as host,
        httpx.Client(base_url=live_server) as client,
    ):
        host.wait_online(client, timeout=60)
        workdir = host.home / "release-notes"
        workdir.mkdir()
        (host.home / ".claude.json").write_text(
            json.dumps(
                {
                    "hasCompletedOnboarding": True,
                    "projects": {str(workdir): {"hasTrustDialogAccepted": True}},
                }
            ),
            encoding="utf-8",
        )
        claude_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("ANTHROPIC_", "CLAUDE_CODE_OAUTH"))
            and key != "CLAUDE_CONFIG_DIR"
        }
        claude_env.update(
            HOME=str(host.home),
            ANTHROPIC_BASE_URL=mock_llm_server_url,
            ANTHROPIC_API_KEY="mock-key",
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        )
        # Unique prompts keep titles distinct on a server that retains earlier runs.
        nonce = uuid.uuid4().hex[:6]
        claude_host = _ClaudeHost(
            host,
            name,
            str(uuid.uuid4()),
            workdir,
            claude_env,
            first_prompt=f"draft the release checklist {nonce}",
            follow_up_prompt=f"add the rollback step {nonce}",
        )
        configure_mock_llm(
            mock_llm_server_url, [{"text": "Checklist drafted."}], match=claude_host.first_prompt
        )
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": "Rollback step added."}],
            match=claude_host.follow_up_prompt,
        )
        _run_claude(claude_host, "--session-id", claude_host.session_id, claude_host.first_prompt)
        yield claude_host


def _open_import_panel(page: Page, live_server: str, claude_host: _ClaudeHost) -> None:
    page.goto(f"{live_server}/settings/import")
    expect(page.get_by_test_id("import-sessions-panel")).to_be_visible(timeout=30_000)
    page.get_by_test_id("import-host-select").click()
    page.get_by_role("option").filter(has_text=claude_host.name).click()


def _choose_claude_session_by_id(page: Page, claude_host: _ClaudeHost) -> None:
    page.get_by_test_id("import-mode-select").click()
    page.get_by_role("option", name="Session by ID").click()
    page.get_by_test_id("import-source-select").click()
    page.get_by_role("option", name="Claude Code").click()
    page.get_by_test_id("import-session-id").fill(claude_host.session_id)


def _open_imported_session(page: Page, live_server: str, claude_host: _ClaudeHost) -> None:
    page.goto(live_server)
    sessions = page.get_by_test_id("sidebar-conversation-list")
    sessions.get_by_role("link", name=claude_host.first_prompt, exact=True).click()
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)
    transcript = page.get_by_role("log")
    expect(transcript.get_by_text(claude_host.first_prompt)).to_be_visible(timeout=30_000)


@pytest.mark.timeout(420)
def test_reimport_by_id_can_refresh_drifted_claude_session(
    request: pytest.FixtureRequest,
    live_server: str,
    claude_host: _ClaudeHost,
) -> None:
    page: Page = request.getfixturevalue("page")
    result = page.get_by_test_id("import-result")

    _open_import_panel(page, live_server, claude_host)
    page.get_by_test_id("import-source-select").click()
    page.get_by_role("option", name="Claude Code").click()
    page.get_by_test_id("import-submit").click()
    expect(result).to_contain_text("Imported 1", timeout=120_000)

    _run_claude(claude_host, "--resume", claude_host.session_id, claude_host.follow_up_prompt)
    assert claude_host.follow_up_prompt in _transcript_text(claude_host)

    _open_import_panel(page, live_server, claude_host)
    _choose_claude_session_by_id(page, claude_host)
    page.get_by_test_id("import-submit").click()
    expect(result).to_contain_text("Imported 0, 1 already imported", timeout=120_000)

    _open_imported_session(page, live_server, claude_host)
    follow_up = page.get_by_role("log").get_by_text(claude_host.follow_up_prompt)
    expect(follow_up).to_have_count(0)

    _open_import_panel(page, live_server, claude_host)
    _choose_claude_session_by_id(page, claude_host)
    page.get_by_role("switch", name="Replace existing snapshot").click()
    page.get_by_test_id("import-submit").click()
    expect(result).to_have_text("Imported 1.", timeout=120_000)

    _open_imported_session(page, live_server, claude_host)
    expect(follow_up).to_be_visible(timeout=30_000)
