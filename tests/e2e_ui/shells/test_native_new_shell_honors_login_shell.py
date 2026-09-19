"""E2E (native-wrapper half): "+ -> Shell" honors the user's $SHELL.

New terminals must use the user's default shell ($SHELL) instead of always
bash. For *native-wrapper* sessions the wrapper spec declares the host's installed shells with the
user's ``$SHELL`` first (``native_shell_terminal_spec`` ->
``omnigent._platform.installed_interactive_shells``), and the "+ (Open
new) -> Shell" default launches that first entry. This test pins that
behavior -- the counterpart of ``test_new_shell_uses_login_shell.py``
(the SDK-agent half) -- so the native half can't regress independently.

``SHELL`` is pinned in *this* process because the fixture materializes the
production wrapper spec in-process (``_materialize_claude_agent_spec``),
exactly like ``omnigent claude`` does on the user's machine -- the machine
whose ``$SHELL`` the shells picker is supposed to follow. ``/bin/sh``
stands in for the reporter's zsh purely as the non-bash login-shell value
(always installed; a member of
``omnigent._platform._KNOWN_INTERACTIVE_SHELLS`` like zsh).

The which-shell probe is a file side-effect at an absolute ``tmp_path``
(the pane and this test share a host) because xterm renders to a WebGL
canvas, so pane output never reaches the DOM.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _create_native_claude_session,
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
    open_right_rail,
)

# The user's login shell for this journey: always installed, non-bash, and a
# member of ``omnigent._platform._KNOWN_INTERACTIVE_SHELLS`` (like the
# reporter's zsh, which Ubuntu CI images don't ship).
_LOGIN_SHELL = "/bin/sh"

pytestmark = pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="claude CLI not installed; the native-wrapper lane can't launch",
)


@pytest.fixture
def native_login_shell_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[str, str]]:
    """A claude-native wrapper session materialized under ``SHELL=/bin/sh``.

    Mirrors ``native_claude_mock_session`` (mock provider config, shared
    runner, wrapper labels) but pins ``$SHELL`` before the in-process spec
    materialization so the declared shells picker follows it -- the exact
    production condition of a user whose login shell is not bash running
    ``omnigent claude``.

    :returns: ``(base_url, session_id)``.
    """
    monkeypatch.setenv("SHELL", _LOGIN_SHELL)
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    with _temp_omnigent_mock_config(mock_llm_server_url, "claude"):
        session_id = _create_native_claude_session(live_server, runner_id)
        try:
            yield (live_server, session_id)
        finally:
            try:
                httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            finally:
                if respawned is not None:
                    respawned.terminate()
                    try:
                        respawned.wait(timeout=5)
                    except Exception:
                        respawned.kill()


def test_native_new_shell_uses_users_login_shell(
    page: Page, native_login_shell_session: tuple[str, str], tmp_path: Path
) -> None:
    """The native session's "+ -> Shell" default launches the user's $SHELL.

    The declared shells follow the login shell (``sh`` first), the "+"
    (Open new) menu names it as the default -- "Shell (sh)" -- and clicking
    it opens a terminal actually running ``sh``, verified by having the
    pane report ``$0`` into a probe file.
    """
    base_url, session_id = native_login_shell_session
    expected = os.path.basename(_LOGIN_SHELL)
    probe = tmp_path / "native_login_shell_probe.txt"

    # The wrapper spec declares the host's installed shells with the user's
    # login shell first -- the picker default. Assert it early over the agent
    # API for a crisp failure mode before driving the UI.
    agent_resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/agent", timeout=10.0)
    agent_resp.raise_for_status()
    declared = agent_resp.json().get("terminals") or []
    assert declared and declared[0] == expected, (
        f"native wrapper should declare the user's login shell first, "
        f"got {declared!r} with SHELL={_LOGIN_SHELL!r}"
    )

    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")

    # Same "+ (Open new) -> Shell (<default>)" journey as the SDK-half test;
    # on this session the default named in the row is the login shell.
    rail.get_by_role("button", name="Open new").click()
    menu_item = page.get_by_role("menuitem", name=re.compile(r"^Shell"))
    expect(menu_item).to_contain_text(f"Shell ({expected})")
    menu_item.click()

    # The user-created shell's terminal id carries the user-minted "u-"
    # session key (terminal_<name>_u-xxxxxx), which distinguishes it from the
    # agent's own claude pane (terminal_claude_main) wherever it mounts.
    shell_view = page.locator('[data-testid="terminal-view"][data-terminal-id*="_u-"]').last
    expect(shell_view).to_be_visible(timeout=60_000)
    expect(shell_view).to_have_attribute("data-state", "connected", timeout=20_000)
    page.wait_for_timeout(1_000)

    textarea = shell_view.locator("textarea.xterm-helper-textarea")
    textarea.focus()
    page.keyboard.type(f'echo "login shell is $SHELL and this terminal runs: $0" | tee {probe}')
    page.keyboard.press("Enter")

    deadline = time.monotonic() + 20
    reported: str | None = None
    while time.monotonic() < deadline:
        if probe.exists():
            match = re.search(r"runs: (\S+)", probe.read_text())
            if match:
                reported = match.group(1)
                break
        page.wait_for_timeout(500)
    assert reported is not None, "the new shell never executed the probe command"

    running = os.path.basename(reported).lstrip("-")
    assert running == expected, (
        f"the native session's new terminal is running {running!r}, but the "
        f"user's login shell ($SHELL) is {_LOGIN_SHELL!r}"
    )
