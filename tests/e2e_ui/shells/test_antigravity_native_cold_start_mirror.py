"""Browser journey: a native-agy session whose cold-start misses the catalog deadline.

Drives the real ``agy`` CLI through the server, runner and tmux. The runner's
``agy`` is delayed past the cold-start deadline, so the runner leaves its
``agy_conv_*`` placeholder in bridge state; a web turn is then sent and agy
answers in its own TUI. The reply must still be mirrored into the chat. Run
without credentials using the local mock Gemini server::

    OMNIGENT_E2E_ANTIGRAVITY=mock uv run --no-sync pytest --ui-skip-build -v \
        tests/e2e_ui/shells/test_antigravity_native_cold_start_mirror.py
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.harnesses.antigravity_native.bridge import (
    _AGY_IDLE_MARKER,
    is_placeholder_conversation_id,
    read_bridge_state,
    read_tmux_info,
)
from tests.e2e_ui.shells.test_antigravity_tmux_recovery import (
    AntigravitySession,
    _antigravity_stack,
    _wait_until,
    antigravity_model,  # noqa: F401 - fixture reused by name
)
from tests.e2e_ui.shells.test_terminal_direct_attach import _BLOCK_LOOPBACK_DIALS

pytestmark = [
    pytest.mark.posix_only,
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_E2E_ANTIGRAVITY") not in {"1", "mock"},
        reason="set OMNIGENT_E2E_ANTIGRAVITY=mock (no credentials) or 1 (live model)",
    ),
    pytest.mark.timeout(600),
]

_COLD_START_GAVE_UP = "did not expose a ready model catalog"
_COLD_START_SUCCEEDED = "Antigravity cold-start: created conversation"
_READER_BOUND = "agy RPC reader bound"
_AGY_START_DELAY_S = float(os.environ.get("OMNIGENT_E2E_AGY_START_DELAY_S", "30"))


@pytest.fixture(autouse=True)
def _playwright_browsers_outside_isolated_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Playwright's browser cache reachable after the mock fixture relocates HOME."""
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        monkeypatch.setenv(
            "PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache" / "ms-playwright")
        )


