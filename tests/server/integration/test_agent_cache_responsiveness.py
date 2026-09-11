"""Event-loop coverage for Server routes that mutate the agent cache."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from omnigent.runtime import get_conversation_store
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.routes._sessions.orchestration import _native_pane_harness
from tests.server.helpers import build_agent_bundle, create_test_agent

pytestmark = pytest.mark.asyncio


@asynccontextmanager
async def cache_pause(monkeypatch: pytest.MonkeyPatch, method: str) -> AsyncIterator[None]:
    """Let the loop release a blocked cache call; direct calls time out."""
    started, release = threading.Event(), threading.Event()
    original = getattr(AgentCache, method)

    def blocked(self: AgentCache, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        started.set()
        assert release.wait(timeout=2), "cache work blocked the event loop"
        return original(self, *args, **kwargs)

    async def heartbeat() -> None:
        try:
            assert await asyncio.to_thread(started.wait, 2)
            await asyncio.sleep(0)
        finally:
            release.set()

    monkeypatch.setattr(AgentCache, method, blocked)
    pulse = asyncio.create_task(heartbeat())
    try:
        yield
        await pulse
    finally:
        release.set()
        await pulse


async def test_update_agent_cache_waits_do_not_block_event_loop(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contended replacement and response loading both yield to the event loop."""
    agent = await create_test_agent(
        client,
        name="cache-responsive-agent",
        description="original",
    )
    updated_bundle = build_agent_bundle(
        name="cache-responsive-agent",
        description="updated",
    )
    async with cache_pause(monkeypatch, "replace"), cache_pause(monkeypatch, "load"):
        response = await client.put(
            f"/v1/sessions/{agent['_session_id']}/agent",
            files={"bundle": ("agent.tar.gz", updated_bundle, "application/gzip")},
        )
    assert response.status_code == 200, response.text
    assert response.json()["id"] == agent["id"]


async def test_no_override_native_pane_resolution_does_not_block_event_loop(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native-pane routing resolves its bound harness without blocking."""
    agent = await create_test_agent(
        client,
        name="harness-responsive-agent",
        executor={"type": "omnigent", "config": {"harness": "codex-native"}},
    )
    conversation_store = get_conversation_store()
    conv = await asyncio.to_thread(
        conversation_store.get_conversation,
        agent["_session_id"],
    )
    assert conv is not None
    assert conv.harness_override is None

    async with cache_pause(monkeypatch, "load"):
        assert await _native_pane_harness(conv) == "codex-native"


async def test_patch_model_override_non_native_check_does_not_block_event_loop(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PATCH route's no-override SDK classification yields during cache load."""
    agent = await create_test_agent(client, name="sdk-classification-responsive-agent")
    async with cache_pause(monkeypatch, "load"):
        response = await client.patch(
            f"/v1/sessions/{agent['_session_id']}",
            json={"model_override": "databricks-gpt-5-4"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["model_override"] == "databricks-gpt-5-4"
