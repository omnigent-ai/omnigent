"""E2E: a native Claude Code write in a non-git workspace must reach Changes.

A claude-native ("Claude Code") session whose workspace is a plain non-git
folder tracks changes through ``AgentEditFilesystemRegistry``, which records
only writes dispatched through the runner's ``sys_os_write`` / ``sys_os_edit``
tools. Claude Code writes files with its own native ``Write`` tool, which
never reaches that registry: the file lands on disk, but
``GET .../environments/{id}/changes`` stays empty and the Workspace rail's
Changes tab shows "No workspace changes yet".

This drives the reported user journey end to end with no interception: a
runner-bound claude-native session (the same terminal-first spec ``omnigent
claude`` ships) pinned to a fresh non-git workspace, a web-composer message
asking Claude to create a file, the real ``claude`` CLI executing its native
Write tool (proven by the file appearing on disk), and the Workspace rail's
Changes tab selected. The assertions encode the CORRECT behavior — the
natively written file must be listed — so this test fails on a build with the
bug and passes once native writes are tracked.

Skips when the ``claude`` CLI is unavailable. With ``LLM_API_KEY`` set the
turn runs against the real gateway; without it the mock LLM scripts the
``Write`` tool call, keeping the journey deterministic in keyless
environments.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
    configure_mock_llm,
    open_right_rail,
    reset_mock_llm,
)

_FILE_NAME = "native_write_report.txt"
_FILE_CONTENT = "hello from Claude Code's native Write tool\n"

# claude-native auto-launch + first-run pre-accept + WS attach.
_TERMINAL_READY_TIMEOUT_MS = 120_000
# How long the native Write may take to land on disk (CLI boot + turn).
_WRITE_TIMEOUT_S = 240.0


def _create_claude_session_in_workspace(base_url: str, runner_id: str, workspace: Path) -> str:
    """Register a claude-native wrapper session launched in *workspace*.

    Mirrors conftest's ``_create_native_claude_session`` (the spec ``omnigent
    claude`` materialises plus the wrapper/terminal labels it stamps), with
    two session-level additions: ``metadata.workspace`` pins the non-git
    launch dir the per-session filesystem registry resolves against, and
    ``--permission-mode acceptEdits`` lets the native Write run without a
    per-file approval pause.

    :param base_url: Spawned server base URL.
    :param runner_id: The token-bound runner id to bind.
    :param workspace: The plain non-git directory to launch Claude Code in.
    :returns: The new session/conversation id.
    """
    from omnigent._wrapper_labels import (
        CLAUDE_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.harnesses.claude_native.main import _materialize_claude_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = _materialize_claude_agent_spec(Path(tmp)).read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname routes the spec (no spec_version) through
        # the omnigent compat translator, matching the conftest fixture.
        info = tarfile.TarInfo("claude-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    metadata = {
        "labels": {
            UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
            WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE,
        },
        "workspace": str(workspace),
        "terminal_launch_args": ["--permission-mode", "acceptEdits"],
    }
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("claude-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    patch = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch.raise_for_status()
    return session_id


@pytest.fixture
def native_claude_nongit_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path]]:
    """A runner-bound claude-native session in a fresh non-git workspace.

    :param live_server: Spawned server fixture; its runner is reused.
    :param mock_llm_server_url: Session-scoped mock LLM server base URL.
    :param tmp_path: Per-test dir for the non-git workspace (outside any repo).
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id, workspace)``.
    """
    if shutil.which("claude") is None:
        pytest.skip("claude CLI is required for the native-write changes e2e")
    workspace = tmp_path / "plain-folder"
    workspace.mkdir()

    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    use_mock = not os.environ.get("LLM_API_KEY")
    ctx: Any = (
        _temp_omnigent_mock_config(mock_llm_server_url, "claude")
        if use_mock
        else contextlib.nullcontext()
    )
    with ctx:
        session_id = _create_claude_session_in_workspace(live_server, runner_id, workspace)
        try:
            yield (live_server, session_id, workspace)
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            if respawned is not None:
                respawned.terminate()
                try:
                    respawned.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    respawned.kill()
                    respawned.wait(timeout=5)


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_native_claude_write_lands_in_changes(
    page: Page,
    native_claude_nongit_session: tuple[str, str, Path],
    mock_llm_server_url: str,
) -> None:
    """A file Claude Code writes natively appears in the session's Changes."""
    base_url, session_id, workspace = native_claude_nongit_session
    target = workspace / _FILE_NAME
    use_mock = not os.environ.get("LLM_API_KEY")

    if use_mock:
        reset_mock_llm(mock_llm_server_url)
        go_token = f"native-write-go-{uuid.uuid4().hex[:6]}"
        write_args = json.dumps({"file_path": str(target), "content": _FILE_CONTENT})
        # Scripted Write turn for the composer message (matched by go_token);
        # the extra copy absorbs a background request draining the queue.
        configure_mock_llm(
            mock_llm_server_url,
            [{"tool_calls": [{"name": "Write", "arguments": write_args}]}] * 2,
            key="native-write",
            match=go_token,
        )
        # Post-tool requests carry the Write tool_result, which names the
        # file; that longer match token outranks go_token, so the turn closes
        # with plain text instead of a second Write.
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": "created the file"}] * 3,
            key="native-write-done",
            match=_FILE_NAME,
        )
        prompt = f"create the report file now {go_token}"
    else:
        prompt = (
            f"Use your Write tool to create the file {target} containing exactly "
            f"'{_FILE_CONTENT.strip()}'. Do nothing else and use no other tools."
        )

    page.goto(f"{base_url}/c/{session_id}")

    # Terminal-first session: wait for the live Claude Code TUI to attach
    # before sending, so the bridge can inject the composer message into it.
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(
        timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    terminal_segment = page.get_by_test_id("view-mode-terminal")
    expect(terminal_segment).to_be_enabled(timeout=30_000)
    terminal_segment.click()
    expect(page.locator('[data-testid="terminal-view"]').last).to_have_attribute(
        "data-state", "connected", timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    chat_segment = page.get_by_test_id("view-mode-chat")
    expect(chat_segment).to_be_enabled(timeout=30_000)
    chat_segment.click()

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(prompt)
    page.get_by_role("button", name="Send", exact=True).click()

    # The native Write's observable side effect: the file lands on disk.
    deadline = time.monotonic() + _WRITE_TIMEOUT_S
    while not target.exists() and time.monotonic() < deadline:
        page.wait_for_timeout(1_000)
    if not target.exists():
        # Failure diagnostics: what the live TUI showed and what the canonical
        # transcript recorded, so a stall is attributable from the report.
        with contextlib.suppress(Exception):
            terminal_segment.click()
            page.wait_for_timeout(1_000)
            tui_text = page.locator('[data-testid="terminal-view"]').last.inner_text()
            print(f"--- terminal pane at write-timeout ---\n{tui_text}\n---")
        with contextlib.suppress(Exception):
            items = httpx.get(f"{base_url}/v1/sessions/{session_id}/items", timeout=15.0).json()
            print(f"--- session items at write-timeout ---\n{json.dumps(items)[-4000:]}\n---")
    assert target.exists(), (
        f"Claude Code's native Write never created {target} — the journey did "
        "not reach the state the changes assertions need."
    )

    # The file exists on disk, so the session's changed-files view must list
    # it. Open the rail only now, so the panel's first fetch is post-write.
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    changes_tab = rail.get_by_role("tab", name=re.compile("^Changes"))
    changes_tab.click()
    expect(changes_tab).to_have_attribute("aria-selected", "true")

    # Correct behavior: the natively written file is listed. On a build with
    # the bug the panel shows "No workspace changes yet" instead, and this
    # assertion is what fails.
    expect(rail.get_by_text(_FILE_NAME).first).to_be_visible(timeout=30_000)

    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/changes",
        timeout=30.0,
    )
    resp.raise_for_status()
    paths = [entry["path"] for entry in resp.json().get("data", [])]
    assert _FILE_NAME in paths, f"changes endpoint omitted the native write: {paths}"
