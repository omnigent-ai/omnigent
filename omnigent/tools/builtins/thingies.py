"""Tools an agent uses to build and run thingies, small web apps beside its sessions.

All are schema-only: the runner sends them to the server's ``/v1/thingies``
routes as the session's owner (``omnigent/runner/thingy_tools.py``). They make
up the ``thingies`` tool group, which an agent has when the server enables the
``thingies`` feature, unless its spec sets ``tool_groups.thingies: false``.

Their descriptions stay short because every turn carries them; how to work as a
thingy session, build a page and use the SDK is in the ``thingy`` framework skill
(``omnigent/resources/skills/thingy/SKILL.md``), offered with these tools.
"""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool

# The most ledger entries or records one list call returns.
THINGY_LIST_PAGE_MAX = 200
# What ``sys_thingy_records`` does, and the frame bridge method each one calls.
THINGY_RECORD_OPS = {
    "get": "data.v1.get",
    "list": "data.v1.list",
    "create": "data.v1.create",
    "replace": "data.v1.replace",
    "delete": "data.v1.delete",
}

_ID = {"type": "string", "description": "The thingy's ID."}
_STRING: dict[str, Any] = {"type": "string"}
_OPERATION_ID = {
    "type": "string",
    "maxLength": 128,
    "description": "Stable ID for this change, reused on retry; defaults to one per call.",
}
_SKILL = " Load the thingy skill first and follow it."


