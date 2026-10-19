"""Known native delivery refusals keep the original web input after reload."""

from __future__ import annotations

import contextlib
import json
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.native_session import create_native_session
from tests.e2e.test_claude_native_delivery_timeout_e2e import _FAKE_CLAUDE_TUI
from tests.e2e.test_native_terminal_start_error_log_correlation_e2e import (
    _bind_session,
    _ensure_claude_terminal,
    _native_stack,
)
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view


@pytest.mark.parametrize("refusal", ["missing_target", "draft_not_accepted"])
@pytest.mark.timeout(180)
def test_claude_refused_message_survives_reload(
    built_spa: None,
    page: Page,
    mock_llm_server_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    refusal: str,
) -> None:
    marker = f"refused-input-{uuid.uuid4().hex[:8]}"
    tui = tmp_path / "controlled-tui.py"
    tui.write_text(_FAKE_CLAUDE_TUI)
    trigger = marker if refusal == "draft_not_accepted" else "unused-trigger"
    script = (
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "2.1.296 (Claude Code)"; exit 0; fi\n'
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(tui))} {shlex.quote(trigger)} 120\n"
    )
    monkeypatch.setenv("OPENAI_BASE_URL", f"{mock_llm_server_url}/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "mock-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", mock_llm_server_url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "mock-key")
    with (
        tempfile.TemporaryDirectory(prefix="omni-refusal-") as native_tmp,
        contextlib.ExitStack() as resources,
    ):
        monkeypatch.setenv("TMPDIR", native_tmp)
        stack = resources.enter_context(_native_stack(tmp_path, script))
        client = resources.enter_context(httpx.Client(trust_env=False, timeout=15))
        session = create_native_session(client, stack.base_url, harness="claude")
        session_id = session["session_id"]
        _bind_session(stack, session_id)
        ensured = _ensure_claude_terminal(stack, session_id)
        assert ensured.status_code == 200, ensured.text
        deadline = time.monotonic() + 20
        adverts = []
        while time.monotonic() < deadline:
            adverts = list(Path(native_tmp).rglob("tmux.json"))
            if adverts:
                break
            time.sleep(0.1)
        assert len(adverts) == 1
        advert = json.loads(adverts[0].read_text())
        if refusal == "missing_target":
            adverts[0].unlink()
        page.goto(f"{stack.base_url}/c/{session_id}")
        _ensure_chat_view(page)
        composer = page.get_by_label("Message the agent")
        composer.fill(marker)
        composer.press("Enter")
        deadline = time.monotonic() + 80
        items = []
        while time.monotonic() < deadline:
            items = client.get(
                f"{stack.base_url}/v1/sessions/{session_id}/items",
                params={"order": "asc", "limit": 100},
            ).json()["data"]
            if any(item["type"] == "error" for item in items):
                break
            page.wait_for_timeout(100)
        assert any(item["type"] == "error" for item in items), stack.runner_log_tail()
        users = [item for item in items if item.get("role") == "user"]
        assert len(users) == 1 and marker in json.dumps(users[0])
        detail = json.dumps([item for item in items if item["type"] == "error"])
        expected = (
            "tmux target is not advertised"
            if refusal == "missing_target"
            else "draft is still in the input box"
        )
        assert expected in detail, detail
        assert (
            not client.get(f"{stack.base_url}/v1/sessions/{session_id}")
            .json()
            .get("pending_inputs")
        )
        alive = subprocess.run(
            [
                "tmux",
                "-S",
                advert["socket_path"],
                "has-session",
                "-t",
                advert["tmux_target"].split(":")[0],
            ],
            capture_output=True,
            timeout=5,
        )
        assert alive.returncode == 0, alive.stderr.decode()
        page.reload()
        _ensure_chat_view(page)
        expect(
            page.locator('[data-testid="message-bubble"][data-role="user"]', has_text=marker)
        ).to_have_count(1)
        expect(page.get_by_test_id("error-pill")).to_have_count(1)
        (tmp_path / "persisted-items.json").write_text(json.dumps(items, indent=2))
