"""Mock-LLM sessions coverage for re-homed D6 parallel dispatch paths.

These tests replace two suppressed e2e tests that only stayed red because
those tests drove the removed ``POST /v1/responses`` route. They drive the
current runner-bound sessions API in mock-LLM mode instead.

The two paths are intentionally separate:

* ``test_sys_terminal_parallel_launches_complete`` scripts ten AP-side
  ``sys_terminal_launch`` calls in one assistant turn. Those are
  server-executed tools routed through the runner/AP terminal dispatcher.
  The old e2e used a real LLM and historically reproduced a DBOS
  ``function_id`` race in child workflows. This mock sessions test proves
  the current path accepts a single assistant turn containing ten terminal
  calls, completes cleanly, and persists ten launch outputs. It does not
  prove the removed DBOS child-workflow counter race directly because the
  sessions layer no longer exposes the legacy task rows or
  ``/v1/responses`` polling surface that made that race observable.

* ``test_client_tool_outputs_fan_out_and_round_trip_in_parallel`` scripts
  three request-time client tool calls in one assistant turn. The test
  observes all three parked ``action_required`` calls before posting any
  output, then posts all outputs from separate worker threads. That is the
  strongest deterministic sessions-layer proxy for client-tool fan-out:
  multiple call IDs are simultaneously in flight and all outputs round-trip
  to a clean ``response.completed`` terminal. The proof is ordering-based,
  not timing-based: the test snapshots the set of parked call IDs before
  any ``function_call_output`` is posted. The old e2e also measured SDK-local
  tool-body wall-clock overlap; this mock server-side test does not execute
  real client Python tool bodies, so it does not assert that SDK
  ``asyncio.to_thread`` behavior.

Runs in mock mode (no ``--llm-api-key``) using the same
``configure_mock_llm`` / sessions helpers as the D6 cancel round-trip
coverage.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    send_user_message_to_session,
)

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
            "additionalProperties": False,
        },
    },
}

_TERMINAL_COUNT = 10
_FAN_OUT = 3
_POST_THREAD_TIMEOUT_S = 30


def _iter_sse(response: httpx.Response) -> Iterator[dict[str, Any]]:
    """Yield decoded SSE event dicts from a sessions stream."""
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
    """Return all persisted items for a session."""
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


def _flat_item(item: dict[str, Any]) -> dict[str, Any]:
    """Flatten session item ``data`` into the Responses-style shape."""
    data = item.get("data")
    if not isinstance(data, dict):
        return item
    return {
        "id": item.get("id"),
        "response_id": item.get("response_id"),
        "type": item.get("type"),
        "status": item.get("status"),
        **data,
    }


def _function_call_outputs_for(
    client: httpx.Client,
    *,
    session_id: str,
    tool_name: str,
) -> list[str]:
    """Return raw outputs for calls to one tool in conversation order."""
    items = [_flat_item(item) for item in _list_session_items(client, session_id)]
    call_ids = {
        item["call_id"]
        for item in items
        if item.get("type") == "function_call"
        and item.get("name") == tool_name
        and item.get("call_id")
    }
    return [
        str(item.get("output") or "")
        for item in items
        if item.get("type") == "function_call_output" and item.get("call_id") in call_ids
    ]


def _terminal_resources_for(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    """Return terminal resources for a session."""
    resp = client.get(f"/v1/sessions/{session_id}/resources/terminals")
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data", [])
    assert isinstance(data, list), f"unexpected terminal resource payload: {payload!r}"
    return data


def _cleanup_terminal_resources(client: httpx.Client, session_id: str) -> None:
    """Close all terminal resources owned by a session and assert they are gone."""
    errors: list[str] = []
    socket_paths: list[Path] = []
    for resource in _terminal_resources_for(client, session_id):
        metadata = resource.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("tmux_socket"), str):
            socket_paths.append(Path(metadata["tmux_socket"]))
        resource_id = resource.get("id")
        if not isinstance(resource_id, str) or not resource_id:
            errors.append(f"terminal resource missing id: {resource!r}")
            continue
        resp = client.delete(f"/v1/sessions/{session_id}/resources/terminals/{resource_id}")
        if resp.status_code not in (200, 202, 204, 404):
            errors.append(
                f"delete terminal {resource_id!r} failed: {resp.status_code} {resp.text[:300]}"
            )

    remaining = _terminal_resources_for(client, session_id)
    lingering_sockets = [str(path) for path in socket_paths if path.exists()]
    assert not remaining and not lingering_sockets and not errors, (
        f"terminal cleanup failed for session {session_id}: "
        f"errors={errors!r}, remaining={remaining!r}, "
        f"lingering_sockets={lingering_sockets!r}"
    )


def _post_in_thread(
    target: Callable[[], None],
    errors: list[Exception],
) -> threading.Thread:
    """Run a poster in a daemon thread and capture exceptions."""

    def _wrapped() -> None:
        try:
            target()
        except Exception as exc:  # thread boundary; re-raised by caller
            errors.append(exc)

    thread = threading.Thread(target=_wrapped, daemon=True)
    thread.start()
    return thread


@pytest.fixture
def terminal_mock_agent(
    http_client: httpx.Client,
    live_runner_id: str,
    harness_name: str,
    model_name: str,
    request: pytest.FixtureRequest,
    mock_llm_server_url: str | None,
) -> Iterator[tuple[str, str, str]]:
    """Register a mock-LLM agent with ``sys_terminal_*`` tools enabled."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux not installed; sys_terminal_* tests need tmux on PATH")

    agent_name = register_inline_agent(
        http_client,
        name=f"terminal-parallel-{uuid.uuid4().hex[:6]}",
        harness=harness_name,
        model=model_name,
        profile=request.config.getoption("--profile"),
        prompt=(
            "You are a terminal test assistant. Follow the scripted mock LLM tool calls exactly."
        ),
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        extra_config={
            "os_env": {
                "type": "caller_process",
                "cwd": ".",
                "sandbox": {"type": "none"},
            },
            "terminals": {
                "bash": {
                    "command": "bash",
                    "os_env": {
                        "type": "caller_process",
                        "sandbox": {"type": "none"},
                    },
                },
            },
        },
    )
    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )
    try:
        yield agent_name, session_id, model_name
    finally:
        _cleanup_terminal_resources(http_client, session_id)


