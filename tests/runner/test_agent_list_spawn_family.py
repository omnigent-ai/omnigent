"""``sys_agent_list`` hides agents a pinned session could never spawn.

A Smart Routing session pinned to one harness routes its spawns inside its
own model family, so a built-in agent from another family is not launchable
for it — and advertising it produced exactly the reported defect: a pinned
codex session spawning a claude child that routing then declined after the
fact. The listing is filtered at the source instead.

Auto-harness sessions (the router owns their family) and plain sessions (no
routing at all) see the whole surface, unchanged.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.runner.subagent_routing import AUTO_HARNESS_LABEL_KEY
from omnigent.runner.tool_dispatch import execute_tool

_BUILTINS = [
    {"id": "ag_claude", "name": "claude-code", "harness": "claude-native"},
    {"id": "ag_codex", "name": "codex", "harness": "codex-native"},
    {"id": "ag_pi", "name": "pi", "harness": "pi"},
    {"id": "ag_unknown", "name": "mystery", "harness": None},
]


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


async def _agent_list(session: dict[str, Any], seen: list[str]) -> dict[str, Any]:  # type: ignore[explicit-any]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler(session, seen)),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_agent_list",
            arguments="{}",
            server_client=server_client,
            conversation_id="conv_parent",
        )
    return json.loads(output)


def _names(listing: dict[str, Any]) -> list[str]:  # type: ignore[explicit-any]
    return [row["name"] for row in listing["builtins"]]


@pytest.mark.asyncio
async def test_pinned_routed_session_sees_only_its_own_family() -> None:
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


@pytest.mark.asyncio
async def test_plain_session_sees_every_family() -> None:
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
    seen: list[str] = []

    async def _serve(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": _BUILTINS})
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(500, json={"error": "boom"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_serve),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_agent_list",
            arguments="{}",
            server_client=server_client,
            conversation_id="conv_parent",
        )
    assert _names(json.loads(output)) == ["claude-code", "codex", "pi", "mystery"]
    assert seen == []
