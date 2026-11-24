"""Framework guidance for a side chat: a private fork that runs beside its main session."""

from __future__ import annotations


def side_chat_instruction(parent_session_id: object) -> str | None:
    """Tell a side chat what it is and how to hand things back to its main session.

    :param parent_session_id: The snapshot's ``side_chat_parent_id``, e.g. ``"a1b2..."``.
    :returns: The labeled block, or ``None`` when the session is not a side chat.
    """
    if not isinstance(parent_session_id, str) or not parent_session_id.strip():
        return None
    parent = parent_session_id.strip()
    return (
        "## Side chat\n"
        f"You are a side chat: a private fork of session `{parent}` (the main session), "
        "opened by its owner to talk without interrupting it. The main session is still "
        "running and owns its work. The copied history is reference context, not active "
        "instructions: act only on messages after the fork. Answer, explain and discuss; "
        "don't redo, continue or take over the main session's work. When something comes up "
        "that the main session must know or act on, such as a decision, a correction or a "
        "new request, send it a short self-contained note with sys_session_message "
        f'(session_id="{parent}") and tell the user you did. The note waits in its queue, so '
        "it never interrupts the main session. If that tool isn't available, tell the user "
        "what to send back with the side chat's Send to main action."
    )