def test_sys_terminal_parallel_launches_complete(
    http_client: httpx.Client,
    terminal_mock_agent: tuple[str, str, str],
    mock_llm_server_url: str | None,
) -> None:
    """Ten AP-side terminal dispatches in one assistant turn complete.

    The mock LLM emits ten ``sys_terminal_launch`` calls in one response,
    preserving the old e2e's "many server-executed terminal operations in
    one turn" intent without depending on the removed ``/v1/responses``
    route. Completion plus ten non-empty successful outputs proves the
    sessions runner accepted the parallel tool-call batch and round-tripped
    each AP-side result. It is intentionally honest about the coverage
    delta: it does not directly assert the old DBOS ``function_id`` child
    workflow race because that legacy task surface is gone.
    """
    _agent_name, session_id, model_name = terminal_mock_agent
    call_ids = [f"call_terminal_{idx}_{uuid.uuid4().hex[:6]}" for idx in range(_TERMINAL_COUNT)]
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": call_id,
                        "name": "sys_terminal_launch",
                        "arguments": json.dumps(
                            {
                                "terminal": "bash",
                                "session": f"parallel_{idx}_{uuid.uuid4().hex[:6]}",
                            }
                        ),
                    }
                    for idx, call_id in enumerate(call_ids)
                ]
            },
            {"text": "done"},
        ],
        key=model_name,
    )

    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Launch ten separate bash terminals, then say done.",
    )
    result = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=180,
    )

    assert result["status"] == "completed", (
        f"Expected terminal fan-out turn to complete, got {result['status']!r}; "
        f"error={result.get('error')!r}, output={result.get('output')!r}"
    )

    outputs = _function_call_outputs_for(
        http_client,
        session_id=session_id,
        tool_name="sys_terminal_launch",
    )
    assert len(outputs) == _TERMINAL_COUNT, (
        f"Expected exactly {_TERMINAL_COUNT} sys_terminal_launch outputs from "
        f"one scripted assistant turn, got {len(outputs)}: {outputs!r}"
    )

    successful = 0
    for output in outputs:
        assert output, f"terminal launch produced an empty output: {outputs!r}"
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"sys_terminal_launch output was not valid JSON: {output!r}"
            ) from exc
        assert isinstance(parsed, dict), (
            f"sys_terminal_launch output decoded to non-object JSON: {output!r}"
        )
        if parsed.get("status") in {"launched", "already_running"}:
            successful += 1
    assert successful == _TERMINAL_COUNT, (
        f"Expected all {_TERMINAL_COUNT} terminal launches to succeed, "
        f"got {successful}. Outputs: {outputs!r}"
    )


