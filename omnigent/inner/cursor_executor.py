"""CursorExecutor: run agents through the Cursor Python SDK (``cursor-sdk``).

Drives Cursor via :mod:`cursor_sdk` over a local bridge — one persistent
``AsyncAgent`` per Omnigent conversation, created on a
:meth:`cursor_sdk.AsyncClient.launch_bridge` client and reused turn to turn.
Each ``run_turn`` issues one ``agent.send`` and translates the streamed
``run.events()`` (``RunStreamEvent`` objects) into ExecutorEvents:
assistant text → :class:`TextChunk`, thinking → :class:`ReasoningChunk`,
tool calls → :class:`ToolCallRequest` / :class:`ToolCallComplete`, completing
on the run's terminal :class:`cursor_sdk.RunResult`.

Policy enforcement covers three phases: PHASE_LLM_REQUEST (pre-send),
PHASE_LLM_RESPONSE (post-response), and PHASE_TOOL_CALL (native tools).
Cursor's native tools execute inside the Cursor process so they cannot be
pre-blocked, but when a non-bridged tool call is observed the executor
evaluates PHASE_TOOL_CALL and cancels the run on DENY.  Bridged Omnigent
tools (MCP-wrapped) are already gated server-side via the dispatch bridge
and are skipped to avoid double evaluation.

Crucially, Omnigent's spec-declared tools (``sys_session_send`` et al.) are
bridged into Cursor **in-process** via the SDK's ``custom_tools``: each
:class:`~omnigent.inner.executor.ToolSpec` becomes a ``cursor_sdk.CustomTool``
whose ``execute`` callback routes back to the executor's ``_tool_executor`` —
the same pattern the claude-sdk harness uses with its in-process MCP tools. So
a Cursor agent can call ``sys_*``, orchestrate sub-agents, and respect policies,
i.e. full first-party parity. (This replaces the earlier ``cursor-agent acp``
transport, whose ACP mode exposed MCP servers only as read-only *resources*,
never callable tools.)

The SDK's tool-callback server runs on a daemon thread, so each ``execute``
hops back to the main event loop with :func:`asyncio.run_coroutine_threadsafe`.

Auth: a Cursor **API key** (``CURSOR_API_KEY`` or a spec ``api_key``). Unlike
``cursor-agent login``, the SDK requires an API key.

Requirements:
    The ``cursor-sdk`` package must be installed (it bundles / locates the
    local bridge it drives).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

from omnigent.llms._usage_observer import notify_from_dict as _notify_usage_from_dict

from .datamodel import OSEnvSpec
from .executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    ToolSpec,
    TurnComplete,
    classify_tool_result,
)

logger = logging.getLogger(__name__)

# Omnigent's bridged-tool callback: (tool_name, args) -> awaitable result.
# Installed by the runtime adapter (see ``_executor_adapter``); mirrors the
# claude-sdk executor's ``ToolExecutor``.
ToolExecutor: TypeAlias = Callable[[str, dict[str, Any]], Awaitable[Any]]  # type: ignore[explicit-any]

# Cursor's auto model-select, used when a spec pins no cursor model (the SDK
# requires a model for local agents, so unlike the old ACP path we can't pass
# ``None``).
_DEFAULT_CURSOR_MODEL = "auto"

# Upper bound (seconds) on one bridged-tool call: generous (sub-agent dispatches
# can run for minutes) but finite, so a wedged tool surfaces a timeout error
# instead of blocking the SDK's daemon callback thread forever.
_TOOL_CALL_TIMEOUT_S = 1800.0


def _resolve_model(model: str | None) -> str:
    """Resolve the cursor model id, dropping ids cursor can't honor.

    cursor-sdk accepts only Cursor model ids (``auto``, ``gpt-5``,
    ``composer-2.5``, ...), so a gateway-routed model id (carried by a spec
    authored for another harness) falls back to cursor's auto-select. ``None``
    likewise resolves to ``auto`` (the SDK requires a model).
    """
    if not model or model.startswith(("databricks-", "databricks/")):
        if model:
            # Warn, not debug: the requested model is silently NOT honored, and
            # a debug line is invisible in the harness subprocess — so a user who
            # pinned a non-Cursor model would otherwise have no idea it was dropped.
            logger.warning(
                "CursorExecutor: requested model %r is not a Cursor model id; "
                "falling back to %r auto-select.",
                model,
                _DEFAULT_CURSOR_MODEL,
            )
        return _DEFAULT_CURSOR_MODEL
    return model


def _first_of(d: dict[str, Any], *keys: str, default: int = 0) -> int:
    """Return the value of the first key present (and not None) in *d*."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return int(v)
    return default


