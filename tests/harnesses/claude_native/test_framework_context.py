"""Hidden context follows the exact prompt through late native submission."""

from __future__ import annotations

import io
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge, framework_context, hook, submit_verification
from omnigent.inner.terminal_lifecycle import TERMINAL_INSTANCE_ID_ENV, TERMINAL_LAUNCH_ID_ENV


@pytest.fixture
def terminal_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path)
    directory = bridge.prepare_bridge_dir("session-a", workspace=tmp_path)
    bridge.write_tmux_target(
        directory,
        socket_path="/tmp/example.sock",
        tmux_target="main",
        launch_id="a" * 32,
        instance_id="b" * 32,
    )
    monkeypatch.setenv(TERMINAL_LAUNCH_ID_ENV, "a" * 32)
    monkeypatch.setenv(TERMINAL_INSTANCE_ID_ENV, "b" * 32)
    return directory


@pytest.fixture
def context_bridge(terminal_launch: Path) -> Path:
    hook.record_hook_payload(
        terminal_launch,
        {"hook_event_name": "SessionStart", "session_id": "native-a"},
        prompt_framework_context=True,
    )
    assert framework_context.prompt_framework_context_capability_ready(terminal_launch)
    return terminal_launch


def _submitted(prompt: str, *, session_id: str = "native-a") -> dict[str, object]:
    return {"hook_event_name": "UserPromptSubmit", "session_id": session_id, "prompt": prompt}


def test_late_framework_context_hook_consumes_owned_notice_once(
    context_bridge: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(framework_context.time, "time", lambda: 100.0)
    handle = framework_context.write_prompt_framework_context(
        context_bridge, "inspect the image", ["image dimensions changed"]
    )
    assert handle is not None
    monkeypatch.setattr(framework_context.time, "time", lambda: 125.0)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_submitted("inspect the image"))))

    assert hook.main(["framework-context", "--bridge-dir", str(context_bridge)]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"] == {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "image dimensions changed",
    }
    assert not handle.path.exists()
    assert (
        framework_context.consume_prompt_framework_context(
            context_bridge, _submitted("inspect the image")
        )
        is None
    )


@pytest.mark.parametrize(
    "foreign",
    [
        _submitted("a" * 1000 + "foreign suffix"),
        _submitted("a" * 1000 + "owned suffix", session_id="native-b"),
        {"hook_event_name": "Stop", "session_id": "native-a", "prompt": "a" * 1000},
        {"hook_event_name": "UserPromptSubmit", "session_id": "native-a"},
        {**_submitted("a" * 1000 + "owned suffix"), "agent_id": "child-a"},
        {
            **_submitted("a" * 1000 + "owned suffix"),
            "transcript_path": "/tmp/example/subagents/child.jsonl",
        },
    ],
    ids=[
        "different-tail",
        "foreign-native-session",
        "different-event",
        "missing-prompt",
        "child-agent",
        "child-transcript",
    ],
)
def test_foreign_prompt_cannot_consume_pending_framework_context(
    context_bridge: Path, foreign: dict[str, object]
) -> None:
    prompt = "a" * 1000 + "owned suffix"
    handle = framework_context.write_prompt_framework_context(context_bridge, prompt, ["notice"])
    assert handle is not None

    assert framework_context.consume_prompt_framework_context(context_bridge, foreign) is None
    assert handle.path.exists()
    assert (
        framework_context.consume_prompt_framework_context(context_bridge, _submitted(prompt))
        == "notice"
    )


