"""Agent-spec resolution on the host runner-launch path.

The launch endpoint resolves the session's bound agent spec twice —
``os_env.cwd`` for the workspace boundary and the harness for the launch
frame. Both go through ``AgentCache.load``, which raises ``KeyError`` when
the bundle is gone from the artifact store (e.g. a server whose artifact
storage was reset while the DB kept the agent and session rows). That load
failure must surface as a structured ``agent_bundle_missing`` client error,
never escape as an unhandled exception (the opaque 500 users hit when
resuming such a session).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.entities import Agent, Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.routes.hosts import (
    _resolve_agent_harness,
    _resolve_agent_spec_cwd,
)
from omnigent.stores.artifact_store.local import LocalArtifactStore

pytestmark = pytest.mark.asyncio


class _FakeAgentStore:
    """Minimal agent store returning a fixed row."""

    def __init__(self, agent: Agent | None) -> None:
        self._agent = agent

    def get(self, agent_id: str) -> Agent | None:
        return self._agent


def _conv(agent_id: str | None) -> Conversation:
    return Conversation(
        id="conv1",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv1",
        agent_id=agent_id,
    )


def _agent(bundle_location: str | None) -> Agent:
    return Agent(
        id="ag1",
        created_at=1,
        name="lost-bundle-agent",
        bundle_location=bundle_location,  # type: ignore[arg-type]
    )


def _empty_cache(tmp_path: Path) -> AgentCache:
    """An agent cache whose artifact store holds nothing."""
    return AgentCache(
        artifact_store=LocalArtifactStore(str(tmp_path / "artifacts")),
        cache_dir=tmp_path / "cache",
    )


async def test_spec_cwd_missing_bundle_raises_structured_error(tmp_path: Path) -> None:
    """A lost bundle surfaces as agent_bundle_missing, not a raw KeyError."""
    store = _FakeAgentStore(_agent("ag1/deadbeef"))
    with pytest.raises(OmnigentError) as exc_info:
        await _resolve_agent_spec_cwd(_conv("ag1"), store, _empty_cache(tmp_path))
    assert exc_info.value.code == ErrorCode.AGENT_BUNDLE_MISSING
    # 410 Gone: a retry can't restore the lost bundle.
    assert exc_info.value.http_status == 410
    # The message must be actionable, naming the agent and the way out.
    assert "lost-bundle-agent" in exc_info.value.message
    assert "re-upload" in exc_info.value.message.lower()


async def test_harness_missing_bundle_raises_structured_error(tmp_path: Path) -> None:
    """The harness resolver guards the same load the cwd resolver does."""
    store = _FakeAgentStore(_agent("ag1/deadbeef"))
    with pytest.raises(OmnigentError) as exc_info:
        await _resolve_agent_harness(_conv("ag1"), store, _empty_cache(tmp_path))
    assert exc_info.value.code == ErrorCode.AGENT_BUNDLE_MISSING


async def test_no_agent_still_resolves_none(tmp_path: Path) -> None:
    """Headless sessions (no agent binding) stay unconstrained — no error."""
    store = _FakeAgentStore(None)
    assert await _resolve_agent_spec_cwd(_conv(None), store, _empty_cache(tmp_path)) is None
    assert await _resolve_agent_harness(_conv(None), store, _empty_cache(tmp_path)) is None


async def test_agent_without_bundle_still_resolves_none(tmp_path: Path) -> None:
    """An agent row with no bundle location resolves to None — no error."""
    store = _FakeAgentStore(_agent(None))
    assert await _resolve_agent_spec_cwd(_conv("ag1"), store, _empty_cache(tmp_path)) is None
    assert await _resolve_agent_harness(_conv("ag1"), store, _empty_cache(tmp_path)) is None
