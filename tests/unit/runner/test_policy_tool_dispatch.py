"""Tests for sys_add_policy and sys_policy_registry tool dispatch."""

from __future__ import annotations

import json

import pytest

from omnigent.runner.tool_dispatch import _execute_policy_tool

# ── Helpers ──────────────────────────────────────────────────────


class _FakePostResponse:
    """Minimal httpx response stub for POST."""

    status_code = 200

    def json(self) -> dict[str, object]:
        """Return a policy creation response."""
        return {
            "id": "pol_abc123",
            "name": "block_shell",
            "type": "python",
            "handler": "omnigent.policies.builtins.cel.cel_policy",
            "enabled": True,
        }

    @property
    def text(self) -> str:
        """Return the JSON body as text."""
        return json.dumps(self.json())


class _FakePostClient:
    """Minimal httpx.AsyncClient stub that records POST calls."""

    def __init__(self) -> None:
        self.post_calls: list[tuple[str, dict[str, object]]] = []

    async def post(
        self,
        url: str,
        json: dict[str, object] | None = None,
        timeout: float = 30.0,
    ) -> _FakePostResponse:
        """Record the call and return a success response."""
        self.post_calls.append((url, json or {}))
        return _FakePostResponse()


# ── sys_add_policy ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_policy_cel() -> None:
    """CEL handler + factory_params forwarded correctly."""
    client = _FakePostClient()
    result = await _execute_policy_tool(
        "sys_add_policy",
        json.dumps(
            {
                "name": "block_shell",
                "handler": "omnigent.policies.builtins.cel.cel_policy",
                "factory_params": {
                    "expression": 'event.type == "tool_call" && event.data.name == "sys_os_shell"',
                    "reason": "Shell blocked.",
                },
            }
        ),
        conversation_id="conv_test",
        server_client=client,  # type: ignore[arg-type]
    )

    parsed = json.loads(result)
    assert parsed["policy_id"] == "pol_abc123"
    assert "successfully" in parsed["message"]

    url, body = client.post_calls[0]
    assert url == "/v1/sessions/conv_test/policies"
    assert body["type"] == "python"
    assert body["handler"] == "omnigent.policies.builtins.cel.cel_policy"
    assert body["factory_params"]["expression"] == (
        'event.type == "tool_call" && event.data.name == "sys_os_shell"'
    )


@pytest.mark.asyncio
async def test_add_policy_builtin() -> None:
    """Builtin handler + factory_params forwarded as-is."""
    client = _FakePostClient()
    await _execute_policy_tool(
        "sys_add_policy",
        json.dumps(
            {
                "name": "rate_limit",
                "handler": "omnigent.policies.builtins.safety.max_tool_calls_per_session",
                "factory_params": {"limit": 50},
            }
        ),
        conversation_id="conv_test",
        server_client=client,  # type: ignore[arg-type]
    )
    _, body = client.post_calls[0]
    assert body["handler"] == "omnigent.policies.builtins.safety.max_tool_calls_per_session"
    assert body["factory_params"] == {"limit": 50}


@pytest.mark.asyncio
async def test_add_policy_callable_no_factory_params() -> None:
    """Callable handler (no factory_params) forwarded without the key."""
    client = _FakePostClient()
    await _execute_policy_tool(
        "sys_add_policy",
        json.dumps(
            {
                "name": "ask_os",
                "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
            }
        ),
        conversation_id="conv_test",
        server_client=client,  # type: ignore[arg-type]
    )
    _, body = client.post_calls[0]
    assert "factory_params" not in body


@pytest.mark.asyncio
async def test_add_policy_requires_handler() -> None:
    """Missing handler returns an error."""
    client = _FakePostClient()
    result = await _execute_policy_tool(
        "sys_add_policy",
        json.dumps({"name": "bad"}),
        conversation_id="conv_test",
        server_client=client,  # type: ignore[arg-type]
    )
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_add_policy_no_server() -> None:
    """Returns error when server_client is None."""
    result = await _execute_policy_tool(
        "sys_add_policy",
        "{}",
        conversation_id="conv_test",
        server_client=None,
    )
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_add_policy_no_session() -> None:
    """Returns error when conversation_id is None."""
    result = await _execute_policy_tool(
        "sys_add_policy",
        "{}",
        conversation_id=None,
        server_client=_FakePostClient(),  # type: ignore[arg-type]
    )
    assert "error" in json.loads(result)


# ── sys_policy_registry ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_policy_registry_returns_entries() -> None:
    """sys_policy_registry proxies the policy registry endpoint."""

    class _FakeGetResponse:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {
                "object": "list",
                "data": [
                    {
                        "handler": "omnigent.policies.builtins.cel.cel_policy",
                        "kind": "factory",
                        "name": "CEL Expression Policy",
                        "description": "Evaluate a CEL expression...",
                        "params_schema": {"type": "object"},
                    },
                ],
            }

    class _FakeGetClient:
        def __init__(self) -> None:
            self.get_calls: list[str] = []

        async def get(self, url: str, timeout: float = 30.0) -> _FakeGetResponse:
            self.get_calls.append(url)
            return _FakeGetResponse()

    client = _FakeGetClient()
    result = await _execute_policy_tool(
        "sys_policy_registry",
        "{}",
        conversation_id=None,
        server_client=client,  # type: ignore[arg-type]
    )
    parsed = json.loads(result)
    assert len(parsed["policies"]) == 1
    assert parsed["policies"][0]["handler"] == "omnigent.policies.builtins.cel.cel_policy"
    assert client.get_calls == ["/v1/policy-registry"]


@pytest.mark.asyncio
async def test_policy_registry_no_server() -> None:
    """Returns error when server_client is None."""
    result = await _execute_policy_tool(
        "sys_policy_registry",
        "{}",
        conversation_id=None,
        server_client=None,
    )
    assert "error" in json.loads(result)
