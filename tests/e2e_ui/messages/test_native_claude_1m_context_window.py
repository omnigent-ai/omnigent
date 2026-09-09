r"""UI journey: a claude-native session on a 1M-capable model runs ``/context``.

A claude-native session is terminal-first: the runner launches the real
``claude`` CLI in the session terminal with the model-tier alias pins
(``ANTHROPIC_DEFAULT_OPUS_MODEL`` / ``ANTHROPIC_DEFAULT_SONNET_MODEL``) and the
launch model taken from the configured provider's model map. Claude Code
decides its context window *client-side* by testing the effective model id for
the ``[1m]`` marker (it strips the suffix before any request and translates it
into the ``anthropic-beta: context-1m-2025-08-07`` header), so pinning the bare
catalog id caps every session at Claude Code's 200K default even when the
model, gateway, and workspace all support 1M — 5x less usable context and
premature auto-compaction on long agentic runs.

This test drives the user journey: configure a Claude provider whose
opus/sonnet models are 1M-capable (the same ``system.ai.claude-*-5`` ids the
reporting sandbox serves) -> start a claude-native session -> open the
session's Terminal view -> run ``/context`` in the live Claude Code TUI -> the
usage readout must report the ~1M window. On the buggy build the launch env
pins the bare id (nothing in the launch path ever constructs the ``[1m]``
spelling), so the TUI reports ``.../200k tokens`` and this test fails on that
readout.

``/context`` renders locally in the TUI (no model call), so the in-process
mock LLM backend never affects the readout; the real ``claude`` CLI computes
the window, keeping the assertion keyed to exactly the client-side gate the
bug lives behind.

The journey is driven through the SPA (Terminal view + keystrokes through the
embedded xterm), while assertions read the pane through the claude-native
bridge's tmux socket: the embedded xterm renders on canvas, so the pane text
is not recoverable from the DOM.
"""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
import textwrap
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.harnesses.claude_native.bridge import bridge_dir_for_conversation_id
from tests.e2e_ui.conftest import (
    _create_native_claude_session,
    _ensure_runner_online,
    _server_state,
)

from .test_native_claude_render_parity import (
    _TERMINAL_READY_TIMEOUT_MS,
    _TERMINAL_VIEW,
    _XTERM_INPUT,
    _open_terminal_view,
    _wait_terminal_connected,
)

# 1M-capable Claude catalog ids, as served by the reporting workspace: the
# gateway accepts >200K-token requests on these with (or without) the 1M beta
# header, so the 200K cap under test is purely Claude Code's client-side
# default for an unmarked model id.
_OPUS_1M_MODEL = "system.ai.claude-opus-5"
_SONNET_1M_MODEL = "system.ai.claude-sonnet-5"

# The TUI presents the 1M window as "1m" (or a ~967k figure when it nets out
# reserved output tokens), so accept any denominator at or above this floor.
# Claude Code's unmarked default is 200k, far below it.
_MIN_1M_WINDOW_TOKENS = 900_000

# "1.5k/200k tokens (1%)" -- the /context usage readout line in the TUI.
_CONTEXT_READOUT_RE = re.compile(
    r"(?P<used>\d+(?:\.\d+)?[km]?)/(?P<window>\d+(?:\.\d+)?[km]?)\s+tokens",
    re.IGNORECASE,
)

# Claude Code's input-box prompt glyph; rendered once the TUI input box is
# live, which is the earliest moment typed keys are not flushed by boot. (The
# textual status hints are truncated at narrow pane widths; the prompt is not.)
_TUI_READY_MARKER = "\u276f"

# /context renders locally, but the readout paints after a TUI redraw cycle.
_CONTEXT_RENDER_TIMEOUT_S = 90.0


def _token_count(readout: str) -> int:
    """Parse a TUI token figure like ``"200k"``, ``"1m"``, or ``"967k"``.

    :param readout: The figure as the TUI prints it, e.g. ``"200k"``.
    :returns: The token count, e.g. ``200_000``.
    """
    scale = {"k": 1_000, "m": 1_000_000}.get(readout[-1].lower())
    if scale is None:
        return int(float(readout))
    return int(float(readout[:-1]) * scale)


