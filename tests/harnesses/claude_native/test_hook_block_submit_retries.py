"""Native refusals stop extra Enters without claiming that a web input was accepted."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.harnesses.claude_native import bridge, delivery_diagnostics, submit_verification
from omnigent.native.input_diagnostics import input_delivery_scope
from omnigent.util.json_types import JsonObject
from tests.harnesses.claude_native.test_claude_native_bridge import _composer_pane
from tests.harnesses.claude_native.test_claude_native_bridge import (
    _trust_tmp_bridge_parent as _trust_tmp_bridge_parent,
)


def _blocked_prompt_entry(reason: str, prompt: str, *, native_session_id: str) -> JsonObject:
    return {
        "type": "system",
        "uuid": "blocked-native-record",
        "sessionId": native_session_id,
        "content": (
            f"UserPromptSubmit operation blocked by hook:\n{reason}\n\nOriginal prompt: {prompt}"
        ),
        "promptSubmitEnding": {
            "ending": {"kind": "blocked_by_hook", "reason": reason},
            "promptKept": True,
        },
    }


@pytest.mark.parametrize(
    "scenario,stops",
    [
        ("fresh", True),
        ("legacy", True),
        ("stale", False),
        ("different-prompt", False),
        ("same-prefix", False),
        ("partial", False),
        ("wrong-session", False),
        ("sidechain", False),
        ("other-ending", False),
        ("ambiguous-legacy", False),
        ("replaced-file", False),
        ("rewritten-prefix", False),
        ("owner-rotation", False),
    ],
)
def test_hook_refusal_stops_only_the_matching_fresh_submit_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    scenario: str,
    stops: bool,
) -> None:
    native_session_id = "11111111-2222-4333-8444-555555555555"
    content = "BLOCKME identical web or Terminal text"
    transcript = tmp_path / "native.jsonl"
    transcript.write_text('{"type":"system","content":"prefix"}\n')
    bridge.record_hook_event(
        tmp_path,
        {
            "hook_event_name": "SessionStart",
            "session_id": native_session_id,
            "transcript_path": str(transcript),
        },
    )
    block = _blocked_prompt_entry("Refused.", content, native_session_id=native_session_id)
    if scenario == "legacy":
        del block["promptSubmitEnding"]
    elif scenario == "different-prompt":
        block = _blocked_prompt_entry(
            "Refused.", "another input", native_session_id=native_session_id
        )
    elif scenario == "same-prefix":
        block = _blocked_prompt_entry(
            "Refused.", content + " suffix", native_session_id=native_session_id
        )
    elif scenario == "wrong-session":
        block["sessionId"] = "other-native-session"
    elif scenario == "sidechain":
        block["isSidechain"] = True
    elif scenario == "other-ending":
        block["promptSubmitEnding"] = {"ending": {"kind": "stopped_by_hook"}}
    elif scenario == "ambiguous-legacy":
        block = _blocked_prompt_entry(
            "Refused.\n\nOriginal prompt: quoted", content, native_session_id=native_session_id
        )
        del block["promptSubmitEnding"]
    serialized = json.dumps(block) + ("" if scenario == "partial" else "\n")
    if scenario == "stale":
        with transcript.open("a") as handle:
            handle.write(serialized)

    elapsed = 0.0
    pane = _composer_pane()
    enters = 0

    def sleep(seconds: float) -> None:
        nonlocal elapsed
        elapsed += seconds

    def run_tmux(_socket: str, *args: str) -> None:
        nonlocal pane, enters
        if args[0] == "paste-buffer":
            pane = _composer_pane(content)
        if args[-1] != "Enter":
            return
        enters += 1
        if scenario == "stale":
            return
        if enters == 1:
            if scenario == "replaced-file":
                replacement = tmp_path / "replacement"
                replacement.write_bytes(transcript.read_bytes())
                replacement.replace(transcript)
            elif scenario == "rewritten-prefix":
                transcript.write_bytes(transcript.read_bytes().replace(b"prefix", b"edited"))
            elif scenario == "owner-rotation":
                bridge.record_hook_event(
                    tmp_path,
                    {
                        "hook_event_name": "SessionStart",
                        "session_id": "rotated-session",
                        "transcript_path": str(transcript),
                    },
                )
        with transcript.open("a") as handle:
            handle.write(serialized)

    monkeypatch.setattr(
        bridge,
        "time",
        SimpleNamespace(monotonic=lambda: elapsed, sleep=sleep, time=lambda: elapsed),
    )
    monkeypatch.setattr(delivery_diagnostics, "time", bridge.time)
    monkeypatch.setattr(bridge, "_run_tmux", run_tmux)
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_args: pane)
    monkeypatch.setattr(bridge, "_wait_for_claude_prompt_ready", lambda *_args, **_kw: None)
    monkeypatch.setattr(bridge, "_restore_occupied_input", lambda *_args, **_kw: None)
    monkeypatch.setattr(
        bridge,
        "_wait_for_tmux_info",
        lambda *_args, **_kw: {"socket_path": "/unused/socket", "tmux_target": "main"},
    )
    monkeypatch.setattr(bridge, "_PASTE_SETTLE_S", 0)
    monkeypatch.setattr(bridge, "_SUBMIT_VERIFY_TIMEOUT_S", 20)
    with (
        caplog.at_level(logging.INFO, logger=delivery_diagnostics.__name__),
        input_delivery_scope({"input_stable_id": "a" * 32, "pending_id": "pending_" + "b" * 32}),
        pytest.raises(RuntimeError) as caught,
    ):
        bridge.inject_user_message(tmp_path, content=content)

    assert enters == (1 if stops else 5)
    if stops:
        assert type(caught.value).__name__ == "ClaudeSubmitBlockedUnconfirmed"
        assert "could not be confirmed" in str(caught.value)
    else:
        assert isinstance(caught.value, bridge.ClaudeMessageNotAccepted)
    records = [
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "claude_native_delivery_finished"
    ]
    assert len(records) == 1
    assert records[0].attributes["outcome"] == "error"
    assert records[0].attributes["delivery_outcome"] == "unknown"
    assert records[0].attributes["verification"] == (
        "hook_block_unowned" if stops else "draft_still_present"
    )
    assert content not in records[0].getMessage()


@pytest.mark.asyncio
async def test_unconfirmed_terminal_refusal_remains_an_executor_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from omnigent.inner.claude_native_executor import ClaudeNativeExecutor
    from omnigent.inner.executor import ExecutorError
    from omnigent.runtime import pending_inputs

    async def refused(*_args: object, **_kwargs: object) -> None:
        raise bridge.ClaudeSubmitBlockedUnconfirmed("Web delivery could not be confirmed.")

    pending_inputs.reset_for_tests()
    session_id = "unconfirmed-refusal"
    content = "BLOCKME identical web or Terminal text"
    try:
        pending_id = pending_inputs.record(
            session_id,
            [{"type": "input_text", "text": content}],
            stable_id="a" * 32,
        )
        executor = ClaudeNativeExecutor(bridge_dir=tmp_path)
        monkeypatch.setattr(executor, "_inject_prompt", refused)
        with caplog.at_level(logging.ERROR):
            events = [
                event
                async for event in executor.run_turn(
                    [{"role": "user", "content": content}], [], ""
                )
            ]
        assert len(events) == 1
        assert isinstance(events[0], ExecutorError)
        assert events[0].undelivered is False
        assert events[0].code is None
        assert any(record.levelno == logging.ERROR for record in caplog.records)
        assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(session_id)] == [
            pending_id
        ]
    finally:
        pending_inputs.reset_for_tests()


@pytest.mark.parametrize(
    "scenario,completed",
    [
        ("inline-system", True),
        ("inline-user", True),
        ("linked-stdout", True),
        ("command-run", True),
        ("compact-noop", True),
        ("echo-only", False),
        ("stdout-only", False),
        ("compact-noop-only", False),
        ("compact-other-command", False),
        ("compact-ambiguous", False),
        ("ambiguous-inline", False),
        ("ambiguous-command-run", False),
        ("different-arguments", False),
        ("same-argument-prefix", False),
        ("conflicting-run-metadata", False),
        ("wrong-session", False),
        ("sidechain", False),
        ("agent", False),
        ("subagent-path", False),
        ("missing-uuid", False),
        ("partial", False),
        ("stale", False),
        ("stale-echo", False),
        ("replaced-file", False),
        ("rewritten-prefix", False),
        ("quoted-markup", False),
        ("ordinary-input", False),
    ],
)
def test_local_command_completion_requires_fresh_full_parent_evidence(
    tmp_path: Path, scenario: str, completed: bool
) -> None:
    native_session_id = "native-parent"
    command, arguments = "model", "example-model"
    submitted = f"/{command} {arguments}"
    if scenario.startswith("compact"):
        command, arguments, submitted = "compact", "", "/compact"
    if scenario == "same-argument-prefix":
        arguments = "a" * 1000 + "owned"
        submitted = f"/model {arguments}"
        arguments = "a" * 1000 + "foreign"
    elif scenario == "different-arguments":
        arguments = "another-model"
    echo_content = (
        f"<command-name>/{command}</command-name><command-args>{arguments}</command-args>"
    )
    stdout = (
        "<local-command-stdout>Not enough messages to compact.</local-command-stdout>"
        if scenario.startswith("compact")
        else "<local-command-stdout>Local operation completed.</local-command-stdout>"
    )
    echo: JsonObject = {
        "type": "system",
        "subtype": "local_command",
        "sessionId": native_session_id,
        "uuid": "command-echo",
        "content": echo_content,
    }
    output: JsonObject = {
        "type": "system",
        "subtype": "local_command",
        "sessionId": native_session_id,
        "uuid": "command-output",
        "parentUuid": "command-echo",
        "content": stdout,
    }
    records = [echo, output]
    if scenario == "inline-system":
        echo["content"] = echo_content + stdout
        records = [echo]
    elif scenario == "inline-user":
        records = [
            {
                "type": "user",
                "sessionId": native_session_id,
                "uuid": "command-echo",
                "message": {"role": "user", "content": echo_content + stdout},
            }
        ]
    elif scenario == "command-run":
        output.pop("parentUuid")
        output["commandRun"] = {"command": command, "args": arguments}
        records = [output]
    elif scenario == "ambiguous-inline":
        echo["content"] = echo_content + stdout
        records = [echo, {**echo, "uuid": "another-command"}]
    elif scenario == "ambiguous-command-run":
        output.pop("parentUuid")
        output["commandRun"] = {"command": command, "args": arguments}
        records = [output, {**output, "uuid": "another-output"}]
    elif scenario == "echo-only":
        records = [echo]
    elif scenario in {"stdout-only", "compact-noop-only"}:
        output.pop("parentUuid")
        records = [output]
    elif scenario.startswith("compact"):
        output.pop("parentUuid")
        if scenario == "compact-other-command":
            other = {**echo, "uuid": "other", "content": "<command-name>/model</command-name>"}
            records = [echo, other, output]
        elif scenario == "compact-ambiguous":
            records = [echo, {**echo, "uuid": "another-echo"}, output]
    elif scenario == "conflicting-run-metadata":
        output["commandRun"] = {"command": command, "args": "another-model"}
    elif scenario == "quoted-markup":
        echo["content"] = "Quoted log:\n" + echo_content + stdout
        records = [echo]
    elif scenario == "ordinary-input":
        submitted = "ordinary input"
    for record in records:
        if scenario == "wrong-session":
            record["sessionId"] = "foreign-native"
        elif scenario == "sidechain":
            record["isSidechain"] = True
        elif scenario == "agent":
            record["agentId"] = "child"
        elif scenario == "subagent-path":
            record["transcriptPath"] = str(tmp_path / "subagents" / "child.jsonl")
        elif scenario == "missing-uuid":
            record.pop("uuid")

    transcript = tmp_path / "native.jsonl"
    transcript.write_text('{"type":"system","content":"prefix"}\n')
    serialized = "".join(json.dumps(record) + "\n" for record in records)
    if scenario == "stale":
        with transcript.open("a") as handle:
            handle.write(serialized)
    elif scenario == "stale-echo":
        with transcript.open("a") as handle:
            handle.write(json.dumps(echo) + "\n")
        serialized = json.dumps(output) + "\n"
    boundary = submit_verification.capture_submit_boundary(transcript, native_session_id)
    assert boundary is not None
    if scenario == "replaced-file":
        replacement = tmp_path / "replacement"
        replacement.write_bytes(transcript.read_bytes())
        replacement.replace(transcript)
    elif scenario == "rewritten-prefix":
        transcript.write_bytes(transcript.read_bytes().replace(b"prefix", b"edited"))
    if scenario != "stale":
        with transcript.open("a") as handle:
            handle.write(serialized.rstrip("\n") if scenario == "partial" else serialized)

    assert (
        submit_verification.has_matching_local_command_completion(boundary, submitted) is completed
    )


@pytest.mark.parametrize(
    "fault", [None, "version", "relative-path", "native", "offset", "prefix", "file-id"]
)
def test_submit_checkpoint_round_trip_and_validation(tmp_path: Path, fault: str | None) -> None:
    transcript = tmp_path / "native.jsonl"
    transcript.write_text('{"type":"system","content":"prefix"}\n')
    boundary = submit_verification.capture_submit_boundary(transcript, "native-parent")
    assert boundary is not None
    serialized = submit_verification.serialize_submit_boundary(boundary)
    payload = json.loads(json.dumps(serialized))
    if fault == "version":
        payload["schema_version"] = True
    elif fault == "relative-path":
        payload["path"] = "native.jsonl"
    elif fault == "native":
        payload["native_session_id"] = ""
    elif fault == "offset":
        payload["byte_offset"] = False
    elif fault == "prefix":
        payload["prefix_sha256"] = "broken"
    elif fault == "file-id":
        payload["file_id"] = [True, 1]
    parsed = submit_verification.parse_submit_boundary(payload)
    assert parsed == (boundary if fault is None else None)
