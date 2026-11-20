"""Mapping of native PostToolUse hook payloads to filesystem change records."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.runner.native_file_observer import (
    NativeFileChange,
    native_file_changes,
    resolve_native_original,
)


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "hook_event_name": "PostToolUse",
        "session_id": "provider-session",
        "tool_name": "Write",
        "tool_input": {"file_path": "/ws/report.md", "content": "hello\n"},
        "tool_response": {"type": "create", "filePath": "/ws/report.md"},
    }
    base.update(overrides)
    return base


def test_write_create_maps_to_created() -> None:
    assert native_file_changes(_payload()) == [
        NativeFileChange(path="/ws/report.md", operation="created", baseline="")
    ]


def test_write_update_maps_to_modified() -> None:
    changes = native_file_changes(
        _payload(tool_response={"type": "update", "filePath": "/ws/report.md"})
    )
    assert changes == [NativeFileChange(path="/ws/report.md", operation="modified", baseline=None)]


def test_write_without_response_defaults_to_modified() -> None:
    changes = native_file_changes(_payload(tool_response=None))
    assert changes == [NativeFileChange(path="/ws/report.md", operation="modified", baseline=None)]


def test_edit_carries_original_file_as_baseline() -> None:
    changes = native_file_changes(
        _payload(
            tool_name="Edit",
            tool_input={"file_path": "src/app.py", "old_string": "a", "new_string": "b"},
            tool_response={"filePath": "src/app.py", "originalFile": "a = 1\n"},
        )
    )
    assert changes == [
        NativeFileChange(path="src/app.py", operation="modified", baseline="a = 1\n")
    ]


def test_multi_edit_uses_file_path() -> None:
    changes = native_file_changes(
        _payload(
            tool_name="MultiEdit",
            tool_input={"file_path": "src/app.py", "edits": [{"old_string": "a"}]},
            tool_response={"originalFile": "a\n"},
        )
    )
    assert changes == [NativeFileChange(path="src/app.py", operation="modified", baseline="a\n")]


def test_notebook_edit_uses_notebook_path() -> None:
    changes = native_file_changes(
        _payload(
            tool_name="NotebookEdit",
            tool_input={"notebook_path": "analysis.ipynb", "new_source": "x"},
            tool_response={},
        )
    )
    assert changes == [
        NativeFileChange(path="analysis.ipynb", operation="modified", baseline=None)
    ]


def test_codex_apply_patch_maps_each_file_change() -> None:
    changes = native_file_changes(
        _payload(
            tool_name="apply_patch",
            tool_input={
                "changes": [
                    {"path": "/ws/new.py", "kind": {"type": "add"}},
                    {"path": "/ws/app.py", "kind": {"type": "update"}},
                    {"path": "/ws/old.py", "kind": {"type": "delete"}},
                ]
            },
            tool_response={},
        )
    )
    assert changes == [
        NativeFileChange(path="/ws/new.py", operation="modified", baseline=None),
        NativeFileChange(path="/ws/app.py", operation="modified", baseline=None),
        NativeFileChange(path="/ws/old.py", operation="deleted", baseline=None),
    ]


def test_missing_input_path_falls_back_to_response_file_path() -> None:
    changes = native_file_changes(
        _payload(
            tool_input={"content": "hello\n"},
            tool_response={"type": "create", "filePath": "/ws/report.md"},
        )
    )
    assert changes == [NativeFileChange(path="/ws/report.md", operation="created", baseline="")]


def test_non_file_tools_and_malformed_payloads_yield_nothing() -> None:
    assert native_file_changes(_payload(tool_name="Bash")) == []
    assert native_file_changes(_payload(hook_event_name="PreToolUse")) == []
    assert native_file_changes(_payload(tool_name=None)) == []
    assert native_file_changes(_payload(tool_input="not-a-dict")) == []
    assert native_file_changes(_payload(tool_input={}, tool_response={})) == []
    assert native_file_changes(_payload(tool_input={"file_path": ""}, tool_response={})) == []
    assert native_file_changes({}) == []


def test_non_string_baseline_is_dropped() -> None:
    changes = native_file_changes(
        _payload(
            tool_name="Edit",
            tool_input={"file_path": "src/app.py"},
            tool_response={"originalFile": {"not": "a string"}},
        )
    )
    assert changes == [NativeFileChange(path="src/app.py", operation="modified", baseline=None)]


@pytest.mark.parametrize(
    "response",
    [
        {"is_error": True},
        {"isError": True},
        {"error": "failed"},
        {"type": "error"},
        {"success": False},
        {"status": "declined"},
    ],
)
def test_failed_native_edits_are_not_observed(response: dict[str, object]) -> None:
    assert native_file_changes(_payload(tool_response=response)) == []


def test_claude_resolved_path_and_original_are_preserved(tmp_path: Path) -> None:
    [change] = native_file_changes(
        _payload(
            tool_input={"file_path": "relative.txt"},
            tool_response={
                "filePath": str(tmp_path / "resolved.txt"),
                "originalFile": "original\n",
            },
        )
    )
    assert change.path == str(tmp_path / "resolved.txt")
    assert resolve_native_original(change) == "original\n"
    assert resolve_native_original(NativeFileChange("new.txt", "created", "")) == ""


@pytest.mark.parametrize("move_key", ["move_path", "movePath"])
def test_codex_delete_and_move_originals(move_key: str) -> None:
    changes = native_file_changes(
        _payload(
            tool_name="apply_patch",
            tool_response={},
            tool_input={
                "changes": [
                    {
                        "path": "deleted.txt",
                        "kind": {"type": "delete"},
                        "diff": "deleted original\n",
                    },
                    {
                        "path": "old.txt",
                        "kind": {"type": "update", move_key: "new.txt"},
                        "diff": "@@ -1 +1 @@\n-old\n+new\n\n\nMoved to: new.txt",
                    },
                    {"path": "overwritten.txt", "kind": {"type": "add"}, "diff": "replacement\n"},
                ]
            },
        )
    )
    assert [(change.path, change.operation) for change in changes] == [
        ("deleted.txt", "deleted"),
        ("old.txt", "deleted"),
        ("new.txt", "modified"),
        ("overwritten.txt", "modified"),
    ]
    assert [resolve_native_original(change, "new\n") for change in changes] == [
        "deleted original\n",
        "old\n",
        None,
        None,
    ]


@pytest.mark.parametrize(
    ("current", "patch", "expected"),
    [
        ("new\n", "@@ -1 +1 @@\n-old\n+new\n", "old\n"),
        ("new\ntail\n", "@@ -1 +1 @@\n-old\n+new\n@@ -2 +2 @@\n tail\n", "old\ntail\n"),
        (
            "new",
            "@@ -1 +1 @@\n-old\n\\ No newline at end of file\n"
            "+new\n\\ No newline at end of file\n",
            "old",
        ),
        ("new\r\n", "@@ -1 +1 @@\n-old\r\n+new\r\n", "old\r\n"),
        ("new\ntail\n", "@@ -1 +1 @@\n-old\n+new\n", None),
        ("new\n", "@@ -1 +1 @@\n-old\n+different\n", None),
        ("new\n", "@@ -1,2 +1 @@\n-old\n+new\n", None),
        ("new\n", "not a patch", None),
    ],
)
def test_codex_original_requires_complete_matching_hunks(
    current: str, patch: str, expected: str | None
) -> None:
    change = NativeFileChange("file.txt", "modified", None, patch)
    assert resolve_native_original(change, current) == expected


@pytest.mark.parametrize("current", [None, "\udcff"])
def test_codex_original_requires_supplied_utf8_text(current: str | None) -> None:
    assert (
        resolve_native_original(
            NativeFileChange("file.txt", "modified", None, "@@ -1 +1 @@\n-old\n+new\n"), current
        )
        is None
    )


def test_native_original_recovery_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omnigent.runner.native_file_observer._MAX_ORIGINAL_BYTES", 64)
    for change in (
        NativeFileChange("file.txt", "modified", "x" * 65),
        NativeFileChange("file.txt", "modified", "é" * 33),
        NativeFileChange("file.txt", "modified", None, "x" * 65),
        NativeFileChange("file.txt", "modified", None, "@@ -1 +1 @@\n-old\n+new\n"),
    ):
        assert resolve_native_original(change, "x" * 65) is None
