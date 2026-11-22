"""Argument parsing helpers for built-in tools."""

from __future__ import annotations

import json
from typing import Any


def parse_json_object_arguments(
    arguments: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Parse raw tool arguments as a JSON object.

    Empty arguments are treated as ``{}``, so required-argument tools can
    return their normal missing-field errors instead of raising from
    ``json.loads``.
    """
    if not arguments.strip():
        return {}, None
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return None, "malformed JSON arguments"
    if not isinstance(parsed, dict):
        return None, "arguments must be a JSON object"
    return parsed, None


#: The session file tools' ``scope`` value for a project's shared files.
PROJECT_FILE_SCOPE = "project"

#: The ``scope`` parameter shared by ``sys_upload_file``, ``sys_list_files`` and
#: ``sys_download_file``.
FILE_SCOPE_PARAMETER: dict[str, Any] = {
    "type": "string",
    "enum": ["session", PROJECT_FILE_SCOPE],
    "description": (
        "'session' (the default) uses this session's own files. 'project' uses the "
        "files shared by every session in this session's project."
    ),
}


def parse_file_scope(arguments: dict[str, Any]) -> str | None:
    """
    Return the file tools' requested scope.

    :param arguments: Parsed tool arguments, e.g. ``{"path": "a.md", "scope": "project"}``.
    :returns: ``"session"`` (the default, also for an explicit ``null``) or ``"project"``;
        ``None`` for any other value.
    """
    scope = arguments.get("scope")
    if scope is None:
        return "session"
    return scope if scope in ("session", PROJECT_FILE_SCOPE) else None
