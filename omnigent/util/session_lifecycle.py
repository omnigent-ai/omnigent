"""Shared session lifecycle markers and display helpers."""

from __future__ import annotations

from collections.abc import Mapping

CLOSED_LABEL_KEY = "omnigent.closed"
CLOSED_LABEL_VALUE = "true"
CLOSED_TITLE_INFIX = ":closed:"


def closed_title_marker(conversation_id: str) -> str:
    """
    Return the legacy closed suffix for *conversation_id*.

    :param conversation_id: The closed session's own id, e.g. ``"conv_abc123"``.
    :returns: The suffix the close path appends, e.g. ``":closed:conv_abc123"``.
    """
    return f"{CLOSED_TITLE_INFIX}{conversation_id}"


def title_without_closed_marker(
    title: str | None, conversation_id: str | None = None
) -> str | None:
    """
    Remove the legacy internal closed marker from a stored title.

    ``sys_session_close`` historically freed the
    ``(parent_conversation_id, title)`` unique slot by rewriting a
    child title from ``"agent:task"`` to
    ``"agent:task:closed:conv_abc123"``. That suffix is persistence
    metadata, not user-facing text.

    :param title: Stored conversation title, e.g.
        ``"researcher:auth:closed:conv_abc123"``.
    :param conversation_id: The row's own id, which the marker ends with. A
        title is only treated as marked when it carries that exact suffix, so a
        user-chosen title containing ``":closed:"`` is left alone.
    :returns: Title without the closed suffix, e.g.
        ``"researcher:auth"``, or the original value when no marker
        is present.
    """
    if title is None or conversation_id is None:
        return title
    return title.removesuffix(closed_title_marker(conversation_id))


def has_closed_title_marker(title: str | None, conversation_id: str | None = None) -> bool:
    """
    Return whether a stored title carries the legacy closed marker.

    :param title: Stored conversation title, e.g.
        ``"researcher:auth:closed:conv_abc123"``.
    :param conversation_id: The row's own id, which the marker ends with.
    :returns: ``True`` when the title ends with this row's closed suffix.
    """
    if not title or conversation_id is None:
        return False
    return title.endswith(closed_title_marker(conversation_id))


def labels_with_closed_status(
    labels: Mapping[str, str] | None,
    title: str | None,
    conversation_id: str | None = None,
) -> dict[str, str]:
    """
    Return labels augmented with the derived closed-state marker.

    New closes persist ``omnigent.closed=true`` directly. Older
    rows only have the title suffix, so API responses synthesize the
    same label for clients and write guards.

    :param labels: Persisted session labels, e.g.
        ``{"omnigent.wrapper": "codex-native-ui"}``.
    :param title: Stored conversation title, e.g.
        ``"researcher:auth:closed:conv_abc123"``.
    :param conversation_id: The row's own id, which the marker ends with.
    :returns: A mutable labels dict with ``omnigent.closed=true``
        added when the title marker is present.
    """
    result = dict(labels or {})
    if has_closed_title_marker(title, conversation_id):
        result[CLOSED_LABEL_KEY] = CLOSED_LABEL_VALUE
    return result


def is_session_closed(
    labels: Mapping[str, str] | None,
    title: str | None = None,
    conversation_id: str | None = None,
) -> bool:
    """
    Return whether a session is closed to new user input.

    :param labels: Session labels, e.g.
        ``{"omnigent.closed": "true"}``.
    :param title: Optional stored title for legacy closed rows, e.g.
        ``"researcher:auth:closed:conv_abc123"``.
    :param conversation_id: The row's own id, which the legacy marker ends
        with. Without it only the label is consulted, so a title is never
        mistaken for internal state.
    :returns: ``True`` when the explicit label is set or the legacy
        title marker is present.
    """
    return (labels or {}).get(CLOSED_LABEL_KEY) == CLOSED_LABEL_VALUE or has_closed_title_marker(
        title, conversation_id
    )
