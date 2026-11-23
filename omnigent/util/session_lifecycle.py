"""Shared session lifecycle markers and display helpers.

Defined in :mod:`omnigent_client.session_lifecycle` so the client and the UI
SDK can use them without this package; re-exported here for server code.
"""

from omnigent_client.session_lifecycle import (  # noqa: F401 -- re-exported
    CLOSED_LABEL_KEY,
    CLOSED_LABEL_VALUE,
    CLOSED_TITLE_INFIX,
    has_closed_title_marker,
    is_session_closed,
    labels_with_closed_status,
    title_without_closed_marker,
)


def closed_title(title: str, session_id: str, max_chars: int) -> str:
    """
    Return the stored title that frees a sub-agent's ``(parent, title)`` slot.

    Appends the closed marker and the session's id, trimming the visible part
    so the result fits *max_chars*. A title that already carries the marker
    is returned unchanged.

    :param title: Visible sub-agent title, e.g. ``"researcher:auth"``.
    :param session_id: The closed session's id, e.g. ``"conv_abc123"``.
    :param max_chars: Longest stored title the server accepts, e.g. ``200``.
    :returns: e.g. ``"researcher:auth:closed:conv_abc123"``.
    """
    if has_closed_title_marker(title):
        return title
    suffix = f"{CLOSED_TITLE_INFIX}{session_id}"
    return title[: max(max_chars - len(suffix), 0)] + suffix
