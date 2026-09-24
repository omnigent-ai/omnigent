"""Mapping of native PostToolUse hook payloads to filesystem change records."""

from __future__ import annotations

from omnigent.runner.native_file_observer import NativeFileChange, native_file_changes


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
        NativeFileChange(path="/ws/report.md", operation="created", baseline=None)
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


def test_missing_input_path_falls_back_to_response_file_path() -> None:
    changes = native_file_changes(
        _payload(
            tool_input={"content": "hello\n"},
            tool_response={"type": "create", "filePath": "/ws/report.md"},
        )
    )
    assert changes == [NativeFileChange(path="/ws/report.md", operation="created", baseline=None)]


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
