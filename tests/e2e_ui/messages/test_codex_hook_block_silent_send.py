r"""UI journey: a native Codex turn blocked by a UserPromptSubmit hook must not
fail silently on the web Chat view.

A user-authored ``UserPromptSubmit`` command hook that exits non-zero blocks the
turn before any model call. The Terminal/TUI view prints ``Blocked by hook`` and
the hook's stderr; the web Chat view must surface the same failure instead of
echoing the user's message and settling to idle with no error, notice, or reply.

The hook runs even though it is untrusted because runner-owned (web) sessions
launch Codex with ``--dangerously-bypass-hook-trust``. The minimal missing-script
hook here blocks identically to a plugin whose ``UserPromptSubmit`` hook script
is absent.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shlex
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.runner.identity import token_bound_runner_id
from tests.e2e_ui.conftest import (
    _HEALTH_POLL_INTERVAL_S,
    _HEALTH_TIMEOUT_S,
    _REPO_ROOT,
    _create_native_codex_session,
    _server_state,
    _temp_omnigent_mock_config,
)

from .test_message_render_parity import _USER, _ensure_chat_view, _send
from .test_native_codex_render_parity import (
    _MOCK_TURN_TIMEOUT_MS,
    _open_terminal_view,
    _wait_terminal_connected,
)


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _spawn_runner_with_codex_home(
    base_url: str,
    codex_home: Path,
    mock_llm_server_url: str,
    log_path: Path,
) -> tuple[subprocess.Popen[bytes], str]:
    """Spawn a runner whose source ``CODEX_HOME`` is *codex_home* and wait until it is online.

    A dedicated runner keeps the blocking hook out of the shared runner's
    ``CODEX_HOME`` (which may be a developer's real ``~/.codex``).

    :param base_url: Spawned server base URL.
    :param codex_home: Private ``CODEX_HOME`` the runner symlinks hooks from.
    :param mock_llm_server_url: Mock LLM server the harness is routed to.
    :param log_path: Runner log file.
    :returns: ``(process, runner_id)``; the caller terminates the process.
    """
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
        "CODEX_HOME": str(codex_home),
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
    }
    with log_path.open("w") as log_handle:
        proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    deadline = time.monotonic() + _HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"hook-blocked runner exited early (code {proc.returncode}); "
                f"log:\n{log_path.read_text()[-3000:]}"
            )
        with contextlib.suppress(httpx.HTTPError):
            resp = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
            if resp.status_code == 200 and resp.json().get("online") is True:
                return proc, runner_id
        time.sleep(_HEALTH_POLL_INTERVAL_S)
    _terminate(proc)
    raise RuntimeError(
        f"hook-blocked runner did not register within {_HEALTH_TIMEOUT_S:.0f}s; "
        f"log:\n{log_path.read_text()[-3000:]}"
    )


@pytest.fixture
def hook_blocked_native_codex_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[tuple[str, str, Path]]:
    """A native Codex session whose first turn is blocked by a UserPromptSubmit hook.

    The session is bound to a dedicated runner whose private ``CODEX_HOME``
    carries the hook. The hook runs ``python3`` on a path under a fresh temp
    dir, which does not exist, so python3 exits with status 2 and Codex blocks
    the turn ("Blocked by hook").

    :returns: ``(base_url, session_id, missing_script)``.
    """
    if request.config.getoption("--ui-base-url") or _server_state.get("workflow_owned"):
        pytest.skip("requires the spawned local server so a dedicated runner can be bound")
    runner_tmp = tmp_path_factory.mktemp("hook_blocked_runner")
    missing_script = runner_tmp / "missing-plugin" / "activate.py"
    codex_home = runner_tmp / "codex-home"
    codex_home.mkdir()
    hook_command = f"python3 {shlex.quote(str(missing_script))}"
    (codex_home / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": hook_command}]}]
                }
            }
        ),
        encoding="utf-8",
    )
    with _temp_omnigent_mock_config(mock_llm_server_url, "codex"):
        proc, runner_id = _spawn_runner_with_codex_home(
            live_server, codex_home, mock_llm_server_url, runner_tmp / "runner.log"
        )
        session_id: str | None = None
        try:
            session_id = _create_native_codex_session(live_server, runner_id)
            yield (live_server, session_id, missing_script)
        finally:
            if session_id is not None:
                with contextlib.suppress(httpx.HTTPError):
                    httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            _terminate(proc)


@pytest.mark.nightly
@pytest.mark.timeout(300)
def test_native_codex_hook_block_surfaces_error_on_web(
    request: pytest.FixtureRequest,
    hook_blocked_native_codex_session: tuple[str, str, Path],
) -> None:
    """A hook-blocked turn surfaces an error on the web Chat view (not silent)."""
    base_url, session_id, missing_script = hook_blocked_native_codex_session
    # Create the recorded page only after the non-browser setup above.
    page: Page = request.getfixturevalue("page")

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    _send(page, "That's a lot of comments we hid, are those all comments we should hide?")

    # The message is accepted and echoed as a user bubble...
    expect(page.locator(_USER)).to_have_count(1, timeout=_MOCK_TURN_TIMEOUT_MS)
    # ...but the UserPromptSubmit hook blocks the turn. The web Chat view must
    # surface the block rather than settling to idle with no error. Info-level
    # notices reuse the pill test id, so match the failure tone explicitly.
    pill = page.locator('[data-testid="error-pill"][data-level="error"]').first
    expect(pill).to_be_visible(timeout=_MOCK_TURN_TIMEOUT_MS)
    # The notice carries the hook's own reason, as the Terminal view does.
    content = page.get_by_test_id("error-message-content").first
    if not content.is_visible():
        pill.click()
    expect(content).to_be_visible(timeout=10_000)
    expect(content).to_contain_text("Blocked by hook")
    expect(content).to_contain_text(str(missing_script))
