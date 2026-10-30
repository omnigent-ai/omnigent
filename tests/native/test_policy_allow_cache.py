"""Native policy hooks reuse a no-policy ALLOW only while the server vouches for it."""

from __future__ import annotations

import json
import math
import subprocess
import threading
import time
from pathlib import Path

import pytest
from filelock import FileLock

from omnigent.native import policy_allow_cache

_NO_POLICY_ALLOW = {"result": "POLICY_ACTION_ALLOW", "allow_cache_seconds": 60.0}


def _window_expiry(bridge_dir: Path) -> int:
    """Return the recorded expiry; ``0`` means the window is closed."""
    return policy_allow_cache._read_state(bridge_dir)[0]


def _answered_in_shell(bridge_dir: Path) -> bool:
    """Run the guard on a valid tool event and report whether it skipped the hook."""
    received = bridge_dir / "received"
    received.unlink(missing_ok=True)
    command = policy_allow_cache.guarded_hook_command(bridge_dir, f"cat > {received}")
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash"})
    subprocess.run(["/bin/sh", "-c", command], input=payload, text=True, check=True, timeout=30)
    return not received.exists()


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_NO_POLICY_ALLOW, 60.0),
        ({"result": "POLICY_ACTION_ALLOW", "allow_cache_seconds": 7}, 7.0),
        ({"result": "POLICY_ACTION_ALLOW", "allow_cache_seconds": 10_000}, 300.0),
        ({"result": "POLICY_ACTION_ALLOW"}, None),
        ({"result": "POLICY_ACTION_ALLOW", "allow_cache_seconds": 0}, None),
        ({"result": "POLICY_ACTION_ALLOW", "allow_cache_seconds": -1}, None),
        ({"result": "POLICY_ACTION_ALLOW", "allow_cache_seconds": True}, None),
        ({"result": "POLICY_ACTION_ALLOW", "allow_cache_seconds": "60"}, None),
        ({"result": "POLICY_ACTION_ALLOW", "allow_cache_seconds": math.inf}, None),
        ({"result": "POLICY_ACTION_ALLOW", "allow_cache_seconds": math.nan}, None),
        ({"result": "POLICY_ACTION_DENY", "allow_cache_seconds": 60.0}, None),
        ("not a dict", None),
    ],
    ids=[
        "window",
        "int_window",
        "capped",
        "no_window",
        "zero",
        "negative",
        "bool",
        "string",
        "infinite",
        "nan",
        "deny",
        "not_object",
    ],
)
def test_allow_cache_seconds_accepts_only_finite_positive_allow_windows(
    response: object, expected: float | None
) -> None:
    assert policy_allow_cache.allow_cache_seconds(response) == expected


def test_record_opens_window_counted_from_request_time(tmp_path: Path) -> None:
    generation = policy_allow_cache.generation(tmp_path)

    policy_allow_cache.record(tmp_path, generation, _NO_POLICY_ALLOW, requested_at=1000.9)

    assert _window_expiry(tmp_path) == 1060


@pytest.mark.parametrize(
    "response",
    [
        {"result": "POLICY_ACTION_ALLOW"},
        {"result": "POLICY_ACTION_DENY", "reason": "blocked"},
        {"result": "POLICY_ACTION_UNSPECIFIED"},
    ],
    ids=["engine_allow", "deny", "unspecified"],
)
def test_record_closes_window_when_server_no_longer_vouches(
    tmp_path: Path, response: dict[str, object]
) -> None:
    """Any answer without a window means policies apply now.

    A no-policy answer requested before that one and delivered after it must
    not reopen the window, whether or not a window was open when it closed.
    """
    for window_open_before in (True, False):
        if window_open_before:
            generation = policy_allow_cache.generation(tmp_path)
            policy_allow_cache.record(tmp_path, generation, _NO_POLICY_ALLOW, time.time())
            assert _window_expiry(tmp_path) > 0
        delayed_generation = policy_allow_cache.generation(tmp_path)

        policy_allow_cache.record(tmp_path, delayed_generation, response, time.time())
        assert _window_expiry(tmp_path) == 0
        policy_allow_cache.record(tmp_path, delayed_generation, _NO_POLICY_ALLOW, time.time())

        assert _window_expiry(tmp_path) == 0


