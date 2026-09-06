"""End-to-end coverage for browser mitigation without a connected renderer."""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
import pytest

from omnigent.tools.builtins.browser import BROWSER_TOOL_NAMES
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    get_mock_requests,
    poll_session_until_terminal,
    register_inline_agent,
    send_user_message_to_session,
)


def _tool_names_in_request(request: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for tool in request.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str):
            function = tool.get("function")
            name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str):
            names.add(name)
    return names


@pytest.mark.timeout(30)
def test_browser_action_request_fails_fast_without_renderer(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
) -> None:
    model = f"mock-browser-timeout-{uuid.uuid4().hex[:6]}"
    agent_name = register_inline_agent(
        http_client,
        name=f"browser-headless-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a browser-capable agent.",
        mock_llm_base_url=(f"{mock_llm_server_url}/v1" if mock_llm_server_url else None),
    )
    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )

    start = time.monotonic()
    response = http_client.post(
        f"/v1/sessions/{session_id}/browser/action_request",
        json={"action": "navigate", "args": {"url": "https://example.com"}},
        timeout=10.0,
    )
    elapsed = time.monotonic() - start

    assert response.status_code == 200, response.text
    assert response.json() == {"error": "no browser renderer is connected"}
    assert elapsed < 2.0, f"browser action took {elapsed:.1f}s without a renderer"


def test_browser_tools_not_advertised_to_request_harness_without_renderer(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
    using_mock_llm: bool,
) -> None:
    if not using_mock_llm:
        pytest.skip("advertisement capture requires the mock LLM server")
    assert mock_llm_server_url is not None

    model = f"mock-browser-advert-{uuid.uuid4().hex[:6]}"
    agent_name = register_inline_agent(
        http_client,
        name=f"browser-advert-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a general-purpose agent.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )
    configure_mock_llm(mock_llm_server_url, [{"text": "Acknowledged."}], key=model)
    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )

    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Say hello.",
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=120,
    )
    assert body["status"] == "completed", body

    # openai-agents consumes the per-turn request schema. Native harnesses use
    # a session-scoped relay and are covered only by the prompt failure path.
    advertised = set().union(
        *(
            _tool_names_in_request(request)
            for request in get_mock_requests(mock_llm_server_url, key=model)
        )
    )
    assert "load_skill" in advertised, "expected captured framework tool schemas"
    assert advertised.isdisjoint(BROWSER_TOOL_NAMES), (
        f"browser tools advertised without a renderer: {sorted(advertised & BROWSER_TOOL_NAMES)}"
    )
