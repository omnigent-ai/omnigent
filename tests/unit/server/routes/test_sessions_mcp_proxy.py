"""Unit tests for the AP-server MCP proxy error handling in sessions routes."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from omnigent.runner.routing import RoutedRunner
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