def _normalize_cursor_usage(raw: dict[str, Any], model: str) -> dict[str, Any]:
    """Map Cursor SDK usage fields to the standard Omnigent usage dict."""
    in_tok = _first_of(raw, "inputTokens", "input_tokens")
    out_tok = _first_of(raw, "outputTokens", "output_tokens")
    total = _first_of(raw, "totalTokens", "total_tokens", default=in_tok + out_tok)
    usage: dict[str, Any] = {
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "total_tokens": total,
        "model": model,
    }
    # Carry cache breakdown if the backend reports it.
    # The Cursor backend sends cacheReadTokens / cacheWriteTokens;
    # map to the Omnigent-standard cache_read_input_tokens /
    # cache_creation_input_tokens names.
    for dst, *sources in (
        (
            "cache_read_input_tokens",
            "cacheReadTokens",
            "cacheReadInputTokens",
            "cache_read_input_tokens",
        ),
        (
            "cache_creation_input_tokens",
            "cacheWriteTokens",
            "cacheCreationInputTokens",
            "cache_creation_input_tokens",
        ),
    ):
        for src in sources:
            val = raw.get(src)
            if val is not None:
                usage[dst] = val
                break
    return usage


def _tools_fingerprint(tools: list[ToolSpec]) -> str:
    """A stable fingerprint of the tool set (names + parameter schemas).

    ``custom_tools`` are fixed at agent creation, so a changed tool set must
    invalidate the persistent agent — otherwise removed tools stay callable and
    newly-added tools are missing for the rest of the conversation.
    """
    entries = sorted(
        (str(t.get("name", "")), json.dumps(t.get("parameters"), sort_keys=True, default=str))
        for t in tools
    )
    return json.dumps(entries)


# ---------------------------------------------------------------------------
# Prompt building (unchanged contract from the ACP harness)
# ---------------------------------------------------------------------------


def _extract_text(msg: Message) -> str:
    """Extract plain text content from a message dict."""
    content = msg.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return " ".join(parts)
    return str(content)