@pytest.fixture
def slow_agy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Put an ``agy`` on PATH that starts the real binary only after the cold-start deadline."""
    real_agy = shutil.which("agy")
    assert real_agy, "install agy before running this test"
    # The directory name must not contain "bin/agy": the runner identifies agy
    # processes by that command-line fragment and must not see the sleeping shim.
    shim_dir = tmp_path / "slow-agy"
    shim_dir.mkdir()
    shim = shim_dir / "agy"
    shim.write_text(
        f'#!/bin/sh\nsleep {_AGY_START_DELAY_S:g}\nexec "{real_agy}" "$@"\n', encoding="utf-8"
    )
    shim.chmod(0o700)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    return shim


@pytest.fixture
def stack_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Give the spawned server and runner a data dir whose logs stay with the test."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(data_dir))
    return data_dir


@pytest.fixture
def antigravity_session(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    antigravity_model: list[str] | None,  # noqa: F811
    slow_agy: Path,
    stack_data_dir: Path,
) -> Iterator[AntigravitySession]:
    assert not request.config.getoption("--ui-base-url"), "this test requires its own server"
    with _antigravity_stack(tmp_path) as session:
        yield session


def _runner_log(session: AntigravitySession) -> str:
    logs = sorted((session.directory / "data" / "logs" / "runner").glob("*.log"))
    return "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in logs)


def _pane_text(session: AntigravitySession) -> str:
    return session.tmux_command(
        "capture-pane", "-p", "-J", "-t", session.pane()["tmux_target"]
    ).stdout


def _assistant_items(session: AntigravitySession) -> list[dict]:
    response = httpx.get(f"{session.base_url}/v1/sessions/{session.session_id}/items", timeout=10)
    response.raise_for_status()
    body = response.json()
    items = body["data"] if isinstance(body, dict) and "data" in body else body
    return [item for item in items if item.get("role") == "assistant"]


def _snapshot(session: AntigravitySession, name: str, **facts: object) -> None:
    """Record evidence best-effort: a vanished pane or server must not mask the assertion."""
    evidence = session.directory / "evidence"
    evidence.mkdir(exist_ok=True)
    state = read_bridge_state(session.bridge_dir)
    log = _runner_log(session)
    record: dict[str, object] = {
        "at": time.time(),
        "session_id": session.session_id,
        "bridge_conversation_id": state.conversation_id if state else None,
        "bridge_is_placeholder": bool(
            state and is_placeholder_conversation_id(state.conversation_id)
        ),
        "runner_log_cold_start": [
            line for line in log.splitlines() if "Antigravity cold-start" in line
        ],
        "runner_log_reader_bound": [line for line in log.splitlines() if _READER_BOUND in line],
        **facts,
    }
    with contextlib.suppress(Exception):
        record["assistant_items"] = _assistant_items(session)
    (evidence / f"{name}.json").write_text(
        json.dumps(record, indent=2, default=str), encoding="utf-8"
    )
    with contextlib.suppress(Exception):
        (evidence / f"{name}.pane.txt").write_text(_pane_text(session), encoding="utf-8")
    # The fixture deletes the bridge dir on teardown; keep its state and agy's own log.
    bridge_copy = evidence / f"{name}.bridge"
    bridge_copy.mkdir(exist_ok=True)
    for path in session.bridge_dir.glob("*"):
        if path.is_file() and (path.suffix in {".json", ".log"}):
            shutil.copy2(path, bridge_copy / path.name)


def test_turn_is_mirrored_after_cold_start_misses_catalog_deadline(
    request: pytest.FixtureRequest,
    antigravity_session: AntigravitySession,
    antigravity_model: list[str] | None,  # noqa: F811
) -> None:
    session = antigravity_session
    _wait_until(
        lambda: read_tmux_info(session.bridge_dir) is not None, "agy pane never launched", 60
    )

    page = request.getfixturevalue("page")
    try:
        _drive_journey(page, session, antigravity_model)
    finally:
        # Requested after setup, so the conftest cannot close the recorded context for us.
        page.context.close()


def _drive_journey(
    page: Page, session: AntigravitySession, model_replies: list[str] | None
) -> None:
    page.add_init_script(_BLOCK_LOOPBACK_DIALS)
    page.goto(f"{session.base_url}/c/{session.session_id}?view=terminal")
    terminal = page.get_by_test_id("main-terminal-view").get_by_test_id("terminal-view")
    expect(terminal).to_have_attribute("data-state", "connected", timeout=120_000)

    # Reported starting state: the cold-start missed its deadline and left the placeholder.
    def cold_start_settled() -> bool:
        log = _runner_log(session)
        if _COLD_START_SUCCEEDED in log:
            pytest.fail(
                "cold-start bound a real cascade; the delayed agy did not miss the deadline"
            )
        return _COLD_START_GAVE_UP in log

    _wait_until(cold_start_settled, "runner never logged the cold-start timeout", timeout=90)
    state = read_bridge_state(session.bridge_dir)
    assert state is not None and is_placeholder_conversation_id(state.conversation_id)
    _wait_until(lambda: _AGY_IDLE_MARKER in _pane_text(session), "agy TUI never became ready", 120)
    _snapshot(session, "1-before-turn")

    token = f"agy-e2e-{uuid.uuid4().hex[:8]}"
    page.get_by_test_id("view-mode-chat").click()
    page.get_by_label("Message the agent").fill(f"Reply {token}. No tools.")
    page.get_by_role("button", name="Send", exact=True).click()

    # agy processes the typed turn in its own TUI: the prompt echo plus the reply
    # show the token twice in the pane, and the mock model saw the request.
    def agy_answered() -> bool:
        if model_replies is not None and token not in model_replies:
            return False
        return len(re.findall(re.escape(token), _pane_text(session))) >= 2

    _wait_until(agy_answered, "agy never answered the turn in its TUI", timeout=120)
    page.get_by_test_id("view-mode-terminal").click()
    expect(terminal).to_have_attribute("data-state", "connected", timeout=30_000)
    page.wait_for_timeout(4_000)
    page.screenshot(path=str(session.directory / "evidence" / "agy-answered-terminal.png"))
    page.get_by_test_id("view-mode-chat").click()
    _snapshot(session, "2-agy-answered", token=token)

    reply = page.locator('[data-testid="message-bubble"][data-role="assistant"]')
    try:
        expect(reply.filter(has_text=token)).to_have_count(1, timeout=90_000)
    finally:
        page.screenshot(path=str(session.directory / "evidence" / "chat-after-wait.png"))
        _snapshot(session, "3-after-wait", token=token)
