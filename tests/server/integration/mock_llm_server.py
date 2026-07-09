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


def json_text_response(text: str, model: str = "mock-model") -> dict:
    """
    Build a non-streaming Responses API JSON body for a text response.

    Used when the request does NOT include ``stream: true`` — for example,
    the cost-advisor judge calls ``responses.create`` without streaming and
    the OpenAI adapter calls ``_send_request`` which expects a plain JSON dict.

    :param text: The assistant response text.
    :param model: Model name to include in the response.
    :returns: Responses API response dict.
    """
    resp_id = _response_id()
    msg_id = f"msg_{resp_id}"
    output_tokens = max(5, len(text.split()))
    now = _time_mod.time()
    return {
        "id": resp_id,
        "object": "response",
        "status": "completed",
        "model": model,
        "output": [
            {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
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


def sse_text_with_native_items(
    text: str,
    native_items: list[dict],
    model: str = "mock-model",
) -> str:
    """Build SSE with text + native tool output items (e.g. web_search_call).

    Native items are emitted as ``response.output_item.done`` events
    before the text message, matching the real API's ordering.
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
    all_output: list[dict] = [*native_items, message_item]
    response_obj = {
        "id": resp_id,
        "object": "response",
        "status": "completed",
        "model": model,
        "output": all_output,
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
    for idx, item in enumerate(all_output):
        _add("response.output_item.added", output_index=idx, item=item)
        if item.get("type") == "message":
            _add(
                "response.output_text.done",
                output_index=idx,
                item_id=msg_id,
                content_index=0,
                text=text,
            )
        _add("response.output_item.done", output_index=idx, item=item)
    _add("response.completed", response=response_obj)
    return "".join(events)


# ── Anthropic Messages API SSE builders ─────────────────


def anthropic_sse_text_response(
    text: str,
    model: str = "mock-model",
) -> str:
    """Build Anthropic Messages API SSE stream for a text response.

    Emits: ``message_start``, ``content_block_start``,
    ``content_block_delta`` (text), ``content_block_stop``,
    ``message_delta``, ``message_stop``.
    """
    msg_id = f"msg_{_uuid_mod.uuid4().hex[:12]}"
    output_tokens = max(5, len(text.split()))

    events: list[str] = []

    def _evt(evt_type: str, data: dict) -> None:
        events.append(f"event: {evt_type}\ndata: {json.dumps(data)}\n\n")

    _evt(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 0},
            },
        },
    )
    _evt(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    )
    _evt(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        },
    )
    _evt(
        "content_block_stop",
        {
            "type": "content_block_stop",
            "index": 0,
        },
    )
    _evt(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        },
    )
    _evt("message_stop", {"type": "message_stop"})
    return "".join(events)


def anthropic_sse_tool_call_response(
    tool_calls: list[dict[str, str]],
    model: str = "mock-model",
) -> str:
    """Build Anthropic Messages API SSE stream for tool use blocks."""
    msg_id = f"msg_{_uuid_mod.uuid4().hex[:12]}"
    events: list[str] = []

    def _evt(evt_type: str, data: dict) -> None:
        events.append(f"event: {evt_type}\ndata: {json.dumps(data)}\n\n")

    _evt(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 0},
            },
        },
    )
    for idx, tc in enumerate(tool_calls):
        tool_id = tc.get("call_id", f"toolu_{_uuid_mod.uuid4().hex[:12]}")
        _evt(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": idx,
                "content_block": {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": tc["name"],
                    "input": {},
                },
            },
        )
        _evt(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": idx,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": tc.get("arguments", "{}"),
                },
            },
        )
        _evt(
            "content_block_stop",
            {
                "type": "content_block_stop",
                "index": idx,
            },
        )
    _evt(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 5},
        },
    )
    _evt("message_stop", {"type": "message_stop"})
    return "".join(events)


# ── Response queue state ─────────────────────────────────


@dataclass
class QueuedResponse:
    """A single pre-configured response in the queue.

    :param text: Response text (for text responses).
    :param tool_calls: Tool call list (for function call responses).
    :param native_items: Raw output items (e.g. ``web_search_call``)
        included alongside text in the response.
    :param block: If True, block until gate is released.
    :param stream: If True, stream text deltas before completed.
    :param error: If set, return an error response with this message.
    :param status_code: HTTP status code for error responses.
    """

    text: str = "Mock LLM response"
    tool_calls: list[dict[str, str]] | None = None
    native_items: list[dict] | None = None
    block: bool = False
    stream: bool = False
    error: str | None = None
    status_code: int = 500
    _gate: asyncio.Event = field(default_factory=asyncio.Event)
    _pending: asyncio.Event = field(default_factory=asyncio.Event)


class _ResponseQueue:
    """Per-key FIFO queue of pre-configured responses.

    An optional *fallback* response is returned when the queue is
    exhausted.  Unlike regular entries, the fallback is NOT cleared by
    :meth:`reset` — it persists for the lifetime of the queue instance
    so session-level callers (e.g. a policy-classifier LLM configured
    by ``live_server``) always receive a valid response even when
    per-test ``reset_mock_llm`` calls clear the regular queue.
    """

    def __init__(self) -> None:
        self.responses: list[QueuedResponse] = []
        self.index: int = 0
        self.fallback: QueuedResponse | None = None
        # Optional content-routing token. When set, a request is served
        # from this queue if the token appears in the request's
        # role="user" input text — regardless of the request's ``model``.
        # Lets each test claim its own queue by the (unique) message it
        # sends, so a stray/late request from another test (which carries
        # a different user message) can't draw from this test's queue —
        # the #523 cross-test contamination, fixed without per-test
        # servers. ``None`` preserves the default model/"default" routing.
        self.match: str | None = None

    def next(self) -> QueuedResponse:
        """Consume the next response, or return the fallback / default."""
        if self.index < len(self.responses):
            resp = self.responses[self.index]
            self.index += 1
            return resp
        if self.fallback is not None:
            return self.fallback
        return QueuedResponse()

    def reset(self) -> None:
        """Clear the regular queue + content-routing token; fallback is preserved."""
        self.responses.clear()
        self.index = 0
        self.match = None


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

    @staticmethod
    def _user_input_text(parsed: object) -> str:
        """Concatenate the text of all ``role="user"`` items in the request.

        Endpoint-agnostic: reads BOTH the Responses-API ``input`` array
        (``/v1/responses``) AND the ``messages`` array used by the
        Anthropic Messages (``/v1/messages``) and OpenAI Chat
        (``/v1/chat/completions``) endpoints — so content routing behaves
        identically no matter which endpoint ``resolve_queue_for_request``
        is called from, rather than silently degrading to model routing
        for ``messages``-shaped requests.

        Scoped to user-role content deliberately — NOT the system prompt
        (``instructions`` / ``system``) or tool outputs — so a
        content-routing token only ever matches what the test itself
        typed, never incidental words in a shared system prompt. Content
        may be a plain string or a list of ``{"type","text"}`` blocks
        (both the Responses and Messages shapes), so both are walked.

        :param parsed: The parsed request body.
        :returns: Space-joined user message text (``""`` if none).
        """
        if not isinstance(parsed, dict):
            return ""
        parts: list[str] = []
        # ``input`` (Responses API) and ``messages`` (Anthropic / Chat)
        # are mutually exclusive in practice, but walk both so the helper
        # is correct for every endpoint that routes through it.
        for key in ("input", "messages"):
            for item in parsed.get(key) or []:
                if not isinstance(item, dict) or item.get("role") != "user":
                    continue
                content = item.get("content")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            parts.append(str(block.get("text", "")))
        return " ".join(parts)

    def resolve_queue_for_request(self, parsed: object) -> _ResponseQueue:
        """Pick the queue for a request: content-routed queues first, then model/default.

        A queue with a ``match`` token wins if that token appears in the
        request's user input — this is how a test claims its own queue by
        the unique message it sends. Otherwise falls back to the existing
        ``model`` / ``"default"`` routing, so tests that don't opt in are
        unaffected.

        When several match-queues are live at once (e.g. a sub-agent test
        with distinct parent/worker queues), the LONGEST matching token
        wins — deterministic regardless of dict order, and robust if one
        token is a substring of another. Tests should still pick mutually
        non-substring tokens; longest-match is a safety net, not a
        license to overlap.

        :param parsed: The parsed request body.
        :returns: The selected response queue.
        """
        user_text = self._user_input_text(parsed)
        if user_text:
            best: _ResponseQueue | None = None
            for queue in self.queues.values():
                if (
                    queue.match
                    and queue.match in user_text
                    and (best is None or len(queue.match) > len(best.match or ""))
                ):
                    best = queue
            if best is not None:
                return best
        model = parsed.get("model") if isinstance(parsed, dict) else None
        return self.resolve_queue(model)

    def reset(self) -> None:
        """Clear all state (queues, captured requests, gates).

        Queues that have a fallback response set (via ``POST /mock/set_fallback``)
        are not deleted — their regular responses are cleared but the fallback
        is preserved so session-level callers (e.g. the policy-classifier LLM)
        continue to receive a valid response after per-test resets.

        Atomically swaps the pending-gates list before releasing
        so a handler that appends between the loop and the clear
        doesn't lose its gate.
        """
        old_gates = self.pending_gates
        self.pending_gates = []
        for qr in old_gates:
            qr._gate.set()
        # Preserve queues that have a non-resettable fallback; delete others.
        for key in list(self.queues):
            queue = self.queues[key]
            if queue.fallback is not None:
                queue.reset()  # clear responses/index, keep fallback
            else:
                del self.queues[key]
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
        queue = _state.resolve_queue_for_request(parsed)
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

    # When the request does not include ``stream: true``, return a plain
    # JSON body (non-streaming Responses API format).  This supports callers
    # like the cost-advisor judge that call ``responses.create`` without
    # streaming and use ``_send_request`` which calls ``resp.json()``.
    # Tool-call responses and native-item responses are streaming-only; fall
    # through to SSE for those.
    is_streaming = isinstance(parsed, dict) and parsed.get("stream")
    if not is_streaming and not qr.tool_calls and not qr.native_items:
        model_name = (
            parsed.get("model", "mock-model") if isinstance(parsed, dict) else "mock-model"
        )
        return JSONResponse(content=json_text_response(qr.text or "", model=model_name))

    # Build SSE body
    if qr.tool_calls:
        sse_body = sse_tool_call_response(qr.tool_calls)
    elif qr.stream:
        sse_body = sse_streaming_text(qr.text)
    elif qr.native_items:
        sse_body = sse_text_with_native_items(qr.text, qr.native_items)
    else:
        sse_body = sse_text_response(qr.text)

    async def _generate() -> AsyncIterator[str]:
        yield sse_body

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
    )


@app.post("/v1/messages", response_model=None)
async def create_message(
    request: Request,
) -> StreamingResponse | JSONResponse:
    """Anthropic Messages API endpoint for claude-sdk harness.

    Same keyed-queue routing as ``/v1/responses`` but returns
    Anthropic SSE format (``message_start``, ``content_block_*``,
    ``message_delta``, ``message_stop``).
    """
    body = await request.body()
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        parsed = {"raw": body.decode(errors="replace")}

    async with _state._lock:
        _state.request_count += 1
        _state.captured_requests.append(parsed)
        queue = _state.resolve_queue_for_request(parsed)
        qr = queue.next()

    if qr.error is not None:
        return JSONResponse(
            status_code=qr.status_code,
            content={
                "type": "error",
                "error": {"type": "mock_error", "message": qr.error},
            },
        )

    if qr.block:
        qr._pending.set()
        _state.pending_gates.append(qr)
        await qr._gate.wait()

    if qr.tool_calls:
        sse_body = anthropic_sse_tool_call_response(qr.tool_calls)
    else:
        sse_body = anthropic_sse_text_response(qr.text)

    async def _generate() -> AsyncIterator[str]:
        yield sse_body

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
    )


@app.post("/v1/chat/completions", response_model=None)
async def create_chat_completion(
    request: Request,
) -> StreamingResponse | JSONResponse:
    """OpenAI Chat Completions API endpoint (for pi and legacy harnesses).

    Returns a non-streaming JSON response in Chat Completions format,
    routing through the same keyed queue as /v1/responses.
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
        queue = _state.resolve_queue_for_request(parsed)
        qr = queue.next()

    if qr.error is not None:
        return JSONResponse(
            status_code=qr.status_code,
            content={"error": {"message": qr.error, "type": "mock_error"}},
        )

    if qr.block:
        qr._pending.set()
        _state.pending_gates.append(qr)
        await qr._gate.wait()

    text = qr.text if not qr.tool_calls else ""
    # Render queued tool_calls in Chat Completions format (harnesses on the
    # openai-completions wire — e.g. pi's gateway models.json — POST here, not
    # /v1/responses). Without this, a tool_call response collapsed to empty
    # content and the tool round-trip silently produced nothing.
    cc_tool_calls = (
        [
            {
                "id": tc.get("call_id", "call-mock"),
                "type": "function",
                "function": {
                    "name": tc.get("name", ""),
                    "arguments": tc.get("arguments", "{}"),
                },
            }
            for tc in qr.tool_calls
        ]
        if qr.tool_calls
        else None
    )
    finish_reason = "tool_calls" if cc_tool_calls else "stop"
    cc_message: dict[str, object] = {"role": "assistant", "content": text or None}
    if cc_tool_calls:
        cc_message["tool_calls"] = cc_tool_calls
    resp_id = _response_id()
    body_json = {
        "id": f"chatcmpl-{resp_id}",
        "object": "chat.completion",
        "model": model or "mock-model",
        "choices": [
            {
                "index": 0,
                "message": cc_message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": max(5, len(text.split())),
            "total_tokens": 15,
        },
    }
    if parsed.get("stream"):
        delta: dict[str, object] = {"role": "assistant", "content": text or None}
        if cc_tool_calls:
            delta["tool_calls"] = [{"index": i, **tc} for i, tc in enumerate(cc_tool_calls)]
        chunk = {
            "id": f"chatcmpl-{resp_id}",
            "object": "chat.completion.chunk",
            "model": model or "mock-model",
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }
            ],
        }

        async def _stream() -> AsyncIterator[str]:
            yield f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"

        return StreamingResponse(_stream(), media_type="text/event-stream")
    return JSONResponse(content=body_json)


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
            "match": "mangosteen-tr",     // optional content-routing token
            "responses": [{"text": "..."}, ...]
        }

    When ``match`` is set, a request is served from this queue if the
    token appears in the request's ``role="user"`` input text, regardless
    of the request's ``model`` — so a test can claim its own queue by the
    unique message it sends (per-test isolation against cross-test
    contamination). Omitting ``match`` keeps the default model/"default"
    routing.

    Multiple calls with different keys accumulate queues; use
    ``POST /mock/reset`` to clear all keys.
    """
    body = await request.json()
    key = body.get("key", _DEFAULT_KEY)
    match = body.get("match")
    async with _state._lock:
        queue = _state.get_queue(key)
        queue.reset()
        queue.match = match
        for entry in body.get("responses", []):
            queue.responses.append(
                QueuedResponse(
                    text=entry.get("text", "Mock LLM response"),
                    tool_calls=entry.get("tool_calls"),
                    native_items=entry.get("native_items"),
                    block=entry.get("block", False),
                    stream=entry.get("stream", False),
                    error=entry.get("error"),
                    status_code=entry.get("status_code", 500),
                )
            )
        count = len(queue.responses)
    return {"configured": True, "key": key, "count": count}


@app.post("/mock/set_fallback")
async def set_fallback(request: Request) -> dict[str, object]:
    """Set a non-resettable fallback response for a queue key.

    The fallback is returned when the regular queue for *key* is
    exhausted.  Unlike regular entries (configured via
    ``POST /mock/configure``), the fallback survives
    ``POST /mock/reset`` — it persists for the lifetime of the server
    process.  Use this for session-level queues that must return a
    valid response even when per-test resets clear the regular queue
    (e.g. the server-level policy-classifier LLM queue).

    Body: ``{"key": "<key>", "text": "<response-text>", "stream": false}``

    ``stream`` (optional, default false): when true the fallback emits
    ``response.output_text.delta`` events (one per word) before the completed
    event — so a caller that subscribes to a streaming response sees
    incremental deltas from the fallback, not just a single completed body.
    """
    body = await request.json()
    key = body.get("key", _DEFAULT_KEY)
    text = body.get("text", "Mock LLM response")
    stream = bool(body.get("stream", False))
    async with _state._lock:
        queue = _state.get_queue(key)
        queue.fallback = QueuedResponse(text=text, stream=stream)
    return {"fallback_set": True, "key": key}


@app.post("/mock/reset")
async def reset() -> dict[str, bool]:
    """Clear all regular queues, captured requests, and gates.

    Fallbacks set via ``POST /mock/set_fallback`` are preserved.
    """
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
