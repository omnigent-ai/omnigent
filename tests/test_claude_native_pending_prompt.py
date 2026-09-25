"""Decision prompts survive unrelated native message and settings delivery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge

_QUESTION = """\
────────────────────────────────────────────────────────────────────────────────
 ☐ Bridge
Which bridge should we use?
❯ 1. Keep overlay
     Retain the existing environment
  2. Try PYTHONPATH
     Test a path bridge
  3. Type something.
────────────────────────────────────────────────────────────────────────────────
  4. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel
"""
_PERMISSION = """\
╭──────────────────────────────────────────────────────────╮
│ Bash command                                             │
│ Do you want to proceed?                                  │
│ ❯ 1. Yes                                                 │
│   2. Yes, and don't ask again for this command             │
│   3. No, and tell Claude what to do differently (esc)      │
╰──────────────────────────────────────────────────────────╯
"""
_COMPOSER = "\n──────────────────────────────\n❯\n──────────────────────────────\n"


@pytest.fixture
def native_bridge(tmp_path: Path) -> Path:
    native = tmp_path / "bridge"
    native.mkdir()
    (native / "bridge.json").write_text(json.dumps({"active_session_id": "session-question"}))
    (native / "tmux.json").write_text(
        json.dumps({"socket_path": "/tmp/example.sock", "tmux_target": "main"})
    )
    return native


def _park_hook(native: Path) -> Path:
    marker = bridge.approval_wait_marker_path("session-question", bridge_dir=native)
    marker.parent.mkdir(exist_ok=True)
    bridge.touch_approval_wait_marker(marker)
    return marker


def test_live_hook_protects_a_prompt_even_during_a_torn_capture(
    native_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use the hook's bridge-relative root even when the reader has another TMPDIR."""
    monkeypatch.setattr(bridge, "_APPROVAL_WAIT_ROOT", native_bridge / "other-root")
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_: "")
    marker = _park_hook(native_bridge)
    assert bridge.has_pending_user_prompt(native_bridge)
    marker.unlink()
    assert not bridge.has_pending_user_prompt(native_bridge)


@pytest.mark.parametrize(
    "pane",
    [
        _QUESTION,
        _QUESTION.replace("Enter to select", "Space to select · Enter to submit"),
        _PERMISSION,
    ],
)
@pytest.mark.parametrize("operation", ["message", "settings"])
def test_terminal_fallback_prompt_receives_no_injected_keystrokes(
    native_bridge: Path, monkeypatch: pytest.MonkeyPatch, pane: str, operation: str
) -> None:
    """A hook may have timed out; its still-visible native prompt remains protected."""
    sent = []
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_: pane)
    monkeypatch.setattr(bridge, "_run_tmux", lambda *args: sent.append(args))
    assert bridge.has_pending_user_prompt(native_bridge)
    with pytest.raises(bridge.ClaudeUserPromptPending):
        if operation == "message":
            bridge.inject_user_message(
                native_bridge, content="Status check. This is not an answer."
            )
        else:
            bridge.inject_slash_command(native_bridge, command="/effort high")
    assert sent == []


@pytest.mark.parametrize("pane", [_QUESTION, _PERMISSION])
def test_decision_text_above_a_ready_composer_does_not_block_delivery(
    native_bridge: Path, monkeypatch: pytest.MonkeyPatch, pane: str
) -> None:
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_: pane + _COMPOSER)
    assert not bridge.has_pending_user_prompt(native_bridge)