def _schema(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def _thingy_tool(
    tool_name: str, text: str, properties: dict[str, Any], required: list[str]
) -> type[Tool]:
    """A schema-only tool class; the runner dispatches it."""

    class _ThingyTool(Tool):
        @classmethod
        def name(cls) -> str:
            return tool_name

        @classmethod
        def description(cls) -> str:
            return text

        def get_schema(self) -> dict[str, Any]:
            return _schema(tool_name, text, properties, required)

    _ThingyTool.__name__ = _ThingyTool.__qualname__ = (
        "".join(part.capitalize() for part in tool_name.split("_")) + "Tool"
    )
    _ThingyTool.__doc__ = f"``{tool_name}``. Runner-dispatched to ``/v1/thingies``."
    return _ThingyTool


SysThingyDescribeTool = _thingy_tool(
    "sys_thingy_describe",
    "Read a thingy's guide, collection schemas, rules and declared actions (empty until it is "
    "published), without its HTML, and what to do next. Call it before working on a thingy.",
    {"thingy_id": _ID},
    ["thingy_id"],
)
SysThingyCreateTool = _thingy_tool(
    "sys_thingy_create",
    "Create a private thingy, a small web app such as a board or a tracker, shown beside your "
    "session (session_id null: not shown)." + _SKILL,
    {
        "name": {"type": "string", "minLength": 1, "maxLength": 120},
        "session_id": {"type": ["string", "null"]},
    },
    ["name"],
)
SysThingyListTool = _thingy_tool(
    "sys_thingy_list",
    "List the thingies your user owns.",
    {},
    [],
)
SysThingyGetTool = _thingy_tool(
    "sys_thingy_get",
    "Read a thingy's name, owner, published revision, version and your permissions.",
    {"thingy_id": _ID},
    ["thingy_id"],
)
SysThingyRenameTool = _thingy_tool(
    "sys_thingy_rename",
    "Rename a thingy for everyone.",
    {"thingy_id": _ID, "name": {"type": "string", "minLength": 1, "maxLength": 120}},
    ["thingy_id", "name"],
)
SysThingyActionListTool = _thingy_tool(
    "sys_thingy_action_list",
    "Read a page of a thingy's action ledger: each requested action's params, state, summary "
    "and version.",
    {
        "thingy_id": _ID,
        "limit": {"type": "integer", "minimum": 1, "maximum": THINGY_LIST_PAGE_MAX},
        "cursor": _STRING,
    },
    ["thingy_id"],
)
SysThingyActionUpdateTool = _thingy_tool(
    "sys_thingy_action_update",
    "Record an action's state, Markdown summary or working session; returns its new version."
    + _SKILL,
    {
        "thingy_id": _ID,
        "id": _STRING,
        "if_version": _STRING,
        "operation_id": _OPERATION_ID,
        "state": {"type": "string", "enum": ["queued", "running", "done", "failed", "cancelled"]},
        "summary": {"type": ["string", "null"], "maxLength": 4000},
        "session_id": {"type": ["string", "null"]},
    },
    ["thingy_id", "id", "if_version"],
)
SysThingyCommentListTool = _thingy_tool(
    "sys_thingy_comment_list",
    "Read the comments people left on parts of a thingy's page, each with the commented text "
    "(anchor) and status.",
    {"thingy_id": _ID, "status": {"type": "string", "enum": ["draft", "addressed"]}},
    ["thingy_id"],
)
SysThingyCommentUpdateTool = _thingy_tool(
    "sys_thingy_comment_update",
    "Mark a thingy comment addressed, or draft again.",
    {
        "thingy_id": _ID,
        "id": _STRING,
        "status": {"type": "string", "enum": ["draft", "addressed"]},
    },
    ["thingy_id", "id", "status"],
)
SysThingyRevisionGetTool = _thingy_tool(
    "sys_thingy_revision_get",
    "Read a revision's HTML and configuration; omit revision_id for the published one.",
    {"thingy_id": _ID, "revision_id": _STRING},
    ["thingy_id"],
)
SysThingyRevisionCreateTool = _thingy_tool(
    "sys_thingy_revision_create",
    "Upload an immutable revision of a thingy: one self-contained HTML page using the thingy "
    "SDK, with its collections (JSON Schemas), rules, actions and a guide for agents. To change "
    "a revision, give base_revision_id and edits instead of html; the rest is carried over."
    + _SKILL,
    {
        "thingy_id": _ID,
        "base_revision_id": _STRING,
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "old": _STRING,
                    "start": _STRING,
                    "end": _STRING,
                    "new": _STRING,
                },
                "required": ["new"],
            },
            "description": "Exact replacements, in order: old (must appear once) with new, or "
            "the text between the start and end markers (each once) with new.",
        },
        "html": _STRING,
        "sdk_version": {"type": "integer", "enum": [1]},
        "capabilities": {
            "type": "array",
            "items": {"type": "string", "enum": ["data.read", "data.write", "sessions.read"]},
        },
        "collections": {"type": "object"},
        "guide": _STRING,
        "constraints": {"type": "array", "items": {"type": "object"}},
        "actions": {"type": "array", "items": {"type": "object"}},
    },
    ["thingy_id"],
)
SysThingyPublishTool = _thingy_tool(
    "sys_thingy_publish",
    "Publish an uploaded revision, keeping the records; if_version is the thingy's current "
    "version (null the first time)." + _SKILL,
    {"thingy_id": _ID, "revision_id": _STRING, "if_version": {"type": ["string", "null"]}},
    ["thingy_id", "revision_id", "if_version"],
)
SysThingyRecordsTool = _thingy_tool(
    "sys_thingy_records",
    "Read or change a published thingy's records in one collection. op get (id): {record}, "
    "null if missing; list (limit, cursor): {items, next_cursor}; create (value, optional id); "
    "replace (id, value, if_version); delete (id, if_version)." + _SKILL,
    {
        "thingy_id": _ID,
        "op": {"type": "string", "enum": list(THINGY_RECORD_OPS)},
        "collection": _STRING,
        "id": _STRING,
        "value": {},
        "if_version": _STRING,
        "limit": {"type": "integer", "minimum": 1, "maximum": THINGY_LIST_PAGE_MAX},
        "cursor": _STRING,
        "operation_id": _OPERATION_ID,
    },
    ["thingy_id", "op", "collection"],
)

THINGY_TOOLS: tuple[type[Tool], ...] = (
    SysThingyDescribeTool,
    SysThingyCreateTool,
    SysThingyListTool,
    SysThingyGetTool,
    SysThingyRenameTool,
    SysThingyActionListTool,
    SysThingyActionUpdateTool,
    SysThingyCommentListTool,
    SysThingyCommentUpdateTool,
    SysThingyRevisionGetTool,
    SysThingyRevisionCreateTool,
    SysThingyPublishTool,
    SysThingyRecordsTool,
)
THINGY_TOOL_NAMES: frozenset[str] = frozenset(tool.name() for tool in THINGY_TOOLS)