def test_answer_requested_before_invalidate_cannot_reopen_window(tmp_path: Path) -> None:
    generation = policy_allow_cache.generation(tmp_path)

    policy_allow_cache.invalidate(tmp_path)
    policy_allow_cache.record(tmp_path, generation, _NO_POLICY_ALLOW, requested_at=time.time())

    assert _window_expiry(tmp_path) == 0


def test_policy_mutation_rejects_answers_from_before_and_during_it(tmp_path: Path) -> None:
    """A late no-policy ALLOW cannot outlive the policy the agent just added."""
    before = policy_allow_cache.generation(tmp_path)
    with policy_allow_cache.policy_mutation(tmp_path):
        during = policy_allow_cache.generation(tmp_path)
        policy_allow_cache.record(tmp_path, during, _NO_POLICY_ALLOW, requested_at=time.time())
        assert _window_expiry(tmp_path) == 0
    policy_allow_cache.record(tmp_path, before, _NO_POLICY_ALLOW, requested_at=time.time())
    policy_allow_cache.record(tmp_path, during, _NO_POLICY_ALLOW, requested_at=time.time())
    assert _window_expiry(tmp_path) == 0

    after = policy_allow_cache.generation(tmp_path)
    policy_allow_cache.record(tmp_path, after, _NO_POLICY_ALLOW, requested_at=time.time())
    assert _window_expiry(tmp_path) > 0


def test_cache_failures_do_not_raise(tmp_path: Path) -> None:
    """A vanished bridge directory leaves hooks asking the server."""
    missing = tmp_path / "gone"

    assert policy_allow_cache.generation(missing) is None
    policy_allow_cache.record(missing, None, _NO_POLICY_ALLOW, requested_at=time.time())
    policy_allow_cache.invalidate(missing)

    assert not missing.exists()


def test_failed_invalidation_cannot_be_undone_by_a_stale_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock timeout during invalidation revokes the window until a locked reset."""
    monkeypatch.setattr(policy_allow_cache, "_LOCK_TIMEOUT_S", 0.05)
    stale = policy_allow_cache.generation(tmp_path)
    policy_allow_cache.record(tmp_path, stale, _NO_POLICY_ALLOW, time.time())
    assert _answered_in_shell(tmp_path)

    with FileLock(str(tmp_path / "policy_allow_until.lock")):
        policy_allow_cache.invalidate(tmp_path)
    assert not _answered_in_shell(tmp_path)

    policy_allow_cache.record(tmp_path, stale, _NO_POLICY_ALLOW, time.time())
    assert not _answered_in_shell(tmp_path)

    fresh = policy_allow_cache.generation(tmp_path)
    assert fresh not in (None, stale)
    policy_allow_cache.record(tmp_path, stale, _NO_POLICY_ALLOW, time.time())
    assert not _answered_in_shell(tmp_path)
    policy_allow_cache.record(tmp_path, fresh, _NO_POLICY_ALLOW, time.time())
    assert _answered_in_shell(tmp_path)


def test_invalidation_that_times_out_holds_against_the_writer_holding_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer inside record() when invalidation times out cannot restore its window.

    Invalidation cannot replace the generation while the writer holds the
    lock, so the revocation it leaves must outlive that writer's write.
    """
    monkeypatch.setattr(policy_allow_cache, "_LOCK_TIMEOUT_S", 0.2)
    held = policy_allow_cache.generation(tmp_path)
    inside, release = threading.Event(), threading.Event()
    write_state = policy_allow_cache._write_state

    def write_after_release(*args: object) -> None:
        inside.set()
        release.wait(10)
        write_state(*args)

    monkeypatch.setattr(policy_allow_cache, "_write_state", write_after_release)
    writer = threading.Thread(
        target=policy_allow_cache.record, args=(tmp_path, held, _NO_POLICY_ALLOW, time.time())
    )
    writer.start()
    assert inside.wait(10)
    monkeypatch.setattr(policy_allow_cache, "_write_state", write_state)

    policy_allow_cache.invalidate(tmp_path)
    release.set()
    writer.join(10)

    assert _window_expiry(tmp_path) > 0, "the writer finished its write"
    assert not _answered_in_shell(tmp_path)
    policy_allow_cache.generation(tmp_path)
    assert _window_expiry(tmp_path) == 0


