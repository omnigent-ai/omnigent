"""Mock-LLM integration coverage for the client-side tool SSE-status
and dispatch-routing invariants.

Re-homes two suppressed ``/v1/responses`` e2e tests (the route was
removed) at the mock-LLM sessions-API layer, the same path
:mod:`tests.integration.test_d6_async_cancel_round_trip` and
:mod:`tests.integration.test_client_tools` prove works in mock mode:

1. ``test_client_side_tool_inline_sse_carries_action_required`` —
   re-homes ``tests/e2e/test_client_tool_sse_status_e2e.py``. A
   request-supplied (client-side) tool call surfaces on the live
   stream's ``response.output_item.done`` event with
   ``status="action_required"`` — the ``ToolCallInProgress.is_client_side``
   plumbing the original e2e pinned. After the test posts the
   ``function_call_output`` (the sessions-API replacement for the
   removed ``PATCH /v1/responses`` ``tool_results``), the sentinel
   round-trips into the text deltas and the turn completes cleanly.

2. ``test_request_supplied_client_tool_result_reaches_model`` —
   re-homes ``tests/e2e/test_tool_dispatch_workflow_client_side_e2e.py``.
   A request-supplied client-side tool routes through the
   client-side dispatch branch (not the ``unknown server-side tool``
   error envelope): the posted sentinel reaches the model verbatim
   in the final answer. Same client-tool round-trip the dispatch
   workflow's ``_is_parent_client_side_tool`` branch must serve.

The mock LLM is scripted with a fixed tool-call sequence, so the
agent prompt is irrelevant — the queued responses drive the turn.

Runs in the default suite in mock mode (no ``--llm-api-key``); the
``tests/integration`` package gate is lifted in mock mode by
``tests/integration/conftest.py``.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import configure_mock_llm, send_user_message_to_session
from tests.integration.conftest import JourneySession

# The mock LLM is scripted with a fixed tool-call sequence, so these
# tests cannot run against a real LLM (it would 401 on the mock base
# URL and could never reproduce the scripted call_ids/markers). The
# central gate in ``tests/integration/conftest.py`` skips ``mock_only``
# tests when a real ``--llm-api-key`` is supplied.
pytestmark = pytest.mark.mock_only

_LOOKUP_SECRET_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "lookup_secret_token",
        "description": (
            "Look up the unguessable secret token for today. The user "
            "does not know it; you MUST call this tool to retrieve it. "
            "Return the token verbatim."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}


def _iter_sse(response: httpx.Response) -> Iterator[dict[str, Any]]:
    """Yield decoded SSE event dicts from a streaming response; stops at [DONE].

    :param response: Open streaming response from
        ``GET /v1/sessions/{id}/stream``.
    """
    buffer = ""
    for chunk in response.iter_text():
        buffer += chunk
        while "\n\n" in buffer:
            frame, _, buffer = buffer.partition("\n\n")
            data_line = next(
                (line for line in frame.splitlines() if line.startswith("data:")), None
            )
            if data_line is None:
                continue
            payload = data_line[len("data:") :].strip()
            if payload == "[DONE]":
                return
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                continue


def _run_client_tool_turn(
    live_server: str,
    sid: str,
    *,
    sentinel: str,
) -> tuple[bool, str, str]:
    """Drive one scripted client-tool round-trip over the session stream.

    Streams the turn, waits for the inline
    ``response.output_item.done`` function_call to park as
    ``action_required``, posts the ``function_call_output`` carrying
    *sentinel*, and assembles the final text from the deltas.

    The tool call_id is read off the stream item (not passed in) — the
    server stamps it, and the ``function_call_output`` must echo that
    exact id.

    :param live_server: Base URL of the live server.
    :param sid: Runner-bound session id.
    :param sentinel: Marker posted as the tool result; the scripted
        final response echoes it so its presence proves the
        round-trip.
    :returns: ``(saw_action_required, terminal_event_type, final_text)``
        where ``saw_action_required`` is True iff the client-side
        function_call surfaced on the stream with
        ``status="action_required"``.
    """
    errors: list[Exception] = []
    text_chunks: list[str] = []
    saw_action_required = False
    terminal: str = ""

    def _post_message() -> None:
        try:
            with httpx.Client(base_url=live_server, timeout=30) as poster:
                send_user_message_to_session(
                    poster,
                    session_id=sid,
                    content="Look up today's secret token and reply with it verbatim.",
                    tools=[_LOOKUP_SECRET_TOOL],
                )
        except Exception as exc:  # thread boundary; re-raised below
            errors.append(exc)

    def _post_output(cid: str) -> None:
        try:
            with httpx.Client(base_url=live_server, timeout=30) as poster:
                resp = poster.post(
                    f"/v1/sessions/{sid}/events",
                    json={
                        "type": "function_call_output",
                        "data": {"call_id": cid, "output": sentinel},
                    },
                )
                assert resp.status_code in (200, 202), (
                    f"function_call_output POST failed: {resp.status_code} {resp.text[:300]}"
                )
        except Exception as exc:  # thread boundary; re-raised below
            errors.append(exc)

    with httpx.Client(base_url=live_server, timeout=90) as streamer:
        with streamer.stream("GET", f"/v1/sessions/{sid}/stream") as response:
            response.raise_for_status()
            posted = False
            answered = False
            for event in _iter_sse(response):
                if not posted:
                    threading.Thread(target=_post_message, daemon=True).start()
                    posted = True
                etype = event.get("type")
                if etype == "response.output_item.done":
                    item = event.get("item") or {}
                    # The function_call surfaces first as ``in_progress``
                    # and then again as ``action_required`` once it parks
                    # waiting for the client result. The action_required
                    # emission is the load-bearing signal — match it
                    # exactly (mirrors test_client_tool_round_trip) and
                    # post the output only then.
                    if (
                        item.get("type") == "function_call"
                        and item.get("name") == "lookup_secret_token"
                        and item.get("status") == "action_required"
                        and not answered
                    ):
                        saw_action_required = True
                        answered = True
                        threading.Thread(
                            target=_post_output, args=(item["call_id"],), daemon=True
                        ).start()
                elif etype == "response.output_text.delta":
                    delta = event.get("delta")
                    if isinstance(delta, str):
                        text_chunks.append(delta)
                elif etype in ("response.completed", "response.failed", "response.cancelled"):
                    terminal = etype
                    break

    if errors:
        raise errors[0]
    return saw_action_required, terminal, "".join(text_chunks)


def test_client_side_tool_inline_sse_carries_action_required(
    live_server: str,
    journey_session: JourneySession,
    mock_llm_server_url: str | None,
) -> None:
    """The inline function_call SSE event for a client-side tool
    carries ``status="action_required"``.

    Turn script (mock LLM queue):
      1. ``lookup_secret_token`` tool call → the server publishes it
         on the live stream as a function_call ``output_item.done``.
      2. The test posts a ``function_call_output`` with a sentinel.
      3. The model emits the sentinel as its final answer.

    Pins the ``ToolCallInProgress.is_client_side`` plumbing: the
    inline event's ``status`` MUST be ``action_required`` (not a
    server-side terminal status), the posted result round-trips into
    the text deltas, and the turn completes cleanly.
    """
    sentinel = f"DBX-SSE-STATUS-{uuid.uuid4().hex[:8]}-CANARY"
    call_id = f"call_{uuid.uuid4().hex[:8]}"
    sid = journey_session.session_id

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": call_id,
                        "name": "lookup_secret_token",
                        "arguments": json.dumps({}),
                    }
                ]
            },
            {"text": f"The secret token is {sentinel}."},
        ],
    )

    saw_action_required, terminal, final_text = _run_client_tool_turn(
        live_server, sid, sentinel=sentinel
    )

    assert saw_action_required, (
        "client-side tool function_call never surfaced on the stream with "
        "status='action_required'; the ToolCallInProgress.is_client_side "
        "plumbing did not publish the inline action_required status"
    )
    assert terminal == "response.completed", (
        f"turn should complete cleanly after the client-tool round-trip; "
        f"ended {terminal!r} with text {final_text!r}"
    )
    assert sentinel in final_text, (
        f"posted tool-output sentinel should round-trip into the reply "
        f"deltas; expected {sentinel!r} in {final_text!r}"
    )


def test_request_supplied_client_tool_result_reaches_model(
    live_server: str,
    journey_session: JourneySession,
    mock_llm_server_url: str | None,
) -> None:
    """A request-supplied client-side tool's posted result reaches the
    model verbatim — not an ``unknown server-side tool`` error envelope.

    Turn script (mock LLM queue):
      1. ``lookup_secret_token`` (a request-supplied client-side
         tool, NOT a builtin) tool call.
      2. The test posts a ``function_call_output`` with a sentinel.
      3. The model echoes the sentinel verbatim.

    Re-homes the dispatch-workflow client-side routing invariant: a
    tool present only in the per-turn ``tools`` list must route
    through the client-side dispatch branch and park as
    ``action_required`` rather than falling through to the
    ``unknown server-side tool`` error. The verbatim sentinel in the
    final answer proves the posted result threaded back into the
    model's context.
    """
    sentinel = f"DBX-CLIENT-TOOL-{uuid.uuid4().hex[:12]}-CANARY"
    call_id = f"call_{uuid.uuid4().hex[:8]}"
    sid = journey_session.session_id

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": call_id,
                        "name": "lookup_secret_token",
                        "arguments": json.dumps({}),
                    }
                ]
            },
            {"text": f"Token: {sentinel}"},
        ],
    )

    saw_action_required, terminal, final_text = _run_client_tool_turn(
        live_server, sid, sentinel=sentinel
    )

    # A request-supplied client tool must park (action_required), not
    # resolve server-side. A regression that dropped the client-side
    # routing branch would surface the tool as an error envelope and
    # the call would never reach action_required on the stream.
    assert saw_action_required, (
        "request-supplied client tool never parked as 'action_required' "
        "on the stream; the client-side dispatch branch did not fire (an "
        "error envelope here means the call fell through to the "
        "unknown-server-side-tool path)"
    )
    assert terminal == "response.completed", (
        f"turn should complete after the client-tool round-trip; "
        f"ended {terminal!r} with text {final_text!r}"
    )
    assert sentinel in final_text, (
        f"posted client-tool result should reach the model verbatim; "
        f"expected {sentinel!r} in {final_text!r}. Absence means the "
        f"result did not thread back into the model's context."
    )
