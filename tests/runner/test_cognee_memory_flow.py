"""End-to-end happy path for the cognee memory tools at the dispatch boundary.

Exercises the full in-process stack — runner dispatch → builtin tool →
``omnigent.runtime.memory`` boundary (store config, timeouts, background
cognify) — against a fake ``cognee`` module that genuinely persists and
recalls per-dataset, so remember → search round-trips are observed rather
than asserted on mock call args. A live-store e2e (real cognee + LLM key)
is tracked in ``designs/cognee-memory-integration.md`` hardening phase.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.runner.tool_dispatch import _execute_cognee_tool
from omnigent.runtime import memory as memory_mod


class _FakeStore:
    """In-memory stand-in for cognee: per-dataset content with substring search."""

    def __init__(self) -> None:
        self.datasets: dict[str, list[str]] = {}
        self.cognified: list[str] = []

    async def add(self, content: str, dataset_name: str, **_: Any) -> None:
        self.datasets.setdefault(dataset_name, []).append(content)

    async def cognify(self, datasets: list[str], **_: Any) -> None:
        self.cognified.extend(datasets)

    async def search(self, *, query_text: str, datasets: list[str], **_: Any) -> list[str]:
        needle = query_text.split()[0].lower()
        return [
            item for ds in datasets for item in self.datasets.get(ds, []) if needle in item.lower()
        ]


@pytest.fixture()
def fake_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Generator[_FakeStore, None, None]:
    """Install a persisting fake cognee module and open the gate."""
    store = _FakeStore()
    fake = MagicMock()
    fake.SearchType = SimpleNamespace(GRAPH_COMPLETION="graph_completion")
    fake.add = AsyncMock(side_effect=store.add)
    fake.cognify = AsyncMock(side_effect=store.cognify)
    fake.search = AsyncMock(side_effect=store.search)
    fake.config = MagicMock()
    monkeypatch.setitem(sys.modules, "cognee", fake)
    monkeypatch.setattr(memory_mod, "cognee_installed", lambda: True)
    monkeypatch.delenv(memory_mod.COGNEE_DISABLE_ENV, raising=False)
    monkeypatch.setattr(
        memory_mod, "cognee_settings", lambda: {"data_root": str(tmp_path / "cognee")}
    )
    memory_mod.breaker.reset()
    memory_mod._store_configured = False
    # Serve the tools even though the real registry gated them out at import
    # (the cognee package is absent from the dev environment).
    import omnigent.tools.builtins as builtins_mod
    from omnigent.tools.builtins.cognee import CogneeRememberTool, CogneeSearchTool

    def serving_get(name: str, config: dict[str, str] | None = None) -> object | None:
        if name == "cognee_search":
            return CogneeSearchTool(config=config)
        if name == "cognee_remember":
            return CogneeRememberTool(config=config)
        return None

    monkeypatch.setattr(builtins_mod, "get_builtin_tool", serving_get)
    yield store
    memory_mod.breaker.reset()
    memory_mod._store_configured = False
    if memory_mod._background_executor is not None:
        memory_mod._background_executor.shutdown(wait=True)
        memory_mod._background_executor = None


def _spec(config: dict[str, str], *names: str) -> SimpleNamespace:
    builtins = [SimpleNamespace(name=n, config=config) for n in names]
    return SimpleNamespace(tools=SimpleNamespace(builtins=builtins))


def _dispatch(args: dict[str, Any], tool_name: str, spec: SimpleNamespace, agent_id: str) -> str:
    return asyncio.run(
        _execute_cognee_tool(
            args,
            tool_name=tool_name,
            agent_spec=spec,
            conversation_id="conv_flow",
            agent_id=agent_id,
        )
    )


def test_remember_then_search_round_trip(fake_store: _FakeStore) -> None:
    """Happy path: enable tools → remember → search recalls the memory."""
    spec = _spec({}, "cognee_remember", "cognee_search")
    stored = _dispatch(
        {"content": "DuckDB is the preferred analytics database"},
        "cognee_remember",
        spec,
        agent_id="ag_flow",
    )
    assert stored == "Stored to long-term memory."
    # The write landed in the agent's private dataset and was queued for
    # background cognify (drain the worker so the assertion is deterministic).
    assert fake_store.datasets["ag_flow"] == ["DuckDB is the preferred analytics database"]
    assert memory_mod._background_executor is not None
    memory_mod._background_executor.shutdown(wait=True)
    assert fake_store.cognified == ["ag_flow"]

    recalled = _dispatch({"query": "duckdb preference"}, "cognee_search", spec, agent_id="ag_flow")
    assert recalled == "- DuckDB is the preferred analytics database"


def test_cross_agent_exchange_round_trip(fake_store: _FakeStore) -> None:
    """Agent A publishes to the shared pool; agent B recalls it."""
    grant = {"shared_dataset": "team_pool"}
    publisher = _spec(grant, "cognee_remember")
    reader = _spec(grant, "cognee_search")
    _dispatch(
        {"content": "release is friday", "scope": "shared"},
        "cognee_remember",
        publisher,
        agent_id="ag_a",
    )
    recalled = _dispatch({"query": "release date"}, "cognee_search", reader, agent_id="ag_b")
    assert recalled == "- release is friday"
    # Isolation: agent B's private search does not see A's publication.
    private_only = _dispatch(
        {"query": "release date", "scope": "agent"}, "cognee_search", reader, agent_id="ag_b"
    )
    assert private_only == "No relevant memories found."


def test_kill_switch_degrades_gracefully(
    fake_store: _FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the kill-switch set, dispatch reports unavailability, never raises."""
    monkeypatch.setenv(memory_mod.COGNEE_DISABLE_ENV, "1")
    spec = _spec({}, "cognee_search")
    result = _dispatch({"query": "anything"}, "cognee_search", spec, agent_id="ag_flow")
    assert "not available" in result
    assert fake_store.datasets == {}
