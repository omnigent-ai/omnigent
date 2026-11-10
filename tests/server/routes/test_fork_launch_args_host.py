"""A forked Claude Code session leaves its source machine's launch args behind."""

from __future__ import annotations

import pytest

from omnigent.entities import Conversation
from omnigent.server.routes._sessions.helpers import _drop_claude_launch_args_from_another_host
from omnigent.stores.conversation_store import FORK_SOURCE_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_SOURCE_HOST = "1a2b3c4d5e6f40718293a4b5c6d7e8f9"
_OTHER_HOST = "9f8e7d6c5b4a40392817a6b5c4d3e2f1"
_CLAUDE_WRAPPER = {"omnigent.wrapper": "claude-code-native-ui"}
_MODE_LABEL = "omnigent.claude_native.permission_mode"
# What openui stores: files under the laptop's home directory.
_OPENUI_ARGS = [
    "--mcp-config",
    "/Users/someone/.openui/mcp-inject.json",
    "--plugin-dir",
    "/Users/someone/.openui/plugin",
]


def _row(store: SqlAlchemyConversationStore, session_id: str) -> Conversation:
    """The stored row for ``session_id``."""
    conv = store.get_conversation(session_id)
    assert conv is not None
    return conv


def _fork(
    store: SqlAlchemyConversationStore,
    *,
    labels: dict[str, str],
    launch_args: list[str],
) -> str:
    """Fork a laptop-hosted source session and return the fork's id."""
    source = store.create_conversation(
        host_id=_SOURCE_HOST,
        workspace="/Users/someone/repo",
        terminal_launch_args=launch_args,
        labels=labels,
    )
    fork = store.fork_conversation(source_conversation_id=source.id)
    assert fork.labels[FORK_SOURCE_LABEL_KEY] == source.id
    assert fork.terminal_launch_args == launch_args
    return fork.id


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("plan", ["--permission-mode", "plan"]),
        ("auto", ["--permission-mode", "auto"]),
        # Not selectable in Omnigent, so another host doesn't inherit it.
        ("bypassPermissions", []),
        (None, []),
    ],
)
def test_fork_bound_to_another_host_keeps_only_the_recorded_mode(
    db_uri: str, mode: str | None, expected: list[str]
) -> None:
    """The launcher's args stay behind; the mode comes from the label, not the args."""
    store = SqlAlchemyConversationStore(db_uri)
    labels = {**_CLAUDE_WRAPPER, **({_MODE_LABEL: mode} if mode else {})}
    # A selector-looking prompt value must never become a flag on the fork.
    stored = [*_OPENUI_ARGS, "--append-system-prompt", "--dangerously-skip-permissions"]
    fork_id = _fork(store, labels=labels, launch_args=stored)

    _drop_claude_launch_args_from_another_host(store, _row(store, fork_id), _OTHER_HOST)

    fork = store.get_conversation(fork_id)
    assert fork is not None
    assert fork.terminal_launch_args == expected


def test_fork_bound_to_its_source_host_keeps_every_arg(db_uri: str) -> None:
    """On the source's own machine the launcher's files are there, so nothing changes."""
    store = SqlAlchemyConversationStore(db_uri)
    stored = [*_OPENUI_ARGS, "--permission-mode", "plan"]
    fork_id = _fork(store, labels={**_CLAUDE_WRAPPER, _MODE_LABEL: "plan"}, launch_args=stored)

    _drop_claude_launch_args_from_another_host(store, _row(store, fork_id), _SOURCE_HOST)

    fork = store.get_conversation(fork_id)
    assert fork is not None
    assert fork.terminal_launch_args == stored


def test_other_harnesses_and_non_forks_keep_their_args(db_uri: str) -> None:
    """Only a Claude Code fork is changed; its source session moving hosts is not."""
    store = SqlAlchemyConversationStore(db_uri)
    other_fork_id = _fork(store, labels={}, launch_args=list(_OPENUI_ARGS))
    source = store.create_conversation(
        host_id=_SOURCE_HOST,
        workspace="/Users/someone/repo",
        terminal_launch_args=list(_OPENUI_ARGS),
        labels=dict(_CLAUDE_WRAPPER),
    )

    _drop_claude_launch_args_from_another_host(store, _row(store, other_fork_id), _OTHER_HOST)
    _drop_claude_launch_args_from_another_host(store, _row(store, source.id), _OTHER_HOST)

    for session_id in (other_fork_id, source.id):
        conv = store.get_conversation(session_id)
        assert conv is not None
        assert conv.terminal_launch_args == _OPENUI_ARGS


def test_fork_dialog_mode_pick_survives_a_bind_to_another_host(db_uri: str) -> None:
    """A mode picked in the fork dialog is the mode another host keeps.

    The dialog's pick replaces the copied args and the copied label, and the
    fork route records it on the label, which the cross-host bind rebuilds from.
    """
    store = SqlAlchemyConversationStore(db_uri)
    source = store.create_conversation(
        host_id=_SOURCE_HOST,
        workspace="/Users/someone/repo",
        terminal_launch_args=[*_OPENUI_ARGS, "--permission-mode", "default"],
        labels={**_CLAUDE_WRAPPER, _MODE_LABEL: "default"},
    )
    fork = store.fork_conversation(
        source_conversation_id=source.id,
        override_terminal_launch_args=["--permission-mode", "plan"],
        override_terminal_launch_args_set=True,
        dropped_label_keys=frozenset({_MODE_LABEL}),
        extra_labels={_MODE_LABEL: "plan"},
    )

    _drop_claude_launch_args_from_another_host(store, _row(store, fork.id), _OTHER_HOST)

    assert _row(store, fork.id).terminal_launch_args == ["--permission-mode", "plan"]
