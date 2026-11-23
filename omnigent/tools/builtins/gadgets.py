"""Tools an agent uses to build and run gadgets, small web apps beside its sessions.

All are schema-only: the runner sends them to the server's ``/v1/gadgets``
routes as the session's owner (``omnigent/runner/gadget_tools.py``). They make
up the ``gadgets`` tool group, which an agent has when the server enables the
``gadgets`` feature, unless its spec sets ``tool_groups.gadgets: false``.

Their descriptions stay short because every turn carries them; how to work as a
gadget session, build a page and use the SDK is in the ``gadget`` framework skill
(``omnigent/resources/skills/gadget/SKILL.md``), offered with these tools.
"""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool

# The most ledger entries or records one list call returns.
GADGET_LIST_PAGE_MAX = 200
# What ``sys_gadget_records`` does, and the frame bridge method each one calls.
GADGET_RECORD_OPS = {
    "get": "data.v1.get",
    "list": "data.v1.list",
    "create": "data.v1.create",
    "replace": "data.v1.replace",
    "delete": "data.v1.delete",
}

_ID = {"type": "string", "description": "The gadget's ID."}
_STRING: dict[str, Any] = {"type": "string"}
_OPERATION_ID = {
    "type": "string",
    "maxLength": 128,
    "description": "Stable ID for this change, reused on retry; defaults to one per call.",
}
_SKILL = " Load the gadget skill first and follow it."


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


def _gadget_tool(
    tool_name: str, text: str, properties: dict[str, Any], required: list[str]
) -> type[Tool]:
    """A schema-only tool class; the runner dispatches it."""

    class _GadgetTool(Tool):
        @classmethod
        def name(cls) -> str:
            return tool_name

        @classmethod
        def description(cls) -> str:
            return text

        def get_schema(self) -> dict[str, Any]:
            return _schema(tool_name, text, properties, required)

    _GadgetTool.__name__ = _GadgetTool.__qualname__ = (
        "".join(part.capitalize() for part in tool_name.split("_")) + "Tool"
    )
    _GadgetTool.__doc__ = f"``{tool_name}``. Runner-dispatched to ``/v1/gadgets``."
    return _GadgetTool


