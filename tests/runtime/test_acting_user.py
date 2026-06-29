"""Tests for the per-turn acting-user context var (#5)."""

from __future__ import annotations

import asyncio

import pytest

from omnigent.runtime.acting_user import acting_user_scope, get_acting_user


def test_defaults_to_none() -> None:
    assert get_acting_user() is None


def test_scope_binds_and_restores() -> None:
    assert get_acting_user() is None
    with acting_user_scope("bob@example.com"):
        assert get_acting_user() == "bob@example.com"
    assert get_acting_user() is None


def test_nested_scopes_restore_outer() -> None:
    with acting_user_scope("alice@example.com"):
        assert get_acting_user() == "alice@example.com"
        with acting_user_scope("bob@example.com"):
            assert get_acting_user() == "bob@example.com"
        # Inner scope exit restores the outer actor, not None.
        assert get_acting_user() == "alice@example.com"
    assert get_acting_user() is None


def test_explicit_none_scope_clears() -> None:
    with acting_user_scope("alice@example.com"):
        with acting_user_scope(None):
            assert get_acting_user() is None
        assert get_acting_user() == "alice@example.com"


@pytest.mark.asyncio
async def test_propagates_into_to_thread() -> None:
    # The whole design rests on this: the runner sets the actor in the async
    # turn task, but the os-env shell tool runs the secret resolution + spawn
    # inside asyncio.to_thread. The copied context must carry the actor across
    # that hop, or per-user injection silently no-ops.
    with acting_user_scope("bob@example.com"):
        seen = await asyncio.to_thread(get_acting_user)
    assert seen == "bob@example.com"
