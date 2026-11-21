"""``sys_advise_models`` is offered on runners attached to a routing-enabled server.

The ``live_server`` fixture's server-level ``llm:`` block builds the OSS judge.
A runner outside that server process (an ``omnigent host`` daemon's runner, or
the tunneled sibling runner) must still advertise the advisor, and the server
must answer the call the model then makes.
"""

from __future__ import annotations

import ast
import json
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.host import isolated_host
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    get_mock_requests,
    lookup_agent_id,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.helpers import POLL_INTERVAL_S

# Both sides must carry the capability: an older server omits the field and an
# older runner cannot consume it.
pytestmark = [
    pytest.mark.min_server_version("0.19.0"),
    pytest.mark.min_runner_version("0.19.0"),
]

_HOST_ONLINE_TIMEOUT_S = 60.0
_TURN_TIMEOUT_S = 180.0
_ADVISOR_ARGUMENTS = {
    "tasks": [{"title": "auth-refactor", "agents": [{"agent": "worker", "models": None}]}]
}


def _assert_server_routes(client: httpx.Client) -> None:
    """Journey precondition: the server itself must report routing on."""
    resp = client.get("/v1/info")
    resp.raise_for_status()
    info = resp.json()
    assert info.get("smart_routing_enabled") is True, (
        "precondition failed: the test server does not report "
        f"smart_routing_enabled=true (got {info.get('smart_routing_enabled')!r}, "
        f"sources={info.get('smart_routing_sources')!r}); the live_server "
        "fixture's server-level llm: block should configure the OSS judge"
    )


def _register_spawn_agent(
    client: httpx.Client, mock_llm_server_url: str, *, model: str, label: str
) -> str:
    """Register an openai-agents agent whose ``spawn: true`` grant registers the
    sub-agent tool block (``sys_list_models`` + the gated ``sys_advise_models``)."""
    return register_inline_agent(
        client,
        name=f"advise-{label}-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a helpful assistant.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        extra_config={"spawn": True},
    )


def _script_advisor_turn(mock_llm_server_url: str, *, model: str, call_id: str) -> None:
    """Queue the advisor call and the reply; tool-less title requests cannot consume them."""
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": call_id,
                        "name": "sys_advise_models",
                        "arguments": json.dumps(_ADVISOR_ARGUMENTS),
                    }
                ]
            },
            {"text": "Advisor consulted."},
        ],
        key=model,
        required_tools=["sys_list_models"],
    )


def _advisor_output(reqs: list[dict[str, Any]], call_id: str) -> dict[str, Any]:
    """The advisor's answer as the model received it on its next request."""

    def _walk(node: Any) -> dict[str, Any] | None:
        if isinstance(node, dict):
            if node.get("type") in {"function_call_output", "tool_result"} and call_id in {
                node.get("call_id"),
                node.get("tool_use_id"),
            }:
                raw = node.get("output", node.get("content"))
                if not isinstance(raw, str):
                    return raw
                try:
                    return json.loads(raw)
                except ValueError:
                    try:
                        return ast.literal_eval(raw)
                    except (ValueError, SyntaxError):
                        return None
            for value in node.values():
                found = _walk(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = _walk(value)
                if found is not None:
                    return found
        return None

    for req in reqs:
        found = _walk(req.get("input"))
        if found is not None:
            return found
    raise AssertionError(f"no model request carried the advisor's output for {call_id}")


def _advertised_tool_names(reqs: list[dict[str, Any]]) -> set[str]:
    """Collect tool names offered to the model, normalizing MCP-style prefixes."""
    names: set[str] = set()
    for req in reqs:
        for tool in req.get("tools", []) or []:
            raw = tool.get("name") or (tool.get("function") or {}).get("name")
            if raw:
                names.add(str(raw).split("__")[-1])
    return names


def _wait_for_session_runner(client: httpx.Client, session_id: str) -> None:
    """A host-bound session's runner spawns after create; wait until it tunnels in."""
    deadline = time.monotonic() + _HOST_ONLINE_TIMEOUT_S
    while time.monotonic() < deadline:
        session = client.get(f"/v1/sessions/{session_id}")
        session.raise_for_status()
        runner_id = session.json().get("runner_id")
        if runner_id:
            status = client.get(f"/v1/runners/{runner_id}/status")
            if status.status_code == 200 and status.json().get("online") is True:
                return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"the runner for session {session_id} never came online")


