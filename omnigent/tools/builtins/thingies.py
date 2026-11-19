"""Tools an agent uses to build and run thingies, small web apps beside its sessions.

All are schema-only: the runner sends them to the server's ``/v1/thingies``
routes as the session's owner (``omnigent/runner/thingy_tools.py``). They make
up the ``thingies`` tool group, which an agent has when the server enables the
``thingies`` feature, unless its spec sets ``tool_groups.thingies: false``.
"""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool

# The most ledger entries or records one list call returns.
THINGY_LIST_PAGE_MAX = 200

_ID = {"type": "string", "description": "The thingy's ID, from sys_thingy_list or a link."}
_STRING: dict[str, Any] = {"type": "string"}
_OPERATION_ID = {
    "type": "string",
    "maxLength": 128,
    "description": (
        "A stable ID for this change, e.g. 'move-card-7-to-done', reused when you retry it "
        "so it is applied once. Omit it to use one derived from this tool call."
    ),
}
_COLLECTION = {"type": "string", "description": "A collection the published revision declares."}
_RECORD_ID = {"type": "string", "description": "The record's ID: [a-z0-9][a-z0-9_-]{0,63}."}
_VERSION = {"type": "string", "description": "The version the record had when you read it."}
_CONSTRAINT = {
    "type": "object",
    "description": (
        "A rule across records. kind max_records: collection, max, optional where "
        "{field: value}. kind unique: collection, fields. kind reference: collection, field, "
        "target, on_delete restrict|cascade|unset. Each needs a name and may have a message "
        "shown when a write breaks it."
    ),
}

