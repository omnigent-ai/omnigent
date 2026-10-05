"""Closed-session markers: the row's own ``:closed:<id>`` suffix is state, user text is not."""

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

    assert has_closed_title_marker(title, conversation_id=_CONV)
    assert is_session_closed(None, title, conversation_id=_CONV)
    assert title_without_closed_marker(title, conversation_id=_CONV) == "researcher:auth"
    assert labels_with_closed_status(None, title, conversation_id=_CONV) == {
        CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE
    }


def test_a_user_title_containing_the_words_is_not_closed() -> None:
    """A user title containing ``:closed:`` remains verbatim and open."""
    title = "release:closed:beta"

    assert not has_closed_title_marker(title, conversation_id=_CONV)
    assert not is_session_closed(None, title, conversation_id=_CONV)
    assert title_without_closed_marker(title, conversation_id=_CONV) == title
    assert labels_with_closed_status(None, title, conversation_id=_CONV) == {}


def test_another_rows_marker_does_not_close_this_row() -> None:
    """The suffix names the closed row itself, so a different id is not ours."""
    title = f"researcher:auth:closed:{'b' * 32}"

    assert not has_closed_title_marker(title, conversation_id=_CONV)
    assert title_without_closed_marker(title, conversation_id=_CONV) == title


def test_an_unknown_or_empty_id_never_matches_a_title() -> None:
    """Without the row's id a title is never read as state, even one ending in ``:closed:``."""
    title = "notes :closed:"

    for conversation_id in (None, ""):
        assert not has_closed_title_marker(title, conversation_id=conversation_id)
        assert not is_session_closed(None, title, conversation_id=conversation_id)
        assert title_without_closed_marker(title, conversation_id=conversation_id) == title
        assert labels_with_closed_status(None, title, conversation_id=conversation_id) == {}


def test_the_explicit_label_still_closes_without_a_title() -> None:
    """New closes persist the label; no title is consulted for them."""
    assert is_session_closed({CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE}, conversation_id=None)
    assert not is_session_closed({"other": "value"}, "any:closed:title", conversation_id=None)
