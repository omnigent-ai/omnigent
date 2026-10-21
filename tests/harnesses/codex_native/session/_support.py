"""Shared support for Codex session tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder
from omnigent.harnesses.codex_native.bridge import (
    CodexNativeBridgeState,
    write_bridge_state,
)


class _FakeCodexAppServerClient:
    """
    Test double for ``CodexAppServerClient``.

    :param response: JSON-RPC response returned from ``request``.
    :param error: Optional exception raised from ``request``.
    :param events: Optional events yielded from ``iter_events``.
    """

    def __init__(
        self,
        response: dict[str, Any] | None = None,
        error: Exception | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> None:
        self.response = (
            response if response is not None else {"result": {"thread": {"id": "thread_123"}}}
        )
        self.error = error
        self.events = events if events is not None else []
        self.connected = False
        self.closed = False
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.responses: list[tuple[int | str, dict[str, Any]]] = []

    async def connect(self) -> None:
        """
        Mark the fake client connected.

        :returns: None.
        """
        self.connected = True

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Capture one JSON-RPC request.

        :param method: JSON-RPC method.
        :param params: JSON-RPC params.
        :returns: Canned JSON-RPC response.
        """
        self.requests.append((method, params))
        if self.error is not None:
            raise self.error
        return self.response

    async def iter_events(self) -> Any:
        """
        Yield the configured event stream.

        :returns: Async iterator over ``self.events``.
        """
        for event in self.events:
            yield event

    async def respond(self, request_id: int | str, result: dict[str, Any]) -> None:
        """
        Capture one JSON-RPC response sent to the fake app-server.

        :param request_id: JSON-RPC request id.
        :param result: JSON-RPC result payload.
        :returns: None.
        """
        self.responses.append((request_id, result))

    async def close(self) -> None:
        """
        Mark the fake client closed.

        :returns: None.
        """
        self.closed = True


def _started_event(turn_id: str) -> dict[str, Any]:
    """
    Build a Codex ``turn/started`` notification.

    :param turn_id: Codex turn id, e.g. ``"turn_123"``.
    :returns: App-server event payload.
    """
    return {"method": "turn/started", "params": {"turn": {"id": turn_id}}}


def _completed_event(turn_id: str | None, *, thread_id: str | None = None) -> dict[str, Any]:
    """
    Build a Codex ``turn/completed`` notification.

    :param turn_id: Codex turn id, e.g. ``"turn_123"``, or ``None``
        when testing legacy or malformed terminal events.
    :param thread_id: Optional Codex thread id, e.g. ``"thread_123"``.
    :returns: App-server event payload.
    """
    params: dict[str, Any] = {}
    if turn_id is not None:
        params["turnId"] = turn_id
    if thread_id is not None:
        params["threadId"] = thread_id
    return {"method": "turn/completed", "params": params}


def _agent_message_event(
    turn_id: str,
    item_id: str,
    text: str,
    *,
    thread_id: str = "thread_123",
) -> dict[str, Any]:
    """
    Build a Codex completed assistant-message notification.

    :param turn_id: Codex turn id, e.g. ``"turn_123"``.
    :param item_id: Codex item id, e.g. ``"item_123"``.
    :param text: Assistant text payload, e.g. ``"done"``.
    :param thread_id: Codex thread id, e.g. ``"thread_123"``.
    :returns: App-server event payload.
    """
    return {
        "method": "item/completed",
        "params": {
            "threadId": thread_id,
            "turnId": turn_id,
            "item": {
                "type": "agentMessage",
                "id": item_id,
                "text": text,
            },
        },
    }


def _agent_message_delta_event(turn_id: str, item_id: str, delta: object) -> dict[str, Any]:
    """
    Build a Codex assistant-message delta notification.

    :param turn_id: Codex turn id, e.g. ``"turn_123"``.
    :param item_id: Codex item id, e.g. ``"item_123"``.
    :param delta: Delta payload to include in the event, e.g. ``"hi"``.
    :returns: App-server event payload.
    """
    return {
        "method": "item/agentMessage/delta",
        "params": {
            "threadId": "thread_123",
            "turnId": turn_id,
            "itemId": item_id,
            "delta": delta,
        },
    }


def _plan_delta_event(turn_id: str, item_id: str, delta: object) -> dict[str, Any]:
    """
    Build a Codex plan delta notification.

    :param turn_id: Codex turn id, e.g. ``"turn_123"``.
    :param item_id: Codex plan item id, e.g. ``"item_plan"``.
    :param delta: Delta payload to include in the event, e.g.
        ``"1. Inspect"``.
    :returns: App-server event payload.
    """
    return {
        "method": "item/plan/delta",
        "params": {
            "threadId": "thread_123",
            "turnId": turn_id,
            "itemId": item_id,
            "delta": delta,
        },
    }


def _expected_delta_data(
    delta: str,
    turn_id: str,
    item_id: str,
    *,
    item_type: str = "agentMessage",
) -> dict[str, Any]:
    """
    Build the Omnigent event data expected for one Codex native text delta.

    :param delta: Coalesced text fragment, e.g. ``"hello"``.
    :param turn_id: Codex turn id, e.g. ``"turn_123"``.
    :param item_id: Codex item id, e.g. ``"item_agent"``.
    :param item_type: Codex item type, e.g. ``"agentMessage"``.
    :returns: Expected ``external_output_text_delta`` data payload.
    """
    return {
        "delta": delta,
        "message_id": f"codex:thread_123:{turn_id}:{item_type}:{item_id}",
        "index": 0,
        "final": False,
    }