def test_unreadable_cache_file_matches_no_held_generation(tmp_path: Path) -> None:
    held = policy_allow_cache.generation(tmp_path)
    (tmp_path / "policy_allow_until").write_text("not a cache line\n")

    policy_allow_cache.record(tmp_path, held, _NO_POLICY_ALLOW, time.time())

    assert _window_expiry(tmp_path) == 0
    assert policy_allow_cache.generation(tmp_path) not in (None, held)


def _compact(payload: dict[str, object]) -> str:
    return json.dumps(payload, separators=(",", ":"))


# Claude Code 2.1.312 sends its effort setting as an object before the event keys.
_CLAUDE_TOOL_CALL = {
    "session_id": "2f722723-fef0-4a6e-b3e0-bee6f5edd71a",
    "transcript_path": "/home/u/.claude/projects/-tmp-w/2f722723.jsonl",
    "cwd": "/tmp/w",
    "prompt_id": "33d1d7ea-3a6b-4827-8bb0-c34c23c14a44",
    "permission_mode": "default",
    "effort": {"level": "medium"},
    "hook_event_name": "PreToolUse",
    "tool_name": "Read",
    "tool_input": {"file_path": "/tmp/w/x.txt"},
}
_CODEX_TOOL_CALL = {
    "session_id": "s",
    "turn_id": "t",
    "agent_id": None,
    "agent_type": None,
    "transcript_path": None,
    "cwd": "/tmp/w",
    "hook_event_name": "PreToolUse",
    "model": "gpt-5.5",
    "permission_mode": "default",
    "tool_name": "exec_command",
    "tool_input": {"cmd": "ls"},
    "tool_use_id": "u",
}


