"""Responses namespace — streaming, tool loop, polling, cancel, steer.

.. deprecated::
    ``ResponsesNamespace`` (``client.responses.*``) targets the
    deprecated ``/v1/responses`` surface.  New code should use
    ``SessionsNamespace`` (``client.sessions.*``) instead.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import warnings
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ._errors import ToolCallDenied, raise_for_status, require_json_object, response_body
from ._events import (
    ClientTaskCancel,
    CompactionCompleted,
    CompactionFailed,
    CompactionInProgress,
    ElicitationRequest,
    ErrorEvent,
    MessageDone,
    NativeToolCall,
    OutputFileDone,
    ReasoningDelta,
    ReasoningStarted,
    ReasoningSummaryDelta,
    ResponseCancelled,
    ResponseCompleted,
    ResponseCreated,
    ResponseFailed,
    ResponseIncomplete,
    RetryEvent,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolResult,
)
from ._sse import parse_sse_stream
from ._tool_handler import (
    CompactionEndCtx,
    CompactionStartCtx,
    ElicitationRequestCtx,
    FileOutputCtx,
    MessageEndCtx,
    MessageStartCtx,
    NativeToolCallCtx,
    ReasoningEndCtx,
    ResponseEndCtx,
    ResponseStartCtx,
    RetryCtx,
    ServerErrorCtx,
    StreamHooks,
    ToolCallEndCtx,
    ToolCallInfo,
    ToolCallStartCtx,
    ToolHandler,
    ToolResultInfo,
    ToolResultsReadyCtx,
)
from ._tool_handler import (
    ReasoningStartCtx as ReasoningStartHookCtx,
)
from ._types import Response

_log = logging.getLogger("omnigent_client.responses")

# Terminal statuses — the response won't change further.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "incomplete", "cancelled"})

# Async dispatch of client tools flows through the
# action_required handler (``_execute_and_patch``) — same path
# the sub-agent tunnel uses. The SDK tracks the spawned local
# tasks by call_id so :class:`ClientTaskCancel` events can find
# and cancel them; everything else is fire-and-forget and
# self-cleans on done.


async def _call_hook(hook: Any, ctx: Any) -> Any:
    """Call a hook (sync or async) and return its result."""
    if hook is None:
        return None
    result = hook(ctx)
    if inspect.isawaitable(result):
        return await result
    return result


class ResponsesNamespace:
    """Methods for ``/v1/responses`` endpoints.

    .. deprecated::
        This namespace targets the deprecated ``/v1/responses``
        surface.  Use :class:`SessionsNamespace`
        (``client.sessions.*``) instead — see
        ``designs/SESSION_REARCHITECTURE.md``.
    """

    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self._http = http
        self._base = base_url
        self._warned = False

    def _deprecation_warn(self, method: str) -> None:
        """Emit a one-shot ``DeprecationWarning`` per instance.

        :param method: Name of the deprecated method, e.g.
            ``"create"``.
        """
        if not self._warned:
            warnings.warn(
                f"ResponsesNamespace.{method}() is deprecated; use client.sessions.* instead",
                DeprecationWarning,
                stacklevel=3,
            )
            self._warned = True

    async def create(
        self,
        *,
        model: str,
        input: str | list[dict[str, object]],
        background: bool = False,
        instructions: str | None = None,
        previous_response_id: str | None = None,
        tools: list[dict[str, object]] | None = None,
        reasoning: dict[str, str] | None = None,
        model_override: str | None = None,
    ) -> Response:
        """Create a response (blocking, non-streaming).

        :param model: Agent name.
        :param input: User text or content block list.
        :param background: If True, returns immediately (poll via get()).
        :param instructions: Per-request system instructions.
        :param previous_response_id: Prior response for multi-turn.
        :param tools: Client-specified tool schemas.
        :param reasoning: Reasoning config, e.g. ``{"effort": "high"}``.
        :param model_override: Optional per-request LLM model override,
            e.g. ``"openai/gpt-5.4-mini"``. Shadows the spec model.
        :returns: The response object.
        """
        self._deprecation_warn("create")
        body = _build_body(
            model=model,
            input=input,
            stream=False,
            background=background,
            instructions=instructions,
            previous_response_id=previous_response_id,
            tools=tools,
            reasoning=reasoning,
            model_override=model_override,
        )
        resp = await self._http.post(f"{self._base}/v1/responses", json=body)
        raise_for_status(resp.status_code, response_body(resp))
        return Response.from_dict(require_json_object(resp, "POST /v1/responses"))

    async def stream(
        self,
        *,
        model: str,
        input: str | list[dict[str, object]],
        background: bool = False,
        instructions: str | None = None,
        previous_response_id: str | None = None,
        tool_handler: ToolHandler | None = None,
        hooks: StreamHooks | None = None,
        reasoning: dict[str, str] | None = None,
        model_override: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a response, optionally running the tool loop.

        If ``tool_handler`` is provided, the client runs the full tool
        execution loop: stream -> detect tool calls -> execute via handler
        -> POST results -> stream again. The consumer sees one continuous
        sequence of events.

        If ``tool_handler`` is None, yields raw events for a single server
        response.

        :param model: Agent name.
        :param input: User text or content block list.
        :param tool_handler: Optional client-side tool execution config.
        :param hooks: Optional lifecycle hooks.
        """
        self._deprecation_warn("stream")
        if hooks is None:
            hooks = StreamHooks()

        current_input: str | list[dict[str, object]] = input
        current_prev_id = previous_response_id
        iteration = 0
        # Track local asyncio.Tasks spawned by the action_required
        # handler so :class:`ClientTaskCancel` SSE events can find
        # and cancel them. Keyed by ``call_id`` because that's the
        # field the cancel event carries (looked up from the
        # server-side pending_tool_calls row when the cancel fires).
        # Persists across the outer ``while True`` so a task
        # dispatched in one iteration can be cancelled by an event
        # arriving in a later iteration's stream. Each entry
        # self-cleans on done via ``add_done_callback``.
        pending_local_tasks: dict[str, asyncio.Task[None]] = {}

        while True:
            tools = tool_handler.schemas if tool_handler is not None else None
            pending_client_calls: list[ToolCall] = []
            completed_call_ids: set[str] = set()
            current_response_id: str | None = None
            current_session_id: str | None = None
            in_reasoning = False
            reasoning_text = ""
            summary_text = ""
            message_started = False

            body = _build_body(
                model=model,
                input=current_input,
                stream=True,
                background=background,
                instructions=instructions,
                previous_response_id=current_prev_id,
                tools=tools,
                reasoning=reasoning,
                model_override=model_override,
            )

            async with self._http.stream("POST", f"{self._base}/v1/responses", json=body) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise_for_status(resp.status_code, response_body(resp))

                async for event in parse_sse_stream(resp.aiter_bytes()):
                    # ── Fire hooks and collect state ──────────

                    if isinstance(event, ResponseCreated):
                        current_response_id = event.response.id
                        if event.response.conversation is not None:
                            current_session_id = event.response.conversation.id
                        await _call_hook(
                            hooks.on_response_start, ResponseStartCtx(response=event.response)
                        )

                    elif isinstance(event, ReasoningStarted):
                        in_reasoning = True
                        reasoning_text = ""
                        summary_text = ""
                        await _call_hook(hooks.on_reasoning_start, ReasoningStartHookCtx())

                    elif isinstance(event, ReasoningDelta):
                        reasoning_text += event.delta

                    elif isinstance(event, ReasoningSummaryDelta):
                        summary_text += event.delta

                    elif isinstance(event, TextDelta):
                        if in_reasoning:
                            in_reasoning = False
                            await _call_hook(
                                hooks.on_reasoning_end,
                                ReasoningEndCtx(
                                    reasoning_text=reasoning_text, summary_text=summary_text
                                ),
                            )
                        if not message_started:
                            message_started = True
                            await _call_hook(
                                hooks.on_message_start,
                                MessageStartCtx(response_id=current_response_id or ""),
                            )

                    elif isinstance(event, CompactionInProgress):
                        await _call_hook(hooks.on_compaction_start, CompactionStartCtx())

                    elif isinstance(event, CompactionCompleted):
                        item: dict[str, object] = {
                            "type": "response.compaction.completed",
                            "status": "completed",
                            "total_tokens": event.total_tokens,
                        }
                        if event.summary is not None:
                            item["summary"] = event.summary
                        if event.summary_model is not None:
                            item["summary_model"] = event.summary_model
                        if event.compacted_messages is not None:
                            item["compacted_messages"] = event.compacted_messages
                        await _call_hook(hooks.on_compaction_end, CompactionEndCtx(item=item))

                    elif isinstance(event, CompactionFailed):
                        await _call_hook(
                            hooks.on_compaction_end,
                            CompactionEndCtx(
                                item={
                                    "type": "response.compaction.failed",
                                    "status": "failed",
                                },
                            ),
                        )

                    elif isinstance(event, ToolCall):
                        is_client_side = (
                            tool_handler is not None and event.call_id not in completed_call_ids
                        )
                        executed_by = "client" if is_client_side else "server"
                        await _call_hook(
                            hooks.on_tool_call_start,
                            ToolCallStartCtx(
                                name=event.name,
                                arguments=event.arguments,
                                call_id=event.call_id,
                                agent_name=event.agent_name,
                                executed_by=executed_by,
                            ),
                        )
                        # Action_required tool calls (sub-agent
                        # tunnel + sys_call_async-dispatched
                        # client tools) execute locally and PATCH
                        # back while the stream continues.
                        # Tracked by call_id so a follow-up
                        # ``ClientTaskCancel`` event can cancel
                        # the local task.
                        if event.status == "action_required" and tool_handler is not None:
                            completed_call_ids.add(event.call_id)
                            local_task = asyncio.create_task(
                                _execute_and_patch(
                                    self._http,
                                    self._base,
                                    tool_handler,
                                    hooks,
                                    event,
                                    current_response_id or "",
                                    iteration,
                                )
                            )
                            pending_local_tasks[event.call_id] = local_task
                            # Auto-clean once the task settles —
                            # PATCH back, exception, or cancel all
                            # land here. Captured in a closure so
                            # the dict reference stays live.
                            _local_task_call_id = event.call_id

                            def _drop_done(
                                _t: asyncio.Task[None],
                                _key: str = _local_task_call_id,
                            ) -> None:
                                pending_local_tasks.pop(_key, None)

                            local_task.add_done_callback(_drop_done)
                        elif is_client_side:
                            # Synchronous client-side tool calls —
                            # the LLM called the tool directly.
                            # The SDK doesn't run them inline; it
                            # accumulates them and the outer
                            # ``stream()`` loop runs them after
                            # the response terminates and PATCHes
                            # ``tool_results`` to feed the next
                            # iteration. Authors who want
                            # background dispatch use
                            # ``sys_call_async`` instead, which
                            # produces an action_required event
                            # handled by the branch above.
                            pending_client_calls.append(event)

                    elif isinstance(event, ElicitationRequest):
                        # MCP-shape elicitation — the server is
                        # waiting on a verdict. Route to the
                        # elicitation hook (not the tool_handler —
                        # this is not a tool call). If no hook is
                        # registered, fail-closed decline so the
                        # parked workflow doesn't stall forever.
                        # Fire-and-forget elicitation response —
                        # see the peer tunneled-call site above for
                        # why we intentionally drop the task
                        # reference.
                        asyncio.ensure_future(  # noqa: RUF006 — see comment above
                            _handle_elicitation_request(
                                self._http,
                                self._base,
                                hooks,
                                event,
                                current_response_id or "",
                                current_session_id or "",
                            )
                        )

                    elif isinstance(event, ToolResult):
                        completed_call_ids.add(event.call_id)
                        await _call_hook(
                            hooks.on_tool_call_end,
                            ToolCallEndCtx(
                                name="",
                                call_id=event.call_id,
                                agent_name="",
                                output=event.output,
                            ),
                        )

                    elif isinstance(event, ClientTaskCancel):
                        # Server is telling us to stop the local
                        # body. Look up the local task by
                        # call_id (the server includes the
                        # synthetic call_id from the
                        # pending_tool_call row in the SSE
                        # payload) and cancel — the body's
                        # ``except CancelledError`` runs to
                        # cleanup. ``ensure_future``-style fires
                        # without a tracked task remain
                        # un-cancellable; that case is benign
                        # because the server has already decided
                        # the task is cancelled and the late
                        # PATCH back will be a G3 no-op.
                        if event.call_id is not None:
                            tracked_task = pending_local_tasks.get(event.call_id)
                            if tracked_task is not None and not tracked_task.done():
                                tracked_task.cancel()

                    elif isinstance(event, NativeToolCall):
                        await _call_hook(
                            hooks.on_native_tool_call,
                            NativeToolCallCtx(tool_type=event.tool_type, data=event.data),
                        )

                    elif isinstance(event, MessageDone):
                        if in_reasoning:
                            in_reasoning = False
                            await _call_hook(
                                hooks.on_reasoning_end,
                                ReasoningEndCtx(
                                    reasoning_text=reasoning_text, summary_text=summary_text
                                ),
                            )
                        await _call_hook(
                            hooks.on_message_end,
                            MessageEndCtx(content=event.content),
                        )
                        message_started = False

                    elif isinstance(event, OutputFileDone):
                        await _call_hook(
                            hooks.on_file_output,
                            FileOutputCtx(
                                file_id=event.file_id,
                                filename=event.filename,
                                content_type=event.content_type,
                            ),
                        )

                    elif isinstance(event, RetryEvent):
                        await _call_hook(
                            hooks.on_retry,
                            RetryCtx(
                                source=event.source,
                                tool_name=event.tool_name,
                                attempt=event.attempt,
                                max_attempts=event.max_attempts,
                                delay_seconds=event.delay_seconds,
                                error=event.error,
                            ),
                        )

                    elif isinstance(event, ErrorEvent):
                        await _call_hook(
                            hooks.on_server_error,
                            ServerErrorCtx(
                                source=event.source,
                                tool_name=event.tool_name,
                                error=event.error,
                            ),
                        )

                    elif isinstance(
                        event,
                        ResponseCompleted
                        | ResponseFailed
                        | ResponseIncomplete
                        | ResponseCancelled,
                    ):
                        if in_reasoning:
                            in_reasoning = False
                            await _call_hook(
                                hooks.on_reasoning_end,
                                ReasoningEndCtx(
                                    reasoning_text=reasoning_text, summary_text=summary_text
                                ),
                            )
                        status = event.response.status
                        await _call_hook(
                            hooks.on_response_end,
                            ResponseEndCtx(response=event.response, status=status),
                        )

                    # ── Yield event to consumer ──────────────
                    yield event

            # ── Post-stream: tool loop ───────────────────
            if current_response_id is not None:
                current_prev_id = current_response_id

            # Filter out calls that already have server-side results.
            pending_client_calls = [
                tc for tc in pending_client_calls if tc.call_id not in completed_call_ids
            ]

            if not pending_client_calls or tool_handler is None:
                break

            # Execute client-side tools and build results.
            results: list[dict[str, object]] = []
            result_infos: list[ToolResultInfo] = []

            for tc in pending_client_calls:
                call_info = ToolCallInfo(
                    name=tc.name,
                    arguments=tc.arguments,
                    call_id=tc.call_id,
                    agent_name=tc.agent_name,
                    response_id=current_response_id or "",
                    iteration=iteration,
                )
                try:
                    # Sync ``execute`` routes through
                    # ``asyncio.to_thread`` so a blocking body
                    # doesn't serialize the sync-tool loop
                    # (each call would otherwise freeze the
                    # event loop for its duration).
                    output = await _call_execute_off_loop(tool_handler, call_info)
                except ToolCallDenied as exc:
                    output = str(exc)

                await _call_hook(
                    hooks.on_tool_call_end,
                    ToolCallEndCtx(
                        name=tc.name,
                        call_id=tc.call_id,
                        agent_name=tc.agent_name,
                        output=output,
                    ),
                )
                yield ToolResult(call_id=tc.call_id, output=output)

                results.append(
                    {
                        "type": "function_call_output",
                        "call_id": tc.call_id,
                        "output": output,
                    }
                )
                result_infos.append(
                    ToolResultInfo(
                        call_id=tc.call_id,
                        name=tc.name,
                        output=output,
                        agent_name=tc.agent_name,
                    )
                )

            await _call_hook(
                hooks.on_tool_results_ready,
                ToolResultsReadyCtx(results=result_infos, iteration=iteration),
            )

            current_input = results
            iteration += 1

    async def get(self, response_id: str) -> Response:
        """Get a response by ID (poll for status).

        :param response_id: The response/task ID.
        :returns: Current response state.
        """
        self._deprecation_warn("get")
        resp = await self._http.get(f"{self._base}/v1/responses/{response_id}")
        raise_for_status(resp.status_code, response_body(resp))
        return Response.from_dict(require_json_object(resp, "GET /v1/responses/{response_id}"))

    async def poll(
        self,
        response_id: str,
        *,
        interval: float = 0.5,
        tool_handler: ToolHandler | None = None,
    ) -> Response:
        """Poll a background response until it reaches a terminal status.

        If ``tool_handler`` is provided, tunneled tool calls
        (``status: "action_required"``) are executed and PATCHed back.

        :param response_id: The response/task ID.
        :param interval: Seconds between polls.
        :param tool_handler: Optional client-side tool handler.
        :returns: The terminal response.
        """
        while True:
            response = await self.get(response_id)
            if response.status in _TERMINAL_STATUSES:
                return response
            # Check for tunneled tool calls needing client execution.
            if tool_handler is not None:
                await self._handle_polling_tool_calls(response_id, response, tool_handler)
            await asyncio.sleep(interval)

    async def _handle_polling_tool_calls(
        self,
        response_id: str,
        response: Response,
        tool_handler: ToolHandler,
    ) -> None:
        """Execute action_required tool calls found during polling."""
        action_required = [
            item
            for item in response.output
            if isinstance(item, dict)
            and item.get("type") == "function_call"
            and item.get("status") == "action_required"
        ]
        if not action_required:
            return

        tool_results = []
        for fc in action_required:
            name = str(fc.get("name", ""))
            call_id = str(fc.get("call_id", ""))
            args_str = str(fc.get("arguments", "{}"))
            try:
                arguments = json.loads(args_str)
            except json.JSONDecodeError:
                arguments = {}

            call_info = ToolCallInfo(
                name=name,
                arguments=arguments,
                call_id=call_id,
                agent_name=str(fc.get("model", "")),
                response_id=response_id,
                iteration=0,
            )
            output = tool_handler.execute(call_info)
            if inspect.isawaitable(output):
                output = await output
            tool_results.append({"call_id": call_id, "output": output})

        if tool_results:
            await self._patch_tool_results(response_id, tool_results)

    async def _patch_tool_results(
        self,
        response_id: str,
        tool_results: list[dict[str, str]],
    ) -> None:
        """PATCH tool results back to the server."""
        resp = await self._http.patch(
            f"{self._base}/v1/responses/{response_id}",
            json={"tool_results": tool_results},
            timeout=60.0,
        )
        if resp.status_code not in (200, 404, 409):
            _log.warning(
                "PATCH tool results failed (%d): %s",
                resp.status_code,
                resp.text[:200],
            )

    async def steer(
        self,
        response_id: str,
        input: str,
        *,
        model: str,
        reasoning: dict[str, str] | None = None,
        model_override: str | None = None,
    ) -> Response:
        """Send a steering message to an in-progress response.

        :param response_id: The in-progress response ID.
        :param input: Steering text.
        :param model: Agent name.
        :param reasoning: Reasoning config if this steer races into a new response.
        :param model_override: LLM model override if this steer races
            into a new response (server ignores it otherwise).
        :returns: The response (same ID if delivered, new ID if agent finished).
        """
        self._deprecation_warn("steer")
        body: dict[str, object] = {
            "model": model,
            "input": input,
            "previous_response_id": response_id,
            "stream": False,
            "background": True,
        }
        if reasoning is not None:
            body["reasoning"] = reasoning
        if model_override is not None:
            body["model_override"] = model_override
        resp = await self._http.post(
            f"{self._base}/v1/responses",
            json=body,
            timeout=120.0,
        )
        raise_for_status(resp.status_code, response_body(resp))
        return Response.from_dict(require_json_object(resp, "POST /v1/responses"))

    async def cancel(self, response_id: str) -> Response:
        """Cancel an in-progress response.

        :param response_id: The response ID to cancel.
        :returns: The cancelled response.
        """
        self._deprecation_warn("cancel")
        resp = await self._http.post(
            f"{self._base}/v1/responses/{response_id}/cancel",
            timeout=10.0,
        )
        raise_for_status(resp.status_code, response_body(resp))
        return Response.from_dict(
            require_json_object(resp, "POST /v1/responses/{response_id}/cancel")
        )

    async def delete(self, response_id: str) -> None:
        """Delete a response.

        :param response_id: The response ID to delete.
        """
        self._deprecation_warn("delete")
        resp = await self._http.delete(
            f"{self._base}/v1/responses/{response_id}",
        )
        if resp.status_code >= 400:
            raise_for_status(resp.status_code, response_body(resp))


