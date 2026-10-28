"""E2E: real ``sys_os_edit`` outcomes cross the server and runner into edit rows.

An applied and a missed edit check the result lines, their colors and the Diff | Raw view.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _create_bundled_session,
    configure_mock_llm,
    set_fallback_mock_llm,
)

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_REPLY = "Greeting updated; the debug flag was not where I expected."

_GREETING = (
    "def greet(name):\n"
    '    print("Hello, " + name)\n'
    '    print("Welcome back")\n'
    '    print("See you soon")\n'
    "    return name\n"
)
_OLD_LINES = '    print("Hello, " + name)\n    print("Welcome back")\n    print("See you soon")\n'
_NEW_LINES = '    message = f"Hello, {name}! Welcome back."\n    print(message)\n'

_AGENT_YAML = """\
spec_version: 1
name: {name}
prompt: |
  You are a deterministic test assistant. When asked to tidy the greeting you
  call sys_os_edit twice and then reply with one short sentence.

executor:
  model: {model}
  config:
    harness: openai-agents

os_env:
  type: caller_process
  cwd: {cwd}
  sandbox:
    type: none
"""


def _edit_call(call_id: str, arguments: dict[str, str]) -> dict[str, object]:
    return {
        "tool_calls": [
            {"call_id": call_id, "name": "sys_os_edit", "arguments": json.dumps(arguments)}
        ]
    }


@pytest.fixture
def edit_session(
    live_server: str,
    runner_id: str,
    mock_llm_server_url: str,
) -> Iterator[tuple[str, str, Path]]:
    """A runner-bound session whose turn applies one edit and fails another.

    :returns: ``(base_url, session_id, workspace)``.
    """
    workspace = Path(tempfile.mkdtemp(prefix="omnigent-e2e-edit-counts-"))
    session_id: str | None = None
    try:
        (workspace / "greet.py").write_text(_GREETING)
        (workspace / "settings.py").write_text("DEBUG = False\n")
        model = f"edit-counts-probe-{uuid.uuid4().hex[:8]}"
        configure_mock_llm(
            mock_llm_server_url,
            [
                _edit_call(
                    "call_edit_greeting",
                    {"path": "greet.py", "oldText": _OLD_LINES, "newText": _NEW_LINES},
                ),
                _edit_call(
                    "call_edit_settings",
                    {"path": "settings.py", "oldText": "DEBUG = True", "newText": "DEBUG = False"},
                ),
            ],
            key=model,
        )
        set_fallback_mock_llm(mock_llm_server_url, model, _REPLY)
        name = f"edit_counts_probe_{uuid.uuid4().hex[:8]}"
        yaml_text = _AGENT_YAML.format(name=name, model=model, cwd=str(workspace))
        session_id = _create_bundled_session(
            live_server, runner_id, yaml_text, operator_authored=True
        )
        yield (live_server, session_id, workspace)
    finally:
        try:
            if session_id is not None:
                httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)


_VIEWPORT = {"width": 1280, "height": 720}


@pytest.mark.timeout(300)
# A recording at full viewport size keeps the one-line results legible.
@pytest.mark.browser_context_args(viewport=_VIEWPORT, record_video_size=_VIEWPORT)
def test_edit_rows_show_line_counts_and_diff(
    request: pytest.FixtureRequest,
    edit_session: tuple[str, str, Path],
) -> None:
    """Applied edits read as line counts; a failed edit keeps its error."""
    base_url, session_id, workspace = edit_session
    # Open the recorded page only after the session and runner are ready.
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Tidy the greeting and turn off debug.")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_ASSISTANT, has_text=_REPLY).first).to_be_visible(timeout=90_000)
    assert (workspace / "greet.py").read_text() == _GREETING.replace(_OLD_LINES, _NEW_LINES)
    assert (workspace / "settings.py").read_text() == "DEBUG = False\n"

    worked = page.get_by_test_id("turn-worked-fold")
    expect(worked).to_be_visible(timeout=30_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)
    worked.locator('[data-slot="collapsible-trigger"]').first.click()
    run = page.get_by_text(re.compile(r"^Edited \d files?$"))
    expect(run).to_be_visible(timeout=15_000)
    run.click()

    applied = page.locator('[data-slot="collapsible-trigger"]', has_text="greet.py").first
    applied_result = applied.get_by_test_id("tool-result-line")
    expect(applied_result).to_have_text("Updated with 2 additions and 3 removals")
    failed = page.locator('[data-slot="collapsible-trigger"]', has_text="settings.py").first
    failed_result = failed.get_by_test_id("tool-result-line")
    expect(failed_result).to_contain_text(
        re.compile(r"Could not find oldText in '.*settings\.py'")
    )
    expect(failed_result).not_to_contain_text("Updated")
    color = "el => getComputedStyle(el).color"
    plain = applied_result.evaluate(color)
    added = applied_result.get_by_text("2", exact=True).evaluate(color)
    removed = applied_result.get_by_text("3", exact=True).evaluate(color)
    noun = applied_result.get_by_text("additions", exact=True).evaluate(color)
    # The counts use diff colors; the words around them keep the line's plain color.
    assert len({plain, added, removed}) == 3
    assert noun == plain
    assert failed_result.evaluate(color) != plain

    applied.click()
    card = applied.locator("xpath=..")
    diff_tab = card.get_by_role("tab", name="Diff")
    expect(diff_tab).to_have_attribute("aria-selected", "true")
    diff = card.locator('[data-language="diff"]')
    expect(diff).to_contain_text('-    print("Welcome back")')
    expect(diff).to_contain_text("+    print(message)")

    raw_tab = card.get_by_role("tab", name="Raw")
    raw_tab.click()
    expect(raw_tab).to_have_attribute("aria-selected", "true")
    expect(card).to_contain_text('"path": "greet.py"')
    expect(diff).to_have_count(0)
    diff_tab.click()
    expect(diff).to_be_visible()