@pytest.mark.parametrize("replacement", ["session", "native", "launch"])
def test_replaced_owner_cannot_inherit_framework_context(
    context_bridge: Path, replacement: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = framework_context.write_prompt_framework_context(context_bridge, "same text", ["old"])
    assert handle is not None
    native_session = "native-a"
    if replacement == "session":
        bridge.write_active_session_id(context_bridge, "session-b")
    elif replacement == "native":
        native_session = "native-b"
        bridge.record_hook_event(
            context_bridge, {"hook_event_name": "SessionStart", "session_id": native_session}
        )
    else:
        bridge.write_tmux_target(
            context_bridge,
            socket_path="/tmp/example.sock",
            tmux_target="main",
            launch_id="c" * 32,
        )
        monkeypatch.setenv(TERMINAL_LAUNCH_ID_ENV, "c" * 32)

    assert (
        framework_context.consume_prompt_framework_context(
            context_bridge, _submitted("same text", session_id=native_session)
        )
        is None
    )
    assert not handle.path.exists()


@pytest.mark.parametrize(
    "new_context", [[], ["old"], ["different notice"]], ids=["no-notice", "same", "different"]
)
def test_identical_prompt_waits_before_another_input_is_staged(
    context_bridge: Path, new_context: list[str]
) -> None:
    old = framework_context.write_prompt_framework_context(context_bridge, "same text", ["old"])
    assert old is not None
    with pytest.raises(framework_context.PromptFrameworkContextPending):
        framework_context.write_prompt_framework_context(context_bridge, "same text", new_context)
    assert list(old.path.parent.glob("*.json")) == [old.path]
    assert (
        framework_context.consume_prompt_framework_context(context_bridge, _submitted("same text"))
        == "old"
    )
    new = framework_context.write_prompt_framework_context(
        context_bridge, "same text", new_context
    )
    assert new is not None
    assert framework_context.consume_prompt_framework_context(
        context_bridge, _submitted("same text")
    ) == "\n\n".join(new_context)


def test_earlier_plain_input_cannot_steal_later_identical_prompts_notice(
    context_bridge: Path,
) -> None:
    plain = framework_context.write_prompt_framework_context(context_bridge, "same text", [])
    assert plain is not None
    with pytest.raises(framework_context.PromptFrameworkContextPending):
        framework_context.write_prompt_framework_context(
            context_bridge, "same text", ["new notice"]
        )
    assert (
        framework_context.consume_prompt_framework_context(context_bridge, _submitted("same text"))
        == ""
    )
    noticed = framework_context.write_prompt_framework_context(
        context_bridge, "same text", ["new notice"]
    )
    assert noticed is not None
    assert (
        framework_context.consume_prompt_framework_context(context_bridge, _submitted("same text"))
        == "new notice"
    )


def test_rejected_input_cleanup_does_not_delete_successor_context(context_bridge: Path) -> None:
    old = framework_context.write_prompt_framework_context(context_bridge, "same text", ["old"])
    new = framework_context.write_prompt_framework_context(context_bridge, "new text", ["new"])

    framework_context.discard_prompt_framework_context(old)
    framework_context.discard_prompt_framework_context(old)

    assert new is not None and new.path.exists()
    assert (
        framework_context.consume_prompt_framework_context(context_bridge, _submitted("new text"))
        == "new"
    )


def test_stopped_input_keeps_empty_fence_until_its_late_hook(context_bridge: Path) -> None:
    stopped = framework_context.write_prompt_framework_context(
        context_bridge, "same text", ["old"]
    )
    assert stopped is not None
    framework_context.discard_prompt_framework_context(stopped, retain_fence=True)

    with pytest.raises(framework_context.PromptFrameworkContextPending):
        framework_context.write_prompt_framework_context(context_bridge, "same text", ["new"])
    assert (
        framework_context.consume_prompt_framework_context(context_bridge, _submitted("same text"))
        == ""
    )
    framework_context.write_prompt_framework_context(context_bridge, "same text", ["new"])
    assert (
        framework_context.consume_prompt_framework_context(context_bridge, _submitted("same text"))
        == "new"
    )


@pytest.mark.parametrize("hook_launch", [None, "c" * 32], ids=["missing", "retired-launch"])
def test_hook_from_another_launch_cannot_consume_current_context(
    context_bridge: Path, hook_launch: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = framework_context.write_prompt_framework_context(
        context_bridge, "same text", ["notice"]
    )
    assert handle is not None
    if hook_launch is None:
        monkeypatch.delenv(TERMINAL_LAUNCH_ID_ENV)
    else:
        monkeypatch.setenv(TERMINAL_LAUNCH_ID_ENV, hook_launch)

    assert (
        framework_context.consume_prompt_framework_context(context_bridge, _submitted("same text"))
        is None
    )
    assert handle.path.exists()


@pytest.mark.parametrize(
    "submitted",
    ["\nfirst\r\nsecond\r\n", '<pasted_content id="paste">\nfirst\nsecond\n</pasted_content>'],
    ids=["line-endings", "vendor-paste-wrapper"],
)
def test_framework_context_matches_complete_normalized_prompt(
    context_bridge: Path, submitted: str
) -> None:
    framework_context.write_prompt_framework_context(context_bridge, "first\nsecond", ["notice"])

    assert (
        framework_context.consume_prompt_framework_context(context_bridge, _submitted(submitted))
        == "notice"
    )


def test_framework_context_is_consumed_once_by_concurrent_hooks(context_bridge: Path) -> None:
    framework_context.write_prompt_framework_context(context_bridge, "prompt", ["notice"])
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: framework_context.consume_prompt_framework_context(
                    context_bridge, _submitted("prompt")
                ),
                range(2),
            )
        )
    assert results.count("notice") == 1
    assert results.count(None) == 1


