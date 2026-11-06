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
