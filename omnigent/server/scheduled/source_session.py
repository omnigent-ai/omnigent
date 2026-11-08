"""Automations whose runs start from a copy of a session.

``POST /v1/scheduled-tasks`` with ``source_session_id`` copies the named session
and records the copy, under the session's own title, as the task's
``source_session_id``. Every run then starts a new session whose history is a
copy of that copy, and sends the task's instructions, or a fixed opener when it
has none.

Both copies rebuild the native harness transcript from the copied Omnigent
items. Neither keeps the fork directive that clones another session's on-disk
transcript (``FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY``): a run can launch days
after Create, on the original session's machine, and that directive would
resume the original's current transcript instead of the snapshot.
"""

from __future__ import annotations

import logging
from typing import Any

from omnigent.entities import Agent, Conversation, ScheduledTask
from omnigent.stores.conversation_store import (
    FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
    FORK_SOURCE_LABEL_KEY,
    SCHEDULED_TASK_ORIGIN_LABEL_KEY,
    SCHEDULED_TASK_SOURCE_LABEL_KEY,
)

_logger = logging.getLogger(__name__)

# Sent when an automation that starts from a session has no instructions of its own.
SOURCE_RUN_OPENER = "Perform the task from this conversation again."

# Never copied from a source session into its copy or a run.
_NEVER_COPIED_LABEL_KEYS = frozenset(
    {
        FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
        SCHEDULED_TASK_ORIGIN_LABEL_KEY,
        SCHEDULED_TASK_SOURCE_LABEL_KEY,
    }
)


class SourceHistoryUnsupportedError(ValueError):
    """The agent's harness cannot start a session from a copied history."""


def run_prompt_text(task: ScheduledTask) -> str:
    """The message a run sends, before the unattended-run note.

    :param task: The task being fired.
    :returns: The task's prompt. A task with a source session sends its
        instructions as typed, e.g. ``"Do it"``, or :data:`SOURCE_RUN_OPENER`
        when it has none.
    """
    if task.source_session_id is None:
        return task.prompt
    return task.prompt.strip() or SOURCE_RUN_OPENER


def source_copy_title(source_title: str | None, name: str) -> str:
    """Title of the copy an automation's runs start from.

    :param source_title: The copied session's title, e.g. ``"Rubric"``.
    :param name: The automation's name, used when the session has no title.
    :returns: The session's title, e.g. ``"Rubric"``, else the automation's name.
    """
    return source_title or name


def carries_native_history(agent: Agent) -> bool:
    """Whether a copy bound to *agent* needs the native carry-history marker.

    Blocking: loads the agent's bundle, so call it off the event loop.

    :param agent: The agent the copy runs.
    :returns: ``True`` for native harnesses that rebuild or replay a copied
        history, ``False`` for SDK agents, which replay the Omnigent
        transcript themselves.
    :raises SourceHistoryUnsupportedError: For a native harness that cannot
        carry a copied history at all.
    """
    from omnigent.server.routes._sessions.helpers import (
        _agent_carries_cursor_fork_history,
        _agent_carries_native_fork_history,
        _agent_is_native,
    )

    if _agent_carries_native_fork_history(agent) or _agent_carries_cursor_fork_history(agent):
        return True
    if _agent_is_native(agent):
        raise SourceHistoryUnsupportedError(
            "This agent can't start a session from a copy of another session's history"
        )
    return False


def last_finished_response_id(conversation_store: Any, conversation_id: str) -> str | None:
    """The response of the newest finished assistant reply in a session.

    A copy taken through it leaves out a turn that is still in progress.

    :param conversation_store: The conversation store.
    :param conversation_id: The session to read, e.g. ``"conv_abc123"``.
    :returns: A response id, e.g. ``"resp_abc123"``, or ``None`` when the
        session has no finished reply yet.
    """
    after: str | None = None
    for _ in range(50):
        page = conversation_store.list_items(
            conversation_id, limit=100, after=after, order="desc", type="message"
        )
        for item in page.data:
            if item.status == "completed" and getattr(item.data, "role", None) == "assistant":
                return item.response_id
        if not page.has_more or not page.data:
            return None
        after = page.last_id
    return None