# ── Helpers ──────────────────────────────────────────────


def _build_body(
    *,
    model: str,
    input: str | list[dict[str, object]],
    stream: bool,
    background: bool,
    instructions: str | None,
    previous_response_id: str | None,
    tools: list[dict[str, object]] | None,
    reasoning: dict[str, str] | None,
    model_override: str | None = None,
) -> dict[str, object]:
    """Build the request body for POST /v1/responses."""
    body: dict[str, object] = {
        "model": model,
        "input": input,
        "stream": stream,
    }
    if background:
        body["background"] = True
    if instructions is not None:
        body["instructions"] = instructions
    if previous_response_id is not None:
        body["previous_response_id"] = previous_response_id
    if tools is not None:
        body["tools"] = tools
    if reasoning is not None:
        body["reasoning"] = reasoning
    if model_override is not None:
        body["model_override"] = model_override
    return body


async def _execute_and_patch(
    http: httpx.AsyncClient,
    base_url: str,
    tool_handler: ToolHandler,
    hooks: StreamHooks,
    tool_call: ToolCall,
    root_response_id: str,
    iteration: int,
) -> None:
    """Execute a tunneled sub-agent tool call and PATCH the result back.

    Runs in the background via ``asyncio.ensure_future`` while the
    SSE stream continues.
    """
    call_info = ToolCallInfo(
        name=tool_call.name,
        arguments=tool_call.arguments,
        call_id=tool_call.call_id,
        agent_name=tool_call.agent_name,
        response_id=root_response_id,
        iteration=iteration,
    )
    try:
        # Sync ``execute`` goes through asyncio.to_thread so a
        # blocking body doesn't stall the SSE stream or the
        # TUI's render loop. See ``_call_execute_off_loop``.
        output = await _call_execute_off_loop(tool_handler, call_info)
    except ToolCallDenied as exc:
        output = str(exc)
    except Exception:
        _log.exception("Error executing tunneled tool call %s", tool_call.name)
        output = f"Error executing tool: {tool_call.name}"

    await _call_hook(
        hooks.on_tool_call_end,
        ToolCallEndCtx(
            name=tool_call.name,
            call_id=tool_call.call_id,
            agent_name=tool_call.agent_name,
            output=output,
        ),
    )

    try:
        resp = await http.patch(
            f"{base_url}/v1/responses/{root_response_id}",
            json={"tool_results": [{"call_id": tool_call.call_id, "output": output}]},
            timeout=60.0,
        )
        if resp.status_code not in (200, 404, 409):
            _log.warning(
                "PATCH failed for call_id %s: %s",
                tool_call.call_id,
                resp.text[:200],
            )
    except Exception:
        _log.exception("Error PATCHing tool result for call_id %s", tool_call.call_id)


