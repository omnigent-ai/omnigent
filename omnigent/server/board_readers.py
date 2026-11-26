"""Board readers: other projects whose Lead the owner lets read this project's board.

Off by default. Only the project owner changes the list, by hand: product approvers,
members and agents can't. A listed project's Lead may read tickets, comments and stage
setup; it never reaches chats, files, the Mandate or credentials.
"""

from __future__ import annotations

import re
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError

BOARD_READERS_CONFIG_KEY = "board_readers"
MAX_BOARD_READERS = 20
_PROJECT_ID = re.compile(r"^[0-9a-f]{32}$")


def board_readers(project: Any) -> list[str]:
    """The project IDs whose Lead may read *project*'s board, in the owner's order."""
    config = getattr(project, "config", None) or {}
    listed = config.get(BOARD_READERS_CONFIG_KEY)
    if not isinstance(listed, list):
        return []
    readers: list[str] = []
    for entry in listed:
        if isinstance(entry, str) and _PROJECT_ID.match(entry) and entry not in readers:
            readers.append(entry)
    return readers


def may_read_board(target: Any, source_project_id: str) -> bool:
    """Whether *target*'s owner has listed *source_project_id* as a board reader."""
    return source_project_id != target.id and source_project_id in board_readers(target)


def validate_board_readers(config: dict[str, Any] | None, project_id: str | None) -> None:
    """Refuse a malformed board-reader list before it's stored.

    :raises OmnigentError: ``INVALID_INPUT`` unless it's a list of at most
        :data:`MAX_BOARD_READERS` distinct project IDs other than the project itself.
    """
    if not config or BOARD_READERS_CONFIG_KEY not in config:
        return
    listed = config[BOARD_READERS_CONFIG_KEY]
    if not isinstance(listed, list) or not all(
        isinstance(entry, str) and _PROJECT_ID.match(entry) for entry in listed
    ):
        raise OmnigentError(
            f"`{BOARD_READERS_CONFIG_KEY}` must be a list of project IDs",
            code=ErrorCode.INVALID_INPUT,
        )
    if len(set(listed)) != len(listed):
        raise OmnigentError(
            f"`{BOARD_READERS_CONFIG_KEY}` lists a project twice", code=ErrorCode.INVALID_INPUT
        )
    if len(listed) > MAX_BOARD_READERS:
        raise OmnigentError(
            f"At most {MAX_BOARD_READERS} projects may read a board",
            code=ErrorCode.INVALID_INPUT,
        )
    if project_id is not None and project_id in listed:
        raise OmnigentError(
            "A project can't list itself as a board reader", code=ErrorCode.INVALID_INPUT
        )
