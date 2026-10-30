"""Scripted terminal and hook files for submit-processing scenarios."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.harnesses.claude_native import bridge, delivery_diagnostics
from omnigent.util.json_types import JsonObject
from tests.harnesses.claude_native.test_claude_native_bridge import (
    _composer_pane,
    _EditableComposer,
)
from tests.harnesses.claude_native.test_hook_block_submit_retries import _blocked_prompt_entry

NATIVE_SESSION_ID = "11111111-2222-4333-8444-555555555555"


def install_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    outcome: str,
    hook_kind: str = "fresh",
    content: str,
) -> SimpleNamespace:
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path)
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    composer = _EditableComposer("", cursor=0)
    composer.install(monkeypatch)
    transcript = tmp_path / "native.jsonl"
    transcript.write_text('{"type":"system","content":"prefix"}\n')
    bridge.record_hook_event(
        tmp_path,
        {
            "hook_event_name": "SessionStart",
            "session_id": NATIVE_SESSION_ID,
            "transcript_path": str(transcript),
        },
    )
    (tmp_path / "bridge.json").write_text('{"active_session_id":"web-original"}')
    hook: JsonObject = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": NATIVE_SESSION_ID,
        "transcript_path": str(transcript),
        "prompt": content,
    }
    if hook_kind == "wrong-session":
        hook["session_id"] = "other-native"
    elif hook_kind == "wrapped":
        hook["prompt"] = f'<pasted_content id="abcd">\n{content}\n</pasted_content>'
    hook_log = tmp_path / "hooks.jsonl"
    if hook_kind == "initially-absent":
        hook_log.unlink()
    state = SimpleNamespace(
        elapsed=0.0,
        enters=0,
        pane=_composer_pane(),
        completed=False,
        commands=[],
        after_submit=[],
        hook=hook,
        transcript=transcript,
        composer=composer,
    )

    def sleep(seconds: float) -> None:
        state.elapsed += seconds

    def capture(*_args: object) -> str:
        if not state.enters:
            return composer._capture_pane("/unused/socket", "main")
        if state.enters and state.elapsed >= 30 and not state.completed:
            state.completed = True
            if outcome == "allow":
                state.pane = _composer_pane()
            elif outcome in {"block", "block-empty"}:
                with transcript.open("a") as handle:
                    handle.write(
                        json.dumps(
                            _blocked_prompt_entry(
                                "Synthetic refusal.", content, native_session_id=NATIVE_SESSION_ID
                            )
                        )
                        + "\n"
                    )
                if outcome == "block-empty":
                    state.pane = _composer_pane()
        return state.pane

    def run_tmux(_socket: str, *args: str) -> None:
        composer._run_tmux(_socket, *args)
        state.commands.append(args)
        if state.enters:
            state.after_submit.append(args)
        if args[0] == "paste-buffer":
            state.pane = _composer_pane(content)
        if args[-1] != "Enter":
            return
        state.enters += 1
        if hook_kind == "none":
            return
        serialized = json.dumps({"payload": hook})
        with hook_log.open("a") as handle:
            handle.write(serialized + ("" if hook_kind == "partial-record" else "\n"))

    monkeypatch.setattr(
        bridge,
        "time",
        SimpleNamespace(monotonic=lambda: state.elapsed, sleep=sleep, time=lambda: state.elapsed),
    )
    monkeypatch.setattr(delivery_diagnostics, "time", bridge.time)
    monkeypatch.setattr(bridge, "_run_tmux", run_tmux)
    monkeypatch.setattr(bridge, "_capture_pane", capture)
    monkeypatch.setattr(bridge, "_wait_for_claude_prompt_ready", lambda *_args, **_kw: None)
    monkeypatch.setattr(bridge, "_restore_occupied_input", lambda *_args, **_kw: None)
    monkeypatch.setattr(
        bridge,
        "_wait_for_tmux_info",
        lambda *_args, **_kw: {"socket_path": "/unused/socket", "tmux_target": "main"},
    )
    monkeypatch.setattr(bridge, "_PASTE_SETTLE_S", 0)
    return state
