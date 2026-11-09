"""Reconnect-safety coverage for parked client-tool calls.

Reproduces and pins the fix for the reconnect bug: an ``action_required``
client-tool call is only ever delivered on the live SSE stream, so a client
that disconnects while a call is parked — then reconnects — could not
previously discover it existed, let alone answer it. Before the fix, the
reconnecting client's ``GET /v1/sessions/{id}`` carried no trace of the
call and the turn would eventually only resolve by failing the idle
watchdog.

Reuses the client-tool turn helpers from
:mod:`tests.integration.test_client_tool_sse_status` (same mock-LLM +
live-server path) and adds the disconnect -> snapshot -> reconnect ->
answer round-trip, plus the answered-by-another-client and
interrupt-clears-the-snapshot contracts the fix must also satisfy.

Runs in the default suite in mock mode (no ``--llm-api-key``), same gating
as its sibling module.
"""

from __future__ import annotations

import json
import threading
import uuid
from typing import Any

import httpx

from tests.e2e.conftest import configure_mock_llm, send_user_message_to_session
from tests.integration.conftest import JourneySession
from tests.integration.test_client_tool_sse_status import _LOOKUP_SECRET_TOOL, _iter_sse


def _disconnect_after_action_required(live_server: str, sid: str) -> str:
    """Stream a client-tool turn until it parks, then disconnect without answering.

    Posts the triggering user message once streaming starts (same shape as
    ``_run_client_tool_turn`` in the sibling module), reads the call_id off
    the live ``action_required`` event, then exits the streaming context —
    closing the HTTP connection the way a dropped client / killed tab would,
    well before any ``function_call_output`` is posted.

    :param live_server: Base URL of the live server.
    :param sid: Runner-bound session id.
    :returns: The call_id the test will answer after reconnecting.
    """
    errors: list[Exception] = []
    call_id: str | None = None

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

    with httpx.Client(base_url=live_server, timeout=90) as streamer:
        with streamer.stream("GET", f"/v1/sessions/{sid}/stream") as response:
            response.raise_for_status()
            posted = False
            for event in _iter_sse(response):
                if not posted:
                    threading.Thread(target=_post_message, daemon=True).start()
                    posted = True
                item = (
                    (event.get("item") or {})
                    if event.get("type") == "response.output_item.done"
                    else {}
                )
                if (
                    item.get("type") == "function_call"
                    and item.get("name") == "lookup_secret_token"
                    and item.get("status") == "action_required"
                ):
                    call_id = item["call_id"]
                    break  # Disconnect immediately — never post the output.

    if errors:
        raise errors[0]
    assert call_id is not None, "client-tool call never parked as action_required"
    return call_id


def _pending_client_tool_calls(live_server: str, sid: str) -> list[dict[str, Any]]:
    """Fetch the session snapshot's ``pending_client_tool_calls`` field.

    :param live_server: Base URL of the live server.
    :param sid: Session id to snapshot.
    """
    with httpx.Client(base_url=live_server, timeout=30) as client:
        resp = client.get(f"/v1/sessions/{sid}")
        resp.raise_for_status()
        return resp.json().get("pending_client_tool_calls") or []


def _post_function_call_output(live_server: str, sid: str, call_id: str, output: str) -> None:
    """Answer a parked client-tool call via ``POST /v1/sessions/{id}/events``.

    :param live_server: Base URL of the live server.
    :param sid: Session id the call belongs to.
    :param call_id: The call id to answer.
    :param output: The tool result string.
    """
    with httpx.Client(base_url=live_server, timeout=30) as client:
        resp = client.post(
            f"/v1/sessions/{sid}/events",
            json={"type": "function_call_output", "data": {"call_id": call_id, "output": output}},
        )
        assert resp.status_code in (200, 202), (
            f"function_call_output POST failed: {resp.status_code} {resp.text[:300]}"
        )


