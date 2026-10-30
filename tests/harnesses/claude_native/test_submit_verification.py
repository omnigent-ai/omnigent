"""Prompt matching and append boundaries do not require a terminal."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native.bridge import _unwrap_pasted_content_markers
from omnigent.harnesses.claude_native.submit_verification import (
    capture_submit_boundary,
    has_matching_prompt_hook,
    read_fresh_submit_records,
)


@pytest.mark.parametrize(
    "hook_kind",
    [
        "fresh",
        "wrapped",
        "stale",
        "different-prompt",
        "same-prefix",
        "wrong-session",
        "wrong-transcript",
        "child",
        "sidechain",
        "wrong-event",
    ],
)
def test_prompt_hook_requires_a_fresh_exact_parent_match(tmp_path: Path, hook_kind: str) -> None:
    transcript = (tmp_path / "native.jsonl").resolve()
    content = "synthetic delayed prompt"
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "native-session",
        "transcript_path": str(transcript),
        "prompt": content,
    }
    changes = {
        "wrapped": {"prompt": f'<pasted_content id="abcd">\n{content}\n</pasted_content>'},
        "different-prompt": {"prompt": "another local input"},
        "same-prefix": {"prompt": content + " suffix"},
        "wrong-session": {"session_id": "other-native"},
        "wrong-transcript": {"transcript_path": str(tmp_path / "other.jsonl")},
        "child": {"agent_id": "native-child"},
        "sidechain": {"isSidechain": True},
        "wrong-event": {"hook_event_name": "Stop"},
    }
    payload.update(changes.get(hook_kind, {}))
    record = json.dumps({"payload": payload}) + "\n"
    hook_log = tmp_path / "hooks.jsonl"
    hook_log.write_text(record if hook_kind == "stale" else "")
    boundary = capture_submit_boundary(hook_log, "native-session")
    assert boundary is not None
    if hook_kind != "stale":
        with hook_log.open("a") as handle:
            handle.write(record)
    assert has_matching_prompt_hook(
        boundary, transcript, content, normalize_prompt=_unwrap_pasted_content_markers
    ) is (hook_kind in {"fresh", "wrapped"})


@pytest.mark.parametrize(
    "log_kind",
    [
        "fresh",
        "initially-absent",
        "partial-boundary",
        "partial-record",
        "malformed",
        "replaced-file",
        "rewritten-prefix",
        "truncated",
    ],
)
def test_fresh_records_require_complete_lines_and_an_unchanged_prefix(
    tmp_path: Path, log_kind: str
) -> None:
    hook_log = tmp_path / "hooks.jsonl"
    prefix = json.dumps({"prefix": "x" * 512}) + "\n"
    if log_kind != "initially-absent":
        hook_log.write_text(prefix)
    if log_kind == "partial-boundary":
        with hook_log.open("a") as handle:
            handle.write('{"unfinished":')
    boundary = capture_submit_boundary(hook_log, "native-session")
    if log_kind == "partial-boundary":
        assert boundary is None
        return
    assert boundary is not None
    if log_kind == "replaced-file":
        replacement = tmp_path / "replacement"
        replacement.write_text(prefix)
        replacement.replace(hook_log)
    elif log_kind == "rewritten-prefix":
        hook_log.write_text(prefix.replace("x", "y"))
    elif log_kind == "truncated":
        hook_log.write_text("")
    record = {"payload": {"hook_event_name": "UserPromptSubmit"}}
    serialized = "{broken}" if log_kind == "malformed" else json.dumps(record)
    with hook_log.open("a") as handle:
        handle.write(serialized + ("" if log_kind == "partial-record" else "\n"))
    expected = [record] if log_kind in {"fresh", "initially-absent"} else []
    assert read_fresh_submit_records(boundary) == expected