SysGadgetDescribeTool = _gadget_tool(
    "sys_gadget_describe",
    "Read a gadget's guide, collection schemas, rules and declared actions (empty until it is "
    "published), without its HTML, and what to do next. Call it before working on a gadget.",
    {"gadget_id": _ID},
    ["gadget_id"],
)
SysGadgetCreateTool = _gadget_tool(
    "sys_gadget_create",
    "Create a private gadget, a small web app such as a board or a tracker, shown beside your "
    "session (session_id null: not shown)." + _SKILL,
    {
        "name": {"type": "string", "minLength": 1, "maxLength": 120},
        "session_id": {"type": ["string", "null"]},
    },
    ["name"],
)
SysGadgetListTool = _gadget_tool(
    "sys_gadget_list",
    "List a page of the gadgets your user owns.",
    {
        "limit": {"type": "integer", "minimum": 1, "maximum": GADGET_LIST_PAGE_MAX},
        "cursor": _STRING,
    },
    [],
)
SysGadgetGetTool = _gadget_tool(
    "sys_gadget_get",
    "Read a gadget's name, owner, published revision, version and your permissions.",
    {"gadget_id": _ID},
    ["gadget_id"],
)
SysGadgetRenameTool = _gadget_tool(
    "sys_gadget_rename",
    "Rename a gadget for everyone.",
    {"gadget_id": _ID, "name": {"type": "string", "minLength": 1, "maxLength": 120}},
    ["gadget_id", "name"],
)
SysGadgetActionListTool = _gadget_tool(
    "sys_gadget_action_list",
    "Read a page of a gadget's action ledger: each requested action's params, state, summary, "
    "version, and whether you requested it (`yours`).",
    {
        "gadget_id": _ID,
        "limit": {"type": "integer", "minimum": 1, "maximum": GADGET_LIST_PAGE_MAX},
        "cursor": _STRING,
    },
    ["gadget_id"],
)
SysGadgetActionUpdateTool = _gadget_tool(
    "sys_gadget_action_update",
    "Record an action's state, Markdown summary or working session; returns its new version."
    + _SKILL,
    {
        "gadget_id": _ID,
        "id": _STRING,
        "if_version": _STRING,
        "operation_id": _OPERATION_ID,
        "state": {"type": "string", "enum": ["queued", "running", "done", "failed", "cancelled"]},
        "summary": {"type": ["string", "null"], "maxLength": 4000},
        "session_id": {"type": ["string", "null"]},
    },
    ["gadget_id", "id", "if_version"],
)
SysGadgetCommentListTool = _gadget_tool(
    "sys_gadget_comment_list",
    "Read a page of the comments on parts of a gadget's page, each with the commented text "
    "(anchor) and status; follow `next_cursor` until null, pages can be short.",
    {
        "gadget_id": _ID,
        "status": {"type": "string", "enum": ["draft", "addressed"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": GADGET_LIST_PAGE_MAX},
        "cursor": _STRING,
    },
    ["gadget_id"],
)
SysGadgetCommentUpdateTool = _gadget_tool(
    "sys_gadget_comment_update",
    "Mark a gadget comment addressed, or draft again.",
    {
        "gadget_id": _ID,
        "id": _STRING,
        "status": {"type": "string", "enum": ["draft", "addressed"]},
    },
    ["gadget_id", "id", "status"],
)
SysGadgetRevisionGetTool = _gadget_tool(
    "sys_gadget_revision_get",
    "Read a revision's HTML and configuration; omit revision_id for the published one.",
    {"gadget_id": _ID, "revision_id": _STRING},
    ["gadget_id"],
)
SysGadgetRevisionCreateTool = _gadget_tool(
    "sys_gadget_revision_create",
    "Upload an immutable revision of a gadget: one self-contained HTML page using the gadget "
    "SDK, with its collections (JSON Schemas), rules, actions and a guide for agents. To change "
    "a revision, give base_revision_id and edits instead of html; the rest is carried over."
    + _SKILL,
    {
        "gadget_id": _ID,
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
    ["gadget_id"],
)
SysGadgetPublishTool = _gadget_tool(
    "sys_gadget_publish",
    "Publish an uploaded revision, keeping the records; if_version is the gadget's current "
    "version (null the first time)." + _SKILL,
    {"gadget_id": _ID, "revision_id": _STRING, "if_version": {"type": ["string", "null"]}},
    ["gadget_id", "revision_id", "if_version"],
)
SysGadgetRecordsTool = _gadget_tool(
    "sys_gadget_records",
    "Read or change a published gadget's records in one collection. op get (id): {record}, "
    "null if missing; list (limit, cursor): {items, next_cursor}; create (value, optional id); "
    "replace (id, value, if_version); delete (id, if_version)." + _SKILL,
    {
        "gadget_id": _ID,
        "op": {"type": "string", "enum": list(GADGET_RECORD_OPS)},
        "collection": _STRING,
        "id": _STRING,
        "value": {},
        "if_version": _STRING,
        "limit": {"type": "integer", "minimum": 1, "maximum": GADGET_LIST_PAGE_MAX},
        "cursor": _STRING,
        "operation_id": _OPERATION_ID,
    },
    ["gadget_id", "op", "collection"],
)

GADGET_TOOLS: tuple[type[Tool], ...] = (
    SysGadgetDescribeTool,
    SysGadgetCreateTool,
    SysGadgetListTool,
    SysGadgetGetTool,
    SysGadgetRenameTool,
    SysGadgetActionListTool,
    SysGadgetActionUpdateTool,
    SysGadgetCommentListTool,
    SysGadgetCommentUpdateTool,
    SysGadgetRevisionGetTool,
    SysGadgetRevisionCreateTool,
    SysGadgetPublishTool,
    SysGadgetRecordsTool,
)
GADGET_TOOL_NAMES: frozenset[str] = frozenset(tool.name() for tool in GADGET_TOOLS)
