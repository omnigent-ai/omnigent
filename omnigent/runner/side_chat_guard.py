"""Keep a side chat of a project role (the Lead or an identity) from acting as that role."""

from __future__ import annotations

from typing import Any

# Coordination writes the role's main session owns; a role side chat may only read and comment.
ROLE_SIDE_CHAT_BLOCKED_TOOLS = frozenset(
    {
        "sys_session_create",
        "sys_session_send",
        "sys_session_close",
        "sys_session_update",
        "sys_session_share",
        "sys_session_link_pr",
        "sys_session_link_ticket",
        "sys_project_identity_session",
        "sys_project_identity_create",
        "sys_project_identity_update",
        "sys_project_update",
        "sys_project_mandate_propose",
        "sys_project_kpis_set",
        "sys_project_orchestrator_rollover",
        "sys_ticket_create",
        "sys_project_ticket_file",
        "sys_project_lead_message",
        "sys_ticket_import_pr",
        "sys_ticket_land",
        "sys_ticket_revert_draft",
        "sys_ticket_link",
        "sys_ticket_stages_update",
        "sys_scheduled_task_create",
        "sys_scheduled_task_update",
        "sys_scheduled_task_delete",
    }
)
# sys_ticket_update with only these fields is just a comment.
_TICKET_COMMENT_FIELDS = frozenset({"ticket_id", "comment", "audience"})
# Aliases sys_session_message resolves to the project's Lead.
_LEAD_ALIASES = frozenset({"lead", "orchestrator"})
_GUARDED_TOOLS = ROLE_SIDE_CHAT_BLOCKED_TOOLS | {"sys_ticket_update", "sys_session_message"}
# Role side chats this runner composed instructions for: session id -> (parent id, role name).
_ROLE_SIDE_CHATS: dict[str, tuple[str, str]] = {}
_ROLE_SIDE_CHATS_MAX = 4096


def record_session_snapshot(session_id: str, snapshot: dict[str, Any]) -> None:
    """Remember whether a session is a side chat of a project role, from its snapshot.

    The runner reads the snapshot to compose instructions before the session can
    call any tool, so the guard needs no extra server read.
    """
    parent_id = snapshot.get("side_chat_parent_id")
    role = snapshot.get("project_side_chat_role")
    if not isinstance(parent_id, str) or not parent_id or not isinstance(role, dict):
        _ROLE_SIDE_CHATS.pop(session_id, None)
        return
    if len(_ROLE_SIDE_CHATS) >= _ROLE_SIDE_CHATS_MAX and session_id not in _ROLE_SIDE_CHATS:
        _ROLE_SIDE_CHATS.pop(next(iter(_ROLE_SIDE_CHATS)))
    _ROLE_SIDE_CHATS[session_id] = (parent_id, str(role.get("name") or "role"))


def _same_session(left: str, right: str) -> bool:
    return left.replace("-", "").lower() == right.replace("-", "").lower()


def role_side_chat_refusal(
    tool_name: str, args: dict[str, Any], parent_id: str, role_name: str
) -> str | None:
    """Why a role side chat may not make this call, or ``None`` when it may.

    :param parent_id: The side chat's main session, the only peer it may message
        besides the Lead.
    :param role_name: The role the main session holds, e.g. ``"Lead"``.
    """
    refused = tool_name in ROLE_SIDE_CHAT_BLOCKED_TOOLS
    if tool_name == "sys_ticket_update":
        refused = not set(args) <= _TICKET_COMMENT_FIELDS
    elif tool_name == "sys_session_message":
        target = str(args.get("session_id") or "").strip()
        refused = not (target.lower() in _LEAD_ALIASES or _same_session(target, parent_id))
    if not refused:
        return None
    return (
        f"{tool_name}: refused in a side chat of the project's {role_name}. The main "
        f"session holds the role; send it what should happen with "
        f'sys_session_message(session_id="{parent_id}"), or comment on the ticket.'
    )


def guard_role_side_chat_tool(
    tool_name: str, args: dict[str, Any], conversation_id: str | None
) -> str | None:
    """Refuse a coordination write from a recorded side chat of a project role."""
    known = _ROLE_SIDE_CHATS.get(conversation_id) if conversation_id else None
    if known is None or tool_name not in _GUARDED_TOOLS:
        return None
    parent_id, role_name = known
    return role_side_chat_refusal(tool_name, args, parent_id, role_name)
