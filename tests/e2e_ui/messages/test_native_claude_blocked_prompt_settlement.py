"""A web message a Claude Code ``UserPromptSubmit`` hook blocks settles with the hook's reason.

Claude Code drops the prompt and shows the hook's reason only in its own
terminal. The web chat must settle the message as soon as Claude records the
block, show that reason under it, and never turn it into the generic
``native_prompt_not_recorded`` error ("Message not delivered. Try sending it
again.") once a later message is answered. Drives the real Claude CLI.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests.e2e_ui.conftest import (
    _REPO_ROOT,
    native_claude_mock_session_context,
    reset_mock_llm,
    set_fallback_mock_llm,
)

from .test_message_render_parity import _ASSISTANT, _USER, _WORKING, _ensure_chat_view
from .test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _MOCK_TURN_TIMEOUT_MS,
    _open_terminal_view,
    _wait_terminal_connected,
)
from .test_native_claude_shell_input import (  # noqa: F401 — autouse skip guard
    _requires_mockable_claude,
)

_MISSING_CODE = "native_prompt_not_recorded"
_BLOCKED_CODE = "native_prompt_blocked_by_hook"
# Collapsed headline the SPA gives the notice (StatusBlocks.tsx).
_BLOCKED_HEADLINE = "A hook blocked this message, so the agent did not run it."
_HOOK_MARKER = "BLOCKME"
_HOOK_REASON = "Blocked: test"
_HOOK_DECISION = json.dumps({"decision": "block", "reason": _HOOK_REASON}, separators=(",", ":"))
# Claude pipes the hook payload to stdin; only its prompt field can carry the marker.
_HOOK_SCRIPT = (
    "#!/bin/sh\n"
    "input=$(cat)\n"
    'case "$input" in\n'
    f"  *{_HOOK_MARKER}*) printf '%s\\n' '{_HOOK_DECISION}' ;;\n"
    "esac\n"
    "exit 0\n"
)
# Claude records the block within seconds of the paste; the mirror follows on the next poll.
_SETTLE_TIMEOUT_S = 45.0
_STALE_WORKSPACE_S = 24 * 3600.0


@pytest.fixture
def hook_blocked_claude_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A real Claude CLI whose project settings block prompts containing ``BLOCKME``.

    The workspace lives under the worktree: the runner may run in a sandbox
    whose ``/tmp`` is private, so a pytest temp dir would be invisible to it.
    """
    workspaces_root = _REPO_ROOT / ".omnigent" / "e2e-hook-workspaces"
    workspaces_root.mkdir(parents=True, exist_ok=True)
    # Each workspace is named by its pytest pid; a killed run's leftovers are
    # swept once that process is gone (or after a day, far beyond the test's
    # timeout, when the pid was reused or cannot be probed).
    for stale in workspaces_root.iterdir():
        pid = stale.name.partition("-")[0]
        try:
            if not pid.isdigit() or not stale.is_dir():
                continue
            if time.time() - stale.stat().st_mtime > _STALE_WORKSPACE_S:
                raise ProcessLookupError
            os.kill(int(pid), 0)
        except ProcessLookupError:
            shutil.rmtree(stale, ignore_errors=True)
        except OSError:
            continue
    workspace = Path(tempfile.mkdtemp(prefix=f"{os.getpid()}-", dir=workspaces_root))
    try:
        hooks_dir = workspace / ".claude" / "hooks"
        hooks_dir.mkdir(parents=True)
        script = hooks_dir / "block_marker.sh"
        script.write_text(_HOOK_SCRIPT, encoding="utf-8")
        script.chmod(0o755)
        hook = {"type": "command", "command": str(script)}
        settings = {"hooks": {"UserPromptSubmit": [{"hooks": [hook]}]}}
        (workspace / ".claude" / "settings.json").write_text(
            json.dumps(settings), encoding="utf-8"
        )
        with native_claude_mock_session_context(
            live_server, mock_llm_server_url, tmp_path_factory, workspace=str(workspace)
        ) as session:
            yield session
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def _compose(page: Page, text: str) -> None:
    # The placeholder changes while a turn runs; the label is stable.
    box = page.get_by_label("Message the agent")
    expect(box).to_be_enabled(timeout=30_000)
    box.fill(text)
    box.press("Enter")


