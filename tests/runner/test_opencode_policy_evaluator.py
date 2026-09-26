"""Unit tests for the OpenCode permission policy evaluator wiring.

The runner wires this evaluator into the OpenCode permission forwarder so
every ``permission.asked`` request is decided by the SAME server-side
policy/approval gate codex-native uses (``POST /policies/evaluate``), not
silently auto-approved. These tests pin the request shape, the verdict
mapping, and — critically — that every failure mode fails CLOSED.
"""

from __future__ import annotations

import json as _json
from typing import Any

import httpx

from omnigent.runner.app import _build_opencode_policy_evaluator


class _FakeServerClient:
    """httpx-shaped stub recording the policy-evaluate POST."""

    def __init__(
        self,
        *,
        status: int = 200,
        body: dict[str, Any] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._status = status
        self._body = body
        self._raise_exc = raise_exc
        self.calls: list[tuple[str, dict[str, Any], Any]] = []

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> httpx.Response:
        self.calls.append((url, json, timeout))
        if self._raise_exc is not None:
            raise self._raise_exc
        content = b"" if self._body is None else _json.dumps(self._body).encode()
        return httpx.Response(self._status, content=content, request=httpx.Request("POST", url))


async def test_evaluator_posts_tool_call_event_and_maps_allow() -> None:
    """ALLOW maps to ``allow``; the POST carries the v2 action as the tool name."""
    client = _FakeServerClient(body={"result": "POLICY_ACTION_ALLOW"})
    evaluate = _build_opencode_policy_evaluator(
        server_client=client,  # type: ignore[arg-type]
        conversation_id="conv_1",
    )
    verdict = await evaluate(
        {"action": "shell", "command": "ls", "path": None, "url": None, "metadata": {}}
    )
    assert verdict == {"decision": "allow"}
    url, body, _timeout = client.calls[0]
    assert url == "/v1/sessions/conv_1/policies/evaluate"
    event = body["event"]
    assert event["type"] == "PHASE_TOOL_CALL"
    assert event["data"]["name"] == "shell"
    assert event["data"]["arguments"] == {"command": "ls"}
    assert event["context"] == {"harness": "opencode-native"}


async def test_evaluator_maps_deny_and_ask() -> None:
    """DENY → ``deny``; ASK → ``ask`` (the forwarder fails an unresolved ask closed)."""
    for action, decision in (("POLICY_ACTION_DENY", "deny"), ("POLICY_ACTION_ASK", "ask")):
        client = _FakeServerClient(body={"result": action})
        evaluate = _build_opencode_policy_evaluator(
            server_client=client,  # type: ignore[arg-type]
            conversation_id="c",
        )
        verdict = await evaluate({"action": "edit"})
        assert verdict == {"decision": decision}


async def test_evaluator_maps_unknown_verdict_to_ask() -> None:
    """An unrecognized verdict fails closed (``ask`` → reject downstream)."""
    client = _FakeServerClient(body={"result": "POLICY_ACTION_SOMETHING_NEW"})
    evaluate = _build_opencode_policy_evaluator(
        server_client=client,  # type: ignore[arg-type]
        conversation_id="c",
    )
    assert (await evaluate({"action": "bash"})) == {"decision": "ask"}


async def test_evaluator_fails_closed_on_transport_error() -> None:
    client = _FakeServerClient(raise_exc=httpx.ConnectError("boom"))
    evaluate = _build_opencode_policy_evaluator(
        server_client=client,  # type: ignore[arg-type]
        conversation_id="c",
    )
    assert (await evaluate({"action": "bash"})) == {"decision": "deny"}


async def test_evaluator_fails_closed_on_non_200_or_empty_body() -> None:
    for status, body in ((500, {"result": "POLICY_ACTION_ALLOW"}), (200, None)):
        client = _FakeServerClient(status=status, body=body)
        evaluate = _build_opencode_policy_evaluator(
            server_client=client,  # type: ignore[arg-type]
            conversation_id="c",
        )
        assert (await evaluate({"action": "bash"})) == {"decision": "deny"}


async def test_evaluator_posts_normalized_arguments() -> None:
    """v2 action arguments (e.g. a grep ``pattern``) reach the policy engine."""
    client = _FakeServerClient(body={"result": "POLICY_ACTION_ALLOW"})
    evaluate = _build_opencode_policy_evaluator(
        server_client=client,  # type: ignore[arg-type]
        conversation_id="conv_1",
    )
    await evaluate(
        {
            "action": "grep",
            "arguments": {"pattern": "secret", "path": "src"},
            "command": None,
            "path": "src",
            "url": None,
            "metadata": {},
        }
    )
    _url, body, _timeout = client.calls[0]
    assert body["event"]["data"] == {
        "name": "grep",
        "arguments": {"pattern": "secret", "path": "src"},
    }


async def test_evaluator_names_v2_shell_action() -> None:
    client = _FakeServerClient(body={"result": "POLICY_ACTION_ALLOW"})
    evaluate = _build_opencode_policy_evaluator(
        server_client=client,  # type: ignore[arg-type]
        conversation_id="c",
    )
    await evaluate({"action": "shell", "arguments": {"command": "ls"}, "metadata": {}})
    assert client.calls[0][1]["event"]["data"] == {"name": "shell", "arguments": {"command": "ls"}}


def test_evaluator_docstring_describes_v2_permission_flow() -> None:
    doc = _build_opencode_policy_evaluator.__doc__ or ""
    assert "permission.v2.asked" not in doc
    assert "always" not in doc
    assert "permission.asked" in doc