async def _call_execute_off_loop(
    tool_handler: ToolHandler,
    call_info: ToolCallInfo,
) -> Any:
    """
    Invoke ``tool_handler.execute(call_info)`` without blocking
    the event loop.

    ``execute`` may be ``async def`` (returns a coroutine) or
    sync ``def`` (returns a plain value). If async, we await
    the coroutine on the loop; async bodies are already
    loop-cooperative. If sync, we run it via
    :func:`asyncio.to_thread` so blocking calls inside —
    ``time.sleep``, file I/O, subprocess, ``requests`` — don't
    stall the loop.

    Sync-vs-async is detected via
    :func:`inspect.iscoroutinefunction` so we only invoke
    ``execute`` once. Without the thread bounce, a single
    sync body with ``time.sleep(5)`` would serialize every
    concurrent tool call AND freeze any render loop (like the
    ``omnigent chat`` TUI) sharing the event loop.

    :param tool_handler: Handler whose ``execute`` will run.
    :param call_info: Call context passed through to the handler.
    :returns: Whatever ``execute`` returns (typically a string).
    """
    if inspect.iscoroutinefunction(tool_handler.execute):
        return await tool_handler.execute(call_info)
    return await asyncio.to_thread(tool_handler.execute, call_info)


async def _invoke_elicitation_hook(
    hooks: StreamHooks,
    ctx: ElicitationRequestCtx,
) -> bool:
    """
    Run the consumer's ``on_elicitation_request`` hook fail-closed.

    Returns True only when the hook explicitly accepts. A missing
    hook, an exception from the hook, or a falsy return all map to
    False (decline) — the parked workflow must not stall on an
    elicitation no consumer can answer.

    :param hooks: Stream hooks — the ``on_elicitation_request``
        callable to invoke.
    :param ctx: Context carrying the parsed elicitation, passed to
        the hook verbatim.
    :returns: ``True`` iff the hook explicitly accepted; ``False``
        on missing-hook, exception, or any falsy return.
    """
    if hooks.on_elicitation_request is None:
        _log.info(
            "No on_elicitation_request hook registered; declining elicitation_id=%s policy=%s",
            ctx.elicitation_id,
            ctx.policy_name,
        )
        return False
    try:
        raw = hooks.on_elicitation_request(ctx)
        if inspect.isawaitable(raw):
            raw = await raw
        return bool(raw)
    except Exception:
        _log.exception(
            "on_elicitation_request hook raised for elicitation_id %s; declining fail-closed",
            ctx.elicitation_id,
        )
        return False


