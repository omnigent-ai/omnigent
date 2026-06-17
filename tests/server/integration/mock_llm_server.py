"""
Mock LLM server with keyed response queues for tests.

Implements the OpenAI Responses API streaming format. Supports
pre-configured response sequences (text, tool calls, errors),
per-request blocking gates, request capture, and **keyed queues**
so concurrent tests / sessions get isolated response streams.

Keyed queues:

Each ``POST /mock/configure`` call specifies an optional ``key``
(defaults to ``"default"``). When ``POST /v1/responses`` arrives,
the server extracts the ``model`` field from the request body and
looks up a queue by that key. If no queue matches the model, the
``"default"`` queue is used. This lets e2e tests register one
queue per agent (keyed by model name) so parent and sub-agent
sessions each get their own response sequence.

Endpoints:

- ``POST /v1/responses`` — consume the next queued response from
  the queue matching the request's ``model`` field.
- ``GET /v1/models`` — return an empty model list (satisfies SDK
  preflight checks).
- ``POST /mock/configure`` — load a keyed response sequence.
- ``POST /mock/reset`` — clear all state.
- ``GET /mock/requests`` — return captured request bodies.
- ``GET /gate/pending`` — check if any request is blocked on a gate.
- ``POST /gate/release`` — release the oldest pending gate.
- ``GET /stats`` — return ``{"request_count": N}``.

Usage::

    python tests/server/integration/mock_llm_server.py 9999

Configuration via ``POST /mock/configure``::

    {
        "key": "mock-model",
        "responses": [
            {"text": "Hello!"},
            {"text": "World!", "block": true},
            {
                "tool_calls": [
                    {"call_id": "c1", "name": "grep", "arguments": "{}"}
                ]
            },
            {"error": "rate limit exceeded", "status_code": 429}
        ]
    }
"""

from __future__ import annotations

import asyncio
import json
import sys
import time as _time_mod
import uuid as _uuid_mod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

# Default queue key when none is specified or no model matches.
_DEFAULT_KEY = "default"


# ── SSE event builders (following Codex pattern) ─────────


def _response_id() -> str:
    """Generate a unique response id."""
    return f"resp_{_uuid_mod.uuid4().hex[:12]}"


def sse_text_response(text: str, model: str = "mock-model") -> str:
    """
    Build a complete SSE stream for a simple text response.

    Emits the full sequence of events the OpenAI Agents SDK expects:
    ``response.created``, ``response.output_item.added``,
    ``response.output_text.done``, ``response.output_item.done``,
    ``response.completed``.

    :param text: The assistant response text.
    :param model: Model name to include in the response.
    :returns: SSE-formatted string.
    """
    resp_id = _response_id()
    msg_id = f"msg_{resp_id}"
    output_tokens = max(5, len(text.split()))
    now = _time_mod.time()

    message_item = {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text}],
    }
    response_obj = {
        "id": resp_id,
        "object": "response",
        "status": "completed",
        "model": model,
        "output": [message_item],
        "parallel_tool_calls": True,
        "tools": [],
        "tool_choice": "auto",
        "usage": {
            "input_tokens": 10,
            "output_tokens": output_tokens,
            "total_tokens": 10 + output_tokens,
        },
        "created_at": now,
        "completed_at": now,
    }
    created_response = {
        **response_obj,
        "status": "in_progress",
        "output": [],
    }

    seq = 0
    events: list[str] = []

    def _add(evt_type: str, **extra: object) -> None:
        nonlocal seq
        data = {"type": evt_type, "sequence_number": seq, **extra}
        events.append(f"event: {evt_type}\ndata: {json.dumps(data)}\n\n")
        seq += 1

    _add("response.created", response=created_response)
    _add(
        "response.output_item.added",
        output_index=0,
        item=message_item,
    )
    _add(
        "response.output_text.done",
        output_index=0,
        item_id=msg_id,
        content_index=0,
        text=text,
    )
    _add(
        "response.output_item.done",
        output_index=0,
        item=message_item,
    )
    _add("response.completed", response=response_obj)
    return "".join(events)


