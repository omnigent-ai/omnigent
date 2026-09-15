r"""UI journey: merely opening a session must not run ``/effort`` in it.

An explicit effort pick in the composer's session-config gear is also saved as
the cross-session sticky preference. ``bindStream``'s sticky-pref handoff then
PATCHes that effort onto any effort-capable session the user merely OPENS that
has no persisted effort of its own. The new-session pre-bind seed sends
``silent: true`` (persist only), but the bind-time handoff omits it, so the
server live-forwards an ``effort_change`` to the runner, which types
``/effort <level>`` into the session's live Claude Code pane and the forwarder
mirrors it back as a command receipt in the chat transcript. The user watches a
slash command they never issued run inside an ongoing session.

The journey (two real claude-native sessions against the mock LLM; no agent
turns are needed — the injection fires on session open, not on a turn):

1. Open Claude Code session A and set Effort → High via the composer config
   gear. This is the user's own, intentional change; it is expected to reach
   A's pane (that propagation is the driving checkpoint proving the live
   injection pipeline works, so a clean session B below is meaningful).
2. Open Claude Code session B — an ongoing session whose effort was never
   touched — and type nothing.
3. Buggy build: ``/effort high`` runs by itself in B's terminal and/or an
   ``effort`` command receipt appears in B's chat transcript; the final
   assertions fail. Fixed build: nothing user-visible happens in B; the test
   passes.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _create_native_claude_session,
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
)
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _TMUX_ADVERT_FILE,
    _open_terminal_view,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)


@pytest.fixture
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    """Record the journey via the sync ``page`` fixture's own context.

    The conftest's ``OMNIGENT_E2E_RECORD_DIR`` hook only patches the async
    Browser; this test drives the sync ``page`` fixture, so wire the same
    record dir onto its context here.
    """
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        return {**browser_context_args, "record_video_dir": record_dir}
    return {**browser_context_args}


_CONFIG_GEAR = "composer-config-gear"
_EFFORT_ROW = "composer-agent-effort-select"
_EFFORT_HIGH = "composer-agent-effort-high"
_SLASH_CARD = '[data-testid="slash-command-card"]'

# Generous: covers the PATCH round-trip, the runner's pane-readiness heal, the
# tmux typing, and Claude Code executing the command.
_INJECTION_TIMEOUT_S = 90.0
# How long session B is watched for an injection before it is declared clean.
_SETTLE_MS = 20_000


@pytest.fixture
def native_claude_session_pair(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, str]]:
    """Two runner-bound claude-native sessions in the same rig.

    Mirrors ``native_claude_mock_session`` (mock anthropic provider when
    ``LLM_API_KEY`` is absent) but creates two sessions: one where the user
    picks an effort, and one that must stay untouched.

    :returns: ``(base_url, session_a_id, session_b_id)``.
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
        session_a = _create_native_claude_session(live_server, runner_id)
        session_b = _create_native_claude_session(live_server, runner_id)
        try:
            yield (live_server, session_a, session_b)
        finally:
            for sid in (session_a, session_b):
                httpx.delete(f"{live_server}/v1/sessions/{sid}", timeout=10.0)
            if respawned is not None:
                respawned.terminate()
                try:
                    respawned.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    respawned.kill()
                    respawned.wait(timeout=5)


def _session_reasoning_effort(base_url: str, session_id: str) -> str | None:
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("reasoning_effort")


def _pane_history(base_url: str, session_id: str) -> str:
    """Capture the session pane's screen plus scrollback from tmux.

    The SPA renders the terminal on a WebGL canvas (no DOM text), so the tmux
    pane — the exact content the Terminal view displays — is read directly,
    including scrollback so an executed command stays observable after the
    TUI redraws.

    :returns: Pane text, or ``""`` before the terminal is advertised.
    """
    from omnigent.harnesses.claude_native.bridge import (
        BRIDGE_ID_LABEL_KEY,
        bridge_dir_for_bridge_id,
    )

    session = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
    labels = session.get("labels") or {}
    bridge_id = labels.get(BRIDGE_ID_LABEL_KEY) or session_id
    advert = bridge_dir_for_bridge_id(bridge_id) / _TMUX_ADVERT_FILE
    if not advert.exists():
        return ""
    info = json.loads(advert.read_text(encoding="utf-8"))
    proc = subprocess.run(
        [
            "tmux",
            "-S",
            info["socket_path"],
            "capture-pane",
            "-t",
            info["tmux_target"],
            "-p",
            "-S",
            "-500",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.stdout if proc.returncode == 0 else ""


def _pane_ran_effort(pane: str) -> bool:
    return "/effort" in pane.lower()


def _wait_for(
    predicate: Callable[[], bool],
    *,
    timeout_s: float,
    interval_s: float = 1.0,
    message: str = "condition not met",
) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    _log.info("wait timed out: %s", message)
    return False


def _pick_effort_high(page: Page) -> None:
    """Set Effort → High through the composer's session-config gear."""
    page.get_by_test_id(_CONFIG_GEAR).click()
    page.get_by_test_id(_EFFORT_ROW).click()
    page.get_by_test_id(_EFFORT_HIGH).click()
    page.keyboard.press("Escape")


@pytest.mark.timeout(600)
def test_opening_a_session_must_not_run_effort_command(
    page: Page,
    native_claude_session_pair: tuple[str, str, str],
) -> None:
    """A session the user merely opens must not execute ``/effort`` on its own."""
    base_url, session_a, session_b = native_claude_session_pair

    # --- Session A: the user's own, intentional effort pick ---------------
    page.goto(f"{base_url}/c/{session_a}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    _pick_effort_high(page)
    expect(page.get_by_test_id("composer-agent-effort-value")).to_contain_text(
        re.compile("high", re.IGNORECASE), timeout=30_000
    )
    assert _wait_for(
        lambda: _session_reasoning_effort(base_url, session_a) == "high",
        timeout_s=60.0,
        message="session A reasoning_effort never persisted",
    ), "the explicit effort pick never persisted on session A — driving problem, not the bug"

    # Driving checkpoint (not the bug): the user's own pick propagates into
    # A's live pane, proving the live-injection pipeline works in this rig.
    # Without it, a clean session B could also mean a dead pipeline.
    assert _wait_for(
        lambda: _pane_ran_effort(_pane_history(base_url, session_a)),
        timeout_s=_INJECTION_TIMEOUT_S,
        message="session A pane never showed /effort",
    ), "the explicit pick never reached session A's pane — driving problem, not the bug"

    # --- Session B: merely opened, never touched ---------------------------
    page.goto(f"{base_url}/c/{session_b}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    # The sticky-pref handoff persists an effort on B's row on both buggy and
    # fixed (silent) builds; a fix that removes the handoff leaves it null, so
    # a timeout here is not a failure — B just gets the full settle window.
    _wait_for(
        lambda: _session_reasoning_effort(base_url, session_b) is not None,
        timeout_s=30.0,
        message="session B reasoning_effort not persisted (handoff may be removed)",
    )
    page.wait_for_timeout(_SETTLE_MS)

    # Show B's terminal, then its chat, so a recording captures both surfaces.
    _open_terminal_view(page)
    page.wait_for_timeout(3_000)
    _ensure_chat_view(page)

    pane_b = _pane_history(base_url, session_b)
    assert not _pane_ran_effort(pane_b), (
        "opening session B injected /effort into its live Claude Code pane — "
        "the user never issued it. Pane tail:\n" + pane_b[-2000:]
    )
    expect(page.locator(_SLASH_CARD).filter(has_text="effort")).to_have_count(0)