def _expected_status_data(status: str, turn_id: str) -> dict[str, Any]:
    """
    Build the Omnigent event data expected for one Codex native status edge.

    :param status: Omnigent session status, e.g. ``"running"``.
    :param turn_id: Codex turn id, e.g. ``"turn_123"``.
    :returns: Expected ``external_session_status`` data payload.
    """
    return {"status": status, "response_id": f"codex_{turn_id}"}


def _write_forwarder_bridge(
    bridge_dir: Path,
    *,
    active_turn_id: str | None,
    session_id: str = "conv_123",
    thread_id: str = "thread_123",
) -> None:
    """Write bridge state with overridable identities and an explicit active turn."""
    write_bridge_state(
        bridge_dir,
        CodexNativeBridgeState(
            session_id=session_id,
            socket_path=str(bridge_dir / "app-server.sock"),
            thread_id=thread_id,
            codex_home=str(bridge_dir / "codex-home"),
            active_turn_id=active_turn_id,
        ),
    )


def _recording_forwarder_client(posted: list[dict[str, Any]]) -> httpx.AsyncClient:
    """Capture event payloads in the caller's list; the caller owns client cleanup."""
    return httpx.AsyncClient(
        base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(_capture_handler(posted))
    )


def _forwarder_context(
    client: httpx.AsyncClient,
    bridge_dir: Path,
    *,
    session_id: str = "conv_123",
) -> dict[str, Any]:
    """Build fresh per-call trackers bound to the requested session."""
    return {
        "session_id": session_id,
        "bridge_dir": bridge_dir,
        "usage_coalescer": _usage_coalescer(client, session_id),
        "elicitation_tracker": _elicitation_tracker(),
    }


def _usage_coalescer(
    client: httpx.AsyncClient,
    session_id: str = "conv_123",
) -> codex_native_forwarder._SessionUsageCoalescer:
    """
    Build the required Codex usage coalescer for direct handler tests.

    :param client: HTTP client used by the coalescer.
    :param session_id: Omnigent session id, e.g. ``"conv_123"``.
    :returns: Usage coalescer bound to ``session_id``.
    """
    return codex_native_forwarder._SessionUsageCoalescer(client, session_id)


def _elicitation_tracker() -> codex_native_forwarder._CodexElicitationTaskTracker:
    """
    Build the required Codex elicitation tracker for direct handler tests.

    :returns: Fresh tracker with no pending hook tasks.
    """
    return codex_native_forwarder._CodexElicitationTaskTracker()


def _capture_handler(posted: list[dict[str, Any]]) -> Callable[[httpx.Request], httpx.Response]:
    """
    Build a MockTransport handler that records forwarder event posts.

    :param posted: List to append each decoded request body to.
    :returns: Handler that records the body and returns ``202``.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture one Omnigent event post from the forwarder.

        :param request: HTTP request sent by the forwarder.
        :returns: Accepted response.
        """
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    return handler


def _write_source_rollout(*, codex_home: Path, thread_id: str, source_cwd: str) -> Path:
    """
    Write a realistic source Codex rollout for clone tests.

    Builds the on-disk shape Codex produces: a date-partitioned
    ``sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl`` whose first
    line is ``session_meta`` (carrying the thread ``id`` and ``cwd``),
    followed by a ``turn_context`` (structural ``cwd``) and two
    *historical* records that mention *source_cwd* inside their bodies —
    a developer message and a function_call_output. The historical
    mentions exist to prove the clone leaves them untouched.

    :param codex_home: The ``CODEX_HOME`` to write under, e.g.
        ``Path("/tmp/.../codex-home")``.
    :param thread_id: Thread id / rollout stem, e.g. ``"019e96aa-...."``.
    :param source_cwd: Working directory recorded in the source, e.g.
        ``"/repo/worktree-a"``.
    :returns: Path to the written rollout.
    """
    rollout_dir = codex_home / "sessions" / "2026" / "06" / "05"
    rollout_dir.mkdir(parents=True, exist_ok=True)
    rollout = rollout_dir / f"rollout-2026-06-05T15-23-07-{thread_id}.jsonl"
    records = [
        {
            "timestamp": "2026-06-05T07:23:34.547Z",
            "type": "session_meta",
            "payload": {"id": thread_id, "cwd": source_cwd, "originator": "test"},
        },
        {
            "timestamp": "2026-06-05T07:23:34.549Z",
            "type": "turn_context",
            "payload": {"turn_id": "turn_1", "cwd": source_cwd, "approval_policy": "on-request"},
        },
        {
            "timestamp": "2026-06-05T07:23:34.554Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": f"<env> cwd: {source_cwd} </env>"}],
            },
        },
        {
            "timestamp": "2026-06-05T07:23:40.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "output": f"{source_cwd}/tests/foo.py:42: AssertionError",
            },
        },
    ]
    with rollout.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return rollout