@contextlib.contextmanager
def _anthropic_1m_provider_config(mock_llm_server_url: str, default_model: str) -> Iterator[None]:
    """Temporarily configure a Claude provider pinned to 1M-capable models.

    Mirrors ``_temp_omnigent_mock_config`` in ``conftest.py`` but pins the
    opus/sonnet tier aliases to the 1M-capable catalog ids the reporting
    workspace serves, so the runner's claude-native launch builds the same
    ``ANTHROPIC_DEFAULT_*_MODEL`` pins the reporter's sandbox got.

    :param mock_llm_server_url: Base URL of the in-process mock LLM server.
    :param default_model: The ``models.default`` entry -- the model the
        session launches on, e.g. ``"system.ai.claude-opus-5"``.
    """
    config_dir = Path.home() / ".omnigent"
    config_path = config_dir / "config.yaml"
    config_dir.mkdir(parents=True, exist_ok=True)
    original = config_path.read_text() if config_path.exists() else None
    config_path.write_text(
        textwrap.dedent(f"""\
            providers:
              mock-claude-1m:
                kind: key
                default: [anthropic]
                anthropic:
                  base_url: "{mock_llm_server_url}"
                  api_key: "mock-key"
                  models:
                    default: {default_model}
                    opus: {_OPUS_1M_MODEL}
                    sonnet: {_SONNET_1M_MODEL}
            """)
    )
    try:
        yield
    finally:
        if original is not None:
            config_path.write_text(original)
        else:
            config_path.unlink(missing_ok=True)


# Function-scoped to match the e2e_ui conftest override it chains to.
@pytest.fixture
def browser_context_args(browser_context_args: dict) -> dict:
    """A tall viewport (and matching recording size) for this module.

    The /context panel is taller than the default 720px viewport's terminal
    pane, which scrolls its usage readout (the journey's outcome) off screen.
    Sizing the context up front also sizes any ``--video on`` recording to
    match, so the readout is on screen for a human watching the clip.
    """
    return {
        **browser_context_args,
        "viewport": {"width": 1280, "height": 1400},
        "record_video_size": {"width": 1280, "height": 1400},
    }


@pytest.fixture(params=[_OPUS_1M_MODEL, _SONNET_1M_MODEL], ids=["opus", "sonnet"])
def native_claude_1m_session(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, str]]:
    """A runner-bound claude-native session launched on a 1M-capable model.

    Follows ``native_claude_mock_session`` (real ``claude`` CLI in the session
    terminal, mock LLM backend), except the provider config always pins the
    1M-capable opus/sonnet catalog ids and launches on the parametrized
    family's model.

    :param request: Supplies the parametrized launch model id.
    :param live_server: Spawned server fixture; its runner is reused.
    :param mock_llm_server_url: Session-scoped mock LLM server base URL.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id, launch_model)``.
    """
    launch_model = str(request.param)
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    with _anthropic_1m_provider_config(mock_llm_server_url, launch_model):
        session_id = _create_native_claude_session(live_server, runner_id)
        try:
            yield (live_server, session_id, launch_model)
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            if respawned is not None:
                respawned.terminate()
                try:
                    respawned.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    respawned.kill()
                    respawned.wait(timeout=5)


