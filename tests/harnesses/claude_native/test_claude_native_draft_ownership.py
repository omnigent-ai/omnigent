"""A native draft is user-owned until an injection has staged its own input."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge
from tests.harnesses.claude_native import test_claude_native_bridge as scenarios
from tests.harnesses.claude_native.test_claude_native_shell_mode_draft import (
    _InputBox,
    _patch_tmux,
)

_trust_tmp_bridge_parent = scenarios._trust_tmp_bridge_parent


@pytest.mark.parametrize(
    ("draft", "visible_rows"),
    [
        ("Synthetic human draft", None),
        ("Draft Δ 😀\nSecond line", None),
        ("ONE\nTWO\nTHREE\nFOUR", 1),
    ],
    ids=["plain-human", "utf8-human", "clipped-human"],
)
def test_unknown_draft_is_preserved_before_web_dispatch(
    tmp_path, monkeypatch, draft, visible_rows
):
    bridge_dir = scenarios._picker_bridge_dir(tmp_path)
    composer = scenarios._EditableComposer(draft, cursor=0, visible_rows=visible_rows)
    composer.install(monkeypatch)
    with pytest.raises(bridge.ClaudeComposerNotCleared):
        bridge.inject_user_message(bridge_dir, content="Synthetic web input")
    assert composer.text == draft
    assert composer.submitted == []


def test_empty_composer_delivers_its_owned_staged_input_once(tmp_path, monkeypatch):
    bridge_dir = scenarios._picker_bridge_dir(tmp_path)
    composer = scenarios._EditableComposer("", cursor=0)
    composer.install(monkeypatch)
    original = composer._run_tmux
    enters = 0
    pastes = 0

    def send(socket_path, *args):
        nonlocal enters, pastes
        if args[0] == "paste-buffer":
            pastes += 1
        if args[0] == "send-keys" and args[-1] == "Enter":
            enters += 1
            if enters == 1:
                return
        original(socket_path, *args)

    monkeypatch.setattr(bridge, "_run_tmux", send)
    bridge.inject_user_message(bridge_dir, content="Owned web input Δ")
    assert enters == 2
    assert pastes == 1
    assert composer.submitted == ["Owned web input Δ\n"]


@pytest.mark.parametrize("draft", ["git status --short", "echo Δ\nprintf next"])
def test_typed_shell_surface_is_preserved(tmp_path, monkeypatch, draft):
    box = _InputBox("bash", draft)
    bridge_dir, calls = _patch_tmux(monkeypatch, tmp_path, box, content="web input")
    with pytest.raises(bridge.ClaudeInputBoxOccupied):
        bridge.inject_user_message(bridge_dir, content="web input")
    assert box.draft == draft
    assert box.mode == "bash"
    assert box.submitted == []
    assert not any("paste-buffer" in call or "send-keys" in call for call in calls)


def test_scrolled_draft_is_preserved(tmp_path, monkeypatch):
    draft = "\n".join(f"Human line {n}" for n in range(10))
    bridge_dir = scenarios._picker_bridge_dir(tmp_path)
    composer = scenarios._EditableComposer(draft, cursor=0, pane_rows=5)
    composer.install(monkeypatch)
    with pytest.raises(bridge.ClaudeInputBoxOccupied):
        bridge.inject_user_message(bridge_dir, content="web input")
    assert composer.text == draft
    assert composer.submitted == []


def test_cancellation_before_dispatch_preserves_the_draft(tmp_path, monkeypatch):
    bridge_dir = scenarios._picker_bridge_dir(tmp_path)
    draft = "Synthetic human draft"
    composer = scenarios._EditableComposer(draft, cursor=0)
    composer.install(monkeypatch)
    capture = composer._capture_pane
    cancel = threading.Event()

    def cancel_on_capture(socket_path, target):
        cancel.set()
        return capture(socket_path, target)

    monkeypatch.setattr(bridge, "_capture_pane", cancel_on_capture)
    with bridge.cancellable_injection(cancel), pytest.raises(bridge.ClaudeInjectionCancelled):
        bridge.inject_user_message(bridge_dir, content="web input")
    assert composer.text == draft
    assert composer.submitted == []


def test_unreadable_empty_capture_never_gets_edit_keys(monkeypatch):
    calls = scenarios._fake_tmux(monkeypatch, [""])
    monkeypatch.setattr(bridge, "_claude_pane_state", lambda *_: bridge._ClaudePaneState(True))
    monkeypatch.setattr(bridge, "_request_claude_repaint", lambda *_: False)
    with pytest.raises(bridge.ClaudeComposerNotCleared):
        bridge._prepare_empty_composer("synthetic.sock", "main", bridge_dir=Path("/tmp/bridge"))
    assert calls == []


def test_matching_web_text_does_not_own_the_human_draft(tmp_path, monkeypatch):
    bridge_dir = scenarios._picker_bridge_dir(tmp_path)
    composer = scenarios._EditableComposer("same synthetic text", cursor=0)
    composer.install(monkeypatch)
    with pytest.raises(bridge.ClaudeComposerNotCleared):
        bridge.inject_user_message(bridge_dir, content="same synthetic text")
    assert composer.text == "same synthetic text"
    assert composer.submitted == []


@pytest.mark.parametrize("exit_status", [None, "0", "7"])
def test_an_unreadable_gone_target_is_an_exit_before_any_edit(monkeypatch, exit_status):
    calls = scenarios._fake_tmux(monkeypatch, [""])
    targets = []

    def target_state(socket_path, target):
        targets.append((socket_path, target))
        return bridge._ClaudePaneState(
            False, exited=exit_status is not None, exit_status=exit_status
        )

    monkeypatch.setattr(bridge, "_claude_pane_state", target_state)
    with pytest.raises(bridge.ClaudeTerminalExited) as raised:
        bridge._prepare_empty_composer(
            "synthetic.sock", "exact:0.1", bridge_dir=Path("/tmp/bridge")
        )
    assert raised.value.exit_status == exit_status
    assert targets == [("synthetic.sock", "exact:0.1")]
    assert calls == []


@pytest.mark.parametrize("alive", [True, None])
def test_an_unreadable_live_or_unknown_target_stays_failclosed(monkeypatch, alive):
    calls = scenarios._fake_tmux(monkeypatch, [""])
    monkeypatch.setattr(bridge, "_claude_pane_state", lambda *_: bridge._ClaudePaneState(alive))
    monkeypatch.setattr(bridge, "_request_claude_repaint", lambda *_: False)
    with pytest.raises(bridge.ClaudeComposerNotCleared):
        bridge._prepare_empty_composer(
            "synthetic.sock", "exact:0.1", bridge_dir=Path("/tmp/bridge")
        )
    assert calls == []
