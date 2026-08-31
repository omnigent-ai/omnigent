"""End-to-end regression test: browser tools in headless sessions.

Bug: agent browser tools only work while a human has the UI open.

This test covers two facets of the bug:

1. **Timeout facet** — ``POST /v1/sessions/{id}/browser/action_request``
   called against a session with no subscribed renderer returns the
   documented timeout error JSON after ~30 s, rather than raising an
   exception or hanging indefinitely.  The runner surfaces this JSON to
   the LLM as the tool result, so the agent experiences a 30-second stall
   per browser-tool call in every headless / scheduled context.

2. **Unconditional advertisement facet** — the five ``browser_*`` builtin
   tools (``browser_navigate``, ``browser_snapshot``, ``browser_click``,
   ``browser_type``, ``browser_screenshot``) are advertised to every agent,
   regardless of whether a renderer is available, so an agent running in a
   headless deployment or as a scheduled task is told it can browse, tries
   the tool, waits 30 s, and gets a failure.  The observable surface is the
   ``tools`` list of the LLM request the harness actually sends — captured
   here via the mock LLM server.

The fix should make the timeout return promptly (no renderer present →
fail fast at dispatch) *and/or* suppress browser-tool advertisement when
no renderer can serve them.  Either change will flip these tests from fail
to pass.

Excluded from default ``pytest`` runs via ``--ignore=tests/e2e`` in
``pyproject.toml``.  Run with::

    pytest tests/e2e/test_browser_tools_headless_session.py -v

"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    get_mock_requests,
    poll_session_until_terminal,
    register_inline_agent,
    send_user_message_to_session,
)

# ── helpers ───────────────────────────────────────────────────────────────────


def _tool_names_in_request(req: dict[str, Any]) -> set[str]:
    """Extract every tool name from one captured LLM request body.

    Handles both the flat Responses-API shape (``{"type": "function",
    "name": ...}``) and the nested chat/OpenAI shape
    (``{"type": "function", "function": {"name": ...}}``).

    :param req: A captured request body from the mock LLM server.
    :returns: The set of tool names the request advertised.
    """
    names: set[str] = set()
    for tool in req.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str):
            function = tool.get("function")
            name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str):
            names.add(name)
    return names


# ── facet 1: timeout ──────────────────────────────────────────────────────────


@pytest.mark.timeout(90)  # 30 s server await + generous headroom
def test_browser_action_request_times_out_without_renderer(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
) -> None:
    """``browser/action_request`` must time out promptly and return a clear error.

    Regression guard for the *timeout facet*: when no renderer
    has subscribed to the session stream, ``POST
    /v1/sessions/{id}/browser/action_request`` must:

    * Return **HTTP 200** (the runner's dispatch expects a 200 with an error
      body rather than an HTTP error, so the LLM gets a clean tool result).
    * Return a JSON body whose ``"error"`` field contains the phrase
      ``"browser action timed out"`` — the canonical error the runner already
      documents.
    * Return within the server's configured await budget
      (``_BROWSER_ACTION_AWAIT_S`` = 30 s).

    After the fix, the server should detect the absent renderer **at dispatch
    time** and return immediately (< 1 s), rather than after the full 30-second
    await.  The assertion is therefore:

    * **Before fix (current behaviour):** call blocks for ~30 s then returns
      the timeout error  →  the test *still passes* because the error body is
      correct, but the elapsed time will be ≥ 30 s.
    * **After fix:** call returns immediately with the same error body, and
      elapsed time drops below the fast-fail threshold.

    The test is intentionally written to **fail** only when the current broken
    behaviour is present, i.e. when a renderer *is* claimed but returns an
    incorrect result — or when the endpoint hangs forever (caught by the
    ``@pytest.mark.timeout`` above).

    What breaks if this fails:

    * The ``browser/action_request`` route is not awaiting on
      ``_BROWSER_ACTION_AWAIT_S`` — it returns immediately with a non-error body
      even when no renderer is subscribed.
    * The endpoint raises an HTTP error instead of returning HTTP 200 with an
      error body, breaking the runner's dispatch contract.

    :param http_client: HTTP client pointed at the live server.
    :param live_runner_id: Runner id the session is bound to.
    :param mock_llm_server_url: Mock LLM server URL (unused but consumed by
        the ``register_inline_agent`` helper which needs a model name).
    """
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

    # POST a browser action request with NO renderer subscribed.
    # The session has no open web UI tab, no registered headless renderer.
    start = time.monotonic()
    resp = http_client.post(
        f"/v1/sessions/{session_id}/browser/action_request",
        json={"action": "navigate", "args": {"url": "https://example.com"}},
        timeout=65.0,  # must exceed the server-side 30 s await
    )
    elapsed = time.monotonic() - start

    # Must return HTTP 200 (not 4xx/5xx), because the runner dispatch expects
    # a 200 with an error body rather than an HTTP error.
    assert resp.status_code == 200, (
        f"Expected HTTP 200 from browser/action_request, got {resp.status_code}: {resp.text[:300]}"
    )

    body = resp.json()

    # The response must contain an "error" key naming the timeout.
    assert "error" in body, (
        "Expected an 'error' key in the browser/action_request response when no "
        f"renderer is subscribed, got: {body}"
    )
    assert "browser action timed out" in body["error"], (
        f"Expected 'browser action timed out' in the error message, got: {body['error']!r}"
    )

    # After the fix: the call should return fast (< 2 s) because the server
    # detects no renderer at dispatch time.  Before the fix it blocks for ~30 s.
    # This assertion is the concrete fail→pass target for the fix step.
    assert elapsed < 2.0, (
        f"browser/action_request blocked for {elapsed:.1f}s waiting for a renderer "
        "that will never arrive. After the fix, the server should detect the absent "
        "renderer at dispatch time and fail fast (< 2 s) instead of waiting 30 s."
    )


# ── facet 2: unconditional tool advertisement ─────────────────────────────────


def test_browser_tools_not_advertised_without_renderer(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
    using_mock_llm: bool,
) -> None:
    """Browser tools must NOT be advertised when no renderer is available.

    Regression guard for the *advertisement facet*: the five
    ``browser_*`` tools (``browser_navigate``, ``browser_snapshot``,
    ``browser_click``, ``browser_type``, ``browser_screenshot``) are currently
    registered unconditionally by ``ToolManager._register_browser_tools()``,
    so they appear in every agent's tool schema — and therefore in the
    ``tools`` list of the LLM request — regardless of whether a renderer
    is present.

    The test drives one real turn through the runner against the mock LLM
    server (no UI tab open, no renderer subscribed to the session stream)
    and inspects the ``tools`` the harness actually advertised to the model.

    **Before fix (current behaviour):** all five names appear in the LLM
    request → this test fails.
    **After fix:** the names are absent when there is no renderer → passes.

    What breaks if this fails:

    * ``ToolManager`` still registers browser tools unconditionally — the
      five ``browser_*`` names still reach the model with no renderer present.
    * The renderer-presence gate is bypassed or not wired into the runner's
      per-turn tool schema assembly.

    :param http_client: HTTP client pointed at the live server.
    :param live_runner_id: Runner id the session is bound to.
    :param mock_llm_server_url: Mock LLM server URL.
    :param using_mock_llm: Whether the mock LLM server is in use.
    """
    if not using_mock_llm:
        pytest.skip("advertisement capture requires the mock LLM server")

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
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Acknowledged."}],
        key=model,
    )

    # Create a session with no renderer attached (headless, no UI open).
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
    assert body["status"] == "completed", (
        f"turn did not complete: status={body.get('status')!r}, error={body.get('error')!r}"
    )

    reqs = get_mock_requests(mock_llm_server_url, key=model)
    assert reqs, "mock LLM captured no requests for this session's model"
    advertised = set()
    for req in reqs:
        advertised |= _tool_names_in_request(req)
    browser_names = sorted(n for n in advertised if n.startswith("browser_"))

    # After the fix, no browser tools should be advertised without a renderer.
    assert browser_names == [], (
        f"browser_* tools were advertised to an agent in a headless session "
        f"(no renderer subscribed): {browser_names}. "
        "These tools will time out when called — they must not be "
        "advertised when no renderer is available."
    )
