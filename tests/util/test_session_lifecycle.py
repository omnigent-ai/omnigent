"""Closed-session markers: internal state must not be read out of user text.

``sys_session_close`` freed the ``(parent, title)`` unique slot by appending
``":closed:<child id>"`` to a child's title. The readers here decide whether a
row is closed, so they have to recognise that suffix without claiming any title
that merely contains the same words.
"""

from __future__ import annotations

from omnigent.util.session_lifecycle import (
    CLOSED_LABEL_KEY,
    CLOSED_LABEL_VALUE,
    has_closed_title_marker,
    is_session_closed,
    labels_with_closed_status,
    title_without_closed_marker,
)

_CONV = "405bfe154d5c0e795a2b87021bc897bf"


def test_legacy_marker_is_recognised_and_stripped() -> None:
    """A row closed the legacy way still reads as closed, and displays clean."""
    title = f"researcher:auth:closed:{_CONV}"

    assert has_closed_title_marker(title, _CONV)
    assert is_session_closed(None, title, _CONV)
    assert title_without_closed_marker(title, _CONV) == "researcher:auth"
    assert labels_with_closed_status(None, title, _CONV) == {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE}


def test_a_user_title_containing_the_words_is_not_closed() -> None:
    """A title the user chose is text, not state.

    Matching the bare ``":closed:"`` substring let a rename to
    ``"release:closed:beta"`` truncate the title, synthesize the closed label,
    and make the session reject every later message.
    """
    title = "release:closed:beta"

    assert not has_closed_title_marker(title, _CONV)
    assert not is_session_closed(None, title, _CONV)
    assert title_without_closed_marker(title, _CONV) == title
    assert labels_with_closed_status(None, title, _CONV) == {}


def test_another_rows_marker_does_not_close_this_row() -> None:
    """The suffix names the closed row itself, so a different id is not ours."""
    title = f"researcher:auth:closed:{'b' * 32}"

    assert not has_closed_title_marker(title, _CONV)
    assert title_without_closed_marker(title, _CONV) == title


def test_the_explicit_label_still_closes_without_a_title() -> None:
    """New closes persist the label; no title is consulted for them."""
    assert is_session_closed({CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE})
    assert not is_session_closed({"other": "value"}, "any:closed:title")
