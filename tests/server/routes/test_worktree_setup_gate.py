"""Tests for the first-turn worktree-setup gate.

``_await_worktree_setup`` is what keeps an agent from starting in a worktree
whose dependency install is still running: ``POST /v1/sessions/{id}/events``
awaits it before resolving a runner. These pin both directions — it blocks while
setup runs, and it fails OPEN rather than wedging a session forever when the
recorded state is stale (a server that restarted mid-hook).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from omnigent.server.routes._sessions.common import (
    _WORKTREE_SETUP_DONE,
    _WORKTREE_SETUP_LABEL_KEY,
    _WORKTREE_SETUP_RUNNING,
)
from omnigent.server.routes._sessions.helpers import (
    _WORKTREE_SETUP_DEADLINE_LABEL_KEY,
    _await_worktree_setup,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

pytestmark = pytest.mark.asyncio


async def test_gate_returns_immediately_without_a_setup_label(db_uri: str) -> None:
    """A session with no setup command is not delayed at all."""
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation()
    await asyncio.wait_for(_await_worktree_setup(conv.id, store), timeout=1.0)


async def test_gate_blocks_while_setup_runs_then_releases(db_uri: str) -> None:
    """The gate holds on ``running`` and returns as soon as it settles."""
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation()
    store.set_labels(
        conv.id,
        {
            _WORKTREE_SETUP_LABEL_KEY: _WORKTREE_SETUP_RUNNING,
            _WORKTREE_SETUP_DEADLINE_LABEL_KEY: f"{time.time() + 300:.0f}",
        },
    )

    gate = asyncio.create_task(_await_worktree_setup(conv.id, store))
    await asyncio.sleep(0.2)
    assert not gate.done(), "the gate let the turn through while setup was running"

    store.set_labels(conv.id, {_WORKTREE_SETUP_LABEL_KEY: _WORKTREE_SETUP_DONE})
    await asyncio.wait_for(gate, timeout=2.0)


async def test_gate_fails_open_on_a_stale_running_state(db_uri: str) -> None:
    """A ``running`` state past its deadline is treated as settled.

    Without this, a server restart mid-hook would leave the label at
    ``running`` with nothing left to clear it, wedging every future turn on
    this session forever.
    """
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation()
    store.set_labels(
        conv.id,
        {
            _WORKTREE_SETUP_LABEL_KEY: _WORKTREE_SETUP_RUNNING,
            _WORKTREE_SETUP_DEADLINE_LABEL_KEY: f"{time.time() - 1:.0f}",
        },
    )
    await asyncio.wait_for(_await_worktree_setup(conv.id, store), timeout=1.0)


async def test_gate_fails_open_when_the_deadline_is_missing(db_uri: str) -> None:
    """A ``running`` state with no parseable deadline also fails open."""
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation()
    store.set_labels(conv.id, {_WORKTREE_SETUP_LABEL_KEY: _WORKTREE_SETUP_RUNNING})
    await asyncio.wait_for(_await_worktree_setup(conv.id, store), timeout=1.0)


async def test_gate_returns_for_a_missing_session(db_uri: str) -> None:
    """A session deleted mid-wait releases the gate instead of spinning."""
    store = SqlAlchemyConversationStore(db_uri)
    gone_id = "0" * 32
    await asyncio.wait_for(_await_worktree_setup(gone_id, store), timeout=1.0)