def _run_advisor_turn(
    client: httpx.Client,
    mock_llm_server_url: str,
    *,
    session_id: str,
    model: str,
    call_id: str,
) -> tuple[set[str], dict[str, Any]]:
    """Run the scripted turn; return the offered tool names and the advisor's answer."""
    _wait_for_session_runner(client, session_id)
    response_id = send_user_message_to_session(
        client,
        session_id=session_id,
        content="Which model should I use for this task?",
    )
    body = poll_session_until_terminal(
        client, session_id=session_id, response_id=response_id, timeout=_TURN_TIMEOUT_S
    )
    assert body["status"] == "completed", (
        f"turn did not complete: status={body['status']!r} error={body.get('error')!r}"
    )
    reqs = get_mock_requests(mock_llm_server_url, key=model)
    assert reqs, f"mock LLM captured no requests for model {model!r}"
    return _advertised_tool_names(reqs), _advisor_output(reqs, call_id)


def _assert_advisor_offered(
    tool_names: set[str], advisor_output: dict[str, Any], *, topology: str
) -> None:
    assert "sys_list_models" in tool_names, (
        f"control failed on the {topology}: sys_list_models missing, so the "
        f"sub-agent tool block never registered at all. Advertised: {sorted(tool_names)}"
    )
    assert "sys_advise_models" in tool_names, (
        f"sys_advise_models is hidden from a session on the {topology} even "
        "though the attached server reports smart_routing_enabled=true "
        f"(the runner-side routing_available(get_caps()) gate). "
        f"Advertised: {sorted(tool_names)}"
    )
    # The server answered the intercepted call from its own router.
    assert advisor_output.get("router_on") is True, advisor_output
    assert isinstance(advisor_output.get("recommendations"), list), advisor_output


@pytest.fixture
def attached_host(live_server: str, http_client: httpx.Client, tmp_path: Path) -> Iterator[str]:
    """Attach a real ``omnigent host`` daemon to the live server; yield its host id."""
    with isolated_host(tmp_path / "host", live_server) as host:
        host.wait_online(http_client, timeout=_HOST_ONLINE_TIMEOUT_S)
        yield host.host_id


def test_advise_models_offered_on_host_attached_runner(
    live_server: str,
    http_client: httpx.Client,
    mock_llm_server_url: str,
    attached_host: str,
    tmp_path: Path,
) -> None:
    """The reported journey: routing server -> ``omnigent host --server`` ->
    session on that host's runner -> the model must be offered ``sys_advise_models``."""
    _assert_server_routes(http_client)
    model = f"mock-advise-host-{uuid.uuid4().hex[:6]}"
    call_id = f"call-{uuid.uuid4().hex[:8]}"
    reset_mock_llm(mock_llm_server_url)
    agent_name = _register_spawn_agent(http_client, mock_llm_server_url, model=model, label="host")
    _script_advisor_turn(mock_llm_server_url, model=model, call_id=call_id)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    resp = http_client.post(
        "/v1/sessions",
        json={
            "agent_id": lookup_agent_id(http_client, agent_name),
            "host_id": attached_host,
            "workspace": str(workspace),
        },
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    resp.raise_for_status()
    session_id = str(resp.json()["id"])

    tool_names, advisor_output = _run_advisor_turn(
        http_client, mock_llm_server_url, session_id=session_id, model=model, call_id=call_id
    )
    _assert_advisor_offered(tool_names, advisor_output, topology="host-attached runner")


def test_advise_models_offered_on_tunneled_sibling_runner(
    live_server: str,
    http_client: httpx.Client,
    mock_llm_server_url: str,
    live_runner_id: str,
) -> None:
    """The wider scope from the issue thread: the default local topology's
    tunneled sibling runner must also be offered ``sys_advise_models``."""
    _assert_server_routes(http_client)
    model = f"mock-advise-sibling-{uuid.uuid4().hex[:6]}"
    call_id = f"call-{uuid.uuid4().hex[:8]}"
    reset_mock_llm(mock_llm_server_url)
    agent_name = _register_spawn_agent(
        http_client, mock_llm_server_url, model=model, label="sibling"
    )
    _script_advisor_turn(mock_llm_server_url, model=model, call_id=call_id)

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    tool_names, advisor_output = _run_advisor_turn(
        http_client, mock_llm_server_url, session_id=session_id, model=model, call_id=call_id
    )
    _assert_advisor_offered(tool_names, advisor_output, topology="tunneled sibling runner")
