"""Regression: an agent-cache reset must survive an in-flight spec resolution.

The runner memoizes resolved agent specs in two caches -- one keyed by agent id
and shared by every conversation, one keyed by session id -- and a reset drops
both. Each cache had an unconditional write after awaiting the spec resolver, so
a resolution that started before a reset could complete afterwards and re-install
the bundle the reset had just retired. The next read was then served a spec the
reset was supposed to have discarded.

The rule under test: once a reset for a session completes, no later write may
re-populate either cache from a resolution that began before it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient

_AGENT_ID = "0e36e3219954d2deaef06b8e2a936f38"
_CONV = "4e92b5a0c0ee6db3f874f9c4a3f855a5"


def _turn_body() -> dict[str, Any]:
    """A sessions-native user turn that resolves the agent spec."""
    return {
        "type": "message",
        "role": "user",
        "agent_id": _AGENT_ID,
        "model": "test-agent",
        "input": [{"type": "input_text", "text": "hi"}],
        "harness": "openai-agents",
        "has_mcp_servers": True,
    }


@pytest.mark.asyncio
async def test_agent_cache_reset_is_not_undone_by_an_in_flight_resolution() -> None:
    """A resolution that raced a reset must not re-populate either spec cache."""
    entered_resolver = asyncio.Event()
    release_resolver = asyncio.Event()
    superseded = AgentSpec(spec_version=1, name="before-reset")
    current = AgentSpec(spec_version=1, name="after-reset")
    resolutions = 0

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Block inside the first resolution so a reset can land mid-flight.

        Only that first resolution predates the reset, so only its bundle is the
        one no cache may end up holding. A resolution started afterwards is
        current and may legitimately be memoized.
        """
        del agent_id, session_id
        nonlocal resolutions
        resolutions += 1
        if resolutions == 1:
            entered_resolver.set()
            await release_resolver.wait()
            return superseded
        return current

    harness_client = _ScriptedHarnessClient(
        [_sse({"type": "response.created", "response": {"id": "resp_1"}})]
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness_client),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    caches = app.state.spec_caches

    async with _runner_client(app) as client:
        turn = asyncio.create_task(client.post(f"/v1/sessions/{_CONV}/events", json=_turn_body()))
        await asyncio.wait_for(entered_resolver.wait(), timeout=5)

        reset = await client.post(
            f"/v1/sessions/{_CONV}/agent-cache/reset",
            json={"agent_id": _AGENT_ID},
        )
        assert reset.status_code == 200
        assert _CONV not in caches["session"], "the reset must drop the session entry"

        # Let the raced resolution finish and attempt its now-stale write.
        release_resolver.set()
        assert (await asyncio.wait_for(turn, timeout=5)).status_code == 202
        await asyncio.sleep(0.1)

    assert caches["session"].get(_CONV) is not superseded, (
        "the resolution that raced the reset re-installed the superseded bundle "
        "in the session cache"
    )
    assert caches["agent"].get(_AGENT_ID) is not superseded, (
        "the resolution that raced the reset re-installed the superseded bundle "
        "in the shared agent cache"
    )


@pytest.mark.asyncio
async def test_shared_agent_cache_reset_is_not_undone_by_an_in_flight_resolution() -> None:
    """A resolution that raced a reset must not re-populate the agent-keyed cache.

    Only the eager MCP path writes that cache, and it resolves only when both
    caches are unset as it starts, so the two resolutions before it return
    ``None`` and leave them that way. A session-scoped generation cannot fence
    this write: it answers whether this session's agent changed, not whether
    this agent did.
    """
    entered_resolver = asyncio.Event()
    release_resolver = asyncio.Event()
    superseded = AgentSpec(spec_version=1, name="before-reset")
    resolutions = 0

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec | None:
        """Resolve nothing for turn setup, then race the reset in the eager path.

        A turn resolves three times here -- turn setup, the harness config, and
        the eager MCP path -- and only the last writes the shared cache. Turn
        setup gets ``None`` so it leaves the session entry unset; the harness
        config still needs a spec or the turn aborts before the eager path.
        """
        del agent_id, session_id
        nonlocal resolutions
        resolutions += 1
        if resolutions == 1:
            return None
        if resolutions == 2:
            return AgentSpec(spec_version=1, name="harness-selection")
        entered_resolver.set()
        await release_resolver.wait()
        return superseded

    harness_client = _ScriptedHarnessClient(
        [_sse({"type": "response.created", "response": {"id": "resp_1"}})]
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness_client),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    caches = app.state.spec_caches

    async with _runner_client(app) as client:
        turn = asyncio.create_task(client.post(f"/v1/sessions/{_CONV}/events", json=_turn_body()))
        await asyncio.wait_for(entered_resolver.wait(), timeout=5)

        reset = await client.post(
            f"/v1/sessions/{_CONV}/agent-cache/reset",
            json={"agent_id": _AGENT_ID},
        )
        assert reset.status_code == 200
        assert _AGENT_ID not in caches["agent"], "the reset must drop the agent entry"

        # Let the raced resolution finish and attempt its now-stale write.
        release_resolver.set()
        assert (await asyncio.wait_for(turn, timeout=5)).status_code == 202
        await asyncio.sleep(0.1)

    assert resolutions == 3, (
        "the turn no longer makes the three resolutions this scenario assumes, "
        "so the raced write is not the eager MCP path's -- re-derive the setup"
    )
    assert caches["agent"].get(_AGENT_ID) is not superseded, (
        "the resolution that raced the reset re-installed the superseded bundle "
        "in the shared agent cache"
    )
