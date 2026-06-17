"""Client-side tool result threading across turns.

Turn 1 forces a tunneled client-tool call, posts a marker payload as
the result, and the model must surface it. Turn 2 asks for the value
with no tool attached: the answer can only come from the turn-1
transcript, so this fails if tool outputs don't thread into the next
dispatch's context on the harness under test.

Turn 1 is driven over ``GET /v1/sessions/{id}/stream``: on the harness
path, ``action_required`` function_calls are published to the live
stream only and never persisted as conversation items (see
``tests/e2e/test_openai_coder_client_tools.py``), so snapshot polling
would hang waiting for an item that never appears.

Runs with either a real LLM or the mock LLM server.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterator
from typing import Any

import httpx

from tests.e2e.conftest import configure_mock_llm, send_user_message_to_session
from tests.integration.conftest import JourneySession
from tests.integration.helpers import all_message_text, failure_detail, run_turn

_LOOKUP_WIDGET_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "lookup_widget",
        "description": "Look up a widget by id and return its color.",
        "parameters": {
            "type": "object",
            "properties": {
                "widget_id": {"type": "integer", "description": "Widget id, e.g. 42."},
            },
            "required": ["widget_id"],
        },
    },
}


def _iter_sse(response: httpx.Response) -> Iterator[dict[str, Any]]:
    """Yield decoded SSE event dicts; stops at ``[DONE]``.

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


def _turn_with_tool(
    base_url: str, session_id: str, prompt: str, marker: str, timeout_s: float = 60
) -> str:
    """Run one turn, answering the first ``lookup_widget`` call with *marker*.

    :param base_url: Live server base URL.
    :param session_id: Runner-bound session id.
    :param prompt: User message for turn 1.
    :param marker: Payload to return as the tool result.
    :param timeout_s: Stream budget; with turn 2's 50s poll this
        keeps each rerun attempt under the CI 180s cap.
    :returns: The assistant text assembled from stream deltas.
    :raises AssertionError: If the model never calls the tool or the
        turn does not complete.
    """
    called = threading.Event()
    text_chunks: list[str] = []
    status: str | None = None
    # Poster threads park failures here; raised after the stream loop.
    # A daemon thread's exception is otherwise swallowed and the test
    # would time out with a misleading "model never called" error.
    errors: list[Exception] = []

    def _post_message() -> None:
        try:
            with httpx.Client(base_url=base_url, timeout=30) as poster:
                send_user_message_to_session(
                    poster, session_id=session_id, content=prompt, tools=[_LOOKUP_WIDGET_TOOL]
                )
        except Exception as exc:  # thread boundary; re-raised below
            errors.append(exc)

    def _post_output(call_id: str) -> None:
        try:
            with httpx.Client(base_url=base_url, timeout=30) as poster:
                resp = poster.post(
                    f"/v1/sessions/{session_id}/events",
                    json={
                        "type": "function_call_output",
                        "data": {"call_id": call_id, "output": json.dumps({"color": marker})},
                    },
                )
                assert resp.status_code in (200, 202), (
                    f"function_call_output POST failed: {resp.status_code} {resp.text[:300]}"
                )
        except Exception as exc:  # thread boundary; re-raised below
            errors.append(exc)

    with httpx.Client(base_url=base_url, timeout=timeout_s) as streamer:
        with streamer.stream("GET", f"/v1/sessions/{session_id}/stream") as response:
            response.raise_for_status()
            posted = False
            for event in _iter_sse(response):
                # Post only after the first frame: the stream does not
                # replay history, so posting earlier can drop events.
                if not posted:
                    threading.Thread(target=_post_message, daemon=True).start()
                    posted = True
                etype = event.get("type")
                if etype == "response.output_item.done":
                    item = event.get("item") or {}
                    if (
                        item.get("type") == "function_call"
                        and item.get("name") == "lookup_widget"
                        and item.get("status") == "action_required"
                        and not called.is_set()
                    ):
                        called.set()
                        threading.Thread(
                            target=_post_output, args=(item["call_id"],), daemon=True
                        ).start()
                elif etype == "response.output_text.delta":
                    delta = event.get("delta")
                    if isinstance(delta, str):
                        text_chunks.append(delta)
                elif etype in ("response.completed", "response.failed"):
                    status = etype
                    break

    if errors:
        raise errors[0]
    assert called.is_set(), (
        f"model never called lookup_widget; turn ended {status!r} "
        f"with text: {''.join(text_chunks)[:500]}"
    )
    assert status == "response.completed", f"turn 1 ended {status!r}"
    return "".join(text_chunks)


def test_client_tool_result_recalled_across_turns(
    live_server: str,
    journey_session: JourneySession,
    mock_llm_server_url: str | None,
) -> None:
    marker = f"widget-color-{uuid.uuid4().hex[:8]}"
    call_id = f"call_{uuid.uuid4().hex[:8]}"
    sid = journey_session.session_id

    # In mock mode, queue: tool call → text with marker → recall text.
    # The first response is a tool call; after the test posts the tool
    # result, the second response includes the marker; the third
    # response (turn 2) recalls from transcript.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": call_id,
                        "name": "lookup_widget",
                        "arguments": json.dumps({"widget_id": 42}),
                    },
                ],
            },
            {"text": f"The widget color is {marker}."},
            {"text": f"The color was {marker}."},
        ],
    )

    text_1 = _turn_with_tool(
        live_server,
        sid,
        prompt=(
            "You MUST call the lookup_widget tool for widget id 42 "
            "before answering. Then reply with the color it returns."
        ),
        marker=marker,
    )
    # The marker only exists in the tool result we posted; seeing it in
    # the reply proves the result reached the model.
    assert marker in text_1, f"tool result never reached the model; turn 1 said: {text_1[:500]}"

    with httpx.Client(base_url=live_server, timeout=300) as client:
        body_2 = run_turn(
            client,
            session_id=sid,
            content="What color was the widget? Reply with the exact color value only.",
        )
    assert body_2["status"] == "completed", f"turn 2 failed: {failure_detail(body_2)}"
    # No tool is attached on turn 2: the only source of the marker is
    # the turn-1 transcript (tool call + result) replayed as context.
    assert marker in all_message_text(body_2), failure_detail(body_2)