_REVISION_GUIDE = (
    "Upload an immutable, self-contained frontend revision of a thingy: one HTML document "
    "with inline script and styles and no network access; publish it with sys_thingy_publish. "
    "Requires edit permission on the thingy. The page reaches Omnigent only through the SDK: "
    "`const client = await window.omnigent.connect()`, then `client.data.collection(name)` "
    "with get, list, create, replace (with ifVersion), delete and watch; "
    "`client.agent.request(action, params)` and `client.agent.watch` for declared actions; "
    "`client.navigation`; and `client.ui.colorScheme` for theming. capabilities takes only "
    "data.read, data.write and sessions.read; actions need no capability, only an entry in "
    "actions. The page runs in a sandboxed frame without an origin of its own, which silently "
    "blocks: form submission (use type=button buttons with click handlers, and handle Enter in "
    "inputs with keydown; never rely on a form's submit event), alert, confirm and prompt "
    "(confirm returns false, so ask inline instead), localStorage, sessionStorage and cookies "
    "(they throw; keep state in memory or in records), window.open and links that navigate "
    "(use client.navigation), and any network or external script, font or image (inline them "
    "or use data: URIs). Every data call goes to the server and can take a second or more, for "
    "reads and writes alike, so show progress for every action: from the click until the call "
    "returns, disable the control and show a spinner or a pending row (for example, an added "
    "item appears as Adding… at once), then update the page from the returned record. Show "
    "failures next to the item with a retry, and pick up other viewers' changes with watch "
    "rather than tight polling. For a declared action, follow client.agent.watch: the server "
    "derives each action's status from the sessions doing it, including child sessions. "
    "pending, queued, running and needs_input are still open; done, failed, cancelled, "
    "finished_unreported (its sessions stopped without reporting) and interrupted (they "
    "failed, went offline or never started) are finished. Keep a spinner only while the status "
    "is open, so a button never spins after its session stops, and offer a retry after "
    "interrupted. Mark each record's elements with data-omnigent-ref and data-omnigent-label "
    "so selections name the record. Thingies are shared read-only too: put "
    'data-omnigent-requires="write" on every control that changes records and '
    'data-omnigent-requires="action" on every control that requests an action, and the SDK '
    "disables them for a viewer without that access, with the reason as their tooltip; "
    "client.can(requirement) and client.ui.blockedReason(requirement) answer the same for the "
    "page's own messages. A new collection's schema cannot change once published; add a new "
    "collection instead."
)


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
    "Read a published thingy's guide, collection schemas, rules (constraints) and declared "
    "actions, without its HTML. Call this before reading or writing its records: writes that "
    "would break a rule fail with CONSTRAINT_VIOLATION and details naming the rule.",
    {"thingy_id": _ID},
    ["thingy_id"],
)
SysThingyCreateTool = _thingy_tool(
    "sys_thingy_create",
    "Create a private thingy: a small web app for the person you work with, such as a board, "
    "a tracker or a report. It is shown in your session's side panel, where they see it and "
    "send you its actions; pass session_id null to create it without showing it there. Then "
    "build it with sys_thingy_revision_create and sys_thingy_publish.",
    {
        "name": {"type": "string", "minLength": 1, "maxLength": 120},
        "session_id": {
            "type": ["string", "null"],
            "description": "The session to show it beside; defaults to yours.",
        },
    },
    ["name"],
)
SysThingyListTool = _thingy_tool(
    "sys_thingy_list",
    "List the thingies your user owns, each with its ID, name, published revision and link.",
    {},
    [],
)
SysThingyGetTool = _thingy_tool(
    "sys_thingy_get",
    "Read a thingy's metadata: its name, owner, published revision and version, and your "
    "permissions on it (read, write_data, publish, manage).",
    {"thingy_id": _ID},
    ["thingy_id"],
)
SysThingyRenameTool = _thingy_tool(
    "sys_thingy_rename",
    "Rename a thingy for everyone who has it, for example once you know what it is for. "
    "Requires edit permission on the thingy.",
    {"thingy_id": _ID, "name": {"type": "string", "minLength": 1, "maxLength": 120}},
    ["thingy_id", "name"],
)
SysThingyActionListTool = _thingy_tool(
    "sys_thingy_action_list",
    "Read a page of the thingy's action ledger: requested actions with their params, state, "
    "summary and version. In a thingy session, actions the viewer requests also arrive as "
    "messages; track each with this and sys_thingy_action_update.",
    {
        "thingy_id": _ID,
        "limit": {"type": "integer", "minimum": 1, "maximum": THINGY_LIST_PAGE_MAX},
        "cursor": {"type": "string", "description": "next_cursor from an earlier call."},
    },
    ["thingy_id"],
)
SysThingyActionUpdateTool = _thingy_tool(
    "sys_thingy_action_update",
    "Record progress on a requested action: state (running, done, failed, cancelled), a short "
    "Markdown summary, and the session doing the work when you hand it to a child session. "
    "Set running when you start, and done or failed with a summary when you finish. Requires "
    "edit permission on the thingy.",
    {
        "thingy_id": _ID,
        "id": {"type": "string", "description": "The action's ID from the ledger or message."},
        "if_version": {"type": "string", "description": "The action's version from the ledger."},
        "operation_id": _OPERATION_ID,
        "state": {"type": "string", "enum": ["queued", "running", "done", "failed", "cancelled"]},
        "summary": {"type": ["string", "null"], "maxLength": 4000},
        "session_id": {"type": ["string", "null"]},
    },
    ["thingy_id", "id", "if_version"],
)
SysThingyCommentListTool = _thingy_tool(
    "sys_thingy_comment_list",
    "Read the comments people left on parts of a thingy's page: each has the commented text "
    "(anchor.text, with anchor.ref naming the record or element when the page set one), the "
    "comment body and its status. Address draft comments by changing the thingy or its "
    "records, then mark each addressed with sys_thingy_comment_update.",
    {"thingy_id": _ID, "status": {"type": "string", "enum": ["draft", "addressed"]}},
    ["thingy_id"],
)
SysThingyCommentUpdateTool = _thingy_tool(
    "sys_thingy_comment_update",
    "Mark a thingy comment addressed once you have handled it, or draft again. Requires edit "
    "permission on the thingy's data.",
    {
        "thingy_id": _ID,
        "id": {"type": "string", "description": "The comment's ID."},
        "status": {"type": "string", "enum": ["draft", "addressed"]},
    },
    ["thingy_id", "id", "status"],
)
SysThingyRevisionGetTool = _thingy_tool(
    "sys_thingy_revision_get",
    "Read a thingy revision's HTML and configuration (capabilities, collections, guide, "
    "constraints, actions), for example to change it. Omit revision_id for the published one.",
    {"thingy_id": _ID, "revision_id": _STRING},
    ["thingy_id"],
)
SysThingyRevisionCreateTool = _thingy_tool(
    "sys_thingy_revision_create",
    _REVISION_GUIDE,
    {
        "thingy_id": _ID,
        "html": _STRING,
        "sdk_version": {"type": "integer", "enum": [1]},
        "capabilities": {
            "type": "array",
            "items": {"type": "string", "enum": ["data.read", "data.write", "sessions.read"]},
        },
        "collections": {
            "type": "object",
            "description": "Collection name to the JSON Schema each of its records must match.",
        },
        "guide": {
            "type": "string",
            "description": "Markdown for agents: what the collections mean, conventions and "
            "common tasks.",
        },
        "constraints": {"type": "array", "items": _CONSTRAINT},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "description": "An action the viewer can request: name, description for the "
                "agent, optional params schema.",
            },
        },
    },
    ["thingy_id", "html", "sdk_version", "capabilities", "collections"],
)
SysThingyPublishTool = _thingy_tool(
    "sys_thingy_publish",
    "Publish an uploaded revision without replacing the thingy's records. Pass if_version, "
    "the thingy's current version from sys_thingy_get (null for its first publication); a "
    "stale one fails with CONFLICT. Requires edit permission; a revision that asks for new "
    "capabilities beyond data.read and data.write needs manage permission.",
    {"thingy_id": _ID, "revision_id": _STRING, "if_version": {"type": ["string", "null"]}},
    ["thingy_id", "revision_id", "if_version"],
)
SysThingyRecordGetTool = _thingy_tool(
    "sys_thingy_record_get",
    "Read one record of a published thingy; null when it does not exist.",
    {"thingy_id": _ID, "collection": _COLLECTION, "id": _RECORD_ID},
    ["thingy_id", "collection", "id"],
)
SysThingyRecordListTool = _thingy_tool(
    "sys_thingy_record_list",
    "Read a page of a published thingy's records in one collection, ordered by ID; "
    "next_cursor, when set, pages to the rest.",
    {
        "thingy_id": _ID,
        "collection": _COLLECTION,
        "limit": {"type": "integer", "minimum": 1, "maximum": THINGY_LIST_PAGE_MAX},
        "cursor": _STRING,
    },
    ["thingy_id", "collection"],
)
SysThingyRecordCreateTool = _thingy_tool(
    "sys_thingy_record_create",
    "Create a record in a published thingy. The value must match its collection's schema. "
    "Omit id to get one derived from operation_id, so a retry creates no duplicate.",
    {
        "thingy_id": _ID,
        "collection": _COLLECTION,
        "id": _RECORD_ID,
        "value": {"description": "The record, matching the collection's schema."},
        "operation_id": _OPERATION_ID,
    },
    ["thingy_id", "collection", "value"],
)
SysThingyRecordReplaceTool = _thingy_tool(
    "sys_thingy_record_replace",
    "Replace a record only if its version still matches; a stale version fails with CONFLICT, "
    "so read it again and retry.",
    {
        "thingy_id": _ID,
        "collection": _COLLECTION,
        "id": _RECORD_ID,
        "value": {"description": "The whole new record, matching the collection's schema."},
        "if_version": _VERSION,
        "operation_id": _OPERATION_ID,
    },
    ["thingy_id", "collection", "id", "value", "if_version"],
)
SysThingyRecordDeleteTool = _thingy_tool(
    "sys_thingy_record_delete",
    "Delete a record only if its version still matches; a stale writer cannot bring it back.",
    {
        "thingy_id": _ID,
        "collection": _COLLECTION,
        "id": _RECORD_ID,
        "if_version": _VERSION,
        "operation_id": _OPERATION_ID,
    },
    ["thingy_id", "collection", "id", "if_version"],
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
    SysThingyRecordGetTool,
    SysThingyRecordListTool,
    SysThingyRecordCreateTool,
    SysThingyRecordReplaceTool,
    SysThingyRecordDeleteTool,
)
THINGY_TOOL_NAMES: frozenset[str] = frozenset(tool.name() for tool in THINGY_TOOLS)