def test_hook_that_parks_between_overlay_captures_prevents_escape(
    native_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captures = 0
    sent = []

    def capture(*_: str) -> str:
        nonlocal captures
        captures += 1
        if captures == 2:
            _park_hook(native_bridge)
        return "Settings panel\nEscape to close"

    monkeypatch.setattr(bridge, "_capture_pane", capture)
    monkeypatch.setattr(bridge, "_run_tmux", lambda *args: sent.append(args))
    with pytest.raises(bridge.ClaudeUserPromptPending):
        bridge.inject_user_message(native_bridge, content="Follow-up")
    assert captures == 2
    assert sent == []


def test_question_appearing_after_readiness_prevents_paste(
    native_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = []
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_: _COMPOSER)
    monkeypatch.setattr(bridge, "_run_tmux", lambda *args: sent.append(args))
    monkeypatch.setattr(
        bridge, "_wait_for_claude_prompt_ready", lambda *_a, **_k: _park_hook(native_bridge)
    )
    with pytest.raises(bridge.ClaudeUserPromptPending):
        bridge.inject_user_message(native_bridge, content="Follow-up")
    assert sent == []


def test_question_appearing_during_readiness_does_not_become_a_terminal_timeout(
    native_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_: _QUESTION)
    with pytest.raises(bridge.ClaudeUserPromptPending):
        bridge._wait_for_claude_prompt_ready(
            "/tmp/example.sock", "main", timeout_s=0, bridge_dir=native_bridge
        )


def test_explicit_interrupt_can_cancel_a_pending_question(
    native_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _park_hook(native_bridge)
    sent = []
    monkeypatch.setattr(bridge, "_run_tmux", lambda *args: sent.append(args))
    bridge.inject_interrupt(native_bridge)
    assert sent == [("/tmp/example.sock", "send-keys", "-t", "main", "Escape")]


@pytest.mark.parametrize(
    "prompt", [_QUESTION, _PERMISSION, None], ids=["question", "permission", "hook"]
)
def test_prompt_appearing_after_slash_paste_receives_no_submit_enter(
    native_bridge: Path, monkeypatch: pytest.MonkeyPatch, prompt: str | None
) -> None:
    """A model-setting command must not submit a dialog that replaces its draft."""
    sent: list[tuple[str, ...]] = []
    pasted = False

    def send(*args: str) -> None:
        nonlocal pasted
        sent.append(args)
        if "-l" in args:
            pasted = True
            if prompt is None:
                _park_hook(native_bridge)

    monkeypatch.setattr(
        bridge, "_capture_pane", lambda *_: (prompt or "") if pasted else _COMPOSER
    )
    monkeypatch.setattr(bridge, "_run_tmux", send)
    monkeypatch.setattr(bridge, "_PASTE_COMMIT_TIMEOUT_S", 0)
    monkeypatch.setattr(bridge, "_PASTE_SETTLE_S", 0)
    with pytest.raises(bridge.ClaudeUserPromptPending):
        bridge.inject_slash_command(native_bridge, command="/model different")
    assert pasted
    assert [args[-1] for args in sent] == ["C-u", "/model different"]


@pytest.mark.parametrize(
    "prompt", [_QUESTION, _PERMISSION, None], ids=["question", "permission", "hook"]
)
def test_prompt_appearing_during_confirmation_receives_no_enter(
    native_bridge: Path, monkeypatch: pytest.MonkeyPatch, prompt: str | None
) -> None:
    """An unrelated prompt takes precedence over a late model confirmation."""
    sent: list[tuple[str, ...]] = []
    captures = 0

    def capture(*_: str) -> str:
        nonlocal captures
        captures += 1
        if captures == 1:
            return _COMPOSER
        if prompt is None:
            _park_hook(native_bridge)
        return prompt or ""

    monkeypatch.setattr(bridge, "_capture_pane", capture)
    monkeypatch.setattr(bridge, "_run_tmux", lambda *args: sent.append(args))
    monkeypatch.setattr(bridge, "_CLAUDE_READY_POLL_INTERVAL_S", 0)
    with pytest.raises(bridge.ClaudeUserPromptPending):
        bridge._confirm_tui_dialog(
            "/tmp/example.sock",
            "main",
            hint=bridge.SWITCH_MODEL_DIALOG_HINT,
            timeout_s=1,
            bridge_dir=native_bridge,
        )
    assert captures >= 2
    assert sent == []


@pytest.mark.parametrize(
    "prompt", [_QUESTION, _PERMISSION, None], ids=["question", "permission", "hook"]
)
def test_confirmation_timeout_cannot_accept_a_pending_prompt(
    native_bridge: Path, monkeypatch: pytest.MonkeyPatch, prompt: str | None
) -> None:
    """The timeout fallback must protect native questions and parked hooks."""
    sent: list[tuple[str, ...]] = []
    if prompt is None:
        _park_hook(native_bridge)
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_: prompt or "")
    monkeypatch.setattr(bridge, "_run_tmux", lambda *args: sent.append(args))
    with pytest.raises(bridge.ClaudeUserPromptPending):
        bridge._confirm_tui_dialog(
            "/tmp/example.sock",
            "main",
            hint=bridge.SWITCH_MODEL_DIALOG_HINT,
            timeout_s=0,
            bridge_dir=native_bridge,
        )
    assert sent == []


@pytest.mark.parametrize(
    "prompt", [_QUESTION, _PERMISSION, None], ids=["question", "permission", "hook"]
)
def test_confirmation_retry_stops_when_a_prompt_replaces_the_dialog(
    native_bridge: Path, monkeypatch: pytest.MonkeyPatch, prompt: str | None
) -> None:
    """A stale model-dialog title must not draw another Enter onto a new prompt."""
    sent: list[tuple[str, ...]] = []
    accepted = False

    def send(*args: str) -> None:
        nonlocal accepted
        sent.append(args)
        accepted = True
        if prompt is None:
            _park_hook(native_bridge)

    def capture(*_: str) -> str:
        if not accepted:
            return bridge.SWITCH_MODEL_DIALOG_HINT
        return f"{bridge.SWITCH_MODEL_DIALOG_HINT}\n{prompt}" if prompt else ""

    monkeypatch.setattr(bridge, "_capture_pane", capture)
    monkeypatch.setattr(bridge, "_run_tmux", send)
    monkeypatch.setattr(bridge, "_CLAUDE_READY_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(bridge, "_CONFIRM_DIALOG_RETRY_INTERVAL_S", 0)
    monkeypatch.setattr(bridge, "_CONFIRM_DIALOG_ACCEPT_TIMEOUT_S", 1)
    with pytest.raises(bridge.ClaudeUserPromptPending):
        bridge._confirm_and_verify_dialog_closed(
            "/tmp/example.sock",
            "main",
            hint=bridge.SWITCH_MODEL_DIALOG_HINT,
            bridge_dir=native_bridge,
        )
    assert accepted
    assert [args[-1] for args in sent] == ["Enter"]


def test_accepted_submit_can_immediately_raise_a_question(
    native_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new question proves the submitted draft left the composer; no retry is needed."""
    sent: list[tuple[str, ...]] = []
    _park_hook(native_bridge)
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_: _QUESTION)
    monkeypatch.setattr(bridge, "_run_tmux", lambda *args: sent.append(args))
    monkeypatch.setattr(bridge, "_CLAUDE_READY_POLL_INTERVAL_S", 0)
    assert bridge._verify_submit_accepted(
        "/tmp/example.sock",
        "main",
        needle="Follow-up",
        what="submitted message",
        bridge_dir=native_bridge,
    )
    assert sent == []


def test_submit_retry_respects_a_hook_even_when_the_draft_is_still_visible(
    native_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale composer frame must not draw a retry Enter onto a parked prompt."""
    sent: list[tuple[str, ...]] = []
    _park_hook(native_bridge)
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_: _COMPOSER.replace("❯", "❯ Follow-up"))
    monkeypatch.setattr(bridge, "_run_tmux", lambda *args: sent.append(args))
    monkeypatch.setattr(bridge, "_CLAUDE_READY_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(bridge, "_SUBMIT_RETRY_INTERVAL_S", 0)
    with pytest.raises(bridge.ClaudeUserPromptPending):
        bridge._verify_submit_accepted(
            "/tmp/example.sock",
            "main",
            needle="Follow-up",
            what="submitted message",
            bridge_dir=native_bridge,
        )
    assert sent == []
