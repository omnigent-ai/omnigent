r"""UI journeys: web input into a native Claude Code whose submit key is rebound.

Claude Code lets a person move ``chat:submit`` off Enter in ``keybindings.json``
(``/keybindings`` -> "change the submit key"), for example Enter -> newline and
Shift+Enter -> submit. The native bridge still has to deliver every web-composer
message, and every slash command Omnigent types on the person's behalf
(``/effort``, ``/model``, ``/compact``), into that TUI and have it submitted.

Each journey launches the real ``claude`` CLI against the mock LLM through the
product's own ``harness.claude-native`` project override: an ``env`` wrapper
points ``CLAUDE_CONFIG_DIR`` at a per-test directory holding the rebinding, so
the runner seeds trust there and Claude Code loads those keybindings on start.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pyte
import pytest
import websockets
import yaml
from playwright.sync_api import Page, expect

from tests._helpers.native_session import create_native_session
from tests._helpers.session import bind_session_runner
from tests.e2e_ui.conftest import (
    _REPO_ROOT,
    _ensure_runner_online,
    _server_state,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.helpers.ui_configuration import (
    _CLAUDE_MOCK_MODEL,
    prepared_repro_environment,
    temp_omnigent_mock_config,
)

from .test_message_render_parity import _ASSISTANT, _ensure_chat_view, _send
from .test_native_claude_render_parity import _open_terminal_view, _wait_terminal_connected

_log = logging.getLogger(__name__)

pytestmark = pytest.mark.skipif(shutil.which("claude") is None, reason="requires the claude CLI")

_ERROR_PILL = '[data-testid="error-pill"]'
_ERROR_MESSAGE = '[data-testid="error-message-content"]'

# The documented "change the submit key" example from Claude Code's /keybindings help.
_REBOUND_KEYBINDINGS = {
    "bindings": [
        {
            "context": "Chat",
            "bindings": {"enter": "chat:newline", "shift+enter": "chat:submit"},
        }
    ]
}

# Bridge budget: up to 5s for the paste to land, 20s of submit verification,
# then the runner reports the failed turn; the mock answers a delivered turn at once.
_TURN_OUTCOME_TIMEOUT_MS = 90_000
_SETTINGS_OUTCOME_TIMEOUT_MS = 60_000
# Claude Code persists an applied ``/effort`` as an ``effortLevel`` in its user settings
# (top-level or under ``modelSettings.<model>`` depending on the release).
_EFFORT_APPLIED_TIMEOUT_S = 45.0
# Claude Code rewrites its global config on start; proves it read the per-test dir.
_CLAUDE_STARTUP_WRITE_TIMEOUT_S = 30.0
# Hold the terminal view on screen while the bridge is still retrying its submit.
_TERMINAL_DWELL_MS = 12_000
_SCREEN_READ_S = 3.0


@dataclass(frozen=True)
class ReboundClaudeSession:
    base_url: str
    session_id: str
    claude_config_dir: Path


def _scratch_root(tmp_path: Path) -> Path:
    # A workflow-owned runner cannot see this process's private /tmp; the worktree is shared.
    if prepared_repro_environment()["OMNIGENT_REPRO_SERVER_URL"]:
        root = _REPO_ROOT / ".omnigent" / "e2e-ui-scratch" / f"rebound-{uuid.uuid4().hex[:8]}"
        root.mkdir(parents=True)
        return root
    return tmp_path


@pytest.fixture
def rebound_claude_session(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[ReboundClaudeSession]:
    """A runner-bound claude-native session whose Claude Code has Enter rebound to newline."""
    failed_before = request.session.testsfailed
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    root = _scratch_root(tmp_path)
    session_id: str | None = None
    yielded = False
    # One cleanup path for setup failures and teardown alike, so a respawned
    # runner or the scratch root never outlives a journey that failed to start.
    try:
        workspace = root / "workspace"
        claude_config_dir = root / "claude-config"
        (workspace / ".omnigent").mkdir(parents=True)
        claude_config_dir.mkdir()
        (claude_config_dir / "keybindings.json").write_text(
            json.dumps(_REBOUND_KEYBINDINGS) + "\n"
        )
        claude = shutil.which("claude") or "claude"
        (workspace / ".omnigent" / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "harness": {
                        "claude-native": {
                            "command": "env",
                            "args": [f"CLAUDE_CONFIG_DIR={claude_config_dir}", claude],
                        }
                    }
                }
            )
        )
        with temp_omnigent_mock_config(
            mock_llm_server_url,
            "claude",
            workflow_owned=bool(_server_state.get("workflow_owned")),
        ):
            created = create_native_session(
                httpx, live_server, harness="claude", metadata={"workspace": str(workspace)}
            )
            session_id = str(created["session_id"])
            bind_session_runner(httpx.patch, live_server, session_id, runner_id, timeout=10.0)
            yielded = True
            yield ReboundClaudeSession(live_server, session_id, claude_config_dir)
    finally:
        if session_id is not None:
            try:
                httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            except httpx.HTTPError as exc:
                _log.warning("session delete failed during teardown: %s", exc)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)
        # Keep Claude Code's config and debug log for a journey that failed or
        # never started (a setup failure is not counted in testsfailed yet).
        if root != tmp_path and yielded and request.session.testsfailed == failed_before:
            shutil.rmtree(root, ignore_errors=True)


def _wait_for_claude_startup_write(config_dir: Path) -> None:
    """Block until Claude Code has written into *config_dir*, proving it loaded that directory."""
    deadline = time.monotonic() + _CLAUDE_STARTUP_WRITE_TIMEOUT_S
    seeded = {".claude.json", "keybindings.json"}
    entries: set[str] = set()
    while time.monotonic() < deadline:
        entries = {entry.name for entry in config_dir.iterdir()}
        global_config = config_dir / ".claude.json"
        touched = False
        if global_config.exists():
            try:
                data = json.loads(global_config.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}  # Claude Code mid-write; the next poll reads it whole.
            touched = any(key in data for key in ("numStartups", "firstStartTime", "userID"))
        if touched or entries - seeded:
            return
        time.sleep(0.5)
    raise AssertionError(
        f"Claude Code never wrote into {config_dir}; the rebinding was not loaded from it. "
        f"Entries: {sorted(entries)}"
    )


def _terminal_resource_id(base_url: str, session_id: str) -> str | None:
    resources = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources", timeout=10.0).json()
    for item in resources.get("data") or []:
        if item.get("type") == "terminal":
            return str(item.get("id"))
    return None


async def _read_terminal_screen(ws_url: str) -> str:
    screen = pyte.Screen(220, 60)
    stream = pyte.ByteStream(screen)
    async with websockets.connect(ws_url, max_size=None) as ws:
        deadline = asyncio.get_running_loop().time() + _SCREEN_READ_S
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                frame = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except TimeoutError:
                break
            if isinstance(frame, bytes):
                stream.feed(frame)
    return "\n".join(line.rstrip() for line in screen.display).strip("\n")


def _terminal_screen(base_url: str, session_id: str) -> str:
    """Return the live TUI screen via the product's read-only terminal attach."""
    try:
        terminal_id = _terminal_resource_id(base_url, session_id)
        if terminal_id is None:
            return ""
        ws_url = (
            base_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
            + f"/v1/sessions/{session_id}/resources/terminals/{terminal_id}/attach?read_only=true"
        )
        # The sync Playwright test may already run inside an event loop.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, _read_terminal_screen(ws_url)).result(
                timeout=_SCREEN_READ_S + 15
            )
    except Exception as exc:
        # Evidence only; a failed read must never fail the journey.
        _log.warning("terminal screen read failed: %s", exc)
        return ""