@pytest.mark.parametrize(
    ("payload", "window", "answered_in_shell"),
    [
        (_compact({"hook_event_name": "PreToolUse", "tool_name": "Bash"}), "open", True),
        (json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Read"}), "open", True),
        (_compact({"hook_event_name": "UserPromptSubmit", "prompt": "hi"}), "open", True),
        (_compact({"hook_event_name": "PreToolUse", "tool_name": ""}), "open", False),
        (_compact({"hook_event_name": "PostToolUse", "tool_input": {}}), "open", False),
        (
            _compact({"hook_event_name": "PreToolUse", "tool_input": {"tool_name": "Bash"}}),
            "open",
            False,
        ),
        (
            _compact({"tool_input": {"hook_event_name": "UserPromptSubmit"}}),
            "open",
            False,
        ),
        (_compact({"hook_event_name": "PermissionRequest", "tool_name": "Bash"}), "open", False),
        (_compact(_CLAUDE_TOOL_CALL), "open", True),
        (
            _compact(
                {**_CLAUDE_TOOL_CALL, "hook_event_name": "PostToolUse", "tool_response": {"ok": 1}}
            ),
            "open",
            True,
        ),
        (_compact(_CODEX_TOOL_CALL), "open", True),
        (
            _compact(
                {
                    "tags": ["a", {"b": [1]}],
                    "meta": {"c": {}},
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                }
            ),
            "open",
            True,
        ),
        (
            _compact(
                {
                    "effort": {"level": "medium"},
                    "tool_input": {"hook_event_name": "UserPromptSubmit"},
                }
            ),
            "open",
            False,
        ),
        (
            _compact(
                {
                    "effort": {"level": "medium"},
                    "hook_event_name": "PreToolUse",
                    "tool_input": {"tool_name": "Bash"},
                }
            ),
            "open",
            False,
        ),
        (
            _compact({"cwd": "/tmp/a]}b", "hook_event_name": "PreToolUse", "tool_name": "Bash"}),
            "open",
            True,
        ),
        (
            _compact({"tool_input": {"a": "}", "hook_event_name": "UserPromptSubmit"}}),
            "open",
            False,
        ),
        (
            _compact(
                {
                    "meta": {"a": "]}", "hook_event_name": "UserPromptSubmit"},
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                }
            ),
            "open",
            False,
        ),
        (
            _compact({"cwd": "C:\\w", "hook_event_name": "PreToolUse", "tool_name": "Bash"}),
            "open",
            False,
        ),
        (
            _compact({"cwd": 'a"b', "hook_event_name": "PreToolUse", "tool_name": "Bash"}),
            "open",
            False,
        ),
        (
            _compact(
                {
                    "hook_event_name": "PostToolUse",
                    "tool_name": "Read",
                    "tool_response": "x" * 2_000_000,
                }
            ),
            "open",
            True,
        ),
        (_compact({"tool_input": {"text": "x" * 2_000_000}}), "open", False),
        (_compact({"hook_event_name": "PreToolUse", "tool_name": "Bash"}), "expired", False),
        (_compact({"hook_event_name": "PreToolUse", "tool_name": "Bash"}), "unreadable", False),
        (_compact({"hook_event_name": "PreToolUse", "tool_name": "Bash"}), "absent", False),
        (_compact({"hook_event_name": "PreToolUse", "tool_name": "Bash"}), "revoked", False),
        (
            _compact({"hook_event_name": "PreToolUse", "tool_name": "Bash"}) + "\n\n",
            "absent",
            False,
        ),
    ],
    ids=[
        "tool_call",
        "spaced_tool_result",
        "prompt",
        "empty_tool_name",
        "missing_tool_name",
        "tool_name_nested_in_input",
        "event_name_nested_in_input",
        "other_event_with_tool_name",
        "claude_tool_call_after_effort",
        "claude_tool_result_after_effort",
        "codex_tool_call",
        "nested_metadata_before_keys",
        "event_name_nested_after_metadata",
        "tool_name_nested_after_metadata",
        "brackets_in_string_value",
        "nested_key_after_closer_in_string",
        "nested_key_in_metadata_after_closers_in_string",
        "escaped_backslash_defers",
        "escaped_quote_defers",
        "large_tool_result",
        "large_payload_without_keys",
        "expired",
        "unreadable",
        "absent",
        "revoked",
        "closed_window_keeps_stdin_exact",
    ],
)
def test_guarded_hook_command_answers_only_open_windows_and_valid_events(
    tmp_path: Path, payload: str, window: str, answered_in_shell: bool
) -> None:
    """Events the server would reject still reach the real hook, stdin intact.

    Only top-level keys count, and large payloads must stay fast: matching
    over the whole payload is quadratic in dash and would stall the hook.
    """
    cache_file = tmp_path / "policy_allow_until"
    if window in ("open", "revoked"):
        cache_file.write_text(f"{int(time.time()) + 60} 0 0\n")
    if window == "revoked":
        (tmp_path / "policy_allow_until.revoked").touch()
    elif window == "expired":
        cache_file.write_text(f"{int(time.time()) - 1} 0 0\n")
    elif window == "unreadable":
        cache_file.write_text("not a number\n")
    received = tmp_path / "received"
    command = policy_allow_cache.guarded_hook_command(tmp_path, f"cat > {received}")

    result = subprocess.run(
        ["/bin/sh", "-c", command], input=payload, capture_output=True, text=True, timeout=30
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    if answered_in_shell:
        assert not received.exists()
    else:
        assert received.read_text() == payload
