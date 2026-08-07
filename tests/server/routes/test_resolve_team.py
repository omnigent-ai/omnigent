"""Unit tests for ``_resolve_team``, the agent-team flag on a session snapshot.

The runner's peer-send gate authorizes a team message by reading ``team`` off
BOTH endpoints' ``GET /v1/sessions`` snapshots. That field is produced here, by
loading the session's bound agent spec. Two properties matter and are pinned
below: the flag reflects the bound spec, and an unresolvable agent yields
``None`` (which the gate treats as deny) rather than raising into the response
path.

Membership deriving from the lead is a consequence of how children are bound:
a declared sub-agent is stamped with its parent's ``agent_id``, so a teammate
resolves the same top-level spec as the lead and cannot self-promote by setting
``team:`` in its own sub-config.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.entities import Conversation
from omnigent.server.routes._sessions import helpers
from omnigent.server.routes._sessions.helpers import _resolve_team


def _conv(agent_id: str | None = "ag_test") -> Conversation:
    """A minimal conversation bound to *agent_id*."""
    return Conversation(
        id="conv_member",
        created_at=100,
        updated_at=200,
        root_conversation_id="conv_lead",
        title="teammate",
        agent_id=agent_id,
    )


class _Cache:
    """Agent cache stub returning a spec with a fixed ``team`` flag."""

    def __init__(self, team: bool) -> None:
        self._team = team

    def load(self, *args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(spec=SimpleNamespace(team=self._team))


def _patch_lookup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent: Any,
    cache: Any,
) -> None:
    """Point the agent store and agent cache at the given stubs."""
    monkeypatch.setattr(
        helpers,
        "_agent_store",
        SimpleNamespace(get=lambda _id: agent),
        raising=False,
    )
    import omnigent.runtime as runtime
    import omnigent.runtime._globals as runtime_globals

    monkeypatch.setattr(runtime_globals, "_agent_store", SimpleNamespace(get=lambda _id: agent))
    monkeypatch.setattr(runtime, "get_agent_cache", lambda: cache)


def _agent() -> Any:
    """A minimal registered agent row."""
    return SimpleNamespace(id="ag_test", bundle_location="ag_test/bundle", session_id=None)


def test_resolve_team_true_when_spec_opts_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """``team: true`` on the bound spec surfaces as ``True``.

    The peer-send gate requires a truthy flag on both endpoints, so if this
    returned ``None``/``False`` for an opted-in team every peer send would be
    refused as ``session_out_of_tree``.
    """
    _patch_lookup(monkeypatch, agent=_agent(), cache=_Cache(team=True))
    assert _resolve_team(_conv()) is True


def test_resolve_team_false_when_spec_opts_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """A spec without ``team:`` surfaces as ``False``, keeping sends child-only."""
    _patch_lookup(monkeypatch, agent=_agent(), cache=_Cache(team=False))
    assert _resolve_team(_conv()) is False


def test_resolve_team_none_for_unbound_conversation() -> None:
    """A conversation with no agent cannot resolve a flag and yields ``None``."""
    assert _resolve_team(_conv(agent_id=None)) is None
    assert _resolve_team(None) is None


def test_resolve_team_none_when_agent_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent id that is not registered yields ``None`` rather than raising."""
    _patch_lookup(monkeypatch, agent=None, cache=_Cache(team=True))
    assert _resolve_team(_conv()) is None


def test_resolve_team_none_when_bundle_load_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bundle that cannot be loaded yields ``None`` instead of propagating.

    ``_resolve_team`` runs inside the session-response build, so a corrupt or
    missing bundle must not turn every read of that session into a 500. The
    gate treats ``None`` as deny, so the failure is fail-closed.
    """

    class _Broken:
        def load(self, *args: Any, **kwargs: Any) -> Any:
            raise OSError("bundle unreadable")

    _patch_lookup(monkeypatch, agent=_agent(), cache=_Broken())
    assert _resolve_team(_conv()) is None