def copy_session(
    *,
    conversation_store: Any,
    file_store: Any | None,
    artifact_store: Any | None,
    source_id: str,
    title: str,
    agent_id: str,
    created_by: str | None,
    project_id: str | None,
    carry_history_into_native: bool,
    labels: dict[str, str] | None = None,
    dropped_label_keys: frozenset[str] = frozenset(),
    up_to_response_id: str | None = None,
    copy_model_settings: bool = True,
    terminal_launch_args: list[str] | None = None,
    override_terminal_launch_args: bool = False,
) -> Conversation:
    """Copy a session's history, files and settings into a new top-level session.

    Blocking: call it off the event loop. The copy is unbound: callers bind a
    host and workspace, or leave it unbound (an automation's copy never runs).

    :param conversation_store: The conversation store.
    :param file_store: Store for the session's uploaded files, or ``None``.
    :param artifact_store: Artifact store the files' blobs live in, or ``None``.
        File rows are copied only when both stores are configured.
    :param source_id: The session to copy, e.g. ``"conv_abc123"``.
    :param title: Title of the copy.
    :param agent_id: Existing agent the copy runs.
    :param created_by: The owner the copy is created for.
    :param project_id: First-class project to file the copy into, or ``None``.
    :param carry_history_into_native: Mark the copy so a native harness
        rebuilds its transcript from the copied items.
    :param labels: Labels stamped on the copy after the drop rules.
    :param dropped_label_keys: Further source labels to leave off the copy.
    :param up_to_response_id: Copy only through this response, e.g.
        ``"resp_abc123"``. ``None`` copies everything.
    :param copy_model_settings: Keep the source's model and effort.
    :param terminal_launch_args: Launch args for the copy, used when
        *override_terminal_launch_args* is set.
    :param override_terminal_launch_args: Use *terminal_launch_args* instead
        of the source's launch args.
    :returns: The new session.
    """
    from omnigent.db.utils import generate_file_id

    file_id_map: dict[str, str] = {}
    source_files: list[Any] = []
    if file_store is not None and artifact_store is not None:
        files_after: str | None = None
        while True:
            files_page = file_store.list(source_id, limit=1000, after=files_after, order="asc")
            for stored_file in files_page.data:
                source_files.append(stored_file)
                file_id_map[stored_file.id] = generate_file_id()
            if not files_page.has_more or not files_page.data:
                break
            files_after = files_page.last_id

    conv = conversation_store.fork_conversation(
        source_id,
        title=title,
        agent_id=agent_id,
        copy_model_settings=copy_model_settings,
        override_terminal_launch_args=terminal_launch_args,
        override_terminal_launch_args_set=override_terminal_launch_args,
        dropped_label_keys=_NEVER_COPIED_LABEL_KEYS | dropped_label_keys,
        extra_labels=labels,
        carry_history_into_native=carry_history_into_native,
        resume_source_native_session=False,
        up_to_response_id=up_to_response_id,
        project_id=project_id,
        file_id_map=file_id_map,
        created_by=created_by,
    )
    # The copy's file rows point at the source's blobs, as a fork's do, so no
    # bytes move. Best-effort: a missing blob yields a row that 404s on read,
    # exactly as it does in the source.
    if file_store is not None and artifact_store is not None:
        for stored_file in source_files:
            try:
                file_store.create(
                    filename=stored_file.filename,
                    bytes=stored_file.bytes,
                    content_type=stored_file.content_type,
                    session_id=conv.id,
                    file_id=file_id_map[stored_file.id],
                    blob_key=stored_file.blob_key or stored_file.id,
                    source_metadata=stored_file.source_metadata,
                )
            except Exception:
                _logger.warning(
                    "could not copy file %s from session %s into %s",
                    stored_file.id,
                    source_id,
                    conv.id,
                    exc_info=True,
                )
    return conv


def run_copy_dropped_label_keys(
    source_labels: dict[str, str], *, agent_changed: bool
) -> frozenset[str]:
    """Labels of the automation's copy that a run leaves off.

    The fork source marks an unbound fork that needs a directory picker, and a
    run binds its own placement. The permission label belongs to the copy; a
    run takes the task's permission mode instead. (The store never copies the
    Codex bypass label.) The Chat/Terminal presentation is re-stamped for the
    run's agent, and MCP service picks belong to the copy's agent, so a run on
    another agent leaves them off, as an agent-switching fork does.

    :param source_labels: The copy's labels.
    :param agent_changed: Whether the run binds a different agent than the copy.
    :returns: The label keys to drop.
    """
    from omnigent._wrapper_labels import UI_MODE_LABEL_KEY, WRAPPER_LABEL_KEY
    from omnigent.mcp_selection import MCP_SELECTION_PREFIX
    from omnigent.server.routes._sessions.common import _CLAUDE_NATIVE_PERMISSION_MODE_LABEL_KEY

    dropped = {
        FORK_SOURCE_LABEL_KEY,
        _CLAUDE_NATIVE_PERMISSION_MODE_LABEL_KEY,
        UI_MODE_LABEL_KEY,
        WRAPPER_LABEL_KEY,
    }
    if agent_changed:
        dropped.update(key for key in source_labels if key.startswith(MCP_SELECTION_PREFIX))
    return frozenset(dropped)
