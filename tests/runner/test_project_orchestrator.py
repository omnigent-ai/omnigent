from __future__ import annotations

import httpx
import pytest

from omnigent.runner.native.orchestration import _native_startup_instructions
from omnigent.runner.project_orchestrator import (
    PROJECT_ORCHESTRATOR_INSTRUCTION,
    append_project_orchestrator_instructions,
)
from omnigent.spec.types import AgentSpec, ToolGroupsConfig


@pytest.mark.parametrize("role", [True, False, None, "true"])
async def test_only_designated_orchestrator_gets_the_role_in_native_and_sdk_prompts(role):
    spec = AgentSpec(
        spec_version=1,
        instructions="Shared agent instructions",
        tool_groups=ToolGroupsConfig(memory=False),
    )
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/v1/sessions/session"
        assert dict(request.url.params) == {
            "include_items": "false",
            "include_liveness": "false",
            "include_live_status": "false",
            "include_usage": "false",
        }
        return httpx.Response(200, json={"project_orchestrator": role})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://server.test"
    ) as client:
        native = await _native_startup_instructions(spec, client, "session")
        sdk = await append_project_orchestrator_instructions(
            "Shared agent instructions\n\nRequest instructions", client, "session", spec
        )
    assert len(requests) == 2
    assert native.startswith(spec.instructions)
    assert sdk.startswith("Shared agent instructions\n\nRequest instructions")
    for prompt in (native, sdk):
        assert (PROJECT_ORCHESTRATOR_INSTRUCTION in prompt) is (role is True)
    assert spec.instructions == "Shared agent instructions"


async def test_agent_without_project_tools_has_no_role_or_server_lookup():
    spec = AgentSpec(spec_version=1, tool_groups=ToolGroupsConfig(projects=False))

    def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unexpected), base_url="http://server.test"
    ) as client:
        assert (
            await append_project_orchestrator_instructions("Author", client, "session", spec)
            == "Author"
        )
    assert (
        await append_project_orchestrator_instructions("Author", None, "session", spec) == "Author"
    )


async def test_unavailable_role_lookup_does_not_silently_start_an_ordinary_worker():
    spec = AgentSpec(spec_version=1)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
        base_url="http://server.test",
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await append_project_orchestrator_instructions("Author", client, "session", spec)
