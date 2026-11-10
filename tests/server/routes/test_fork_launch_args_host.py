"""A forked Claude Code session leaves its source machine's launch args behind."""

from __future__ import annotations

import pytest

from omnigent.entities import Conversation
from omnigent.server.routes._sessions.helpers import _drop_claude_launch_args_from_another_host
from omnigent.stores.conversation_store import (
    FORK_SOURCE_LABEL_KEY,
    SIDE_CHAT_LABEL_KEY,
    SIDE_CHAT_SOURCE_LABEL_KEY,
)
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


def _bind(store: SqlAlchemyConversationStore, session_id: str, host_id: str) -> Conversation:
    """Bind ``session_id`` to ``host_id`` and return the row the helper sees."""
    return store.set_host_id(session_id, host_id, "/workspace")


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

    _drop_claude_launch_args_from_another_host(store, _bind(store, fork_id, _OTHER_HOST))

    fork = store.get_conversation(fork_id)
    assert fork is not None
    assert fork.terminal_launch_args == expected


def test_fork_bound_to_its_source_host_keeps_every_arg(db_uri: str) -> None:
    """On the source's own machine the launcher's files are there, so nothing changes."""
    store = SqlAlchemyConversationStore(db_uri)
    stored = [*_OPENUI_ARGS, "--permission-mode", "plan"]
    fork_id = _fork(store, labels={**_CLAUDE_WRAPPER, _MODE_LABEL: "plan"}, launch_args=stored)

    _drop_claude_launch_args_from_another_host(store, _bind(store, fork_id, _SOURCE_HOST))

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

    _drop_claude_launch_args_from_another_host(store, _bind(store, other_fork_id, _OTHER_HOST))
    _drop_claude_launch_args_from_another_host(store, _bind(store, source.id, _OTHER_HOST))

    for session_id in (other_fork_id, source.id):
        conv = store.get_conversation(session_id)
        assert conv is not None
        assert conv.terminal_launch_args == _OPENUI_ARGS


@pytest.mark.parametrize(
    ("source_args", "own_args"),
    [
        (_OPENUI_ARGS, ["--mcp-config", "/home/me/servers.json", "--permission-mode", "plan"]),
        # Text after ``--`` is positional, so a selector-looking difference there counts.
        (
            ["--mcp-config", "/workspace/config.json", "--", "--permission-mode=plan"],
            ["--mcp-config", "/workspace/config.json", "--", "--permission-mode=auto"],
        ),
    ],
    ids=["new-config", "positional-text"],
)
def test_fork_with_its_own_args_keeps_them_on_another_host(
    db_uri: str, source_args: list[str], own_args: list[str]
) -> None:
    """Args that no longer match the source's are the fork's own, whatever host it binds."""
    store = SqlAlchemyConversationStore(db_uri)
    fork_id = _fork(store, labels=dict(_CLAUDE_WRAPPER), launch_args=list(source_args))
    store.update_conversation(fork_id, terminal_launch_args=own_args)

    _drop_claude_launch_args_from_another_host(store, _bind(store, fork_id, _OTHER_HOST))

    fork = store.get_conversation(fork_id)
    assert fork is not None
    assert fork.terminal_launch_args == own_args


def test_fork_of_a_side_chat_uses_the_host_the_side_chat_runs_on(db_uri: str) -> None:
    """A side chat shares its source's runner without its own host, so its source's host counts."""
    store = SqlAlchemyConversationStore(db_uri)
    source = store.create_conversation(
        host_id=_SOURCE_HOST,
        workspace="/Users/someone/repo",
        terminal_launch_args=list(_OPENUI_ARGS),
        labels=dict(_CLAUDE_WRAPPER),
    )
    # A live side chat runs on its source's runner without a host of its own.
    store.set_runner_id(source.id, "runner_token_shared")
    side_chat = store.fork_conversation(
        source_conversation_id=source.id,
        extra_labels={SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: source.id},
    )
    assert side_chat.host_id is None
    store.set_runner_id(side_chat.id, "runner_token_shared")
    fork = store.fork_conversation(source_conversation_id=side_chat.id)
    assert fork.labels[FORK_SOURCE_LABEL_KEY] == side_chat.id

    _drop_claude_launch_args_from_another_host(store, _bind(store, fork.id, _OTHER_HOST))

    bound = store.get_conversation(fork.id)
    assert bound is not None
    assert bound.terminal_launch_args == []
