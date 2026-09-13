"""``sys_agent_list`` hides agents a pinned session could never spawn.

A Smart Routing session pinned to one harness routes its spawns inside its
own model family, so a built-in agent from another family is not launchable
for it — and advertising it produced exactly the reported defect: a pinned
codex session spawning a claude child that routing then declined after the
fact. The listing is filtered at the source instead.

Auto-harness sessions (the router owns their family) and plain sessions (no
routing at all) see the whole surface, unchanged. A plain session never even
reaches the server for the confinement answer: the runner already knows it
has no routing armed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from omnigent.runner import tool_dispatch
from omnigent.runner.subagent_routing import (
    AUTO_HARNESS_LABEL_KEY,
    SessionRoutingClass,
    forget_session_routing_class,
    remember_session_routing_class,
)
from omnigent.runner.tool_dispatch import execute_tool

_BUILTINS = [
    {"id": "ag_claude", "name": "claude-code", "harness": "claude-native"},
    {"id": "ag_codex", "name": "codex", "harness": "codex-native"},
    {"id": "ag_pi", "name": "pi", "harness": "pi"},
    {"id": "ag_unknown", "name": "mystery", "harness": None},
]

_PINNED = SessionRoutingClass(routing_enabled=True)
_AUTO = SessionRoutingClass(routing_enabled=True, auto_harness=True)


@pytest.fixture(autouse=True)
def _clean_session_state() -> Iterator[None]:
    yield
    forget_session_routing_class("conv_parent")
    tool_dispatch.forget_spawn_family("conv_parent")


def _handler(
    session: dict[str, Any],  # type: ignore[explicit-any]
    seen: list[str],
) -> Any:  # type: ignore[explicit-any]
    """Build a MockTransport handler serving *session* as the caller's row."""

    async def _serve(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": _BUILTINS})
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"data": []})
        if request.url.path == "/v1/sessions/conv_parent":
            return httpx.Response(200, json=session)
        return httpx.Response(404, json={"error": str(request.url)})

    return _serve


async def _run(handler: Any, seen: list[str]) -> dict[str, Any]:  # type: ignore[explicit-any]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_agent_list",
            arguments="{}",
            server_client=server_client,
            conversation_id="conv_parent",
        )
    return json.loads(output)


async def _agent_list(session: dict[str, Any], seen: list[str]) -> dict[str, Any]:  # type: ignore[explicit-any]
    return await _run(_handler(session, seen), seen)


def _names(listing: dict[str, Any]) -> list[str]:  # type: ignore[explicit-any]
    return [row["name"] for row in listing["builtins"]]


@pytest.mark.asyncio
async def test_pinned_routed_session_sees_only_its_own_family() -> None:
    remember_session_routing_class("conv_parent", _PINNED)
    seen: list[str] = []
    listing = await _agent_list(
        {
            "id": "conv_parent",
            "harness": "codex-native",
            "subagent_routing_override": "on",
            "labels": {},
        },
        seen,
    )
    # Every other family goes — the same rule the routing decline applies,
    # where pi is its own family. An agent whose harness resolves to no
    # family stays: nothing proves it is out of family.
    assert _names(listing) == ["codex", "mystery"]


@pytest.mark.asyncio
async def test_auto_harness_session_sees_every_family() -> None:
    remember_session_routing_class("conv_parent", _AUTO)
    seen: list[str] = []
    listing = await _agent_list(
        {
            "id": "conv_parent",
            "harness": "codex-native",
            "subagent_routing_override": "on",
            "labels": {AUTO_HARNESS_LABEL_KEY: "1"},
        },
        seen,
    )
    assert _names(listing) == ["claude-code", "codex", "pi", "mystery"]
    assert "/v1/sessions/conv_parent" not in seen


@pytest.mark.asyncio
async def test_plain_session_skips_the_routing_lookup_entirely() -> None:
    seen: list[str] = []
    listing = await _agent_list(
        {
            "id": "conv_parent",
            "harness": "codex-native",
            "subagent_routing_override": "on",
            "labels": {},
        },
        seen,
    )
    assert _names(listing) == ["claude-code", "codex", "pi", "mystery"]
    assert "/v1/sessions/conv_parent" not in seen


@pytest.mark.asyncio
async def test_routed_session_with_subagent_routing_off_sees_every_family() -> None:
    remember_session_routing_class("conv_parent", _PINNED)
    seen: list[str] = []
    listing = await _agent_list(
        {
            "id": "conv_parent",
            "harness": "codex-native",
            "subagent_routing_override": None,
            "labels": {},
        },
        seen,
    )
    assert _names(listing) == ["claude-code", "codex", "pi", "mystery"]


@pytest.mark.asyncio
async def test_unreadable_session_leaves_the_listing_alone() -> None:
    remember_session_routing_class("conv_parent", _PINNED)
    seen: list[str] = []

    async def _serve(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": _BUILTINS})
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(500, json={"error": "boom"})

    assert _names(await _run(_serve, seen)) == ["claude-code", "codex", "pi", "mystery"]
    assert "/v1/sessions/conv_parent" in seen


@pytest.mark.asyncio
async def test_timed_out_lookup_fails_open_to_the_full_listing() -> None:
    remember_session_routing_class("conv_parent", _PINNED)
    seen: list[str] = []

    async def _serve(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": _BUILTINS})
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"data": []})
        raise httpx.ReadTimeout("wedged server", request=request)

    assert _names(await _run(_serve, seen)) == ["claude-code", "codex", "pi", "mystery"]
    assert "/v1/sessions/conv_parent" in seen


def test_confinement_lookup_budget_stays_short() -> None:
    assert tool_dispatch._SPAWN_FAMILY_TIMEOUT_S <= 5.0


@pytest.mark.asyncio
async def test_repeat_listings_reuse_the_cached_confinement() -> None:
    remember_session_routing_class("conv_parent", _PINNED)
    seen: list[str] = []
    session = {
        "id": "conv_parent",
        "harness": "codex-native",
        "subagent_routing_override": "on",
        "labels": {},
    }
    assert _names(await _agent_list(session, seen)) == ["codex", "mystery"]
    assert _names(await _agent_list(session, seen)) == ["codex", "mystery"]
    assert seen.count("/v1/sessions/conv_parent") == 1
