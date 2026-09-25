"""UI journey regression: big-instructions Claude agents must boot their terminal.

The runner launches Claude Code by handing the whole ``claude`` argv - including
``--append-system-prompt <AgentSpec.instructions>`` - to a single ``tmux
new-session`` command. tmux caps one client command at ~16KB, so an agent whose
instructions run to a long playbook never got a terminal: tmux exited with
``command too long``, the runner marked the session ``failed``
(``native_terminal_start_failed``), the chat band showed an error pill and the
Terminal view only offered to resume the session.

Journey: start a session on a claude-native agent whose instructions are ~20KB,
open the session page and switch to the Terminal view.

* Buggy build: the session flips to ``failed``; the chat shows "Native Claude
  terminal failed to start (RuntimeError) ..." and the runner log ends in ``tmux
  launch failed (rc=1): command too long``. This test FAILS with that cause.
* Fixed build: the Terminal view attaches to a live Claude Code pane and the
  session never fails. This test PASSES.

The stock wrapper session (small prompt) boots fine in this exact harness - the
native render-parity suite proves it - so instructions size is the only variable
under test. The sibling backend regression test
(``tests/e2e/test_claude_native_big_instructions_launch_e2e.py``) drives the
same journey server-side with a stub CLI and additionally asserts the
instructions still reach the CLI.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
)

# Well past tmux's ~16KB per-command cap; the stock ~2KB prompt launches fine.
_INSTRUCTIONS_CHARS = 20_000
# The switcher renders as soon as the session page loads. Attaching the pane
# includes bridge prep + model-catalog probes + tmux boot + WS attach; generous
# for CI. The buggy build fails the session within seconds, which
# short-circuits the wait.
_SWITCHER_TIMEOUT_MS = 60_000
_TERMINAL_READY_TIMEOUT_S = 240.0
_POLL_S = 1.0

# CI shells carry an egress proxy that must not intercept loopback requests.
_client = httpx.Client(trust_env=False)

pytestmark = [
    pytest.mark.nightly,
    pytest.mark.timeout(420),
    pytest.mark.skipif(
        shutil.which("tmux") is None,
        reason="claude-native terminals run inside tmux; tmux not installed",
    ),
    pytest.mark.skipif(
        shutil.which("claude") is None,
        reason="requires the claude CLI on PATH (native wrapper launch)",
    ),
]


def _oversized_claude_native_spec() -> str:
    """The stock ``omnigent claude`` wrapper spec with a ~20KB playbook as its prompt.

    Reusing the production wrapper spec keeps everything but the instructions
    size identical to what ``omnigent claude`` and the web UI ship; the stock
    prompt already rides the same ``--append-system-prompt`` channel.
    """
    from omnigent.harnesses.claude_native.main import _materialize_claude_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        raw = yaml.safe_load(_materialize_claude_agent_spec(Path(tmp)).read_text())
    playbook = "You are a meticulous release engineer. Follow the playbook exactly. "
    raw["prompt"] = (playbook * (_INSTRUCTIONS_CHARS // len(playbook) + 1))[:_INSTRUCTIONS_CHARS]
    return yaml.safe_dump(raw, sort_keys=False)


def _create_claude_native_session(base_url: str, runner_id: str, yaml_text: str) -> str:
    """Register the wrapper agent from *yaml_text*, create its session and bind the runner.

    Mirrors ``conftest._create_native_claude_session`` (wrapper/terminal-first
    labels, compat-translator arcname) with the oversized spec swapped in.
    """
    from omnigent._wrapper_labels import (
        CLAUDE_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # A non-config.yaml arcname routes the spec_version-less wrapper spec
        # through the compat translator, as the omnigent claude CLI does.
        info = tarfile.TarInfo("claude-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE,
    }
    create = _client.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"labels": labels})},
        files={"bundle": ("claude-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    bind = _client.patch(
        f"{base_url}/v1/sessions/{session_id}", json={"runner_id": runner_id}, timeout=10.0
    )
    bind.raise_for_status()
    return session_id


@pytest.fixture
def oversized_claude_native_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A runner-bound claude-native session whose agent instructions are ~20KB.

    Mirrors ``native_claude_mock_session``: a standalone run installs the mock
    anthropic provider config so the real ``claude`` CLI needs no credentials;
    a workflow-owned runner is already configured.

    :returns: ``(base_url, session_id)``.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    ctx: Any = (
        contextlib.nullcontext()
        if _server_state.get("workflow_owned")
        else _temp_omnigent_mock_config(mock_llm_server_url, "claude")
    )
    with ctx:
        session_id = _create_claude_native_session(
            live_server, runner_id, _oversized_claude_native_spec()
        )
        try:
            yield live_server, session_id
        finally:
            _client.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            if respawned is not None:
                respawned.terminate()
                try:
                    respawned.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    respawned.kill()
                    respawned.wait(timeout=5)


def _session_snapshot(base_url: str, session_id: str) -> dict[str, Any]:
    snapshot: dict[str, Any] = _client.get(
        f"{base_url}/v1/sessions/{session_id}", timeout=10.0
    ).json()
    return snapshot


def _runner_log_cause(message: str) -> str | None:
    """Return the tmux launch failure line from the runner log the error points at."""
    match = re.search(r"see the runner log for details: (\S+)", message)
    if match is None:
        return None
    log_path = Path(match.group(1)).expanduser()
    if not log_path.is_file():
        return None
    for line in reversed(log_path.read_text(errors="replace").splitlines()):
        if "tmux launch failed" in line:
            return line.strip()
    return None


def _launch_failure_report(snapshot: dict[str, Any], shown: str) -> str:
    error = snapshot.get("last_task_error") or {}
    lines = [
        "Claude Code terminal never started for a claude-native session whose agent "
        f"instructions are {_INSTRUCTIONS_CHARS} chars.",
        f"UI shows: {shown}",
        f"session status={snapshot.get('status')} last_task_error={json.dumps(error)}",
    ]
    cause = _runner_log_cause(str(error.get("message", "")))
    if cause:
        lines.append(f"runner log: {cause}")
    return "\n".join(lines)


def _show_chat_failure(page: Page) -> str:
    """Switch to the Chat view, expand the failure the user sees and return its headline.

    The Terminal view of a failed session only offers to resume it; the chat
    band carries the error pill (or the disconnected indicator) with the cause.
    """
    page.get_by_test_id("view-mode-chat").click()
    failure = page.locator(
        '[data-testid="error-pill"], [data-testid="disconnected-indicator"]'
    ).first
    expect(failure).to_be_visible(timeout=30_000)
    error_pill = page.get_by_test_id("error-pill")
    if error_pill.count() == 0:
        shown = failure.inner_text()
    else:
        error_pill.first.click()
        expect(error_pill.first.get_by_test_id("error-message-content")).to_be_visible()
        shown = error_pill.first.get_by_test_id("error-headline").inner_text()
    # Keep the cause on screen long enough to read in a recording.
    page.wait_for_timeout(2_000)
    return shown


def test_big_instructions_session_boots_terminal(
    page: Page,
    oversized_claude_native_session: tuple[str, str],
) -> None:
    """Switching to the Terminal view attaches a live Claude Code pane, not a start failure."""
    base_url, session_id = oversized_claude_native_session
    page.goto(f"{base_url}/c/{session_id}")
    deadline = time.monotonic() + _TERMINAL_READY_TIMEOUT_S

    # The switcher renders for every terminal-mode session, so it does not prove
    # the terminal launched; only the Terminal view attaching does.
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=_SWITCHER_TIMEOUT_MS)
    segment = page.get_by_test_id("view-mode-terminal")
    expect(segment).to_be_enabled(timeout=int(_TERMINAL_READY_TIMEOUT_S * 1000))
    segment.click()

    connected = page.locator('[data-testid="terminal-view"][data-state="connected"]')
    while time.monotonic() < deadline and connected.count() == 0:
        snapshot = _session_snapshot(base_url, session_id)
        if snapshot.get("status") == "failed":
            pytest.fail(_launch_failure_report(snapshot, _show_chat_failure(page)))
        page.wait_for_timeout(int(_POLL_S * 1000))

    if connected.count() == 0:
        pytest.fail(
            _launch_failure_report(
                _session_snapshot(base_url, session_id),
                f"Terminal view did not attach within {_TERMINAL_READY_TIMEOUT_S:.0f}s",
            )
        )
    snapshot = _session_snapshot(base_url, session_id)
    assert snapshot["status"] != "failed", snapshot.get("last_task_error")
    # Linger so a recording of the fixed run ends on the attached terminal.
    page.wait_for_timeout(2_000)