def _hold(page: Page, ms: int, filming: bool) -> None:
    """Keep the current state on screen long enough to read, only while filming."""
    if filming:
        page.wait_for_timeout(ms)


def _mark(t0: float, label: str) -> None:
    print(f"[cue] +{time.monotonic() - t0:5.1f}s {label}", flush=True)


def _snapshot(base_url: str, session_id: str) -> dict[str, Any]:
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=15.0)
    resp.raise_for_status()
    return resp.json()


def _items(base_url: str, session_id: str) -> list[dict[str, Any]]:
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 200, "order": "asc"},
        timeout=15.0,
    )
    resp.raise_for_status()
    return list(resp.json()["data"])


def _field(item: dict[str, Any], name: str) -> Any:
    return item.get(name) if name in item else (item.get("data") or {}).get(name)


def _error_items(base_url: str, session_id: str) -> list[tuple[Any, Any]]:
    return [
        (_field(item, "code"), _field(item, "message"))
        for item in _items(base_url, session_id)
        if item.get("type") == "error"
    ]


def _dump(base_url: str, session_id: str, label: str) -> None:
    pending = json.dumps(_snapshot(base_url, session_id).get("pending_inputs"))
    errors = json.dumps([i for i in _items(base_url, session_id) if i.get("type") == "error"])
    print(f"[http] {label} GET /v1/sessions/{session_id} pending_inputs={pending}", flush=True)
    print(f"[http] {label} GET /v1/sessions/{session_id}/items error_items={errors}", flush=True)


def _wait_for_settlement(
    base_url: str, session_id: str, *, notice: bool
) -> tuple[Any, list[tuple[Any, Any]]]:
    """Poll until the queue is empty (and, for a block, its notice is persisted), or give up."""
    deadline = time.monotonic() + _SETTLE_TIMEOUT_S
    while True:
        pending = _snapshot(base_url, session_id).get("pending_inputs")
        errors = _error_items(base_url, session_id)
        settled = not pending and (
            not notice or any(code == _BLOCKED_CODE for code, _message in errors)
        )
        if settled or time.monotonic() >= deadline:
            return pending, errors
        time.sleep(1.0)


def _wait_until_idle(base_url: str, session_id: str) -> Any:
    """Poll the session status until it is idle, so the next paste meets a free composer."""
    deadline = time.monotonic() + _SETTLE_TIMEOUT_S
    status = _snapshot(base_url, session_id).get("status")
    while status != "idle" and time.monotonic() < deadline:
        time.sleep(1.0)
        status = _snapshot(base_url, session_id).get("status")
    return status


def _arm_mock_reply(mock_llm_server_url: str, token: str) -> None:
    reset_mock_llm(mock_llm_server_url)
    for model in ("default", _CLAUDE_MOCK_MODEL):
        set_fallback_mock_llm(mock_llm_server_url, model, token)