async def _post_elicitation_result(
    http: httpx.AsyncClient,
    base_url: str,
    session_id: str,
    elicitation_id: str,
    accepted: bool,
) -> None:
    """
    POST a verdict to one elicitation's dedicated resolve URL.

    URL-based elicitation: the verdict goes to
    ``POST /v1/sessions/{id}/elicitations/{eid}/resolve`` with the
    MCP ``ElicitResult`` body (``{"action": "accept"}`` or
    ``{"action": "decline"}``) — the elicitation id rides in the URL
    path. ``content`` is omitted for binary accept/decline (future
    richer hooks would populate it). This matches the REPL and web
    clients; the server routes all of them through the same
    ``_resolve_elicitation``.

    Status codes the server returns (202/404/409) are all benign —
    the verdict is either applied or moot. Anything else gets a
    warning log; the call still returns normally because the
    background task is fire-and-forget.

    :param http: Shared HTTPX client.
    :param base_url: Server base URL.
    :param session_id: Session/conversation id that owns the
        elicitation's resolve endpoint.
    :param elicitation_id: Server-assigned elicitation id.
    :param accepted: Hook verdict — ``True`` → ``"accept"``,
        ``False`` → ``"decline"``.
    """
    if not session_id:
        _log.warning(
            "Cannot resolve elicitation_id %s: missing session id",
            elicitation_id,
        )
        return
    body = {"action": "accept" if accepted else "decline"}
    try:
        resp = await http.post(
            f"{base_url}/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
            json=body,
            timeout=60.0,
        )
        # 202 = delivered, 404 = session/parked workflow already gone
        # (cancel race), 409 = already completed. All three are
        # benign: the verdict is either applied or moot.
        if resp.status_code not in (202, 404, 409):
            _log.warning(
                "Resolve elicitation_id %s failed: %s",
                elicitation_id,
                resp.text[:200],
            )
    except Exception:
        _log.exception(
            "Error POSTing approval event for elicitation_id %s",
            elicitation_id,
        )


