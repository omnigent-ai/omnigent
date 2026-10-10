"""Primary runner coverage for sub-agent dispatch constraints."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from omnigent.runner import subagent_work
from omnigent.runner.tool_dispatch import execute_tool
from omnigent.spec import AgentSpec
from omnigent.spec.types import ExecutorSpec, ToolsConfig

_PARENT_SESSION = "conv_parent_constraints"
_CHILD_SESSION = "conv_child_constraints"


def _parent_spec(
    harness: str = "claude-native",
    *,
    reasoning_effort: str | None = None,
) -> AgentSpec:
    """Build a real parent/worker spec for runner dispatch tests."""
    worker = AgentSpec(
        spec_version=1,
        name="worker",
        executor=ExecutorSpec(
            config={"harness": harness},
            reasoning_effort=reasoning_effort,
        ),
    )
    return AgentSpec(
        spec_version=1,
        name="parent",
        tools=ToolsConfig(agents=["worker"]),
        sub_agents=[worker],
    )


@pytest.fixture(autouse=True)
def _disable_harness_cli_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests at dispatch constraints rather than host CLI availability."""
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.missing_harness_cli",
        lambda _harness: None,
    )


@pytest.fixture(autouse=True)
def _clean_subagent_state() -> Iterator[None]:
    """Remove runner-local state created by successful dispatches."""
    ordinal_key = (_PARENT_SESSION, "worker")
    previous_ordinal = subagent_work._subagent_ordinal_counters.pop(ordinal_key, None)
    previous_inbox = subagent_work._session_inboxes_ref.pop(_PARENT_SESSION, None)
    try:
        yield
    finally:
        subagent_work.unregister_subagent_work(_CHILD_SESSION)
        subagent_work.unregister_child_session(_CHILD_SESSION)
        subagent_work._session_inboxes_ref.pop(_PARENT_SESSION, None)
        subagent_work._subagent_ordinal_counters.pop(ordinal_key, None)
        if previous_ordinal is not None:
            subagent_work._subagent_ordinal_counters[ordinal_key] = previous_ordinal
        if previous_inbox is not None:
            subagent_work._session_inboxes_ref[_PARENT_SESSION] = previous_inbox


