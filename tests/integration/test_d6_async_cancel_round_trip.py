"""Mock-LLM integration coverage for the D6 server->client round-trip.

Re-homes the suppressed D6 e2e coverage (which targeted the removed
``POST /v1/responses`` route and a real LLM) at the mock-LLM
sessions-API integration layer. Drives the real ``omnigent server``
+ runner + harness over the sessions stream/events surface — the
same path :mod:`tests.integration.test_client_tools` proves works in
mock mode.

Two surfaces this file pins, neither covered before (only the SSE
*parser* was unit-tested):

1. ``test_client_tool_round_trip`` — a client-side (action_required)
   tool call is dispatched on the stream, the test posts the
   ``function_call_output``, the model emits a final answer, and the
   turn reaches a clean ``response.completed`` terminal. The full
   server->client round-trip.

2. ``test_direct_cancel_parks_then_interrupts_cleanly`` — a direct
   cancel (``interrupt`` event) issued while a client-tool call is
   parked must drive the turn to a clean, idle terminal: the stream
   emits ``session.interrupted``, the session settles to ``idle``
   (NOT ``failed``), and the runner persists the cancellation marker
   + synthetic ``function_call_output`` for the dangling call.

   This is the sessions-layer cancel contract. The scaffold's own
   ``_build_terminal_event`` builds ``response.cancelled`` correctly
   (proven by ``tests/runtime/harnesses/test_scaffold.py``); on the
   sessions surface that terminal is not relayed — the runner
   synthesizes the idle terminal + cancellation history instead, the
   shape ``session.interrupted`` and ``GET /v1/sessions/{id}`` expose
   to clients (see ``tests/e2e/test_cancel_history.py``).

The mock LLM is scripted with a fixed tool-call sequence, so the
agent prompt is irrelevant — the queued responses drive the turn.

Runs in the default suite in mock mode (no ``--llm-api-key``); the
``tests/integration`` package gate is lifted in mock mode by
``tests/integration/conftest.py``.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx

from tests.e2e.conftest import configure_mock_llm, send_user_message_to_session
from tests.integration.conftest import JourneySession

_COMPUTE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "compute",
        "description": "Compute a value and return it.",
        "parameters": {
            "type": "object",
            "properties": {
                "value": {"type": "string", "description": "Marker to echo."},
            },
            "required": ["value"],
        },
    },
}

# The runner persists an interrupted turn as a synthetic user message
# carrying this marker text. Mirrors
# ``tests/e2e/test_cancel_history.py::_CANCELLATION_MARKER_TEXT``; kept
# centralized so a server wording change updates one place.
_CANCELLATION_MARKER_TEXT = "interrupted"


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


def _list_session_items(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    """Return all persisted items for a session in one paginated snapshot.

    :param client: HTTP client pointed at the live server.
    :param session_id: Session/conversation id.
    """
    items: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        params: dict[str, Any] = {"order": "asc", "limit": 1000}
        if after is not None:
            params["after"] = after
        resp = client.get(f"/v1/sessions/{session_id}/items", params=params)
        resp.raise_for_status()
        page = resp.json()
        items.extend(page["data"])
        if not page.get("has_more"):
            return items
        after = page.get("last_id")
        if after is None:
            raise AssertionError(f"items page had has_more without last_id: {page}")


def test_client_tool_round_trip(
    live_server: str,
    journey_session: JourneySession,
    mock_llm_server_url: str | None,
) -> None:
    """A client-tool call round-trips and the turn completes cleanly.

    Turn script (mock LLM queue):
      1. ``compute`` tool call (the server publishes it as an
         ``action_required`` function_call on the live stream).
      2. The test posts a ``function_call_output`` with a marker.
      3. The model emits the marker as its final answer.

    Asserts the turn terminal is ``response.completed`` — the
    full server->client round-trip the SSE-parser unit tests
    never reach.
    """
    marker = f"D6-ROUND-TRIP-{uuid.uuid4().hex[:8]}"
    call_id = f"call_{uuid.uuid4().hex[:8]}"
    sid = journey_session.session_id

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": call_id,
                        "name": "compute",
                        "arguments": json.dumps({"value": marker}),
                    }
                ]
            },
            {"text": f"ANSWER:{marker}"},
        ],
    )

    errors: list[Exception] = []
    text_chunks: list[str] = []
    status: str | None = None

    def _post_message() -> None:
        try:
            with httpx.Client(base_url=live_server, timeout=30) as poster:
                send_user_message_to_session(
                    poster,
                    session_id=sid,
                    content="Run compute and answer with the value.",
                    tools=[_COMPUTE_TOOL],
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
                        "data": {"call_id": cid, "output": marker},
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
                    if (
                        item.get("type") == "function_call"
                        and item.get("name") == "compute"
                        and item.get("status") == "action_required"
                        and not answered
                    ):
                        answered = True
                        threading.Thread(
                            target=_post_output, args=(item["call_id"],), daemon=True
                        ).start()
                elif etype == "response.output_text.delta":
                    delta = event.get("delta")
                    if isinstance(delta, str):
                        text_chunks.append(delta)
                elif etype in ("response.completed", "response.failed", "response.cancelled"):
                    status = etype
                    break

    if errors:
        raise errors[0]
    assert status == "response.completed", (
        f"D6 round-trip should complete cleanly; turn ended {status!r} "
        f"with text {''.join(text_chunks)!r}"
    )
    final_text = "".join(text_chunks)
    assert marker in final_text, (
        f"D6 round-trip final answer should echo the tool-output marker; "
        f"expected {marker!r} in reply text {final_text!r}"
    )


def test_direct_cancel_parks_then_interrupts_cleanly(
    live_server: str,
    journey_session: JourneySession,
    mock_llm_server_url: str | None,
) -> None:
    """A direct cancel while a client tool is parked settles the turn cleanly.

    Turn script (mock LLM queue):
      1. ``compute`` tool call → published as an ``action_required``
         function_call. The turn parks waiting for the result.
      2. (never reached) final text.

    Instead of posting the result, the test posts an ``interrupt``
    event while the call is parked. The sessions-layer cancel
    contract (see :mod:`tests.e2e.test_cancel_history`) is:

    - the live stream emits ``session.interrupted``,
    - the session settles to ``idle`` (NOT ``failed`` — a failed
      terminal here would be the regression this guards), and
    - the runner persists the cancellation marker + a synthetic
      ``function_call_output`` so the dangling parked call is closed
      and the next turn does not reject on a missing tool output.

    ``response.cancelled`` is NOT asserted: the scaffold builds that
    terminal (covered at the unit layer by
    ``tests/runtime/harnesses/test_scaffold.py``), but on the
    sessions surface the runner synthesizes the idle terminal +
    history instead, and that synthesized shape is what clients see.
    """
    call_id = f"call_{uuid.uuid4().hex[:8]}"
    sid = journey_session.session_id

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": call_id,
                        "name": "compute",
                        "arguments": json.dumps({"value": "blocked"}),
                    }
                ]
            },
            {"text": "should-not-reach"},
        ],
    )

    errors: list[Exception] = []
    interrupted = False
    saw_session_interrupted = False

    def _post_message() -> None:
        try:
            with httpx.Client(base_url=live_server, timeout=30) as poster:
                send_user_message_to_session(
                    poster,
                    session_id=sid,
                    content="Run compute and wait.",
                    tools=[_COMPUTE_TOOL],
                )
        except Exception as exc:  # thread boundary; re-raised below
            errors.append(exc)

    def _post_interrupt() -> None:
        try:
            with httpx.Client(base_url=live_server, timeout=30) as poster:
                resp = poster.post(
                    f"/v1/sessions/{sid}/events",
                    json={"type": "interrupt", "data": {}},
                )
                assert resp.status_code in (200, 202, 204), (
                    f"interrupt POST failed: {resp.status_code} {resp.text[:300]}"
                )
        except Exception as exc:  # thread boundary; re-raised below
            errors.append(exc)

    # Read the stream until we've both seen the parked call (so we
    # interrupt at the right moment) and observed ``session.interrupted``,
    # the sessions-layer signal the cancel reached the loop. A hard
    # deadline bounds the read so a regression that drops the signal
    # surfaces as an assertion, not a hang.
    deadline = time.monotonic() + 60
    with httpx.Client(base_url=live_server, timeout=90) as streamer:
        with streamer.stream("GET", f"/v1/sessions/{sid}/stream") as response:
            response.raise_for_status()
            posted = False
            for event in _iter_sse(response):
                if not posted:
                    threading.Thread(target=_post_message, daemon=True).start()
                    posted = True
                etype = event.get("type")
                if etype == "response.output_item.done":
                    item = event.get("item") or {}
                    if (
                        item.get("type") == "function_call"
                        and item.get("name") == "compute"
                        and item.get("status") == "action_required"
                        and not interrupted
                    ):
                        interrupted = True
                        threading.Thread(target=_post_interrupt, daemon=True).start()
                elif etype == "session.interrupted":
                    saw_session_interrupted = True
                    break
                if time.monotonic() > deadline:
                    break

    if errors:
        raise errors[0]
    assert interrupted, (
        "never saw the action_required compute call to interrupt; the parked "
        "client-tool dispatch the cancel must unwind was never reached"
    )
    assert saw_session_interrupted, (
        "stream never emitted session.interrupted after the interrupt POST; the "
        "cancel signal did not reach the live stream"
    )

    # The session must settle to a clean idle terminal — never failed.
    # A failed terminal here is the cancel-path regression this guards.
    settle_deadline = time.monotonic() + 30
    final_status: str | None = None
    with httpx.Client(base_url=live_server, timeout=30) as client:
        while time.monotonic() < settle_deadline:
            snap = client.get(f"/v1/sessions/{sid}")
            snap.raise_for_status()
            final_status = snap.json().get("status")
            assert final_status != "failed", (
                f"cancelled turn settled to 'failed' (cancel-path regression): {snap.json()}"
            )
            if final_status == "idle":
                break
            time.sleep(0.25)
        assert final_status == "idle", (
            f"cancelled session never settled to idle; last status={final_status!r}"
        )

        # The runner closes the dangling parked call by persisting a
        # cancellation marker (synthetic user message) AND a synthetic
        # function_call_output for the parked call, so the next turn does
        # not reject on a missing tool output. Persistence runs on an
        # async background task that can lag the idle transition, so poll.
        marker_deadline = time.monotonic() + 30
        items: list[dict[str, Any]] = []
        marker_items: list[dict[str, Any]] = []
        fc_outputs: list[dict[str, Any]] = []
        while time.monotonic() < marker_deadline:
            items = _list_session_items(client, sid)
            marker_items = [
                item
                for item in items
                if item.get("type") == "message"
                and item.get("role") == "user"
                and any(
                    _CANCELLATION_MARKER_TEXT in (c.get("text") or "")
                    for c in item.get("content", [])
                )
            ]
            fc_outputs = [item for item in items if item.get("type") == "function_call_output"]
            if marker_items and fc_outputs:
                break
            time.sleep(0.25)

    assert len(marker_items) == 1, (
        f"expected exactly one cancellation marker after interrupt, found "
        f"{len(marker_items)}; items={[i.get('type') for i in items]}"
    )
    assert fc_outputs, (
        "no synthetic function_call_output persisted for the parked call; the "
        "dangling action_required call was left unclosed and the next turn "
        f"would reject. items={[i.get('type') for i in items]}"
    )
