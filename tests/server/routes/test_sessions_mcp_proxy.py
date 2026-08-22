"""Unit tests for the AP-server MCP proxy error handling in sessions routes."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from omnigent.runner.routing import RoutedRunner
from omnigent.server.routes._sessions import helpers as _helpers_mod
from omnigent.server.routes.sessions import _handle_mcp_tools_list


class _RaisingRunnerClient:
    """Runner HTTP client stub whose POST always fails with a leaky error.

    The error text embeds an internal-looking host so the test can prove it
    does NOT survive into the client-facing JSON-RPC response.
    """

    raw_error = "Connection to internal-runner-host:9443 failed"

    async def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        """Raise a transport error carrying sensitive text.

        :returns: Never returns.
        :raises httpx.ConnectError: Always.
        """
        raise httpx.ConnectError(self.raw_error)


class _RaisingRunnerRouter:
    """RunnerRouter stub that hands back a client whose POST raises."""

    def client_for_session_resources(self, conversation_id: str) -> RoutedRunner:
        """Return a routed runner whose client fails on use.

        :param conversation_id: Ignored session id.
        :returns: A :class:`RoutedRunner` wrapping the raising client.
        """
        del conversation_id
        return RoutedRunner(
            runner_id="runner_test",
            client=_RaisingRunnerClient(),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_mcp_tools_list_runner_failure_is_genericized(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A runner MCP failure returns a fixed message, not the raw exception.

    The ``tools/list`` proxy delegates to the runner's ``/mcp/execute``. When
    that call raises, the JSON-RPC error returned to the caller must carry the
    fixed string ``"Runner MCP execute failed."`` and MUST NOT include the raw
    transport error (which can embed internal hosts). The raw cause must still
    be logged for operators. A failure here means the log-and-genericize
    contract for the AP-server MCP error path regressed.

    :param caplog: Pytest log capture fixture.
    """
    with caplog.at_level(logging.WARNING, logger="omnigent.server.routes.sessions"):
        response = await _handle_mcp_tools_list(
            rpc_id=7,
            session_id="conv_test",
            runner_router=_RaisingRunnerRouter(),  # type: ignore[arg-type]
        )

    payload = json.loads(bytes(response.body))
    # JSON-RPC envelope is preserved (id echoed, application error code).
    assert payload["id"] == 7
    assert payload["error"]["code"] == -32000
    # The client-facing message is the fixed generic string...
    assert payload["error"]["message"] == "Runner MCP execute failed."
    # ...and the raw transport detail (internal host) is absent from it.
    assert _RaisingRunnerClient.raw_error not in json.dumps(payload)
    # ...but IS logged server-side for operators (the other half of the
    # contract — if missing, the failure has no diagnostic record).
    assert _RaisingRunnerClient.raw_error in caplog.text


class _StubRunnerClient:
    """Runner HTTP client stub returning a fixed ``tools/list`` result."""

    def __init__(self, failures: dict[str, str]) -> None:
        self._failures = failures

    async def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        """Return a canned ``tools/list`` result carrying *failures*.

        :returns: An ``httpx.Response`` whose JSON body mirrors the
            runner's ``/mcp/execute`` response shape.
        """
        return httpx.Response(
            200,
            json={
                "result": {
                    "schemas": [],
                    "tool_names": [],
                    "failures": self._failures,
                }
            },
            request=httpx.Request("POST", "http://runner.test/v1/sessions/conv/mcp/execute"),
        )


class _StubRunnerRouter:
    """RunnerRouter stub that hands back a client with a canned response."""

    def __init__(self, failures: dict[str, str]) -> None:
        self._failures = failures

    def client_for_session_resources(self, conversation_id: str) -> RoutedRunner:
        """Return a routed runner whose client returns canned failures.

        :param conversation_id: Ignored session id.
        :returns: A :class:`RoutedRunner` wrapping the stub client.
        """
        del conversation_id
        return RoutedRunner(
            runner_id="runner_test",
            client=_StubRunnerClient(self._failures),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_mcp_tools_list_failure_publishes_startup_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner ``tools/list`` failure is published as a ``session.mcp_startup``
    event, not just logged.

    This is the runner/claude-sdk-path counterpart to the native-harness
    ``external_mcp_startup`` publish: before this wiring, a bad PipesHub MCP
    token failed every turn with nothing but a server log line, and the web
    UI's ``McpStartupIndicator`` band never lit up. A failure here means that
    regressed back to log-only.
    """
    published: list[dict[str, object]] = []

    class _RecordingStream:
        @staticmethod
        def publish(conversation_id: str, event: dict[str, object]) -> None:
            published.append({"_conversation_id": conversation_id, **event})

    monkeypatch.setattr(_helpers_mod, "session_stream", _RecordingStream)
    monkeypatch.setattr(_helpers_mod, "_session_mcp_startup_cache", {})

    session_id = "conv_pipeshub_bad_token"
    response = await _handle_mcp_tools_list(
        rpc_id=1,
        session_id=session_id,
        runner_router=_StubRunnerRouter({"pipeshub": "401 Unauthorized"}),  # type: ignore[arg-type]
    )

    # The JSON-RPC response itself stays a plain tools/list result — no new
    # fields grafted onto it; failure visibility rides the SSE event instead.
    payload = json.loads(bytes(response.body))
    assert payload["result"]["tools"] == []

    assert len(published) == 1
    event = published[0]
    assert event["_conversation_id"] == session_id
    assert event["type"] == "session.mcp_startup"
    assert event["servers"]["pipeshub"]["status"] == "failed"
    assert event["servers"]["pipeshub"]["error"] == "401 Unauthorized"


@pytest.mark.asyncio
async def test_mcp_tools_list_recovery_clears_startup_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server that stops failing is republished as ``ready``, so a token
    refresh clears the web UI band instead of leaving a stale error."""
    published: list[dict[str, object]] = []

    class _RecordingStream:
        @staticmethod
        def publish(conversation_id: str, event: dict[str, object]) -> None:
            published.append({"_conversation_id": conversation_id, **event})

    monkeypatch.setattr(_helpers_mod, "session_stream", _RecordingStream)
    monkeypatch.setattr(_helpers_mod, "_session_mcp_startup_cache", {})

    session_id = "conv_pipeshub_recovers"
    await _handle_mcp_tools_list(
        rpc_id=1,
        session_id=session_id,
        runner_router=_StubRunnerRouter({"pipeshub": "401 Unauthorized"}),  # type: ignore[arg-type]
    )
    await _handle_mcp_tools_list(
        rpc_id=2,
        session_id=session_id,
        runner_router=_StubRunnerRouter({}),  # type: ignore[arg-type]
    )

    assert len(published) == 2
    recovered = published[1]
    assert recovered["servers"]["pipeshub"]["status"] == "ready"
    assert recovered["servers"]["pipeshub"]["error"] is None
    assert session_id not in _helpers_mod._session_mcp_startup_cache