@pytest.mark.parametrize("notices", [[], ["old"]], ids=["plain", "noticed"])
def test_elapsed_time_cannot_retire_an_unresolved_live_launch_fence(
    context_bridge: Path, monkeypatch: pytest.MonkeyPatch, notices: list[str]
) -> None:
    monkeypatch.setattr(framework_context.time, "time", lambda: 100.0)
    handle = framework_context.write_prompt_framework_context(context_bridge, "prompt", notices)
    monkeypatch.setattr(framework_context.time, "time", lambda: 701.0)

    with pytest.raises(framework_context.PromptFrameworkContextPending):
        framework_context.write_prompt_framework_context(context_bridge, "prompt", [])
    assert handle is not None and handle.path.exists()
    assert framework_context.consume_prompt_framework_context(
        context_bridge, _submitted("prompt")
    ) == "\n\n".join(notices)


@pytest.mark.parametrize("notices", [[], ["notice"]], ids=["plain", "noticed"])
def test_context_capacity_preserves_existing_unconfirmed_inputs(
    context_bridge: Path, monkeypatch: pytest.MonkeyPatch, notices: list[str]
) -> None:
    monkeypatch.setattr(framework_context, "MAX_PENDING_PROMPT_CONTEXTS", 2)
    first = framework_context.write_prompt_framework_context(context_bridge, "first", notices)
    second = framework_context.write_prompt_framework_context(context_bridge, "second", notices)

    with pytest.raises(bridge.ClaudeMessageNotAccepted, match="too many pending"):
        framework_context.write_prompt_framework_context(context_bridge, "third", notices)

    assert first is not None and first.path.exists()
    assert second is not None and second.path.exists()


