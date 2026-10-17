"""A prompt appearing during draft clearing remains owned by its user."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge
from tests.harnesses.claude_native.test_claude_native_bridge import (
    _picker_bridge_dir,
)
from tests.harnesses.claude_native.test_claude_native_bridge import (
    _trust_tmp_bridge_parent as _trust_tmp_bridge_parent,
)
from tests.harnesses.claude_native.test_claude_native_pending_prompt import _PERMISSION
from tests.harnesses.claude_native.test_composer_clear import _EditableComposer


@pytest.mark.parametrize("operation", ["message", "settings"])
@pytest.mark.parametrize("prompt", ["hook-with-torn-pane", "visible-permission"])
def test_prompt_appearing_during_clear_receives_no_further_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, prompt: str
) -> None:
    native = _picker_bridge_dir(tmp_path)
    (native / "bridge.json").write_text(json.dumps({"active_session_id": "session-a"}))
    marker = bridge.approval_wait_marker_path("session-a", bridge_dir=native)

    class PromptDuringClear(_EditableComposer):
        clearing_bursts = 0

        def _run_tmux(self, socket_path: str, *args: str) -> None:
            super()._run_tmux(socket_path, *args)
            if args[0] == "send-keys" and "\x1b[107;5u" in args[-1]:
                self.clearing_bursts += 1
                if prompt == "hook-with-torn-pane":
                    marker.parent.mkdir(exist_ok=True)
                    bridge.touch_approval_wait_marker(marker)

        def _capture_pane(self, socket_path: str, tmux_target: str) -> str:
            if self.clearing_bursts:
                return "" if prompt == "hook-with-torn-pane" else _PERMISSION
            return super()._capture_pane(socket_path, tmux_target)

    composer = PromptDuringClear("leftover\nmultiline\ndraft", cursor=3)
    composer.install(monkeypatch)
    with pytest.raises(bridge.ClaudeUserPromptPending):
        if operation == "message":
            bridge.inject_user_message(native, content="new web message")
        else:
            bridge.inject_slash_command(native, command="/effort high")
    assert composer.clearing_bursts == 1
    assert composer.submitted == []
    assert "new web message" not in composer.text and "/effort" not in composer.text
    assert marker.exists() is (prompt == "hook-with-torn-pane")


@pytest.mark.parametrize("operation", ["message", "settings"])
def test_empty_composer_still_delivers_exactly_one_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    native = _picker_bridge_dir(tmp_path)
    composer = _EditableComposer("", cursor=0)
    composer.install(monkeypatch)
    if operation == "message":
        bridge.inject_user_message(native, content="new web message")
        expected = "new web message"
    else:
        bridge.inject_slash_command(native, command="/effort high")
        expected = "/effort high"
    assert [text.strip() for text in composer.submitted] == [expected]