def sse_tool_call_response(
    tool_calls: list[dict[str, str]],
    model: str = "mock-model",
) -> str:
    """
    Build a complete SSE stream for a function call response.

    :param tool_calls: List of tool call dicts, each with
        ``"call_id"``, ``"name"``, and ``"arguments"`` keys.
    :param model: Model name to include in the response.
    :returns: SSE-formatted string.
    """
    resp_id = _response_id()
    now = _time_mod.time()
    output = []
    for tc in tool_calls:
        output.append(
            {
                "id": tc.get("call_id", "call-mock"),
                "type": "function_call",
                "call_id": tc.get("call_id", "call-mock"),
                "name": tc["name"],
                "arguments": tc.get("arguments", "{}"),
                "status": "completed",
            }
        )
    response_obj = {
        "id": resp_id,
        "object": "response",
        "status": "completed",
        "model": model,
        "output": output,
        "parallel_tool_calls": True,
        "tools": [],
        "tool_choice": "auto",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        },
        "created_at": now,
        "completed_at": now,
    }
    created_response = {
        **response_obj,
        "status": "in_progress",
        "output": [],
    }

    seq = 0
    events: list[str] = []

    def _add(evt_type: str, **extra: object) -> None:
        nonlocal seq
        data = {"type": evt_type, "sequence_number": seq, **extra}
        events.append(f"event: {evt_type}\ndata: {json.dumps(data)}\n\n")
        seq += 1

    _add("response.created", response=created_response)
    for idx, item in enumerate(output):
        _add(
            "response.output_item.added",
            output_index=idx,
            item=item,
        )
        _add(
            "response.output_item.done",
            output_index=idx,
            item=item,
        )
    _add("response.completed", response=response_obj)
    return "".join(events)


def sse_streaming_text(text: str, model: str = "mock-model") -> str:
    """
    Build SSE with text deltas followed by a completed event.

    :param text: The assistant response text.
    :param model: Model name.
    :returns: SSE-formatted string with delta events.
    """
    events = []
    for word in text.split():
        delta = {"delta": word + " "}
        events.append(f"event: response.output_text.delta\ndata: {json.dumps(delta)}\n\n")
    events.append(sse_text_response(text, model))
    return "".join(events)


# ── Response queue state ─────────────────────────────────


@dataclass
class QueuedResponse:
    """A single pre-configured response in the queue.

    :param text: Response text (for text responses).
    :param tool_calls: Tool call list (for function call responses).
    :param block: If True, block until gate is released.
    :param stream: If True, stream text deltas before completed.
    :param error: If set, return an error response with this message.
    :param status_code: HTTP status code for error responses.
    """

    text: str = "Mock LLM response"
    tool_calls: list[dict[str, str]] | None = None
    block: bool = False
    stream: bool = False
    error: str | None = None
    status_code: int = 500
    _gate: asyncio.Event = field(default_factory=asyncio.Event)
    _pending: asyncio.Event = field(default_factory=asyncio.Event)


class _ResponseQueue:
    """Per-key FIFO queue of pre-configured responses."""

    def __init__(self) -> None:
        self.responses: list[QueuedResponse] = []
        self.index: int = 0

    def next(self) -> QueuedResponse:
        """Consume the next response, or return a default."""
        if self.index < len(self.responses):
            resp = self.responses[self.index]
            self.index += 1
            return resp
        return QueuedResponse()

    def reset(self) -> None:
        """Clear the queue."""
        self.responses.clear()
        self.index = 0


class MockState:
    """Mutable server state with keyed response queues.

    All mutations are guarded by ``_lock`` so concurrent coroutines
    (e.g. two ``POST /v1/responses`` handlers) don't interleave on
    shared structures.
    """

    def __init__(self) -> None:
        self.queues: dict[str, _ResponseQueue] = {}
        self.captured_requests: list[dict] = []
        self.request_count: int = 0
        self.pending_gates: list[QueuedResponse] = []
        self._lock = asyncio.Lock()

    def get_queue(self, key: str) -> _ResponseQueue:
        """Get or create a queue for *key*."""
        if key not in self.queues:
            self.queues[key] = _ResponseQueue()
        return self.queues[key]

    def resolve_queue(self, model: str | None) -> _ResponseQueue:
        """Find the queue for a request's model field.

        Lookup order:
        1. Exact match on *model* in ``self.queues``.
        2. The ``"default"`` queue.
        3. A lazily-stored ``"default"`` queue (so subsequent
           requests for unknown models share the same queue).
        """
        if model and model in self.queues:
            return self.queues[model]
        if _DEFAULT_KEY in self.queues:
            return self.queues[_DEFAULT_KEY]
        # Lazily create and store the default queue so concurrent
        # requests to unknown models share the same instance.
        self.queues[_DEFAULT_KEY] = _ResponseQueue()
        return self.queues[_DEFAULT_KEY]

    def reset(self) -> None:
        """Clear all state (queues, captured requests, gates).

        Atomically swaps the pending-gates list before releasing
        so a handler that appends between the loop and the clear
        doesn't lose its gate.
        """
        old_gates = self.pending_gates
        self.pending_gates = []
        for qr in old_gates:
            qr._gate.set()
        self.queues.clear()
        self.captured_requests.clear()
        self.request_count = 0


