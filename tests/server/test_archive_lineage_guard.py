"""The agent-tool archive guard refuses a caller's ancestors and unknowable lineages."""

from types import SimpleNamespace

import pytest

from omnigent.errors import OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server.session_orchestration import (
    _MAX_ARCHIVE_LINEAGE_DEPTH,
    require_archive_outside_source_lineage,
)
from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY, SIDE_CHAT_SOURCE_LABEL_KEY

UNRELATED = "f" * 32


def _session_id(index: int) -> str:
    return f"{index:032x}"


class _Conversations:
    """A parent chain: session 0 is the caller, session N's parent is N + 1."""

    def __init__(self, length: int, *, cycle: bool = False) -> None:
        self.rows = {}
        for index in range(length):
            parent = index + 1 if index + 1 < length else (0 if cycle else None)
            self.rows[_session_id(index)] = SimpleNamespace(
                id=_session_id(index),
                parent_conversation_id=None if parent is None else _session_id(parent),
                labels={},
            )

    def get_conversation(self, session_id: str):
        return self.rows.get(session_id)


def _check(conversations, target: str, source: str | None = None) -> None:
    headers = {ORCHESTRATION_SOURCE_HEADER: source or _session_id(0)}
    require_archive_outside_source_lineage(SimpleNamespace(headers=headers), target, conversations)


def test_refuses_the_caller_and_its_deepest_checkable_ancestor():
    conversations = _Conversations(_MAX_ARCHIVE_LINEAGE_DEPTH)
    for target in (_session_id(0), _session_id(_MAX_ARCHIVE_LINEAGE_DEPTH - 1)):
        with pytest.raises(OmnigentError, match="a session it runs under"):
            _check(conversations, target)


def test_allows_an_unrelated_session_once_the_walk_reaches_the_root():
    _check(_Conversations(_MAX_ARCHIVE_LINEAGE_DEPTH), UNRELATED)


@pytest.mark.parametrize("target", [_session_id(_MAX_ARCHIVE_LINEAGE_DEPTH + 1), UNRELATED])
def test_refuses_when_the_lineage_is_deeper_than_the_walk(target):
    # The target, when an ancestor, sits one step past the last session the walk reads.
    with pytest.raises(OmnigentError, match="lineage cannot be checked"):
        _check(_Conversations(_MAX_ARCHIVE_LINEAGE_DEPTH + 2), target)


def test_refuses_a_cyclic_lineage():
    with pytest.raises(OmnigentError, match="lineage cannot be checked"):
        _check(_Conversations(3, cycle=True), UNRELATED)


def test_follows_a_side_chat_to_its_source():
    conversations = _Conversations(1)
    conversations.rows[_session_id(0)].labels = {
        SIDE_CHAT_LABEL_KEY: "1",
        SIDE_CHAT_SOURCE_LABEL_KEY: _session_id(9),
    }
    conversations.rows[_session_id(9)] = SimpleNamespace(
        id=_session_id(9), parent_conversation_id=None, labels={}
    )
    with pytest.raises(OmnigentError, match="a session it runs under"):
        _check(conversations, _session_id(9))


def test_ignores_requests_without_a_calling_session():
    require_archive_outside_source_lineage(
        SimpleNamespace(headers={}), _session_id(0), _Conversations(1)
    )


def test_follows_a_child_through_its_side_chat_to_the_source_parent():
    # caller (child) -> side chat -> its source -> the source's parent
    conversations = _Conversations(1)
    rows = conversations.rows
    side_chat, source, grandparent = _session_id(1), _session_id(2), _session_id(3)
    rows[_session_id(0)].parent_conversation_id = side_chat
    rows[side_chat] = SimpleNamespace(
        id=side_chat,
        parent_conversation_id=None,
        labels={SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: source},
    )
    rows[source] = SimpleNamespace(id=source, parent_conversation_id=grandparent, labels={})
    rows[grandparent] = SimpleNamespace(id=grandparent, parent_conversation_id=None, labels={})
    for target in (side_chat, source, grandparent):
        with pytest.raises(OmnigentError, match="a session it runs under"):
            _check(conversations, target)
    _check(conversations, UNRELATED)