def _tmux_pane_text(session_id: str) -> str:
    """Capture the session's Claude terminal pane through the bridge's tmux.

    The runner advertises the pane's private socket + target in the bridge
    dir's ``tmux.json`` once the terminal launches. The embedded xterm
    renders on canvas (no DOM text), so tmux capture-pane is the
    machine-checkable read of what the user sees in the Terminal view.

    :param session_id: The claude-native session id (also the bridge id).
    :returns: The pane's current text, or ``""`` while the terminal (or its
        ``tmux.json`` advertisement) does not exist yet.
    """
    tmux_file = bridge_dir_for_conversation_id(session_id) / "tmux.json"
    try:
        info = json.loads(tmux_file.read_text())
    except (OSError, ValueError):
        return ""
    capture = subprocess.run(
        [
            "tmux",
            "-S",
            str(info["socket_path"]),
            "capture-pane",
            "-p",
            "-J",
            # Include scrollback: the /context panel is taller than the pane,
            # so its usage readout can scroll past the visible rows.
            "-S",
            "-200",
            "-t",
            str(info["tmux_target"]),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return capture.stdout if capture.returncode == 0 else ""


def _wait_for_pane(
    page: Page,
    session_id: str,
    predicate: Callable[[str], bool],
    *,
    timeout_s: float,
    description: str,
) -> str:
    """Poll the Claude terminal pane until *predicate* matches its text.

    :param page: The Playwright page (used only to pace polling so the
        browser keeps pumping events while we wait).
    :param session_id: The claude-native session whose pane to read.
    :param predicate: ``str -> bool`` over the pane text.
    :param timeout_s: Seconds to keep polling.
    :param description: What was awaited, for the failure message.
    :returns: The pane text that satisfied the predicate.
    """
    deadline = time.monotonic() + timeout_s
    text = ""
    while time.monotonic() < deadline:
        text = _tmux_pane_text(session_id)
        if predicate(text):
            return text
        page.wait_for_timeout(1_000)
    raise AssertionError(f"terminal pane never showed {description}; pane text:\n{text}")


@pytest.mark.nightly
@pytest.mark.timeout(300)
def test_native_claude_1m_model_gets_1m_context_window(
    page: Page,
    native_claude_1m_session: tuple[str, str, str],
) -> None:
    """``/context`` on a 1M-capable model must report the ~1M window, not 200k.

    Buggy build: the launch env pins the bare model id (no ``[1m]`` marker),
    Claude Code caps the session at its 200K default, and the readout says
    ``.../200k tokens`` -- which fails the window assertion below.
    """
    base_url, session_id, launch_model = native_claude_1m_session
    page.goto(f"{base_url}/c/{session_id}")

    _open_terminal_view(page)
    _wait_terminal_connected(page)

    # Type only after Claude's input box is live: the TUI flushes any
    # keystrokes that arrive during boot, which would silently eat "/context".
    _wait_for_pane(
        page,
        session_id,
        lambda text: _TUI_READY_MARKER in text,
        timeout_s=_TERMINAL_READY_TIMEOUT_MS / 1_000,
        description="Claude Code's input prompt (boot never finished)",
    )

    # The user asks Claude Code itself how big the window is, typing into the
    # SPA's embedded terminal exactly like a person at the Terminal view.
    xterm_input = page.locator(_TERMINAL_VIEW).last.locator(_XTERM_INPUT)
    expect(xterm_input).to_be_attached(timeout=30_000)
    xterm_input.focus()
    page.keyboard.type("/context", delay=30)
    # Let the slash-command menu settle on the exact match before submitting.
    page.wait_for_timeout(500)
    page.keyboard.press("Enter")

    pane = _wait_for_pane(
        page,
        session_id,
        lambda text: _CONTEXT_READOUT_RE.search(text) is not None,
        timeout_s=_CONTEXT_RENDER_TIMEOUT_S,
        description="the /context usage readout",
    )
    readout = _CONTEXT_READOUT_RE.search(pane)
    assert readout is not None  # narrowed by _wait_for_pane
    window = _token_count(readout.group("window"))

    assert window >= _MIN_1M_WINDOW_TOKENS, (
        f"claude-native session launched on 1M-capable {launch_model} reports a "
        f"{readout.group('window')} context window ({readout.group(0)!r} in /context) -- "
        "the launch env pinned the bare model id without the [1m] marker, so Claude Code "
        "capped the session at its 200K default instead of the 1M window"
    )