_state = MockState()


# ── Endpoints ────────────────────────────────────────────


@app.post("/v1/responses", response_model=None)
async def create_response(
    request: Request,
) -> StreamingResponse | JSONResponse:
    """
    Accept an LLM request, optionally block on gate, then return SSE.

    Routes to the keyed queue matching the request's ``model`` field.
    Falls back to the ``"default"`` queue when no key matches.
    """
    body = await request.body()
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        parsed = {"raw": body.decode(errors="replace")}

    async with _state._lock:
        _state.request_count += 1
        _state.captured_requests.append(parsed)
        model = parsed.get("model") if isinstance(parsed, dict) else None
        queue = _state.resolve_queue(model)
        qr = queue.next()

    # Error response
    if qr.error is not None:
        return JSONResponse(
            status_code=qr.status_code,
            content={"error": {"message": qr.error, "type": "mock_error"}},
        )

    # Block on gate if configured
    if qr.block:
        qr._pending.set()
        _state.pending_gates.append(qr)
        await qr._gate.wait()

    # Build SSE body
    if qr.tool_calls:
        sse_body = sse_tool_call_response(qr.tool_calls)
    elif qr.stream:
        sse_body = sse_streaming_text(qr.text)
    else:
        sse_body = sse_text_response(qr.text)

    async def _generate() -> AsyncIterator[str]:
        yield sse_body

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
    )


@app.get("/v1/models")
async def list_models() -> dict:
    """Return an empty model list (satisfies SDK preflight checks)."""
    return {"object": "list", "data": []}


@app.post("/mock/configure")
async def configure(request: Request) -> dict[str, object]:
    """
    Load a keyed response sequence.

    Body::

        {
            "key": "mock-model",          // optional, default "default"
            "responses": [{"text": "..."}, ...]
        }

    Multiple calls with different keys accumulate queues; use
    ``POST /mock/reset`` to clear all keys.
    """
    body = await request.json()
    key = body.get("key", _DEFAULT_KEY)
    async with _state._lock:
        queue = _state.get_queue(key)
        queue.reset()
        for entry in body.get("responses", []):
            queue.responses.append(
                QueuedResponse(
                    text=entry.get("text", "Mock LLM response"),
                    tool_calls=entry.get("tool_calls"),
                    block=entry.get("block", False),
                    stream=entry.get("stream", False),
                    error=entry.get("error"),
                    status_code=entry.get("status_code", 500),
                )
            )
        count = len(queue.responses)
    return {"configured": True, "key": key, "count": count}


@app.post("/mock/reset")
async def reset() -> dict[str, bool]:
    """Clear all state (all keyed queues, captured requests, gates)."""
    async with _state._lock:
        _state.reset()
    return {"reset": True}


@app.get("/mock/requests")
async def get_requests(key: str | None = None) -> dict[str, list]:
    """Return captured request bodies, optionally filtered by model.

    :param key: When set, only return requests whose ``model`` field
        matches this key.
    """
    if key is None:
        return {"requests": _state.captured_requests}
    filtered = [
        r for r in _state.captured_requests if isinstance(r, dict) and r.get("model") == key
    ]
    return {"requests": filtered}


@app.get("/gate/pending")
async def gate_pending() -> dict[str, bool]:
    """Check if any request is waiting on a gate."""
    pending = any(qr._pending.is_set() and not qr._gate.is_set() for qr in _state.pending_gates)
    return {"pending": pending}


@app.post("/gate/release")
async def gate_release() -> dict[str, bool]:
    """Release the oldest pending gate."""
    for qr in _state.pending_gates:
        if qr._pending.is_set() and not qr._gate.is_set():
            qr._gate.set()
            return {"released": True}
    return {"released": False}


@app.get("/stats")
async def stats() -> dict[str, int]:
    """Return the total number of LLM requests received."""
    return {"request_count": _state.request_count}


if __name__ == "__main__":
    port = int(sys.argv[1])
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
