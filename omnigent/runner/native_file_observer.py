"""Extract workspace file changes from native PostToolUse hook payloads.

Native harness CLIs (Claude Code and its derivatives) write files with their
own tools (``Write``/``Edit``/``MultiEdit``/``NotebookEdit``) instead of the
runner's ``sys_os_write``/``sys_os_edit``, so those writes never reach
:meth:`FilesystemRegistry.record_change` on their own.  The shared post-tool
observer hook already delivers every ``PostToolUse`` event to the runner's
tool relay; this module maps those payloads to registry change records so
native writes appear in ``GET .../changes`` for non-git workspaces.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping

_MAX_ORIGINAL_BYTES = 1_048_576
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)\n?$")

# Native file-mutating tools and the tool_input key naming the touched file.
# Shell-like tools (Bash) are deliberately absent: their side effects cannot
# be attributed to specific paths, matching the sys_os_shell exclusion in the
# runner's tool dispatch.
_FILE_PATH_KEYS: Mapping[str, str] = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}


@dataclasses.dataclass(frozen=True)
class NativeFileChange:
    """One workspace file change extracted from a native PostToolUse payload.

    :param path: File path as reported by the tool (absolute or
        workspace-relative; the registry normalizes it).
    :param operation: ``"created"``, ``"modified"``, or ``"deleted"``.
    :param baseline: Pre-modification file content when the payload carries
        it (Claude Code's ``tool_response.originalFile``), else ``None``.
    """

    path: str
    operation: str
    baseline: str | None
    patch: str | None = None
    move_path: str | None = None


def native_file_changes(payload: Mapping[str, object]) -> list[NativeFileChange]:
    """Map one native hook payload to the file changes it describes.

    Tolerates arbitrary payload shapes: anything that is not a successful
    ``PostToolUse`` for a known file-mutating tool yields an empty list.

    :param payload: Hook JSON object as delivered to ``/hook/observe-tool``,
        e.g. ``{"hook_event_name": "PostToolUse", "tool_name": "Write",
        "tool_input": {"file_path": "/ws/report.md", "content": "..."}}``.
    :returns: Extracted changes, possibly empty.
    """
    if payload.get("hook_event_name") != "PostToolUse":
        return []
    response = payload.get("tool_response")
    response_map: Mapping[str, object] = response if isinstance(response, dict) else {}
    for value in (payload, response_map):
        if (
            any(value.get(key) is True for key in ("is_error", "isError", "blocked"))
            or value.get("success") is False
            or value.get("error")
            or value.get("type") in ("error", "failed")
            or value.get("status") in ("failed", "error", "declined", "cancelled", "canceled")
        ):
            return []
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
        return []
    if tool_name == "apply_patch":
        changes = tool_input.get("changes")
        if not isinstance(changes, list):
            return []
        observed: list[NativeFileChange] = []
        for change in changes:
            if not isinstance(change, dict):
                continue
            path = change.get("path")
            if not isinstance(path, str) or not path:
                continue
            kind = change.get("kind")
            kind_type = kind.get("type") if isinstance(kind, dict) else kind
            if kind_type not in ("add", "update", "delete"):
                continue
            diff = change.get("diff")
            diff = diff if isinstance(diff, str) else None
            destination = (
                kind.get("move_path", kind.get("movePath"))
                if kind_type == "update" and isinstance(kind, dict)
                else None
            )
            destination = (
                destination
                if isinstance(destination, str) and destination and destination != path
                else None
            )
            # Codex Add File can overwrite an existing file; absence is not established.
            operation = "deleted" if kind_type == "delete" or destination else "modified"
            observed.append(
                NativeFileChange(
                    path,
                    operation,
                    diff if kind_type == "delete" else None,
                    diff if kind_type == "update" else None,
                    destination,
                )
            )
            if destination:
                observed.append(NativeFileChange(destination, "modified", None))
        return observed
    path_key = _FILE_PATH_KEYS.get(tool_name)
    if path_key is None:
        return []
    path = response_map.get("filePath")
    if not isinstance(path, str) or not path:
        fallback = tool_input.get(path_key)
        if not isinstance(fallback, str) or not fallback:
            return []
        path = fallback
    operation = "created" if response_map.get("type") == "create" else "modified"
    baseline = "" if operation == "created" else response_map.get("originalFile")
    return [
        NativeFileChange(
            path=path,
            operation=operation,
            baseline=baseline if isinstance(baseline, str) else None,
        )
    ]


def _bounded_original(value: str | None) -> str | None:
    try:
        return (
            value
            if value is not None
            and len(value) <= _MAX_ORIGINAL_BYTES
            and len(value.encode("utf-8")) <= _MAX_ORIGINAL_BYTES
            else None
        )
    except UnicodeError:
        return None


def resolve_native_original(change: NativeFileChange, current: str | None = None) -> str | None:
    """Recover an original from supplied content, keeping uncertain evidence unknown."""
    if change.baseline is not None:
        return _bounded_original(change.baseline)
    patch = _bounded_original(change.patch)
    current = _bounded_original(current)
    if patch is None or current is None:
        return None
    if change.move_path:
        patch = patch.removesuffix(f"\n\nMoved to: {change.move_path}")
    return _reverse_unified_diff(current, patch)


def _reverse_unified_diff(current: str, patch: str) -> str | None:
    """Recover only matching hunks that cover the whole file, including unchanged lines."""
    current_lines, patch_lines = current.splitlines(keepends=True), patch.splitlines(keepends=True)
    original: list[str] = []
    cursor = index = hunks = 0
    while index < len(patch_lines):
        header = _HUNK_HEADER.fullmatch(patch_lines[index])
        if header is None:
            if hunks or not patch_lines[index].startswith(
                ("--- ", "+++ ", "diff --git ", "index ")
            ):
                return None
            index += 1
            continue
        try:
            old_start, old_count, new_start, new_count = (
                int(value) if value is not None else 1 for value in header.groups()
            )
        except ValueError:
            return None
        position = new_start - 1 if new_count else new_start
        if position != cursor or len(original) != (old_start - 1 if old_count else old_start):
            return None
        before: list[str] = []
        after: list[str] = []
        previous_prefix = ""
        index += 1
        while index < len(patch_lines) and not patch_lines[index].startswith("@@ "):
            line = patch_lines[index]
            if line.rstrip("\r\n") == "\\ No newline at end of file":
                if not previous_prefix:
                    return None
                if previous_prefix in (" ", "-"):
                    before[-1] = before[-1].removesuffix("\n")
                if previous_prefix in (" ", "+"):
                    after[-1] = after[-1].removesuffix("\n")
                previous_prefix = ""
            elif line.startswith((" ", "-", "+")):
                previous_prefix = line[0]
                if previous_prefix != "+":
                    before.append(line[1:])
                if previous_prefix != "-":
                    after.append(line[1:])
            else:
                return None
            index += 1
        if len(before) != old_count or len(after) != new_count:
            return None
        if current_lines[position : position + new_count] != after:
            return None
        original.extend(before)
        cursor = position + new_count
        hunks += 1
    return _bounded_original("".join(original)) if hunks and cursor == len(current_lines) else None