def _effort_levels(value: object) -> Iterator[object]:
    if isinstance(value, dict):
        if "effortLevel" in value:
            yield value["effortLevel"]
        for child in value.values():
            yield from _effort_levels(child)


def _wait_for_claude_effort(config_dir: Path, effort: str) -> bool:
    settings = config_dir / "settings.json"
    deadline = time.monotonic() + _EFFORT_APPLIED_TIMEOUT_S
    while time.monotonic() < deadline:
        if settings.exists():
            try:
                data = json.loads(settings.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
            if effort in set(_effort_levels(data)):
                return True
        time.sleep(0.5)
    return False


def _error_pill_text(page: Page) -> str:
    pill = page.locator(_ERROR_PILL).first
    if pill.count() == 0:
        return ""
    pill.click()
    detail = page.locator(_ERROR_MESSAGE)
    if detail.count():
        expect(detail.first).to_be_visible(timeout=5_000)
    return pill.inner_text()


def _show_terminal_while_bridge_submits(
    page: Page, session: ReboundClaudeSession, request: pytest.FixtureRequest
) -> str:
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    page.wait_for_timeout(_TERMINAL_DWELL_MS)
    screen = _terminal_screen(session.base_url, session.session_id)
    _log.info("%s: TUI screen while the bridge submits:\n%s", request.node.name, screen)
    output_dir = request.config.getoption("--output", default=None)
    if output_dir:
        page.screenshot(path=str(Path(output_dir) / f"{request.node.name}-terminal.png"))
    return screen


@pytest.mark.nightly
@pytest.mark.timeout(420)
def test_web_message_submits_when_enter_is_rebound(
    request: pytest.FixtureRequest,
    rebound_claude_session: ReboundClaudeSession,
    mock_llm_server_url: str,
) -> None:
    """A web-composer message is delivered and answered when Enter is bound to newline."""
    session = rebound_claude_session
    _log.info("rebound session: base_url=%s session_id=%s", session.base_url, session.session_id)
    page: Page = request.getfixturevalue("page")

    page.goto(f"{session.base_url}/c/{session.session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _wait_for_claude_startup_write(session.claude_config_dir)
    _ensure_chat_view(page)

    nonce = uuid.uuid4().hex[:8]
    token = f"ast-{nonce}"
    reset_mock_llm(mock_llm_server_url)
    set_fallback_mock_llm(mock_llm_server_url, "default", token)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, token)
    _send(page, f"rebind-check {nonce}: reply with exactly {token}")
    screen = _show_terminal_while_bridge_submits(page, session, request)
    _ensure_chat_view(page)

    answered = page.locator(_ASSISTANT, has_text=token)
    expect(page.locator(_ERROR_PILL).or_(answered).first).to_be_visible(
        timeout=_TURN_OUTCOME_TIMEOUT_MS
    )
    error_text = _error_pill_text(page)
    assert not error_text, (
        f"the web message was not delivered: {error_text!r}\n"
        f"TUI screen while submitting:\n{screen}"
    )
    expect(answered.first).to_be_visible(timeout=_TURN_OUTCOME_TIMEOUT_MS)


@pytest.mark.nightly
@pytest.mark.timeout(420)
def test_effort_command_applies_when_enter_is_rebound(
    request: pytest.FixtureRequest,
    rebound_claude_session: ReboundClaudeSession,
    mock_llm_server_url: str,
) -> None:
    """``/effort high`` from the web composer reaches Claude Code with Enter bound to newline."""
    session = rebound_claude_session
    _log.info("rebound session: base_url=%s session_id=%s", session.base_url, session.session_id)
    page: Page = request.getfixturevalue("page")

    page.goto(f"{session.base_url}/c/{session.session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _wait_for_claude_startup_write(session.claude_config_dir)
    _ensure_chat_view(page)
    reset_mock_llm(mock_llm_server_url)

    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible(timeout=30_000)
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and response.url.endswith(f"/v1/sessions/{session.session_id}")
        ),
        timeout=_SETTINGS_OUTCOME_TIMEOUT_MS,
    ) as patched:
        composer.fill("/effort high")
        composer.press("Enter")
        _show_terminal_while_bridge_submits(page, session, request)
    # The server persists the pick and forwards it; only Claude Code's own
    # settings show whether the typed command was ever submitted.
    applied = _wait_for_claude_effort(session.claude_config_dir, "high")
    screen = _terminal_screen(session.base_url, session.session_id)
    try:
        persisted = httpx.get(
            f"{session.base_url}/v1/sessions/{session.session_id}", timeout=10.0
        ).json()
    except (httpx.HTTPError, ValueError) as exc:
        # Evidence only; the assertion below must not be masked by its collection.
        _log.warning("session fetch failed while collecting evidence: %s", exc)
        persisted = {}
    assert applied, (
        f"/effort high never reached Claude Code (PATCH returned HTTP {patched.value.status}; "
        f"session reasoning_effort={persisted.get('reasoning_effort')!r}).\n"
        f"TUI screen afterwards:\n{screen}"
    )