def _new_child_handler(
    *,
    writes: list[tuple[str, dict[str, Any]]],
    child_id: str = _CHILD_SESSION,
) -> Callable[[httpx.Request], httpx.Response]:
    """Serve the minimal parent/child REST exchange for a fresh send."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == f"/v1/sessions/{_PARENT_SESSION}":
            return httpx.Response(
                200,
                json={"id": _PARENT_SESSION, "agent_id": "ag_parent_constraints", "labels": {}},
            )
        if request.method == "GET" and path == f"/v1/sessions/{_PARENT_SESSION}/child_sessions":
            return httpx.Response(200, json={"data": []})
        if request.method == "POST" and path == "/v1/sessions":
            body = json.loads(request.content)
            writes.append((path, body))
            return httpx.Response(201, json={"id": child_id})
        if request.method == "POST" and path == f"/v1/sessions/{child_id}/policies":
            writes.append((path, json.loads(request.content)))
            return httpx.Response(201, json={"ok": True})
        if request.method == "POST" and path == f"/v1/sessions/{child_id}/events":
            writes.append((path, json.loads(request.content)))
            return httpx.Response(202, json={"queued": True})
        raise AssertionError(f"unexpected {request.method} {path}")

    return handler


async def _send_new_child(
    *,
    spec: AgentSpec,
    args: dict[str, Any],
    writes: list[tuple[str, dict[str, Any]]],
) -> str:
    """Dispatch one fresh named child through the real execute_tool path."""
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    handler = _new_child_handler(writes=writes)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as server_client:
        return await execute_tool(
            tool_name="sys_session_send",
            arguments=json.dumps(args),
            server_client=server_client,
            conversation_id=_PARENT_SESSION,
            agent_spec=spec,
            session_inbox=inbox,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("budget", "expected_factory_params"),
    [
        pytest.param(
            {"max_cost_usd": "12.5"},
            {"max_cost_usd": 12.5},
            id="max-only-normalizes-string",
        ),
        pytest.param(
            {"ask_thresholds_usd": ["1", 2.5]},
            {"ask_thresholds_usd": [1.0, 2.5]},
            id="thresholds-only-normalize-values",
        ),
        pytest.param(
            {"max_cost_usd": 20, "ask_thresholds_usd": [5, "10"]},
            {"max_cost_usd": 20.0, "ask_thresholds_usd": [5.0, 10.0]},
            id="combined-budget-normalizes-all-values",
        ),
    ],
)
async def test_cost_budget_is_normalized_and_attached_to_child_policy(
    budget: dict[str, Any],
    expected_factory_params: dict[str, Any],
) -> None:
    """Valid budgets reach the child policy in canonical numeric form."""
    writes: list[tuple[str, dict[str, Any]]] = []
    output = await _send_new_child(
        spec=_parent_spec(),
        args={
            "agent": "worker",
            "title": "budgeted",
            "args": {"input": "do work", "cost_budget": budget},
        },
        writes=writes,
    )

    handle = json.loads(output)
    policy_posts = [body for path, body in writes if path.endswith("/policies")]
    event_posts = [body for path, body in writes if path.endswith("/events")]
    assert handle["conversation_id"] == _CHILD_SESSION
    assert [path for path, _body in writes] == [
        "/v1/sessions",
        f"/v1/sessions/{_CHILD_SESSION}/policies",
        f"/v1/sessions/{_CHILD_SESSION}/events",
    ]
    assert policy_posts == [
        {
            "name": "__subagent_cost_budget",
            "type": "python",
            "handler": "omnigent.policies.builtins.cost.subagent_cost_budget",
            "factory_params": expected_factory_params,
            "enabled": True,
        }
    ]
    assert event_posts[0]["data"]["content"] == [{"type": "input_text", "text": "do work"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "budget",
    [
        pytest.param("cheap", id="budget-not-object"),
        pytest.param({}, id="budget-empty"),
        pytest.param({"max_cost_usd": None}, id="max-missing-value"),
        pytest.param({"max_cost_usd": 0}, id="max-zero"),
        pytest.param({"max_cost_usd": -1}, id="max-negative"),
        pytest.param({"max_cost_usd": "not-a-number"}, id="max-not-numeric"),
        pytest.param({"max_cost_usd": {"amount": 1}}, id="max-wrong-type"),
        pytest.param({"ask_thresholds_usd": "1"}, id="thresholds-not-array"),
        pytest.param({"ask_thresholds_usd": ["not-a-number"]}, id="threshold-not-numeric"),
        pytest.param(
            {"ask_thresholds_usd": [{"amount": 1}]},
            id="threshold-wrong-type",
        ),
        pytest.param({"ask_thresholds_usd": [None]}, id="threshold-null"),
        pytest.param({"ask_thresholds_usd": [0]}, id="threshold-zero"),
        pytest.param(
            {"max_cost_usd": 5, "ask_thresholds_usd": [5]},
            id="threshold-equals-cap",
        ),
        pytest.param(
            {"max_cost_usd": 5, "ask_thresholds_usd": [6]},
            id="threshold-exceeds-cap",
        ),
    ],
)
async def test_invalid_cost_budget_is_rejected_before_server_side_effects(
    budget: object,
) -> None:
    """Malformed or unsafe budgets do not create children or policies."""
    requests_seen = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests_seen
        requests_seen += 1
        raise AssertionError(f"unexpected request for invalid budget: {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_send",
            arguments=json.dumps(
                {
                    "agent": "worker",
                    "title": "invalid-budget",
                    "args": {"input": "do not start", "cost_budget": budget},
                }
            ),
            server_client=server_client,
            conversation_id=_PARENT_SESSION,
            agent_spec=_parent_spec(),
            session_inbox=asyncio.Queue(),
        )

    assert output.startswith("Error: sys_session_send invalid 'cost_budget':")
    assert requests_seen == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "effort",
    [pytest.param(42, id="non-string"), pytest.param("", id="empty-string")],
)
async def test_malformed_reasoning_effort_is_rejected_before_server_side_effects(
    effort: object,
) -> None:
    """Malformed effort metadata fails before any child lookup or create."""
    requests_seen = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests_seen
        requests_seen += 1
        raise AssertionError(f"unexpected request for invalid effort: {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_send",
            arguments=json.dumps(
                {
                    "agent": "worker",
                    "title": "invalid-effort",
                    "args": {"input": "do not start", "reasoning_effort": effort},
                }
            ),
            server_client=server_client,
            conversation_id=_PARENT_SESSION,
            agent_spec=_parent_spec(),
            session_inbox=asyncio.Queue(),
        )

    assert output.startswith("Error: sys_session_send invalid 'reasoning_effort':")
    assert requests_seen == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "effort", "expected"),
    [
        pytest.param("claude-native", "max", "max", id="anthropic-max"),
        pytest.param("codex-native", "ultra", "ultra", id="codex-native-ultra"),
        pytest.param("pi-native", "ultra", "ultra", id="pi-native-ultra"),
        pytest.param("openai-agents", "xhigh", "xhigh", id="openai-xhigh"),
    ],
)
async def test_reasoning_effort_is_validated_and_carried_to_child_create(
    harness: str,
    effort: str,
    expected: str,
) -> None:
    """Supported harness effort vocabularies reach the create request."""
    writes: list[tuple[str, dict[str, Any]]] = []
    output = await _send_new_child(
        spec=_parent_spec(harness),
        args={
            "agent": "worker",
            "title": "effort",
            "args": {"input": "use the requested effort", "reasoning_effort": effort},
        },
        writes=writes,
    )

    assert json.loads(output)["conversation_id"] == _CHILD_SESSION
    create_body = next(body for path, body in writes if path == "/v1/sessions")
    assert create_body["reasoning_effort"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "effort"),
    [
        pytest.param("claude-native", "minimal", id="anthropic-rejects-minimal"),
        pytest.param("antigravity-native", "xhigh", id="gemini-rejects-xhigh"),
        pytest.param("cursor-native", "low", id="cursor-has-no-effort-plumbing"),
    ],
)
async def test_reasoning_effort_is_rejected_for_unsupported_harness(
    harness: str,
    effort: str,
) -> None:
    """Unsupported effort requests fail before child creation."""
    writes: list[tuple[str, dict[str, Any]]] = []
    output = await _send_new_child(
        spec=_parent_spec(harness),
        args={
            "agent": "worker",
            "title": "unsupported-effort",
            "args": {"input": "do not create", "reasoning_effort": effort},
        },
        writes=writes,
    )

    assert output.startswith("Error: invalid 'reasoning_effort'")
    assert writes == []


async def test_spec_default_reasoning_effort_is_carried_to_child_create() -> None:
    """A worker's declared effort applies when the dispatch omits one."""
    writes: list[tuple[str, dict[str, Any]]] = []
    output = await _send_new_child(
        spec=_parent_spec("claude-native", reasoning_effort="high"),
        args={"agent": "worker", "title": "default-effort", "args": "use the default"},
        writes=writes,
    )

    assert json.loads(output)["conversation_id"] == _CHILD_SESSION
    create_body = next(body for path, body in writes if path == "/v1/sessions")
    assert create_body["reasoning_effort"] == "high"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "field"),
    [
        pytest.param("session_id", "cost_budget", id="by-id-cost-budget"),
        pytest.param("session_id", "reasoning_effort", id="by-id-reasoning-effort"),
        pytest.param("named", "cost_budget", id="named-cost-budget"),
        pytest.param("named", "reasoning_effort", id="named-reasoning-effort"),
    ],
)
async def test_existing_child_rejects_budget_and_effort_changes(
    mode: str,
    field: str,
) -> None:
    """Create-time constraints cannot mutate an existing child session."""
    writes: list[tuple[str, dict[str, Any]]] = []
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        path = request.url.path
        if request.method == "GET" and path == f"/v1/sessions/{_PARENT_SESSION}":
            return httpx.Response(
                200,
                json={"id": _PARENT_SESSION, "agent_id": "ag_parent_constraints", "labels": {}},
            )
        if request.method == "GET" and path == f"/v1/sessions/{_PARENT_SESSION}/child_sessions":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": _CHILD_SESSION,
                            "title": "worker:existing",
                            "tool": "worker",
                            "session_name": "existing",
                            "busy": False,
                        }
                    ]
                },
            )
        writes.append((path, json.loads(request.content)))
        return httpx.Response(500, json={"unexpected": path})

    raw_setting: object = {"max_cost_usd": 5} if field == "cost_budget" else "high"
    args: dict[str, Any] = {"input": "continue"}
    args[field] = raw_setting
    tool_args: dict[str, Any] = {
        "args": args,
    }
    if mode == "session_id":
        tool_args["session_id"] = _CHILD_SESSION
    else:
        tool_args.update({"agent": "worker", "title": "existing"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_send",
            arguments=json.dumps(tool_args),
            server_client=server_client,
            conversation_id=_PARENT_SESSION,
            agent_spec=_parent_spec(),
            session_inbox=asyncio.Queue(),
        )

    assert field in output
    assert "existing session" in output or "already exists" in output
    assert not any(method in {"POST", "PATCH"} for method, _path in requests)
    assert writes == []