@pytest.mark.nightly
@pytest.mark.timeout(480)
def test_hook_blocked_web_message_settles_with_the_hook_reason(
    request: pytest.FixtureRequest,
    hook_blocked_claude_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = hook_blocked_claude_session
    token = f"tok-{uuid.uuid4().hex[:8]}"
    _arm_mock_reply(mock_llm_server_url, token)
    shots = Path(request.config.getoption("--output")) / request.node.name
    shots.mkdir(parents=True, exist_ok=True)
    filming = bool(os.environ.get("OMNIGENT_E2E_RECORD_DIR")) or request.config.getoption(
        "--video", default="off"
    ) not in (None, "off")

    page: Page = request.getfixturevalue("page")
    t0 = time.monotonic()
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _mark(t0, "Claude TUI attached")
    _ensure_chat_view(page)

    blocked = f"{_HOOK_MARKER} hello"
    _compose(page, blocked)
    _mark(t0, f"sent {blocked!r}")
    expect(page.locator(_USER, has_text=blocked).first).to_be_visible(timeout=30_000)
    pending_after_block, errors_after_block = _wait_for_settlement(
        base_url, session_id, notice=True
    )
    _mark(t0, f"after the block: pending={pending_after_block} errors={errors_after_block}")
    _dump(base_url, session_id, "after-hook-block")
    notice = page.get_by_test_id("error-pill").filter(has_text=_BLOCKED_HEADLINE)
    with contextlib.suppress(PlaywrightTimeoutError):
        notice.first.wait_for(state="visible", timeout=15_000)
    page.screenshot(path=str(shots / "chat-after-hook-block.png"))
    headlines = page.get_by_test_id("error-headline").all_inner_texts()
    notice_text = ""
    if notice.count():
        notice.first.click()
        expect(notice.first.get_by_text(_HOOK_REASON)).to_be_visible(timeout=10_000)
        _hold(page, 1_500, filming)
        notice_text = notice.first.inner_text()
    print(f"[probe] pill headlines={headlines} expanded notice={notice_text!r}", flush=True)
    page.screenshot(path=str(shots / "chat-notice-expanded.png"))
    _mark(t0, "notice under the blocked message")
    _open_terminal_view(page)
    _hold(page, 4_000, filming)
    page.screenshot(path=str(shots / "terminal-after-hook-block.png"))
    _mark(t0, "terminal view: Claude's own refusal")
    _ensure_chat_view(page)
    status = _wait_until_idle(base_url, session_id)
    print(f"[probe] session status before the follow-up: {status}", flush=True)
    assert status == "idle", f"session did not return to idle after the block: {status}"

    followup = f"Reply with exactly this token and nothing else: {token}"
    _compose(page, followup)
    _mark(t0, "sent the follow-up message")
    expect(page.locator(_ASSISTANT, has_text=token).first).to_be_visible(
        timeout=2 * _MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)
    _mark(t0, "follow-up answered")
    pending_after_followup, errors = _wait_for_settlement(base_url, session_id, notice=False)
    _hold(page, 4_000, filming)
    page.screenshot(path=str(shots / "chat-after-follow-up.png"))
    _mark(t0, "chat view after settlement")
    _dump(base_url, session_id, "after-follow-up")
    print(
        "[probe] on screen: "
        f"error pills={page.get_by_test_id('error-pill').count()} "
        f"'Message not delivered'={page.get_by_text('Message not delivered').count()} "
        f"'{_BLOCKED_HEADLINE}'={page.get_by_text(_BLOCKED_HEADLINE).count()}",
        flush=True,
    )
    _hold(page, 3_000, filming)

    expect(page.locator(_USER, has_text=blocked)).to_have_count(1)
    expect(page.locator(_USER, has_text=followup)).to_have_count(1)
    failures: list[str] = []
    if pending_after_block:
        failures.append(
            f"{blocked!r} was still pending {_SETTLE_TIMEOUT_S:.0f}s after the hook "
            f"refused it: {pending_after_block}"
        )
    if not any(
        code == _BLOCKED_CODE and _HOOK_REASON in str(message)
        for code, message in errors_after_block
    ):
        failures.append(
            f"no {_BLOCKED_CODE} item names the hook's reason {_HOOK_REASON!r} after the "
            f"block; errors={errors_after_block}"
        )
    if _BLOCKED_HEADLINE not in headlines or _HOOK_REASON not in notice_text:
        failures.append(
            "the chat does not say a hook blocked the message and why; "
            f"headlines={headlines} expanded={notice_text!r}"
        )
    missing = [error for error in errors if error[0] == _MISSING_CODE]
    if pending_after_followup or missing:
        failures.append(
            f"once {followup!r} was answered, {blocked!r} was persisted as never recorded: "
            f"{missing}; pending={pending_after_followup}; all errors: {errors}"
        )
    assert not failures, "\n".join(failures)
