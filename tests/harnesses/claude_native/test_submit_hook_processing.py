"""A prompt hook can be processing a draft without having accepted it."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge
from omnigent.util.json_types import JsonObject
from tests.harnesses.claude_native._submit_hook_support import NATIVE_SESSION_ID, install_submit
from tests.harnesses.claude_native.test_claude_native_bridge import _composer_pane
from tests.harnesses.claude_native.test_hook_block_submit_retries import _blocked_prompt_entry

_CONTENT = "synthetic delayed prompt"


def test_slow_allowed_hook_waits_for_the_composer_without_repeating_enter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = install_submit(tmp_path, monkeypatch, outcome="allow", content=_CONTENT)
    bridge.inject_user_message(tmp_path, content=_CONTENT)
    assert 30 <= state.elapsed < 31
    assert state.enters == 1
    assert state.composer.submitted == [_CONTENT + "\n"]


@pytest.mark.parametrize("outcome", ["block", "block-empty"])
def test_slow_blocked_hook_stops_as_unknown_without_repeating_enter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    state = install_submit(tmp_path, monkeypatch, outcome=outcome, content=_CONTENT)
    with pytest.raises(bridge.ClaudeSubmitBlockedUnconfirmed):
        bridge.inject_user_message(tmp_path, content=_CONTENT)
    assert 30 <= state.elapsed < 31
    assert state.enters == 1
    assert state.after_submit == []


def test_matching_hook_with_a_retained_draft_never_proves_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = install_submit(tmp_path, monkeypatch, outcome="never", content=_CONTENT)
    with pytest.raises(RuntimeError, match="could not be confirmed") as raised:
        bridge.inject_user_message(tmp_path, content=_CONTENT)
    assert not isinstance(raised.value, bridge.ClaudeMessageNotAccepted)
    assert 120 <= state.elapsed < 121
    assert state.enters == 1
    assert _CONTENT in state.pane


@pytest.mark.parametrize("hook_kind", ["wrapped", "initially-absent"])
def test_fresh_matching_hook_can_use_a_new_log_or_pasted_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hook_kind: str
) -> None:
    state = install_submit(
        tmp_path, monkeypatch, outcome="allow", hook_kind=hook_kind, content=_CONTENT
    )
    bridge.inject_user_message(tmp_path, content=_CONTENT)
    assert state.enters == 1
    assert 30 <= state.elapsed < 31


@pytest.mark.parametrize("hook_kind", ["none", "wrong-session", "partial-record"])
def test_unmatched_or_unreadable_hook_keeps_the_existing_submit_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hook_kind: str
) -> None:
    state = install_submit(
        tmp_path, monkeypatch, outcome="never", hook_kind=hook_kind, content=_CONTENT
    )
    with pytest.raises(bridge.ClaudeMessageNotAccepted):
        bridge.inject_user_message(tmp_path, content=_CONTENT)
    assert 20 <= state.elapsed < 21
    assert state.enters == 5


@pytest.mark.parametrize("rotation", ["native-session", "transcript", "web-session"])
@pytest.mark.parametrize("before_hook", [False, True])
def test_session_rotation_while_processing_stops_without_touching_the_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rotation: str, before_hook: bool
) -> None:
    state = install_submit(
        tmp_path,
        monkeypatch,
        outcome="never",
        hook_kind="none" if before_hook else "fresh",
        content=_CONTENT,
    )
    capture = bridge._capture_pane

    def rotate(*args: str) -> str:
        if state.enters and (before_hook or state.elapsed >= 5):
            if rotation == "web-session":
                bridge.write_active_session_id(tmp_path, "web-replacement")
            else:
                bridge.record_hook_event(
                    tmp_path,
                    {
                        "hook_event_name": "SessionStart",
                        "session_id": "native-replacement"
                        if rotation == "native-session"
                        else NATIVE_SESSION_ID,
                        "transcript_path": str(tmp_path / "replacement.jsonl")
                        if rotation == "transcript"
                        else str(state.transcript),
                    },
                )
            if before_hook:
                state.pane = _composer_pane()
        return capture(*args)

    monkeypatch.setattr(bridge, "_capture_pane", rotate)
    with pytest.raises(RuntimeError, match="session changed") as raised:
        bridge.inject_user_message(tmp_path, content=_CONTENT)
    assert not isinstance(raised.value, bridge.ClaudeMessageNotAccepted)
    assert 0 <= state.elapsed < 1 if before_hook else 5 <= state.elapsed < 6
    assert state.after_submit == []
    if not before_hook:
        assert _CONTENT in state.pane


def test_later_hooks_do_not_renew_the_processing_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = install_submit(tmp_path, monkeypatch, outcome="never", content=_CONTENT)
    capture = bridge._capture_pane

    def append(*args: str) -> str:
        if state.enters:
            with (tmp_path / "hooks.jsonl").open("a") as handle:
                handle.write(json.dumps({"payload": state.hook}) + "\n")
        return capture(*args)

    monkeypatch.setattr(bridge, "_capture_pane", append)
    with pytest.raises(RuntimeError, match="could not be confirmed"):
        bridge.inject_user_message(tmp_path, content=_CONTENT)
    assert 120 <= state.elapsed < 121
    assert state.enters == 1


def test_a_late_hook_uses_the_original_submit_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = install_submit(
        tmp_path, monkeypatch, outcome="never", hook_kind="none", content=_CONTENT
    )
    capture = bridge._capture_pane
    monkeypatch.setattr(bridge, "_SUBMIT_RETRY_INTERVAL_S", 60)

    def append_late(*args: str) -> str:
        if state.elapsed >= 19:
            with (tmp_path / "hooks.jsonl").open("a") as handle:
                handle.write(json.dumps({"payload": state.hook}) + "\n")
        return capture(*args)

    monkeypatch.setattr(bridge, "_capture_pane", append_late)
    with pytest.raises(RuntimeError, match="could not be confirmed"):
        bridge.inject_user_message(tmp_path, content=_CONTENT)
    assert 120 <= state.elapsed < 121
    assert state.enters == 1


def test_cancellation_while_processing_stops_the_worker_before_any_more_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = install_submit(tmp_path, monkeypatch, outcome="never", content=_CONTENT)
    cancel = threading.Event()
    capture = bridge._capture_pane

    def cancel_after_processing(*args: str) -> str:
        if state.elapsed >= 5:
            cancel.set()
        return capture(*args)

    monkeypatch.setattr(bridge, "_capture_pane", cancel_after_processing)
    with bridge.cancellable_injection(cancel), pytest.raises(bridge.ClaudeInjectionCancelled):
        bridge.inject_user_message(tmp_path, content=_CONTENT)
    assert 5 <= state.elapsed < 6
    assert state.after_submit == []
    assert _CONTENT in state.pane


def test_empty_captures_during_hook_processing_never_prove_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = install_submit(tmp_path, monkeypatch, outcome="never", content=_CONTENT)
    capture = bridge._capture_pane
    monkeypatch.setattr(
        bridge, "_capture_pane", lambda *args: "" if state.elapsed >= 1 else capture(*args)
    )
    with pytest.raises(RuntimeError, match="could not be confirmed"):
        bridge.inject_user_message(tmp_path, content=_CONTENT)
    assert 120 <= state.elapsed < 121
    assert state.enters == 1
    assert state.after_submit == []


@pytest.mark.parametrize("rotate_web", [False, True], ids=["native-only", "web-and-native"])
@pytest.mark.parametrize(
    ("command", "evidence"),
    [
        ("fork", "valid"),
        ("branch", "valid"),
        ("clear", "valid"),
        ("fork", "ordinary-resume"),
        ("fork", "wrong-parent"),
        ("fork", "seen-session"),
        ("clear", "wrong-source"),
        ("clear", "child"),
        ("clear", "stale"),
        ("clear", "wrong-transcript"),
        ("clear", "still-draft"),
        ("clear", "empty-capture"),
        ("clear", "blocked"),
        ("clear", "wrong-web"),
        ("clear", "delayed-hook"),
    ],
)
def test_intentional_rotation_requires_a_fresh_matching_hook_and_cleared_composer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    evidence: str,
    rotate_web: bool,
) -> None:
    content = f"/{command}"
    state = install_submit(
        tmp_path, monkeypatch, outcome="never", hook_kind="none", content=content
    )
    capture = bridge._capture_pane
    payload: JsonObject = {
        "hook_event_name": "SessionStart",
        "source": "clear" if command == "clear" else "resume",
        "session_id": "native-replacement",
        "transcript_path": str(tmp_path / "replacement.jsonl"),
    }
    marker = "omnigent_clear_rotated_to" if command == "clear" else "omnigent_fork_rotated_to"
    if rotate_web:
        payload[marker] = "web-replacement"
    if command != "clear":
        payload.update(
            omnigent_previous_claude_session_id=NATIVE_SESSION_ID,
            omnigent_claude_session_was_seen=False,
            omnigent_fork_detected=True,
        )
    if evidence == "stale":
        bridge.record_hook_event(tmp_path, payload)
        bridge.record_hook_event(
            tmp_path,
            {
                "hook_event_name": "SessionStart",
                "session_id": NATIVE_SESSION_ID,
                "transcript_path": str(state.transcript),
            },
        )

    def rotate(*args: str) -> str:
        if state.enters and not state.completed:
            if evidence == "delayed-hook" and state.elapsed < 2 and rotate_web:
                bridge.write_active_session_id(tmp_path, "web-replacement")
                state.pane = _composer_pane()
                return state.pane
            state.completed = True
            if evidence == "ordinary-resume":
                payload.pop("omnigent_fork_detected")
            elif evidence == "wrong-parent":
                payload["omnigent_previous_claude_session_id"] = "unrelated-native"
            elif evidence == "seen-session":
                payload["omnigent_claude_session_was_seen"] = True
            elif evidence in {"wrong-source", "stale"}:
                payload["source"] = "startup"
            elif evidence == "child":
                payload["agent_id"] = "native-child"
            elif evidence == "wrong-web":
                payload[marker] = "unrelated-web"
            bridge.record_hook_event(tmp_path, payload)
            if evidence == "child":
                bridge.record_hook_event(
                    tmp_path,
                    {**payload, "agent_id": "", "source": "startup"},
                )
            if rotate_web:
                bridge.write_active_session_id(tmp_path, "web-replacement")
            if evidence == "wrong-transcript":
                (tmp_path / "state.json").write_text(
                    json.dumps(
                        {
                            "claude_session_id": "native-replacement",
                            "transcript_path": str(tmp_path / "unrelated.jsonl"),
                        }
                    )
                )
            if evidence == "blocked":
                with state.transcript.open("a") as handle:
                    handle.write(
                        json.dumps(
                            _blocked_prompt_entry(
                                "Synthetic refusal.", content, native_session_id=NATIVE_SESSION_ID
                            )
                        )
                        + "\n"
                    )
            if evidence != "still-draft":
                state.pane = _composer_pane()
        if state.enters and evidence == "empty-capture":
            return ""
        return capture(*args)

    monkeypatch.setattr(bridge, "_capture_pane", rotate)
    accepted = evidence in {"valid", "delayed-hook"} or (
        evidence == "wrong-web" and not rotate_web
    )
    if accepted:
        bridge.inject_user_message(tmp_path, content=content)
    else:
        with pytest.raises(RuntimeError, match="session changed"):
            bridge.inject_user_message(tmp_path, content=content)
    assert state.enters == 1
    assert state.after_submit == []
    if evidence == "delayed-hook" and rotate_web:
        assert 2 <= state.elapsed < 3
    assert bridge.read_active_session_id(tmp_path) == (
        "web-replacement" if rotate_web else "web-original"
    )
