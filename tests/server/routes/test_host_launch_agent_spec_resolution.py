"""Launch-time spec lookups report ``agent_bundle_missing`` for a lost bundle.

A runner launch reads the bound agent's spec twice (workspace boundary, then
harness). When the row survives but its bundle blob is gone, both lookups must
raise the structured 409 rather than fall back to ``None`` and skip the
boundary check.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from omnigent.entities import Agent, Conversation
from omnigent.errors import AGENT_BUNDLE_MISSING_MESSAGE, ErrorCode, OmnigentError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.bundles import bundle_location
from omnigent.server.routes.hosts import _resolve_agent_harness, _resolve_agent_spec_cwd
from omnigent.stores.artifact_store.local import LocalArtifactStore

pytestmark = pytest.mark.asyncio

_AGENT_ID = "0f1a2b3c4d5e6f708192a3b4c5d6e7f8"


class _FakeAgentStore:
    """Minimal agent store returning one fixed row."""

    def __init__(self, agent: Agent | None) -> None:
        self._agent = agent

    def get(self, agent_id: str) -> Agent | None:
        return self._agent


def _conv(agent_id: str | None) -> Conversation:
    return Conversation(
        id="conv1", created_at=1, updated_at=1, root_conversation_id="conv1", agent_id=agent_id
    )


def _agent(location: str | None) -> Agent:
    return Agent(
        id=_AGENT_ID,
        created_at=1,
        name="lost-bundle-agent",
        bundle_location=location,  # type: ignore[arg-type]
    )


def _bundle(yaml_text: str) -> bytes:
    data = yaml_text.encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="agent.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _stores(tmp_path: Path) -> tuple[AgentCache, LocalArtifactStore]:
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    return AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"), artifacts


async def test_spec_cwd_missing_bundle_raises_agent_bundle_missing(tmp_path: Path) -> None:
    cache, _ = _stores(tmp_path)
    store = _FakeAgentStore(_agent(f"{_AGENT_ID}/deadbeef"))

    with pytest.raises(OmnigentError) as exc_info:
        await _resolve_agent_spec_cwd(_conv(_AGENT_ID), store, cache)

    assert exc_info.value.code == ErrorCode.AGENT_BUNDLE_MISSING
    assert exc_info.value.http_status == 409
    assert exc_info.value.message == AGENT_BUNDLE_MISSING_MESSAGE
    # The store's KeyError stays attached for operators; clients never see it.
    assert isinstance(exc_info.value.__cause__, KeyError)


async def test_harness_missing_bundle_raises_agent_bundle_missing(tmp_path: Path) -> None:
    cache, _ = _stores(tmp_path)
    store = _FakeAgentStore(_agent(f"{_AGENT_ID}/deadbeef"))

    with pytest.raises(OmnigentError) as exc_info:
        await _resolve_agent_harness(_conv(_AGENT_ID), store, cache)

    assert exc_info.value.code == ErrorCode.AGENT_BUNDLE_MISSING


async def test_present_bundle_still_resolves(tmp_path: Path) -> None:
    """The guard only converts a missing blob; a stored bundle loads as before."""
    cache, artifacts = _stores(tmp_path)
    bundle = _bundle(
        "name: lost-bundle-agent\nprompt: hi\nexecutor:\n  harness: claude-sdk\n"
        "os_env:\n  cwd: workspace/app\n"
    )
    location = bundle_location(_AGENT_ID, bundle)
    artifacts.put(location, bundle)
    store = _FakeAgentStore(_agent(location))

    assert await _resolve_agent_harness(_conv(_AGENT_ID), store, cache) == "claude-sdk"
    assert await _resolve_agent_spec_cwd(_conv(_AGENT_ID), store, cache) == "workspace/app"


async def test_no_agent_still_resolves_none(tmp_path: Path) -> None:
    """Headless sessions (no agent binding) stay unconstrained."""
    cache, _ = _stores(tmp_path)
    store = _FakeAgentStore(None)

    assert await _resolve_agent_spec_cwd(_conv(None), store, cache) is None
    assert await _resolve_agent_harness(_conv(None), store, cache) is None


async def test_agent_without_bundle_still_resolves_none(tmp_path: Path) -> None:
    """An agent row with no bundle location resolves to None rather than an error."""
    cache, _ = _stores(tmp_path)
    store = _FakeAgentStore(_agent(None))

    assert await _resolve_agent_spec_cwd(_conv(_AGENT_ID), store, cache) is None
    assert await _resolve_agent_harness(_conv(_AGENT_ID), store, cache) is None