def _latest_user_text(messages: list[Message]) -> str:
    """Return the text of the latest user message (multimodal parts joined)."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return _extract_text(msg)
    return ""


def _build_cursor_prompt(
    messages: list[Message],
    *,
    is_first_turn: bool,
    system_prompt: str,
) -> str:
    """Build the prompt text for an ``agent.send``.

    The SDK agent persists conversation history across ``send`` calls, so on the
    first turn the Omnigent system prompt is prepended (the SDK has no separate
    system-prompt field), and any prior history (a sub-agent with
    ``pass_history=True``) is serialized for context. On subsequent turns the
    agent already holds the history, so only the latest user message is sent.

    :returns: The prompt string (empty when there is nothing to send).
    """
    # Serialize prior history on the first turn whenever there is any (e.g. a
    # ``pass_history=True`` sub-agent handed a single user message plus assistant
    # / tool context) — not only when multiple *user* messages are present, which
    # would drop that context.
    if is_first_turn and len(messages) > 1:
        lines = ["Conversation so far:"]
        for msg in messages:
            role = str(msg.get("role") or "user").replace("_", " ")
            lines.append(f"{role}: {_extract_text(msg)}")
        lines.append("")
        lines.append(
            "Respond to the latest user message, using the conversation above as context."
        )
        body = "\n".join(lines)
    else:
        body = _latest_user_text(messages)

    if is_first_turn and system_prompt:
        return f"{system_prompt}\n\n{body}" if body else system_prompt
    return body


# ---------------------------------------------------------------------------
# SDKMessage → ExecutorEvent
# ---------------------------------------------------------------------------


def _sdk_message_to_events(message: Any) -> list[ExecutorEvent]:  # type: ignore[explicit-any]
    """Map one ``cursor_sdk`` ``SDKMessage`` to zero or more ExecutorEvents.

    Handles the message types the harness surfaces; everything else (status,
    system, task, user echoes) yields nothing.
    """
    mtype = getattr(message, "type", None)
    events: list[ExecutorEvent] = []

    if mtype == "assistant":
        content = getattr(getattr(message, "message", None), "content", ()) or ()
        for block in content:
            if getattr(block, "type", None) == "text":
                text = getattr(block, "text", "") or ""
                if text:
                    events.append(TextChunk(text=text))
        return events

    if mtype == "thinking":
        text = getattr(message, "text", "") or ""
        if text:
            events.append(ReasoningChunk(delta=text, event_type="reasoning_text"))
        return events

    if mtype == "tool_call":
        status = getattr(message, "status", "")
        name = str(getattr(message, "name", "") or "tool")
        raw_args = getattr(message, "args", None)
        args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
        # Cursor surfaces host custom tools under an envelope: name == "mcp",
        # args == {providerIdentifier, toolName, args}. Unwrap to the real
        # Omnigent tool name + args so the observed events (and any name-keyed
        # policy / UI) see the actual tool, not "mcp".
        if "toolName" in args:
            name = str(args.get("toolName") or name)
            inner = args.get("args")
            args = inner if isinstance(inner, dict) else {}
            is_bridged = True
        else:
            is_bridged = False
        call_id = getattr(message, "call_id", None)
        if status == "running":
            events.append(
                ToolCallRequest(
                    name=name, args=args, metadata={"call_id": call_id, "is_bridged": is_bridged}
                )
            )
        elif status in ("completed", "error"):
            result = getattr(message, "result", None)
            classification = classify_tool_result(result)
            tool_status = classification.status
            error = classification.error or None
            if status == "error":
                tool_status = ToolCallStatus.ERROR
                error = error or (str(result) if result else "tool call failed")
            events.append(
                ToolCallComplete(
                    name=name,
                    status=tool_status,
                    result=result,
                    error=error,
                    metadata={"call_id": call_id, "is_bridged": is_bridged},
                )
            )
        return events

    return events


# ---------------------------------------------------------------------------
# Bridged-tool result encoding
# ---------------------------------------------------------------------------


def _tool_error_payload(text: str) -> dict[str, Any]:  # type: ignore[explicit-any]
    """An SDK custom-tool *error* result.

    A mapping with a ``content`` list and ``isError`` is passed through unchanged
    by the SDK's ``_normalize_custom_tool_result``, so the Cursor model sees a
    failure — unlike a bare string, which the SDK wraps as a *successful* result.
    """
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _encode_tool_result(result: Any) -> Any:  # type: ignore[explicit-any]
    """Encode a bridged-tool result for the SDK custom-tool return.

    A dict carrying a truthy ``error`` or ``blocked`` is a dispatch failure or a
    policy block (the shapes ``_bridge_one_dispatch`` / the policy layer return):
    surface it as an ``isError`` payload so the model sees a failure — parity with
    the claude-sdk handler, which the cursor harness otherwise diverged from by
    delivering errors as ordinary, apparently-successful results. Everything else
    returns its text: a ``str`` passthrough (the SDK wraps it as success), else
    JSON.
    """
    if isinstance(result, dict) and (result.get("error") or result.get("blocked")):
        return _tool_error_payload(json.dumps(result, default=str))
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str)
    except (TypeError, ValueError):
        return str(result)


# ---------------------------------------------------------------------------
# CursorExecutor
# ---------------------------------------------------------------------------


@dataclass
class _CursorSessionState:
    """Per-Omnigent-conversation SDK session state."""

    client: Any = None  # cursor_sdk.AsyncClient
    agent: Any = None  # cursor_sdk.AsyncAgent
    system_prompt: str | None = None
    model: str | None = None
    tools_fingerprint: str | None = None
    has_sent_prompt: bool = False


class CursorExecutor(Executor):
    """Execute agent turns via a persistent ``cursor_sdk.AsyncAgent``."""

    def __init__(
        self,
        *,
        cwd: str | None = None,
        os_env: OSEnvSpec | None = None,
        model: str | None = None,
        api_key: str | None = None,
        bundle_dir: Path | None = None,
        agent_name: str | None = None,
        skills_filter: str | list[str] = "all",
    ) -> None:
        """Create a CursorExecutor.

        :param cwd: Working directory the local agent operates in. ``None``
            falls back to ``os_env.cwd`` then the process cwd.
        :param os_env: Optional OS environment / sandbox spec (its ``cwd`` is
            used when *cwd* is unset).
        :param model: Cursor model id (e.g. ``"gpt-5"``); a gateway-routed id
            or ``None`` falls back to cursor's ``auto`` select.
        :param api_key: Cursor API key. ``None`` falls back to ``CURSOR_API_KEY``
            in the environment.
        :param bundle_dir: Reserved for future skill wiring; unused in v1.
        :param agent_name: Optional agent name passed to the SDK.
        :param skills_filter: Accepted for parity; cursor has no skill mechanism here.
        """
        self._cwd = cwd or (os_env.cwd if os_env is not None else None)
        self._os_env_spec = os_env
        self._model_override = model
        self._api_key = api_key or os.environ.get("CURSOR_API_KEY") or None
        self._bundle_dir = bundle_dir
        self._agent_name = agent_name
        self._skills_filter = skills_filter
        self._session_states: dict[str, _CursorSessionState] = {}
        # Installed by the runtime adapter; routes a bridged-tool call back into
        # Omnigent's session (policy gating, sub-agent dispatch, logging).
        self._tool_executor: ToolExecutor | None = None
        # Installed by the runtime adapter; evaluates PHASE_LLM_REQUEST,
        # PHASE_LLM_RESPONSE, and PHASE_TOOL_CALL policies (the same round-trip
        # pi / claude-sdk use). ``None`` on single-process / pre-turn paths
        # (then policy is a no-op).
        self._policy_evaluator: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None

    def supports_streaming(self) -> bool:
        return True

    def supports_tool_calling(self) -> bool:
        return True

    def handles_tools_internally(self) -> bool:
        # Bridged tools execute in-band via the SDK custom_tools callback (which
        # calls ``_tool_executor``), so the runtime adapter must NOT re-dispatch
        # the observed tool events — same contract as claude-sdk.
        return True

    def supports_live_message_queue(self) -> bool:
        # The SDK exposes no confirmed mid-turn steer, so a message can't be
        # injected into a running turn.
        return False

    def _session_key(self, messages: list[Message]) -> str:
        if messages:
            last = messages[-1]
            if last.get("session_id"):
                return str(last["session_id"])
            meta = last.get("metadata", {})
            if isinstance(meta, dict) and meta.get("session_id"):
                return str(meta["session_id"])
        return "__default__"

    # -- native-tool policy gate --------------------------------------------

    async def _evaluate_native_tool_policy(
        self, name: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Evaluate PHASE_TOOL_CALL policy for a Cursor native tool.

        Returns ``{"block": bool, "reason": str}``.
        """
        evaluator = self._policy_evaluator
        if evaluator is None:
            return {"block": False, "reason": ""}
        verdict = await evaluator("PHASE_TOOL_CALL", {"name": name, "arguments": args})
        action = getattr(verdict, "action", None)
        if action == "POLICY_ACTION_DENY":
            return {"block": True, "reason": getattr(verdict, "reason", "") or "blocked by policy"}
        return {"block": False, "reason": ""}

    # -- custom-tool bridge -------------------------------------------------

    def _make_custom_tools(
        self, tools: list[ToolSpec], loop: asyncio.AbstractEventLoop
    ) -> dict[str, Any]:  # type: ignore[explicit-any]
        """Build the SDK ``custom_tools`` mapping from Omnigent ToolSpecs.

        Each tool's ``execute`` runs on the SDK callback server's daemon thread,
        so it hops back to *loop* (the main event loop) to await
        ``_tool_executor`` — the bridge into Omnigent's tool dispatch.
        """
        from cursor_sdk import CustomTool  # lazy: optional dependency

        custom: dict[str, Any] = {}  # type: ignore[explicit-any]
        for spec in tools:
            name = spec.get("name")
            if not isinstance(name, str) or not name:
                continue
            params = spec.get("parameters")
            custom[name] = CustomTool(
                execute=self._make_execute(name, loop),
                description=spec.get("description"),
                input_schema=params
                if isinstance(params, dict)
                else {"type": "object", "properties": {}},
            )
        return custom

    def _make_execute(
        self, tool_name: str, loop: asyncio.AbstractEventLoop
    ) -> Callable[[dict[str, Any], Any], Any]:  # type: ignore[explicit-any]
        """Build a sync ``execute`` that bridges a cursor tool call to Omnigent.

        Runs on the SDK callback server's daemon thread and blocks it on the
        main-loop coroutine via ``run_coroutine_threadsafe``. The wait is bounded
        by ``_TOOL_CALL_TIMEOUT_S`` (generous — ``sys_session_send`` and friends
        can legitimately run for minutes) so a wedged tool surfaces as a tool
        error instead of hanging the daemon thread / Cursor turn forever, and any
        exception (a failed or cancelled coroutine) becomes a tool error rather
        than propagating raw onto the daemon thread.
        """

        def execute(args: dict[str, Any], _ctx: Any) -> Any:  # type: ignore[explicit-any]
            if self._tool_executor is None:
                return _tool_error_payload(
                    f"Tool {tool_name!r} is unavailable: no tool executor wired."
                )
            coro = self._tool_executor(tool_name, dict(args or {}))
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            try:
                result = future.result(timeout=_TOOL_CALL_TIMEOUT_S)
            except concurrent.futures.TimeoutError:
                future.cancel()
                return _tool_error_payload(
                    f"Tool {tool_name!r} timed out after {_TOOL_CALL_TIMEOUT_S:.0f}s."
                )
            # Exception (not BaseException) still covers a cancelled coroutine —
            # future.result() raises concurrent.futures.CancelledError, an
            # Exception — while letting KeyboardInterrupt / SystemExit propagate.
            except Exception as exc:  # noqa: BLE001 — surface as a tool error
                future.cancel()
                return _tool_error_payload(f"Tool {tool_name!r} failed: {exc}")
            return _encode_tool_result(result)

        return execute

    # -- session lifecycle --------------------------------------------------

    async def _ensure_session(
        self, state: _CursorSessionState, model: str, tools: list[ToolSpec]
    ) -> None:
        """Launch the local bridge and create the SDK agent if not already live.

        On any bring-up failure the partially-created client is closed before
        propagating, so a bad ``CURSOR_API_KEY`` / launch error can't orphan a
        bridge subprocess.
        """
        if state.agent is not None:
            return
        try:
            from cursor_sdk import AsyncAgent, AsyncClient, LocalAgentOptions
        except ImportError as exc:
            raise ImportError(
                "CursorExecutor requires the 'cursor-sdk' package. "
                "Install it with: uv pip install cursor-sdk"
            ) from exc

        loop = asyncio.get_running_loop()
        cwd = self._cwd or os.getcwd()
        client = await AsyncClient.launch_bridge(workspace=cwd)
        try:
            local = LocalAgentOptions(
                cwd=cwd,
                custom_tools=self._make_custom_tools(tools, loop) or None,
            )
            agent = await AsyncAgent.create(
                client=client,
                model=model,
                api_key=self._api_key,
                name=self._agent_name,
                local=local,
            )
        except BaseException:
            await _safe_close(client)
            raise
        state.client = client
        state.agent = agent

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        session_key = self._session_key(messages)
        model = _resolve_model((config.model if config else None) or self._model_override)
        tools_fp = _tools_fingerprint(tools)
        state = self._session_states.setdefault(session_key, _CursorSessionState())

        # System prompt, model, and tool set are all fixed at agent creation, so
        # a change to any of them means a fresh agent (otherwise a changed tool
        # set would leave the initial custom_tools stale for the conversation).
        if state.agent is not None and (
            state.system_prompt != system_prompt
            or state.model != model
            or state.tools_fingerprint != tools_fp
        ):
            await self._close_state(state)
            state = _CursorSessionState()
            self._session_states[session_key] = state
        is_first_turn = not state.has_sent_prompt
        state.system_prompt = system_prompt
        state.model = model
        state.tools_fingerprint = tools_fp

        try:
            await self._ensure_session(state, model, tools)
        except Exception as exc:  # noqa: BLE001 — surfaced as ExecutorError (CancelledError propagates)
            await self.close_session(session_key)
            yield ExecutorError(message=f"Failed to start cursor-sdk agent: {exc}")
            return

        prompt = _build_cursor_prompt(
            messages, is_first_turn=is_first_turn, system_prompt=system_prompt
        )
        if not prompt:
            yield TurnComplete(response=None)
            return

        # PHASE_LLM_REQUEST policy (parity with claude-sdk / pi): evaluate before
        # the LLM call so a DENY blocks it. No-op when no evaluator is wired.
        policy_eval = self._policy_evaluator
        if policy_eval is not None:
            req_verdict = await policy_eval(
                "PHASE_LLM_REQUEST",
                {
                    "model": model,
                    "messages_count": sum(1 for m in messages if m.get("role") == "user") or 1,
                    "tools_count": len(tools),
                    "system_prompt_preview": system_prompt[:200] if system_prompt else "",
                    "last_user_message": _latest_user_text(messages)[:500],
                },
            )
            if getattr(req_verdict, "action", "") == "POLICY_ACTION_DENY":
                reason = getattr(req_verdict, "reason", "") or "no reason given"
                yield ExecutorError(message=f"LLM call denied by policy: {reason}")
                return

        state.has_sent_prompt = True
        response_text = ""
        tool_calls = 0
        # A tool call between two assistant text blocks means they are distinct
        # narration segments (pre- vs post-tool); insert a paragraph break so
        # they don't render as one run-on string ("...by the tool.- Exit: 2").
        # Streamed deltas of a single response (no tool between) still
        # concatenate seamlessly, so this never splits one sentence.
        separate_next_text = False
        turn_usage: dict[str, Any] | None = None
        try:
            # Pass SendOptions with on_delta so the backend sends
            # interaction updates (including TurnEndedUpdate with usage).
            # Without enableDeltas the backend omits them entirely.
            from cursor_sdk import SendOptions as _SendOptions  # lazy: optional dep

            def _capture_delta(update: Any) -> None:  # type: ignore[explicit-any]
                nonlocal turn_usage
                if getattr(update, "type", None) == "turn-ended":
                    raw = getattr(update, "usage", None)
                    if isinstance(raw, dict) and raw:
                        turn_usage = _normalize_cursor_usage(raw, model)

            run = await state.agent.send(prompt, options=_SendOptions(on_delta=_capture_delta))
            async for stream_event in run.events():
                if stream_event.sdk_message is not None:
                    for event in _sdk_message_to_events(stream_event.sdk_message):
                        if isinstance(event, TextChunk):
                            if separate_next_text and response_text and event.text:
                                # Guarantee a blank-line (paragraph) boundary between
                                # pre- and post-tool narration, regardless of any single
                                # trailing/leading newline the two blocks already carry
                                # (a lone space or "\n" must still become a blank line).
                                trailing = len(response_text) - len(response_text.rstrip("\n"))
                                leading = len(event.text) - len(event.text.lstrip("\n"))
                                if trailing + leading < 2:
                                    pad = "\n" * (2 - trailing - leading)
                                    event = TextChunk(text=pad + event.text)
                            separate_next_text = False
                            response_text += event.text
                        elif isinstance(event, ToolCallRequest):
                            tool_calls += 1
                            separate_next_text = True
                            # Evaluate PHASE_TOOL_CALL for native tools.
                            # Bridged tools are already gated by the
                            # dispatch bridge — skip to avoid double eval.
                            if not event.metadata.get("is_bridged") and policy_eval is not None:
                                gate = await self._evaluate_native_tool_policy(
                                    event.name, event.args if isinstance(event.args, dict) else {}
                                )
                                if gate["block"]:
                                    # Cancel the run to prevent further
                                    # native tool use.
                                    with contextlib.suppress(Exception):
                                        await run.cancel()
                                    yield event  # emit so observers see what was attempted
                                    reason = gate["reason"]
                                    msg = f"Native tool {event.name!r} denied by policy: {reason}"
                                    yield ExecutorError(message=msg)
                                    return
                        elif isinstance(event, ToolCallComplete):
                            separate_next_text = True
                        yield event
                # Capture usage from TurnEndedUpdate interaction updates.
                iu = stream_event.interaction_update
                if iu is not None and getattr(iu, "type", None) == "turn-ended":
                    raw_usage = getattr(iu, "usage", None)
                    if isinstance(raw_usage, dict) and raw_usage:
                        turn_usage = _normalize_cursor_usage(raw_usage, model)
            result = await run.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the SDK run failed mid-turn
            await self.close_session(session_key)
            yield ExecutorError(message=f"cursor-sdk turn failed: {exc}", retryable=True)
            return

        status = getattr(result, "status", "")
        if status == "error":
            await self.close_session(session_key)
            detail = getattr(result, "result", "") or "cursor-sdk run reported an error"
            yield ExecutorError(message=f"cursor-sdk run error: {detail}", retryable=True)
            return

        # Prefer the streamed text we accumulated (which carries the paragraph
        # breaks inserted above) over the SDK's aggregate ``result`` (which does
        # not) whenever any text was streamed; fall back to ``result`` only when
        # nothing streamed (e.g. a tool-only turn).
        final = response_text or getattr(result, "result", "") or None
        # PHASE_LLM_RESPONSE policy (parity with the peer harnesses): evaluate the
        # completed response before TurnComplete so a DENY blocks persistence.
        if policy_eval is not None:
            resp_verdict = await policy_eval(
                "PHASE_LLM_RESPONSE",
                {
                    "model": model,
                    "text_preview": response_text[:500] if response_text else "",
                    "tool_calls_count": tool_calls,
                },
            )
            if getattr(resp_verdict, "action", "") == "POLICY_ACTION_DENY":
                reason = getattr(resp_verdict, "reason", "") or "no reason given"
                yield ExecutorError(message=f"LLM response denied by policy: {reason}")
                return

        if turn_usage:
            _notify_usage_from_dict(model=model, usage=turn_usage)
        yield TurnComplete(response=final, usage=turn_usage)

    async def _close_state(self, state: _CursorSessionState) -> None:
        if state.agent is not None:
            await _safe_close(state.agent)
            state.agent = None
        if state.client is not None:
            await _safe_close(state.client)
            state.client = None

    async def close_session(self, session_key: str) -> None:
        state = self._session_states.pop(session_key, None)
        if state is not None:
            await self._close_state(state)

    async def interrupt_session(self, session_key: str) -> bool:
        state = self._session_states.get(session_key)
        if state is None:
            return False
        # Drop the session so the next turn starts a fresh agent — mirrors the
        # pi/cursor-acp executors (a resumed turn would bypass the runner's
        # interrupt marker).
        try:
            await self.close_session(session_key)
            return True
        except Exception as exc:  # noqa: BLE001 — close failures surface as False
            logger.debug("CursorExecutor: close after interrupt failed: %s", exc)
            return False

    async def close(self) -> None:
        for key in list(self._session_states.keys()):
            await self.close_session(key)


async def _safe_close(obj: Any) -> None:  # type: ignore[explicit-any]
    """Best-effort async close of a ``cursor_sdk`` object, preferring ``aclose()``.

    The SDK's :class:`cursor_sdk.AsyncClient` exposes only ``aclose()`` — and
    that is the *only* path that terminates the launched bridge subprocess and
    shuts down the tool-callback server's daemon HTTP thread. :class:`AsyncAgent`
    exposes ``close()`` instead. Calling a method the object doesn't have raised
    ``AttributeError`` (swallowed below), so the client was never torn down and
    every session leaked its bridge subprocess + daemon thread. Prefer ``aclose``
    and fall back to ``close``; a teardown failure must not mask the original
    error or leave the closer raising.
    """
    closer = getattr(obj, "aclose", None) or getattr(obj, "close", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception as exc:  # noqa: BLE001 — best-effort teardown
        logger.debug("CursorExecutor: close failed: %s", exc)