def test_reconnect_recovers_and_answers_a_parked_client_tool_call(
    live_server: str,
    journey_session: JourneySession,
    mock_llm_server_url: str | None,
) -> None:
    """A client-tool call parked before a disconnect is recoverable after reconnect.

    Turn script (mock LLM queue):
      1. ``lookup_secret_token`` tool call parks as ``action_required``.
      2. The test disconnects without answering, then reconnects.
      3. ``GET /v1/sessions/{id}`` must show the call — proving the
         reconnect bug this change fixes is closed.
      4. The test answers using the call_id read off the *snapshot*, not
         the original stream, then reconnects to the live stream and
         confirms the turn completes with the sentinel.
      5. After completion, the snapshot's ``pending_client_tool_calls`` is
         empty again, and re-posting the same output a second time is a
         harmless no-op (proves idempotent duplicate answers).
    """
    sentinel = f"DBX-RECONNECT-{uuid.uuid4().hex[:8]}-CANARY"
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

    disconnected_call_id = _disconnect_after_action_required(live_server, sid)
    assert disconnected_call_id == call_id

    # Before the fix: this is empty — the parked call was only ever on the
    # live stream the disconnected client missed, with no snapshot replay.
    snapshot_calls = _pending_client_tool_calls(live_server, sid)
    assert len(snapshot_calls) == 1, (
        f"reconnecting client should see the parked call in "
        f"pending_client_tool_calls; got {snapshot_calls!r}. An empty list "
        f"here means the reconnect-safety fix regressed: the call parked "
        f"before reconnect is invisible again."
    )
    recovered_item = snapshot_calls[0]["item"]
    assert recovered_item["call_id"] == call_id
    assert recovered_item["status"] == "action_required"
    assert recovered_item["name"] == "lookup_secret_token"
    # The envelope is documented as byte-identical to the live SSE event, not
    # a reduced {type, item} pair — so a real event's other top-level fields
    # (e.g. sequence_number) must survive into the snapshot entry too. Assert
    # presence rather than a fixed key set: _SSEEventBase's own
    # forward-compatible ``extra="ignore"`` contract means a future field
    # must not need this test updated.
    assert "sequence_number" in snapshot_calls[0], (
        f"snapshot entry must carry every top-level field a live "
        f"response.output_item.done event has, not just type/item; "
        f"got keys {sorted(snapshot_calls[0].keys())}"
    )

    # Reconnect to the live stream, then answer using only the call_id
    # recovered from the snapshot (never the original stream's variable) —
    # this is what makes the scenario a genuine recovery, not a replay of
    # state the test already held.
    terminal = ""
    text_chunks: list[str] = []
    with httpx.Client(base_url=live_server, timeout=90) as streamer:
        with streamer.stream("GET", f"/v1/sessions/{sid}/stream") as response:
            response.raise_for_status()
            answered = False
            for event in _iter_sse(response):
                if not answered:
                    threading.Thread(
                        target=_post_function_call_output,
                        args=(live_server, sid, recovered_item["call_id"], sentinel),
                        daemon=True,
                    ).start()
                    answered = True
                etype = event.get("type")
                if etype == "response.output_text.delta":
                    delta = event.get("delta")
                    if isinstance(delta, str):
                        text_chunks.append(delta)
                elif etype in ("response.completed", "response.failed", "response.cancelled"):
                    terminal = etype
                    break

    assert terminal == "response.completed", (
        f"turn should complete after answering the recovered call; "
        f"ended {terminal!r} with text {''.join(text_chunks)!r}"
    )
    assert sentinel in "".join(text_chunks), (
        f"the answer posted after reconnect should reach the model; "
        f"expected {sentinel!r} in {''.join(text_chunks)!r}"
    )

    # The snapshot clears once the turn completes.
    assert _pending_client_tool_calls(live_server, sid) == []

    # A duplicate answer (e.g. a retried POST) is a harmless no-op, not an
    # error — idempotent per the module's resolve() contract.
    _post_function_call_output(live_server, sid, call_id, sentinel)


def test_a_second_client_answering_clears_the_first_clients_snapshot(
    live_server: str,
    journey_session: JourneySession,
    mock_llm_server_url: str | None,
) -> None:
    """A call answered by one client no longer appears for another.

    Simulates two clients observing the same session: the first parks and
    disconnects without answering (as above); a second client answers on
    its behalf. The first client's later reconnect snapshot must come back
    empty — the index drains on the first accepted answer, not per-client.
    """
    sentinel = f"DBX-OTHER-CLIENT-{uuid.uuid4().hex[:8]}-CANARY"
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

    _disconnect_after_action_required(live_server, sid)
    assert len(_pending_client_tool_calls(live_server, sid)) == 1

    # A second client (a different connection entirely) answers.
    _post_function_call_output(live_server, sid, call_id, sentinel)

    # The "first client" reconnects later and must see nothing outstanding.
    assert _pending_client_tool_calls(live_server, sid) == [], (
        "a call answered by a different client must drain for every "
        "client's view, not just the one that posted the answer"
    )


def test_interrupting_the_turn_clears_the_parked_calls_snapshot(
    live_server: str,
    journey_session: JourneySession,
    mock_llm_server_url: str | None,
) -> None:
    """Interrupting a turn with a parked call clears it from the snapshot.

    A cancelled ``dispatch_tool`` never emits its own completion event, so
    the ``session.interrupted`` event is the only signal the index gets
    that the call is no longer answerable. Without this, a reconnecting
    client could be shown a call whose harness awaiter was already
    cancelled — answering it would do nothing.
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
                        "name": "lookup_secret_token",
                        "arguments": json.dumps({}),
                    }
                ]
            },
        ],
    )

    _disconnect_after_action_required(live_server, sid)
    assert len(_pending_client_tool_calls(live_server, sid)) == 1

    with httpx.Client(base_url=live_server, timeout=30) as client:
        resp = client.post(f"/v1/sessions/{sid}/events", json={"type": "interrupt"})
        assert resp.status_code in (200, 202), (
            f"interrupt POST failed: {resp.status_code} {resp.text[:300]}"
        )

    assert _pending_client_tool_calls(live_server, sid) == [], (
        "interrupting the turn must clear its parked client-tool calls — "
        "a cancelled call can never be shown as still answerable"
    )
