"""Browser tools must not be offered to sessions with only non-renderer viewers.

An agent running in a headless sandbox is prompted to approve
``browser_navigate`` (via the omnigent MCP server), but approving leads
nowhere: the browser tool cannot succeed without a browser-capable
renderer. A headless sandbox has no embedded browser at all, so the
user is asked to grant a permission that can never produce a working action.

Root cause: the browser tools are offered to the agent whenever a session
merely *has* a stream subscriber. The server computes
``browser_renderer_available = session_stream.has_subscribers(session_id)``
and the runner only drops the ``browser_*`` schemas for a turn when that is
``False``. As the server itself notes, "any stream subscriber counts (the
protocol has no renderer-capability registration), so a non-renderer viewer
keeps tools advertised" -- a plain web tab, a monitor, or a sandbox observer
that can never claim a browser action still flips the hint to ``True``. The
native-harness relay (``build_native_relay_tool_schemas``, the surface a
native Codex CLI is offered) goes further and advertises the browser tools
unconditionally. In both cases the offering ignores whether a
browser-capable renderer is actually present, so the agent calls the tool,
the user is prompted, and the action dead-ends.

This test drives the request-harness manifestation of that gap
deterministically against a live server + runner: a runner-bound session in a
headless sandbox (no browser renderer) with only a plain SSE viewer attached
still advertises the five ``browser_*`` tools, and a browser action against
that session dead-ends with ``no browser renderer is connected``.

The regression assertion encodes the EXPECTED (fixed) behavior -- a browser
tool that cannot succeed must not be advertised to the agent -- so it FAILS
on the buggy build (the tools are advertised) and PASSES once the offering
reflects real renderer capability. The complementary
``test_browser_tools_headless_session.py`` already covers the no-subscriber
case; this test covers the sandbox case where a viewer is present but no
renderer is.
"""

from __future__ import annotations

import threading
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
    """Collect advertised tool names from a captured mock-LLM request.

    Handles both the flat (``{"name": ...}``) and nested OpenAI
    (``{"function": {"name": ...}}``) tool-schema shapes.
    """
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


class _StreamViewer:
    """Hold ``GET /v1/sessions/{id}/stream`` open in a background thread.

    Registering as a live subscriber is what flips the server's
    ``has_subscribers(session_id)`` -- and therefore the
    ``browser_renderer_available`` hint sent to the runner -- to ``True``,
    exactly like a plain (non-renderer) viewer watching a sandbox session.
    The viewer waits for the stream's ``ready`` heartbeat so the caller
    knows the subscriber slot is registered before it dispatches a turn.
    """

    def __init__(self, base_url: str, session_id: str) -> None:
        self._base_url = base_url
        self._session_id = session_id
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            with httpx.Client(base_url=self._base_url, timeout=30.0) as client:
                with client.stream(
                    "GET",
                    f"/v1/sessions/{self._session_id}/stream",
                ) as response:
                    response.raise_for_status()
                    for _line in response.iter_lines():
                        # The first bytes across the wire are the ``ready``
                        # heartbeat, emitted right after the subscriber slot
                        # is registered -- that is our "subscribed" signal.
                        self._ready.set()
                        if self._stop.is_set():
                            return
        except (httpx.HTTPError, RuntimeError):
            # A transport error still means we tried; unblock the waiter so
            # the test proceeds and asserts on captured request state.
            self._ready.set()

    def __enter__(self) -> _StreamViewer:
        self._thread.start()
        if not self._ready.wait(timeout=20.0):
            raise AssertionError("session stream subscriber never became ready")
        # Small settle so has_subscribers() is observed by the turn dispatch.
        time.sleep(0.5)
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)


@pytest.mark.timeout(180)
def test_browser_tools_advertised_to_nonrenderer_viewer(
    live_server: str,
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
    using_mock_llm: bool,
) -> None:
    """A sandbox session with only a non-renderer viewer must not be offered
    browser tools it can never serve."""
    if not using_mock_llm:
        pytest.skip("advertisement capture requires the mock LLM server")
    assert mock_llm_server_url is not None

    model = f"mock-nonrenderer-viewer-{uuid.uuid4().hex[:6]}"
    agent_name = register_inline_agent(
        http_client,
        name=f"nonrenderer-viewer-{uuid.uuid4().hex[:6]}",
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

    # A plain viewer subscribes to the session stream: no embedded browser,
    # no renderer that can claim a browser action -- just someone watching.
    with _StreamViewer(live_server, session_id):
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

        # The browser action a user would reach after approving is a
        # dead-end even with the viewer attached: no renderer can serve it.
        action = http_client.post(
            f"/v1/sessions/{session_id}/browser/action_request",
            json={"action": "navigate", "args": {"url": "https://example.com"}},
            timeout=10.0,
        )
        assert action.status_code == 200, action.text
        assert action.json() == {"error": "no browser renderer is connected"}

    advertised = set().union(
        *(
            _tool_names_in_request(request)
            for request in get_mock_requests(mock_llm_server_url, key=model)
        )
    )
    # Sanity: we captured a real tool-bearing request from the turn.
    assert "load_skill" in advertised, "expected captured framework tool schemas"

    # Regression: a browser tool that cannot succeed in this sandbox (only a
    # non-renderer viewer is present) must not be offered to the agent --
    # offering it is what prompts the user for a dead-end permission. This
    # FAILS on the buggy build (browser tools advertised whenever
    # has_subscribers() is True) and PASSES once the offering reflects real
    # renderer capability.
    offered_browser_tools = sorted(advertised & BROWSER_TOOL_NAMES)
    assert not offered_browser_tools, (
        "browser tools were advertised to a sandbox session whose only "
        "stream subscriber is a non-renderer viewer; a browser tool that "
        f"cannot be served must not be offered. Advertised: {offered_browser_tools}"
    )
