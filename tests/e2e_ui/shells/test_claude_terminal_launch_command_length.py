"""UI journey regression: big-instructions Claude agents must boot.

The reported failure: a claude-native session dies at terminal auto-create
with ``RuntimeError: tmux launch failed (rc=1): command too long``. The
journey behind it is user-observable in the SPA: an author packages a
``claude-native`` agent whose instructions (the spec's ``prompt:``, resolved
to ``AgentSpec.instructions``) are large - e.g. a >16KB playbook-style system
prompt - and starts a session on it. The runner threads the instructions
verbatim onto the Claude CLI argv (``--append-system-prompt``), and
``TerminalInstance.launch`` packs the whole shell-quoted argv into ONE ``tmux
new-session`` client command; tmux's client->server protocol caps a single
command at ~16KB, so the launch exits rc=1 "command too long". The user sees
a session that never gets its terminal: the header's Chat/Terminal switcher
never appears, the session drops to ``status: failed``
(``native_terminal_start_failed``), and the chat band shows the
"Agent disconnected" pill.

The stock wrapper session (small prompt) boots fine in this exact harness -
the whole native render-parity suite proves it - so instructions size is the
only variable under test.

Expected (asserted): the session page brings up the terminal-first surface
regardless of instructions size - the view switcher appears and the Terminal
view attaches - instead of failing the session.

The sibling backend regression test
(``tests/e2e/test_claude_native_big_instructions_launch_e2e.py``) drives the
same journey server-side with a stub CLI and additionally asserts the
instructions still reach the CLI after a fix.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _bind_session_runner,
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
)

# claude-native auto-launch includes bridge prep + model-catalog probes +
# tmux boot + WS attach; generous for CI. The buggy build fails much sooner
# (the session flips to ``failed``), which short-circuits the wait.
_TERMINAL_READY_TIMEOUT_S = 240.0
_POLL_S = 1.0

# A marker planted inside the big instructions, so the artifact under test is
# unmistakably the oversized author prompt.
_PROMPT_MARKER = "OMNIGENT-BIG-PROMPT-DELIVERY-MARKER"

# tmux's client->server imsg cap is ~16KB for one command; 20K characters of
# instructions guarantee the composed ``new-session`` command exceeds it.
_INSTRUCTIONS_SIZE = 20_000

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


def _big_instructions() -> str:
    """Author-style instructions text of ``_INSTRUCTIONS_SIZE`` characters.

    :returns: A playbook-shaped prompt with the delivery marker embedded.
    """
    paragraph = (
        "Follow the team playbook: review the diff, run the linters, check "
        "the migration plan, and summarize risks before approving. "
    )
    text = f"{_PROMPT_MARKER}\n" + paragraph * (_INSTRUCTIONS_SIZE // len(paragraph) + 1)
    return text[:_INSTRUCTIONS_SIZE]


def _create_big_prompt_claude_session(base_url: str, runner_id: str) -> str:
    """Create a claude-native session whose agent carries ~20KB instructions.

    Mirrors ``conftest._create_native_claude_session`` (production wrapper
    spec, same wrapper/terminal-first labels, compat-translator arcname), with
    ``prompt:`` swapped for the oversized author text - the stock small prompt
    already rides the same ``--append-system-prompt`` channel, so size is the
    only variable.

    :param base_url: Spawned server base URL.
    :param runner_id: The token-bound runner id to bind.
    :returns: The new session/conversation id.
    """
    import io
    import json as _json
    import tarfile
    import tempfile
    from pathlib import Path

    import yaml

    from omnigent._wrapper_labels import (
        CLAUDE_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.harnesses.claude_native.main import _materialize_claude_agent_spec

    with tempfile.TemporaryDirectory() as _tmp:
        raw = yaml.safe_load(_materialize_claude_agent_spec(Path(_tmp)).read_text())
    raw["prompt"] = _big_instructions()
    yaml_text = yaml.safe_dump(raw, sort_keys=False)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname -> omnigent compat translator (the spec has
        # no spec_version), matching the native_claude_session fixture.
        info = tarfile.TarInfo("claude-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE,
    }
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": _json.dumps({"labels": labels})},
        files={"bundle": ("claude-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    _bind_session_runner(base_url, session_id, runner_id)
    return session_id


@pytest.fixture
def big_prompt_claude_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Any:
    """A runner-bound claude-native session on a ~20KB-instructions agent.

    Mirrors ``native_claude_mock_session``: mock anthropic provider config
    when ``LLM_API_KEY`` is absent, real gateway when it is set.

    :param live_server: Spawned server fixture; its runner is reused.
    :param mock_llm_server_url: Session-scoped mock LLM server base URL.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id)``.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    use_mock = not os.environ.get("LLM_API_KEY")
    ctx: Any = (
        _temp_omnigent_mock_config(mock_llm_server_url, "claude")
        if use_mock
        else contextlib.nullcontext()
    )
    with ctx:
        session_id = _create_big_prompt_claude_session(live_server, runner_id)
        try:
            yield (live_server, session_id)
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            if respawned is not None:
                respawned.terminate()
                try:
                    respawned.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    respawned.kill()
                    respawned.wait(timeout=5)