async def _handle_elicitation_request(
    http: httpx.AsyncClient,
    base_url: str,
    hooks: StreamHooks,
    event: ElicitationRequest,
    response_id: str,
    session_id: str,
) -> None:
    """
    Route an elicitation through the hook and POST the verdict.

    Runs on a background task so the SSE stream continues to drain
    while the user is deciding. Two-step: invoke the hook
    (:func:`_invoke_elicitation_hook`), POST the approval event
    (:func:`_post_elicitation_result`).

    :param http: Shared HTTPX client.
    :param base_url: Server base URL.
    :param hooks: Stream hooks — the ``on_elicitation_request``
        hook gets the decision.
    :param event: The parsed elicitation request.
    :param response_id: Root response id (the parked workflow);
        carried into the ctx for the hook's audit/logging only.
    :param session_id: Session/conversation id that emitted the
        elicitation. Mirrored child prompts override this with the
        event's ``target_session_id`` when posting the verdict.
    """
    ctx = ElicitationRequestCtx(
        elicitation_id=event.elicitation_id,
        message=event.message,
        requested_schema=event.requested_schema,
        mode=event.mode,
        phase=event.phase,
        policy_name=event.policy_name,
        content_preview=event.content_preview,
        response_id=response_id,
        url=event.url,
        target_session_id=event.target_session_id,
    )
    accepted = await _invoke_elicitation_hook(hooks, ctx)
    target_session_id = event.target_session_id or session_id
    await _post_elicitation_result(
        http,
        base_url,
        target_session_id,
        event.elicitation_id,
        accepted,
    )