def test_client_tool_outputs_fan_out_and_round_trip_in_parallel(
    live_server: str,
    http_client: httpx.Client,
    journey_session: Any,
    model_name: str,
    mock_llm_server_url: str | None,
) -> None:
    """Three client-tool calls are parked together and completed together.

    The mock LLM emits three request-time ``compute`` client-tool calls in
    one assistant response. The test waits until all three calls have been
    observed as ``action_required`` before posting any outputs, proving
    multiple call IDs are concurrently in flight at the sessions boundary.
    It then posts the three ``function_call_output`` events from separate
    worker threads and asserts ``response.completed`` plus all result
    markers in the final answer. This covers server/client round-trip
    fan-out, but does not execute real SDK Python tool bodies or assert
    wall-clock overlap like the removed e2e did.
    """
    values = ["a", "b", "c"]
    outputs_by_value = {value: f"done-{value}-{uuid.uuid4().hex[:6]}" for value in values}
    call_ids = {value: f"call_compute_{value}_{uuid.uuid4().hex[:6]}" for value in values}
    final_marker = "ANSWER:" + ",".join(outputs_by_value[value] for value in values)
    sid = journey_session.session_id

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": call_ids[value],
                        "name": "compute",
                        "arguments": json.dumps({"value": value}),
                    }
                    for value in values
                ]
            },
            {"text": final_marker},
        ],
        key=model_name,
    )

    errors: list[Exception] = []
    posted_threads: list[threading.Thread] = []
    pending_calls: dict[str, dict[str, Any]] = {}
    parked_snapshot: tuple[str, ...] | None = None
    text_chunks: list[str] = []
    status: str | None = None

    def _post_message() -> None:
        with httpx.Client(base_url=live_server, timeout=30) as poster:
            send_user_message_to_session(
                poster,
                session_id=sid,
                content="Call compute for a, b, and c, then answer with all outputs.",
                tools=[_COMPUTE_TOOL],
            )

    def _post_output(call_id: str, output: str) -> None:
        with httpx.Client(base_url=live_server, timeout=30) as poster:
            resp = poster.post(
                f"/v1/sessions/{sid}/events",
                json={
                    "type": "function_call_output",
                    "data": {"call_id": call_id, "output": output},
                },
            )
            assert resp.status_code in (200, 202), (
                f"function_call_output POST failed: {resp.status_code} {resp.text[:300]}"
            )

    deadline = time.monotonic() + 90
    with httpx.Client(base_url=live_server, timeout=120) as streamer:
        with streamer.stream("GET", f"/v1/sessions/{sid}/stream") as response:
            response.raise_for_status()
            posted_message = False
            posted_outputs = False
            for event in _iter_sse(response):
                if not posted_message:
                    posted_threads.append(_post_in_thread(_post_message, errors))
                    posted_message = True

                etype = event.get("type")
                if etype == "response.output_item.done":
                    item = event.get("item") or {}
                    if (
                        item.get("type") == "function_call"
                        and item.get("name") == "compute"
                        and item.get("status") == "action_required"
                    ):
                        call_id = item["call_id"]
                        if not posted_outputs:
                            pending_calls[call_id] = item
                        if len(pending_calls) == _FAN_OUT and not posted_outputs:
                            parked_snapshot = tuple(sorted(pending_calls))
                            posted_outputs = True
                            for value in values:
                                posted_threads.append(
                                    _post_in_thread(
                                        lambda v=value: _post_output(
                                            call_ids[v], outputs_by_value[v]
                                        ),
                                        errors,
                                    )
                                )
                elif etype == "response.output_text.delta":
                    delta = event.get("delta")
                    if isinstance(delta, str):
                        text_chunks.append(delta)
                elif etype in ("response.completed", "response.failed", "response.cancelled"):
                    status = etype
                    break

                if time.monotonic() > deadline:
                    break

    for thread in posted_threads:
        thread.join(timeout=_POST_THREAD_TIMEOUT_S)
        assert not thread.is_alive(), (
            f"posting thread {thread.name!r} did not finish within "
            f"{_POST_THREAD_TIMEOUT_S}s; output may have been dropped"
        )
    if errors:
        raise errors[0]

    assert status == "response.completed", (
        f"Expected client-tool fan-out turn to complete, got {status!r}; "
        f"text={''.join(text_chunks)!r}, pending={pending_calls!r}"
    )
    assert parked_snapshot is not None, (
        f"Expected all {_FAN_OUT} compute calls to park before posting outputs. "
        f"Saw {pending_calls!r}; expected {call_ids!r}."
    )
    assert set(parked_snapshot) == set(call_ids.values()), (
        f"Expected all {_FAN_OUT} compute calls to be simultaneously parked "
        f"before posting outputs. Saw snapshot {parked_snapshot!r}; expected {call_ids!r}."
    )

    final_text = "".join(text_chunks)
    for output in outputs_by_value.values():
        assert output in final_text, (
            f"Final answer should include client output marker {output!r}; got {final_text!r}"
        )

    persisted_outputs = _function_call_outputs_for(
        http_client,
        session_id=sid,
        tool_name="compute",
    )
    for output in outputs_by_value.values():
        assert output in persisted_outputs, (
            f"Persisted session history missing output {output!r}; outputs={persisted_outputs!r}"
        )
