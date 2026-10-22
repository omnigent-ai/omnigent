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
from collections.abc import Mapping

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
    :param operation: ``"created"`` or ``"modified"``.
    :param baseline: Pre-modification file content when the payload carries
        it (Claude Code's ``tool_response.originalFile``), else ``None``.
    """

    path: str
    operation: str
    baseline: str | None


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
            operation = (
                "created"
                if kind_type == "add"
                else "deleted"
                if kind_type == "delete"
                else "modified"
            )
            observed.append(NativeFileChange(path=path, operation=operation, baseline=None))
        return observed
    path_key = _FILE_PATH_KEYS.get(tool_name)
    if path_key is None:
        return []
    response = payload.get("tool_response")
    response_map: Mapping[str, object] = response if isinstance(response, dict) else {}
    path = tool_input.get(path_key)
    if not isinstance(path, str) or not path:
        # Claude Code echoes the resolved path in the response.
        fallback = response_map.get("filePath")
        if not isinstance(fallback, str) or not fallback:
            return []
        path = fallback
    operation = "created" if response_map.get("type") == "create" else "modified"
    baseline = response_map.get("originalFile")
    return [
        NativeFileChange(
            path=path,
            operation=operation,
            baseline=baseline if isinstance(baseline, str) else None,
        )
    ]