def test_big_instructions_session_boots_terminal(
    page: Page,
    big_prompt_claude_session: tuple[str, str],
) -> None:
    """A claude-native session with ~20KB instructions must get its terminal.

    Journey: start a session on a claude-native agent with a large system
    prompt, open the session page, and use it. Expected: the terminal-first
    surface comes up (Chat/Terminal switcher, terminal attaches). Buggy
    behavior: the tmux launch command carrying the instructions exceeds
    tmux's ~16KB per-command cap, the terminal never starts, the session
    flips to ``failed`` (``native_terminal_start_failed``), and the page
    shows the "Agent disconnected" pill instead of a terminal.

    :param page: Playwright page from the pytest-playwright fixture.
    :param big_prompt_claude_session: ``(base_url, session_id)``.
    """
    base_url, session_id = big_prompt_claude_session
    page.goto(f"{base_url}/c/{session_id}")

    # The terminal bring-up and the failure race; poll both so the buggy
    # build fails fast with the structured cause instead of timing out.
    deadline = time.monotonic() + _TERMINAL_READY_TIMEOUT_S
    failed_error: dict[str, str] | None = None
    while time.monotonic() < deadline:
        if page.get_by_test_id("view-mode-toggle").count() > 0:
            break
        snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
        if snapshot.get("status") == "failed":
            failed_error = snapshot.get("last_task_error")
            break
        page.wait_for_timeout(int(_POLL_S * 1000))

    if failed_error is not None:
        # Let the failure land on screen (the disconnected pill) so a video
        # of this run ends on the user-visible outcome, then fail with the
        # structured cause.
        with contextlib.suppress(AssertionError):
            expect(page.get_by_test_id("disconnected-indicator")).to_be_visible(timeout=30_000)
        page.wait_for_timeout(2_000)
        pytest.fail(
            f"claude-native session with {_INSTRUCTIONS_SIZE} chars of agent "
            "instructions failed instead of booting its terminal: "
            f"{failed_error} - the tmux new-session command carrying "
            "--append-system-prompt exceeded tmux's ~16KB per-command cap "
            "('command too long'); see the runner log it references"
        )

    # Desired behavior: the terminal-first surface is up - switch to the
    # Terminal view and see the pane attach.
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=30_000)
    segment = page.get_by_test_id("view-mode-terminal")
    expect(segment).to_be_enabled(timeout=30_000)
    segment.click()
    terminal = page.locator('[data-testid="terminal-view"]').last
    expect(terminal).to_have_attribute("data-state", "connected", timeout=120_000)
    # Linger so a recording of the fixed run ends on the attached terminal.
    page.wait_for_timeout(2_000)