@pytest.mark.parametrize(
    "fault",
    [
        "unreadable",
        "malformed",
        "digest",
        "record-id",
        "owner-launch",
        "owner-socket",
        "owner-legacy",
        "checkpoint",
    ],
)
def test_unverifiable_pending_record_does_not_admit_another_input(
    context_bridge: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    original = framework_context.write_prompt_framework_context(context_bridge, "old", ["notice"])
    read_json = bridge._read_json_file

    def unreadable(path: Path) -> dict[str, object]:
        if path == original.path:
            if fault == "unreadable":
                raise OSError("temporarily unreadable")
            if fault == "malformed":
                return {}
            record = read_json(path)
            if fault == "digest":
                record["prompt_sha256"] = "broken"
            elif fault == "record-id":
                record["record_id"] = "broken"
            elif fault == "checkpoint":
                record["submit_boundary"] = {}
            else:
                recorded_owner = record["owner"]
                assert isinstance(recorded_owner, dict)
                if fault == "owner-launch":
                    recorded_owner["launch_id"] = 123
                elif fault == "owner-socket":
                    recorded_owner["socket_path"] = []
                else:
                    recorded_owner["legacy_advertisement"] = [True, "bad", 1.5]
            return record
        return read_json(path)

    monkeypatch.setattr(bridge, "_read_json_file", unreadable)
    with pytest.raises(framework_context.PromptFrameworkContextPending, match="could not be"):
        framework_context.write_prompt_framework_context(context_bridge, "new", ["new notice"])
    assert list(original.path.parent.glob("*.json")) == [original.path]


def test_unbound_or_legacy_framework_context_is_not_guessed(
    context_bridge: Path, tmp_path: Path
) -> None:
    legacy = context_bridge / bridge.CLAUDE_FRAMEWORK_CONTEXT_FILE
    legacy.write_text("unowned notice", encoding="utf-8")
    assert (
        framework_context.consume_prompt_framework_context(
            context_bridge, _submitted("unrelated prompt")
        )
        is None
    )
    unknown = bridge.prepare_bridge_dir("unbound-session", workspace=tmp_path)
    with pytest.raises(bridge.ClaudeMessageNotAccepted, match="session identity is not ready"):
        framework_context.write_prompt_framework_context(unknown, "prompt", ["notice"])
    with pytest.raises(bridge.ClaudeMessageNotAccepted, match="session identity is not ready"):
        framework_context.write_prompt_framework_context(unknown, "plain input", [])


def test_legacy_terminal_requires_launch_identity_for_notices(context_bridge: Path) -> None:
    bridge.write_tmux_target(context_bridge, socket_path="/tmp/example.sock", tmux_target="main")
    with pytest.raises(
        bridge.ClaudeMessageNotAccepted, match="terminal launch identity is not ready"
    ):
        framework_context.write_prompt_framework_context(context_bridge, "prompt", ["notice"])


def test_only_new_session_start_option_confirms_hook_capability(
    terminal_launch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {"hook_event_name": "SessionStart", "session_id": "native-a"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert hook.main(["--bridge-dir", str(terminal_launch)]) == 0
    assert not framework_context.prompt_framework_context_capability_ready(terminal_launch)

    bridge.write_tmux_target(
        terminal_launch,
        socket_path="/tmp/example.sock",
        tmux_target="main",
        launch_id="a" * 32,
        instance_id="b" * 32,
    )
    assert not framework_context.prompt_framework_context_capability_ready(terminal_launch)
    with pytest.raises(bridge.ClaudeMessageNotAccepted, match="has not loaded"):
        framework_context.write_prompt_framework_context(terminal_launch, "plain input", [])

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert hook.main(["--bridge-dir", str(terminal_launch), "--prompt-framework-context"]) == 0
    assert framework_context.prompt_framework_context_capability_ready(terminal_launch)


def test_session_start_capability_can_precede_terminal_advertisement(
    terminal_launch: Path,
) -> None:
    (terminal_launch / bridge._TMUX_FILE).unlink()
    hook.record_hook_payload(
        terminal_launch,
        {"hook_event_name": "SessionStart", "session_id": "native-a"},
        prompt_framework_context=True,
    )
    assert not framework_context.prompt_framework_context_capability_ready(terminal_launch)

    bridge.write_tmux_target(
        terminal_launch,
        socket_path="/tmp/example.sock",
        tmux_target="main",
        launch_id="a" * 32,
        instance_id="b" * 32,
    )
    assert framework_context.prompt_framework_context_capability_ready(terminal_launch)


@pytest.mark.parametrize(
    "change",
    ["missing-launch", "missing-instance", "foreign-launch", "foreign-instance", "invalid-launch"],
)
def test_capability_requires_actual_matching_hook_process_identities(
    terminal_launch: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    if change == "missing-launch":
        monkeypatch.delenv(TERMINAL_LAUNCH_ID_ENV)
    elif change == "missing-instance":
        monkeypatch.delenv(TERMINAL_INSTANCE_ID_ENV)
    elif change == "foreign-launch":
        monkeypatch.setenv(TERMINAL_LAUNCH_ID_ENV, "c" * 32)
    elif change == "foreign-instance":
        monkeypatch.setenv(TERMINAL_INSTANCE_ID_ENV, "d" * 32)
    else:
        monkeypatch.setenv(TERMINAL_LAUNCH_ID_ENV, "not-a-launch")
    hook.record_hook_payload(
        terminal_launch,
        {"hook_event_name": "SessionStart", "session_id": "native-a"},
        prompt_framework_context=True,
    )
    assert not framework_context.prompt_framework_context_capability_ready(terminal_launch)


@pytest.mark.parametrize(
    "payload",
    [
        _submitted("prompt"),
        {"hook_event_name": "SessionStart", "session_id": "native-a", "agent_id": "child-a"},
        {
            "hook_event_name": "SessionStart",
            "session_id": "native-a",
            "transcript_path": "/tmp/example/subagents/child.jsonl",
        },
    ],
    ids=["different-event", "child-agent", "child-transcript"],
)
def test_child_or_nonstartup_hook_cannot_confirm_parent_capability(
    terminal_launch: Path, payload: dict[str, object]
) -> None:
    hook.record_hook_payload(terminal_launch, payload, prompt_framework_context=True)
    assert not framework_context.prompt_framework_context_capability_ready(terminal_launch)


def test_failed_observer_cannot_confirm_capability_from_old_identity(
    terminal_launch: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"hook_event_name": "SessionStart", "session_id": "native-a"}
    bridge.record_hook_event(terminal_launch, payload)

    def unavailable(*args: object) -> None:
        raise OSError("observer unavailable")

    monkeypatch.setattr(hook, "record_hook_event", unavailable)
    hook.record_hook_payload(terminal_launch, payload, prompt_framework_context=True)
    assert "failed to record hook" in capsys.readouterr().err
    assert bridge.read_claude_session_id(terminal_launch) == "native-a"
    assert not framework_context.prompt_framework_context_capability_ready(terminal_launch)


def test_foreign_instance_cannot_replace_an_adopted_capability(context_bridge: Path) -> None:
    hook.record_hook_payload(
        context_bridge,
        {"hook_event_name": "SessionStart", "session_id": "native-a"},
        prompt_framework_context=True,
        env={TERMINAL_LAUNCH_ID_ENV: "a" * 32, TERMINAL_INSTANCE_ID_ENV: "d" * 32},
    )
    assert framework_context.prompt_framework_context_capability_ready(context_bridge)


@pytest.mark.parametrize("change", ["session", "native", "launch", "instance"])
def test_capability_tracks_physical_launch_not_session_mapping(
    context_bridge: Path, change: str
) -> None:
    if change == "session":
        bridge.write_active_session_id(context_bridge, "session-b")
    elif change == "native":
        bridge.record_hook_event(
            context_bridge, {"hook_event_name": "SessionStart", "session_id": "native-b"}
        )
    else:
        bridge.write_tmux_target(
            context_bridge,
            socket_path="/tmp/example.sock",
            tmux_target="main",
            launch_id="c" * 32 if change == "launch" else "a" * 32,
            instance_id="d" * 32 if change == "instance" else "b" * 32,
        )
    assert framework_context.prompt_framework_context_capability_ready(context_bridge) is (
        change in {"session", "native"}
    )


@pytest.mark.parametrize(
    "proof", ["complete", "missing", "stale", "foreign-native", "changed-path", "required-notice"]
)
def test_only_completed_local_command_retires_a_persisted_empty_fence(
    context_bridge: Path, tmp_path: Path, proof: str
) -> None:
    transcript = tmp_path / "native.jsonl"
    transcript.write_text('{"type":"system","content":"prefix"}\n')
    bridge.record_hook_event(
        context_bridge,
        {
            "hook_event_name": "SessionStart",
            "session_id": "native-a",
            "transcript_path": str(transcript),
        },
    )
    completion = {
        "type": "system",
        "subtype": "local_command",
        "sessionId": "native-a",
        "uuid": "completed-command",
        "content": "<command-name>/model</command-name><command-args>example</command-args>"
        "<local-command-stdout>Done.</local-command-stdout>",
    }
    if proof == "stale":
        with transcript.open("a") as handle:
            handle.write(json.dumps(completion) + "\n")
    boundary = submit_verification.capture_submit_boundary(transcript, "native-a")
    assert boundary is not None
    old = framework_context.write_prompt_framework_context(
        context_bridge,
        "/model example",
        ["required"] if proof == "required-notice" else [],
        submit_boundary=boundary,
    )
    record = json.loads(old.path.read_text())
    assert "/model example" not in old.path.read_text()
    assert submit_verification.parse_submit_boundary(record["submit_boundary"]) == boundary
    if proof == "foreign-native":
        completion["sessionId"] = "native-other"
    if proof not in {"missing", "stale"}:
        with transcript.open("a") as handle:
            handle.write(json.dumps(completion) + "\n")
    if proof == "changed-path":
        bridge.record_hook_event(
            context_bridge,
            {
                "hook_event_name": "SessionStart",
                "session_id": "native-a",
                "transcript_path": str(tmp_path / "other.jsonl"),
            },
        )

    if proof == "complete":
        fresh = framework_context.write_prompt_framework_context(
            context_bridge, "/model example", []
        )
        assert fresh.path.exists() and not old.path.exists()
    else:
        with pytest.raises(framework_context.PromptFrameworkContextPending):
            framework_context.write_prompt_framework_context(context_bridge, "/model example", [])
        assert old.path.exists()


@pytest.mark.parametrize("change", ["native", "path"])
def test_submit_checkpoint_must_still_describe_current_native_transcript(
    context_bridge: Path, tmp_path: Path, change: str
) -> None:
    transcript = tmp_path / "native.jsonl"
    transcript.write_text("")
    bridge.record_hook_event(
        context_bridge,
        {
            "hook_event_name": "SessionStart",
            "session_id": "native-a",
            "transcript_path": str(transcript),
        },
    )
    boundary = submit_verification.capture_submit_boundary(
        transcript if change == "native" else tmp_path / "other.jsonl",
        "native-other" if change == "native" else "native-a",
    )
    assert boundary is not None
    with pytest.raises(bridge.ClaudeMessageNotAccepted, match="native transcript changed"):
        framework_context.write_prompt_framework_context(
            context_bridge, "input", [], submit_boundary=boundary
        )


def test_context_writer_rechecks_pinned_owner_after_waiting_for_metadata_lock(
    context_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager
    from threading import Event

    owner = bridge.read_delivery_owner(context_bridge)
    assert bridge.delivery_owner_is_current(context_bridge, owner)
    replacement_holds_lock = Event()
    writer_waiting = Event()
    release_replacement = Event()
    write_json = bridge._write_json_file
    metadata_lock = framework_context.bridge_dir_preparation_lock

    def paused_replacement(path: Path, payload: dict[str, object]) -> None:
        if path.name == "bridge.json" and payload.get("active_session_id") == "session-b":
            replacement_holds_lock.set()
            assert release_replacement.wait(3)
        write_json(path, payload)

    @contextmanager
    def observed_writer_lock(directory: Path):
        writer_waiting.set()
        with metadata_lock(directory):
            yield

    monkeypatch.setattr(bridge, "_write_json_file", paused_replacement)
    monkeypatch.setattr(framework_context, "bridge_dir_preparation_lock", observed_writer_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replacement = pool.submit(bridge.write_active_session_id, context_bridge, "session-b")
        try:
            assert replacement_holds_lock.wait(3)
            writer = pool.submit(
                framework_context.write_prompt_framework_context,
                context_bridge,
                "prompt",
                ["old owner's notice"],
                expected_owner=owner,
            )
            assert writer_waiting.wait(3)
            assert not writer.done()
        finally:
            release_replacement.set()
        replacement.result(timeout=3)
        with pytest.raises(bridge.ClaudeMessageNotAccepted, match="input owner changed"):
            writer.result(timeout=3)
    assert bridge.read_active_session_id(context_bridge) == "session-b"
    assert not list(
        (context_bridge / framework_context.PROMPT_FRAMEWORK_CONTEXT_DIR).glob("*.json")
    )


@pytest.mark.parametrize("cold", [False, True], ids=["exact", "proven-cold-binding"])
def test_context_writer_accepts_only_current_or_proven_cold_owner(
    context_bridge: Path, cold: bool
) -> None:
    from dataclasses import replace

    current = bridge.read_delivery_owner(context_bridge)
    captured = replace(current, native_session_id=None) if cold else current
    handle = framework_context.write_prompt_framework_context(
        context_bridge, "prompt", ["notice"], expected_owner=captured
    )
    assert handle.owner == current


def test_context_writer_rejects_unproven_cold_binding(context_bridge: Path) -> None:
    from dataclasses import replace

    current = bridge.read_delivery_owner(context_bridge)
    (context_bridge / f"lifecycle-{current.launch_id}.json").unlink()
    with pytest.raises(bridge.ClaudeMessageNotAccepted, match="input owner changed"):
        framework_context.write_prompt_framework_context(
            context_bridge,
            "prompt",
            ["notice"],
            expected_owner=replace(current, native_session_id=None),
        )
