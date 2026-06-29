"""CopilotExecutor: run agents through the GitHub Copilot SDK (``github-copilot-sdk``).

Drives GitHub Copilot via :mod:`copilot` (the ``github-copilot-sdk`` package) —
one persistent :class:`copilot.CopilotClient` + :class:`copilot.CopilotSession`
per Omnigent conversation, created once and reused turn to turn. Each
``run_turn`` issues one ``session.send_and_wait`` and translates the session's
streamed :class:`copilot.SessionEvent` objects into ExecutorEvents:
assistant text deltas → :class:`TextChunk`, reasoning deltas →
:class:`ReasoningChunk`, tool execution → :class:`ToolCallRequest` /
:class:`ToolCallComplete`, completing on ``ASSISTANT_TURN_END`` /
``SESSION_IDLE`` (when ``send_and_wait`` returns).

Crucially, Omnigent's spec-declared tools (``sys_session_send`` et al.) are
bridged into Copilot **in-process** via the SDK's ``tools``: each
:class:`~omnigent.inner.executor.ToolSpec` becomes a :class:`copilot.Tool`
whose async ``handler`` routes back to the executor's ``_tool_executor`` — the
same pattern the claude-sdk / cursor harnesses use. So a Copilot agent can call
``sys_*``, orchestrate sub-agents, and respect policies, i.e. full first-party
parity. Unlike cursor's daemon-thread bridge, the Copilot SDK awaits tool
handlers *in its own event loop* (``CopilotSession._execute_tool_and_respond``),
so the bridged handler is a plain coroutine that ``await``\\s ``_tool_executor``
directly — no ``run_coroutine_threadsafe`` hop.

Known limitation — Copilot's **native** tools (its own ``create`` / ``view`` /
``edit`` / ``bash``) run *inside* the SDK and never flow through Omnigent's
bridged-tool dispatch, so (a) they are NOT gated by ``on:[tool_call]`` policies
(e.g. ``blast_radius``) and (b) they leave no ``function_call`` item in the
persisted transcript — only the streamed narration is recorded. This mirrors the
cursor harness's built-in tools. Bridged ``sys_*`` tools ARE policy-gated and
recorded. Don't rely on ``on:[tool_call]`` guardrails to constrain Copilot's
built-in file/shell tools; gate at the LLM phase (``PHASE_LLM_REQUEST`` /
``PHASE_LLM_RESPONSE``, which DO fire) or via the OS-env sandbox instead.

Auth: a **GitHub token** that carries Copilot access — a fine-grained PAT with
the "Copilot Requests" permission, or an OAuth token from the GitHub CLI (``gh``)
/ Copilot CLI app. Resolved from a spec ``api_key`` or the ambient
``COPILOT_GITHUB_TOKEN`` / ``GH_TOKEN`` / ``GITHUB_TOKEN`` (the same precedence
the bundled CLI uses). Classic ``ghp_`` tokens are not accepted by Copilot.

Requirements:
    The ``github-copilot-sdk`` package must be installed (it bundles the
    Copilot CLI binary it drives as a backing server). Installed via the
    ``copilot`` extra; imported lazily so a missing install surfaces as a
    request-time error, not an app-boot crash.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeAlias

from omnigent.llms._usage_observer import notify_from_dict as _notify_usage_from_dict
from omnigent.reasoning_effort import COPILOT_EFFORTS, validate_effort

from .datamodel import OSEnvSpec
from .executor import (
    CompactionComplete,
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
# claude-sdk / cursor executors' ``ToolExecutor``.
ToolExecutor: TypeAlias = Callable[[str, dict[str, Any]], Awaitable[Any]]  # type: ignore[explicit-any]

# Ambient GitHub-token env vars, in the precedence the Copilot CLI/SDK itself
# honors (``copilot login --help``): a fine-grained PAT with the "Copilot
# Requests" permission, or an OAuth token from the gh / Copilot CLI app.
GITHUB_TOKEN_ENV_VARS = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")

# Upper bound (seconds) on one ``send_and_wait``. The SDK default is 60s, far
# too short for an agentic turn (sub-agent dispatches, long tool calls), so we
# pass a generous finite bound: a wedged turn surfaces a timeout error instead
# of hanging forever. The runner enforces its own per-turn timeout on top.
_SEND_TIMEOUT_S = 3600.0


def _resolve_model(model: str | None) -> str | None:
    """Resolve the Copilot model id, dropping ids Copilot can't honor.

    The Copilot SDK accepts only ids in the account's catalog (``auto``,
    ``claude-haiku-4.5``, ``gpt-5-mini``, ...), so a gateway-routed model id
    (carried by a spec authored for another harness) falls back to Copilot's
    own auto-select (``None`` → the SDK picks). ``None`` likewise lets the SDK
    choose.
    """
    if not model or model.startswith(("databricks-", "databricks/")):
        if model:
            # Warn, not debug: the requested model is silently NOT honored, and
            # a debug line is invisible in the harness subprocess — so a user who
            # pinned a non-Copilot model would otherwise have no idea it was dropped.
            logger.warning(
                "CopilotExecutor: requested model %r is not a Copilot model id; "
                "falling back to Copilot's auto-select.",
                model,
            )
        return None
    return model


def _resolve_reasoning_effort(config: ExecutorConfig | None) -> str | None:
    """Resolve the per-turn Copilot reasoning effort from ``config.extra``.

    The runtime adapter threads a web ``/reasoning`` pick into
    ``config.extra["reasoning_effort"]`` (see
    :class:`~omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter`).
    The Copilot SDK exposes it only as ``create_session(reasoning_effort=...)``,
    so like the model it is fixed at session creation (a change recreates the
    session in :meth:`run_turn`). ``None`` lets the model use its default.

    A value Copilot can't honor is dropped with a warning rather than raising:
    an unsupported effort must not sink the whole turn (parity with the codex
    native path). Per-model support is enforced by the Copilot backend.

    Note: ``config.extra`` may also carry ``max_tokens`` (set by the adapter),
    but the Copilot SDK has no per-turn output-token cap — the only
    ``max_output_tokens`` lever is a model *capability* override folded into
    context-window math, not a generation limit — so it is intentionally not
    forwarded here.
    """
    if config is None:
        return None
    raw_effort = config.extra.get("reasoning_effort")
    try:
        return validate_effort(raw_effort, "copilot", COPILOT_EFFORTS)
    except ValueError:
        logger.warning("Ignoring unsupported copilot reasoning effort: %r", raw_effort)
        return None


def _tools_fingerprint(tools: list[ToolSpec]) -> str:
    """A stable fingerprint of the tool set (names + parameter schemas).

    ``tools`` are fixed at session creation, so a changed tool set must
    invalidate the persistent session — otherwise removed tools stay callable
    and newly-added tools are missing for the rest of the conversation.
    """
    entries = sorted(
        (str(t.get("name", "")), json.dumps(t.get("parameters"), sort_keys=True, default=str))
        for t in tools
    )
    return json.dumps(entries)


# ---------------------------------------------------------------------------
# Prompt building
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


def _build_copilot_prompt(messages: list[Message], *, is_first_turn: bool) -> str:
    """Build the prompt text for a ``send_and_wait``.

    The SDK session persists conversation history across ``send`` calls and the
    Omnigent system prompt is delivered separately (``system_message``), so on
    the first turn any prior history (e.g. a ``pass_history=True`` sub-agent that
    handed a single user message plus assistant / tool context) is serialized for
    context. On subsequent turns the session already holds the history, so only
    the latest user message is sent.

    :returns: The prompt string (empty when there is nothing to send).
    """
    if is_first_turn and len(messages) > 1:
        lines = ["Conversation so far:"]
        for msg in messages:
            role = str(msg.get("role") or "user").replace("_", " ")
            lines.append(f"{role}: {_extract_text(msg)}")
        lines.append("")
        lines.append(
            "Respond to the latest user message, using the conversation above as context."
        )
        return "\n".join(lines)
    return _latest_user_text(messages)


# ---------------------------------------------------------------------------
# Bridged-tool result encoding
# ---------------------------------------------------------------------------


def _encode_tool_result(result: Any) -> Any:  # type: ignore[explicit-any]
    """Encode a bridged-tool result as a :class:`copilot.ToolResult`.

    A dict carrying a truthy ``error`` or ``blocked`` is a dispatch failure or a
    policy block (the shapes ``_bridge_one_dispatch`` / the policy layer return):
    surface it as a ``failure`` result so the Copilot model sees the failure —
    parity with the claude-sdk / cursor handlers. Everything else is a success:
    a ``str`` is passed through; anything else is JSON-encoded.
    """
    from copilot import ToolResult  # lazy: optional dependency

    if isinstance(result, dict) and (result.get("error") or result.get("blocked")):
        text = json.dumps(result, default=str)
        return ToolResult(text_result_for_llm=text, result_type="failure", error=text)
    if isinstance(result, str):
        return ToolResult(text_result_for_llm=result)
    try:
        text = json.dumps(result, default=str)
    except (TypeError, ValueError):
        text = str(result)
    return ToolResult(text_result_for_llm=text)


# ---------------------------------------------------------------------------
# CopilotExecutor
# ---------------------------------------------------------------------------


@dataclass
class _CopilotSessionState:
    """Per-Omnigent-conversation SDK session state."""

    client: Any = None  # copilot.CopilotClient
    session: Any = None  # copilot.CopilotSession
    system_prompt: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    tools_fingerprint: str | None = None
    has_sent_prompt: bool = False
    # call_id -> tool name, populated on TOOL_EXECUTION_START so the matching
    # TOOL_EXECUTION_COMPLETE (which carries only the id) can name its tool.
    call_names: dict[str, str] = field(default_factory=dict)


class CopilotExecutor(Executor):
    """Execute agent turns via a persistent GitHub Copilot SDK session."""

    def __init__(
        self,
        *,
        cwd: str | None = None,
        os_env: OSEnvSpec | None = None,
        model: str | None = None,
        github_token: str | None = None,
        bundle_dir: Path | None = None,
        agent_name: str | None = None,
        skills_filter: str | list[str] = "all",
    ) -> None:
        """Create a CopilotExecutor.

        :param cwd: Working directory the Copilot session operates in. ``None``
            falls back to ``os_env.cwd`` then the process cwd.
        :param os_env: Optional OS environment / sandbox spec (its ``cwd`` is
            used when *cwd* is unset).
        :param model: Copilot model id (e.g. ``"claude-haiku-4.5"``); a
            gateway-routed id or ``None`` falls back to Copilot's auto-select.
        :param github_token: GitHub token carrying Copilot access. ``None``
            falls back to ``COPILOT_GITHUB_TOKEN`` / ``GH_TOKEN`` /
            ``GITHUB_TOKEN`` in the environment.
        :param bundle_dir: Reserved for future skill wiring; unused in v1.
        :param agent_name: Optional agent name (reserved for parity).
        :param skills_filter: Accepted for parity; copilot has no skill
            mechanism wired here.
        """
        self._cwd = cwd or (os_env.cwd if os_env is not None else None)
        self._os_env_spec = os_env
        self._model_override = model
        self._github_token = github_token or _ambient_github_token()
        self._bundle_dir = bundle_dir
        self._agent_name = agent_name
        self._skills_filter = skills_filter
        self._session_states: dict[str, _CopilotSessionState] = {}
        # Installed by the runtime adapter; routes a bridged-tool call back into
        # Omnigent's session (policy gating, sub-agent dispatch, logging).
        self._tool_executor: ToolExecutor | None = None
        # Installed by the runtime adapter; evaluates PHASE_LLM_REQUEST /
        # PHASE_LLM_RESPONSE policies (the same round-trip pi / claude-sdk /
        # cursor use). ``None`` on single-process / pre-turn paths (no-op).
        self._policy_evaluator: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None

    def supports_streaming(self) -> bool:
        return True

    def supports_tool_calling(self) -> bool:
        return True

    def handles_tools_internally(self) -> bool:
        # Bridged tools execute in-band via the SDK tool handler (which calls
        # ``_tool_executor``), so the runtime adapter must NOT re-dispatch the
        # observed tool events — same contract as claude-sdk / cursor.
        return True

    def supports_live_message_queue(self) -> bool:
        # The SDK exposes no confirmed mid-turn steer wired here, so a message
        # can't be injected into a running turn.
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

    # -- tool bridge --------------------------------------------------------

    def _make_tools(self, tools: list[ToolSpec]) -> list[Any]:  # type: ignore[explicit-any]
        """Build the SDK ``tools`` list from Omnigent ToolSpecs.

        Each tool's async ``handler`` is awaited by the SDK in its own event
        loop, so it bridges straight into ``_tool_executor`` — no thread hop.
        ``skip_permission=True`` because Omnigent's policy layer (and the
        ``_tool_executor`` round-trip) already governs these tools; the SDK
        permission prompt would be redundant and would stall a headless turn.
        """
        from copilot import Tool  # lazy: optional dependency

        built: list[Any] = []  # type: ignore[explicit-any]
        for spec in tools:
            name = spec.get("name")
            if not isinstance(name, str) or not name:
                continue
            params = spec.get("parameters")
            built.append(
                Tool(
                    name=name,
                    description=spec.get("description") or "",
                    handler=self._make_handler(name),
                    parameters=params
                    if isinstance(params, dict)
                    else {"type": "object", "properties": {}},
                    skip_permission=True,
                )
            )
        return built

    def _make_handler(self, tool_name: str) -> Callable[[Any], Awaitable[Any]]:  # type: ignore[explicit-any]
        """Build an async ``handler`` that bridges a Copilot tool call to Omnigent.

        Awaited by the SDK in its event loop, so it ``await``\\s the main-loop
        ``_tool_executor`` directly. Any exception becomes a tool *failure*
        result rather than propagating into the SDK turn.
        """

        async def handler(invocation: Any) -> Any:  # type: ignore[explicit-any]
            from copilot import ToolResult  # lazy: optional dependency

            if self._tool_executor is None:
                return ToolResult(
                    text_result_for_llm=(
                        f"Tool {tool_name!r} is unavailable: no tool executor wired."
                    ),
                    result_type="failure",
                    error="no tool executor wired",
                )
            raw_args = getattr(invocation, "arguments", None)
            args = _coerce_args(raw_args)
            try:
                result = await self._tool_executor(tool_name, dict(args))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — surface as a tool failure
                return ToolResult(
                    text_result_for_llm=f"Tool {tool_name!r} failed: {exc}",
                    result_type="failure",
                    error=str(exc),
                )
            return _encode_tool_result(result)

        return handler

    # -- session lifecycle --------------------------------------------------

    async def _ensure_session(
        self,
        state: _CopilotSessionState,
        model: str | None,
        tools: list[ToolSpec],
        system_prompt: str,
        reasoning_effort: str | None = None,
    ) -> None:
        """Start the SDK client and create the session if not already live.

        On any bring-up failure the partially-started client is stopped before
        propagating, so a bad token / launch error can't orphan the bundled
        CLI subprocess.
        """
        if state.session is not None:
            return
        try:
            from copilot import CopilotClient, PermissionHandler
        except ImportError as exc:
            raise ImportError(
                "CopilotExecutor requires the 'github-copilot-sdk' package. "
                "Install it with: uv pip install github-copilot-sdk "
                "(or `pip install 'omnigent[copilot]'`)."
            ) from exc

        # The Copilot SDK rejects a relative working_directory ("Directory path
        # must be absolute"), and a spec / os_env can hand us a relative cwd
        # (e.g. ``.``), so always resolve to an absolute path.
        cwd = os.path.abspath(self._cwd or os.getcwd())
        client = CopilotClient(
            github_token=self._github_token,
            working_directory=cwd,
            log_level="error",
        )
        try:
            # ``start()`` is inside the try: it spawns the bundled Copilot CLI
            # subprocess *before* connecting/verifying, and the SDK's own error
            # path re-raises without terminating that subprocess — only
            # ``client.stop()`` reaps it. So a start failure (bad token, version
            # skew) must still hit ``_safe_stop`` below, or it orphans the CLI.
            await client.start()
            # ``append`` keeps Copilot's own operating instructions (how to use
            # its built-in tools) and layers the Omnigent agent's system prompt
            # on top — a ``replace`` would strip the tool-use guidance the model
            # relies on. ``None`` when the spec carries no system prompt.
            system_message = (
                {"mode": "append", "content": system_prompt} if system_prompt else None
            )
            session = await client.create_session(
                model=model,
                streaming=True,
                system_message=system_message,
                tools=self._make_tools(tools) or None,
                on_permission_request=PermissionHandler.approve_all,
                working_directory=cwd,
                reasoning_effort=reasoning_effort or None,
            )
        except BaseException:
            await _safe_stop(client)
            raise
        state.client = client
        state.session = session

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        session_key = self._session_key(messages)
        model = _resolve_model((config.model if config else None) or self._model_override)
        reasoning_effort = _resolve_reasoning_effort(config)
        tools_fp = _tools_fingerprint(tools)
        state = self._session_states.setdefault(session_key, _CopilotSessionState())

        # System prompt, model, reasoning effort, and tool set are all fixed at
        # session creation, so a change to any of them means a fresh session
        # (otherwise a changed tool set would leave the initial ``tools`` stale
        # for the conversation, and a new ``/reasoning`` pick would never apply).
        if state.session is not None and (
            state.system_prompt != system_prompt
            or state.model != model
            or state.reasoning_effort != reasoning_effort
            or state.tools_fingerprint != tools_fp
        ):
            await self._close_state(state)
            state = _CopilotSessionState()
            self._session_states[session_key] = state
        is_first_turn = not state.has_sent_prompt
        state.system_prompt = system_prompt
        state.model = model
        state.reasoning_effort = reasoning_effort
        state.tools_fingerprint = tools_fp

        try:
            await self._ensure_session(state, model, tools, system_prompt, reasoning_effort)
        except Exception as exc:  # noqa: BLE001 — surfaced as ExecutorError (CancelledError propagates)
            await self.close_session(session_key)
            yield ExecutorError(message=f"Failed to start copilot-sdk session: {exc}")
            return

        prompt = _build_copilot_prompt(messages, is_first_turn=is_first_turn)
        if not prompt:
            yield TurnComplete(response=None)
            return

        # PHASE_LLM_REQUEST policy (parity with claude-sdk / cursor / pi):
        # evaluate before the LLM call so a DENY blocks it. No-op when unwired.
        policy_eval = self._policy_evaluator
        if policy_eval is not None:
            req_verdict = await policy_eval(
                "PHASE_LLM_REQUEST",
                {
                    "model": model or "auto",
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
        session = state.session

        # Stream the session's events into a queue from the SDK's ``on`` callback
        # (invoked in the SDK loop), then drain-and-translate them here while the
        # ``send_and_wait`` task runs. Unsubscribe + drain when the send completes.
        queue: asyncio.Queue[Any] = asyncio.Queue()  # type: ignore[explicit-any]

        def _on_event(event: Any) -> None:  # type: ignore[explicit-any]
            queue.put_nowait(event)

        unsubscribe = session.on(_on_event)
        send_task: asyncio.Task[Any] = asyncio.ensure_future(  # type: ignore[explicit-any]
            session.send_and_wait(prompt, timeout=_SEND_TIMEOUT_S)
        )

        response_text = ""
        tool_calls = 0
        usage_acc: dict[str, int] = {}
        usage_model: str | None = None
        turn_error: str | None = None
        # A tool call between two assistant text blocks means they are distinct
        # narration segments (pre- vs post-tool); insert a paragraph break so
        # they don't render as one run-on string. Streamed deltas of a single
        # response (no tool between) still concatenate seamlessly.
        separate_next_text = False

        async def _drain(event: Any) -> AsyncIterator[ExecutorEvent]:  # type: ignore[explicit-any]
            nonlocal response_text, tool_calls, separate_next_text, usage_model, turn_error
            etype = str(getattr(event, "type", ""))
            data = _event_data(event)
            if etype.endswith("ASSISTANT_MESSAGE_DELTA"):
                text = str(data.get("deltaContent") or "")
                if text:
                    if separate_next_text and response_text:
                        trailing = len(response_text) - len(response_text.rstrip("\n"))
                        leading = len(text) - len(text.lstrip("\n"))
                        if trailing + leading < 2:
                            text = "\n" * (2 - trailing - leading) + text
                    separate_next_text = False
                    response_text += text
                    yield TextChunk(text=text)
            elif etype.endswith("ASSISTANT_REASONING_DELTA"):
                text = str(data.get("deltaContent") or "")
                if text:
                    yield ReasoningChunk(delta=text, event_type="reasoning_text")
            elif etype.endswith("TOOL_EXECUTION_START"):
                name = str(data.get("toolName") or "tool")
                call_id = data.get("toolCallId")
                if call_id:
                    state.call_names[str(call_id)] = name
                tool_calls += 1
                separate_next_text = True
                yield ToolCallRequest(
                    name=name,
                    args=_coerce_args(data.get("arguments")),
                    metadata={"call_id": call_id},
                )
            elif etype.endswith("TOOL_EXECUTION_COMPLETE"):
                call_id = data.get("toolCallId")
                name = state.call_names.get(str(call_id), "tool") if call_id else "tool"
                result = _unwrap_tool_result(data.get("result"))
                classification = classify_tool_result(result)
                status = classification.status
                error = classification.error or None
                raw_error = data.get("error")
                if data.get("success") is False or raw_error:
                    status = ToolCallStatus.ERROR
                    error = error or _unwrap_tool_error(raw_error) or "tool call failed"
                separate_next_text = True
                yield ToolCallComplete(
                    name=name,
                    status=status,
                    result=result,
                    error=error,
                    metadata={"call_id": call_id},
                )
            elif etype.endswith("ASSISTANT_USAGE"):
                usage_model = data.get("model") or usage_model
                _accumulate_usage(usage_acc, data)
            elif etype.endswith(("SESSION_ERROR", "MODEL_CALL_FAILURE")):
                turn_error = str(
                    data.get("message") or data.get("errorMessage") or "copilot session error"
                )
            elif etype.endswith("SESSION_COMPACTION_COMPLETE"):
                # The Copilot SDK auto-compacted the session's context window.
                # Surface it as a CompactionComplete so the runner persists a
                # compaction item and a resumed session gets the pre-compacted
                # summary instead of replaying the full transcript (parity with
                # claude-sdk / openai-agents). Only a *successful* compaction
                # counts; ``success`` is False on a failed/aborted attempt.
                if data.get("success"):
                    post_tokens = data.get("postCompactionTokens")
                    yield CompactionComplete(
                        # Copilot uniquely reports the real summary text; fall
                        # back to a synthetic placeholder like the peers do.
                        summary=str(
                            data.get("summaryContent")
                            or "[GitHub Copilot compaction: context was automatically compacted]"
                        ),
                        # Post-compaction context size (same semantic as the
                        # peers' ``context_tokens``).
                        token_count=(
                            int(post_tokens) if isinstance(post_tokens, (int, float)) else 0
                        ),
                        model=usage_model or model,
                        # The event carries no message list; resume falls back to
                        # the summary (allowed by the field's contract).
                        compacted_messages=None,
                    )

        try:
            while True:
                getter: asyncio.Task[Any] = asyncio.ensure_future(queue.get())  # type: ignore[explicit-any]
                done, _pending = await asyncio.wait(
                    {getter, send_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if getter in done:
                    event = getter.result()
                    async for ev in _drain(event):
                        yield ev
                else:
                    getter.cancel()
                if send_task.done():
                    # The turn ended — drain whatever is still queued, then stop.
                    while not queue.empty():
                        async for ev in _drain(queue.get_nowait()):
                            yield ev
                    break
            final_event = send_task.result()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the SDK turn failed mid-stream
            await self.close_session(session_key)
            yield ExecutorError(message=f"copilot-sdk turn failed: {exc}", retryable=True)
            return
        finally:
            unsubscribe()
            if not send_task.done():
                send_task.cancel()

        # Surface a mid-turn error event (``SESSION_ERROR`` / ``MODEL_CALL_FAILURE``)
        # whenever the turn did NOT produce a successful final message — even if
        # some text streamed first. Reporting a failed turn as a clean
        # ``TurnComplete`` with partial text would mask the failure (a turn that
        # errored after partial output would look successful). A real final
        # ASSISTANT_MESSAGE (``final_event`` set) means the SDK completed the turn,
        # so a stray earlier event is not treated as fatal.
        if turn_error and final_event is None:
            await self.close_session(session_key)
            yield ExecutorError(message=f"copilot-sdk run error: {turn_error}", retryable=True)
            return

        # Prefer the streamed text (which carries the paragraph breaks inserted
        # above); fall back to the final ASSISTANT_MESSAGE content only when
        # nothing streamed (e.g. a tool-only turn).
        final = response_text or _final_message_text(final_event) or None

        # PHASE_LLM_RESPONSE policy (parity with the peer harnesses).
        if policy_eval is not None:
            resp_verdict = await policy_eval(
                "PHASE_LLM_RESPONSE",
                {
                    "model": model or "auto",
                    "text_preview": response_text[:500] if response_text else "",
                    "tool_calls_count": tool_calls,
                },
            )
            if getattr(resp_verdict, "action", "") == "POLICY_ACTION_DENY":
                reason = getattr(resp_verdict, "reason", "") or "no reason given"
                yield ExecutorError(message=f"LLM response denied by policy: {reason}")
                return

        usage = _finalize_usage(usage_acc)
        if usage is not None:
            _notify_usage_from_dict(model=usage_model or model or "copilot", usage=usage)
        yield TurnComplete(response=final, usage=usage)

    async def _close_state(self, state: _CopilotSessionState) -> None:
        if state.session is not None:
            await _safe_stop(state.session)
            state.session = None
        if state.client is not None:
            await _safe_stop(state.client)
            state.client = None
        state.call_names.clear()

    async def close_session(self, session_key: str) -> None:
        state = self._session_states.pop(session_key, None)
        if state is not None:
            await self._close_state(state)

    async def interrupt_session(self, session_key: str) -> bool:
        state = self._session_states.get(session_key)
        if state is None:
            return False
        # Best-effort SDK abort first, to cleanly cancel the in-flight turn on
        # the bundled CLI *before* teardown. ``session.abort()`` is the SDK's
        # blessed cancel (it keeps the session valid), so it stops the next
        # ``stop()`` from racing a live generation — which can orphan the CLI's
        # tool subprocesses or dump a post-cancel stream. Mirrors pi / claude-sdk.
        if state.session is not None:
            try:
                await asyncio.wait_for(state.session.abort(), timeout=0.5)
            except Exception as exc:  # noqa: BLE001 — abort is best-effort
                logger.debug("CopilotExecutor: interrupt abort failed: %s", exc)
        # Always drop the session so the next turn starts a fresh one — mirrors
        # the cursor / pi executors. A resumed Copilot session sends only the
        # latest user message (see ``_build_copilot_prompt``), which would bypass
        # the runner's "[System: interrupted]" marker and silently continue the
        # abandoned request; a fresh session replays full history (marker
        # included). See ``claude_sdk_executor.interrupt_session`` for the rationale.
        try:
            await self.close_session(session_key)
            return True
        except Exception as exc:  # noqa: BLE001 — close failures surface as False
            logger.debug("CopilotExecutor: close after interrupt failed: %s", exc)
            return False

    async def close(self) -> None:
        for key in list(self._session_states.keys()):
            await self.close_session(key)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ambient_github_token() -> str | None:
    """Return the first set ambient GitHub token, in CLI precedence order."""
    for var in GITHUB_TOKEN_ENV_VARS:
        value = os.environ.get(var)
        if value:
            return value
    return None


def _coerce_args(raw: Any) -> dict[str, Any]:  # type: ignore[explicit-any]
    """Coerce a tool-call ``arguments`` payload to a dict.

    The SDK delivers arguments as a parsed dict, but tolerate a JSON string (or
    anything else) by best-effort parsing, falling back to an empty mapping.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _event_data(event: Any) -> dict[str, Any]:  # type: ignore[explicit-any]
    """Return an event's ``data`` payload as a (camelCase-keyed) dict.

    Uses ``to_dict()`` (the wire form) so reads are schema-stable across the
    SDK's snake_case attributes vs. camelCase wire keys.
    """
    try:
        payload = event.to_dict()
    except Exception:  # noqa: BLE001 — fall back to attribute access below
        payload = None
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            return data
    data_attr = getattr(event, "data", None)
    if isinstance(data_attr, dict):
        return data_attr
    return {}


# SDK ``ToolExecutionCompleteResult.to_dict`` wraps the payload as
# ``{"content": ..., "contents": [...], "detailedContent": ..., "uiResource": ...}``
# and ``ToolExecutionCompleteError.to_dict`` as ``{"message": ..., "code": ...}``.
_TOOL_RESULT_WRAPPER_KEYS = ("content", "contents", "detailedContent", "uiResource")


def _unwrap_tool_result(raw: Any) -> Any:  # type: ignore[explicit-any]
    """Unwrap the SDK ``ToolExecutionCompleteResult`` wrapper to its content payload.

    A ``TOOL_EXECUTION_COMPLETE`` ``result`` arrives in wire form as a wrapper
    dict (``{"content": ..., "detailedContent": ..., ...}``). Carry the bare
    content downstream (classification, tracing, the persisted ``ToolCallComplete``
    result) rather than the wrapper — matching the bare-payload convention the
    peer executors use. Only a recognized wrapper shape is unwrapped, so an
    Omnigent-convention result (e.g. ``{"blocked": True}``) passes through
    untouched for :func:`classify_tool_result`.
    """
    if isinstance(raw, dict) and any(k in raw for k in _TOOL_RESULT_WRAPPER_KEYS):
        for key in ("content", "detailedContent"):
            value = raw.get(key)
            if value is not None:
                return value
    return raw


def _unwrap_tool_error(raw: Any) -> str | None:  # type: ignore[explicit-any]
    """Extract the message from the SDK's structured tool error.

    A failed ``TOOL_EXECUTION_COMPLETE`` ``error`` arrives as
    ``{"message": ..., "code": ...}`` (``ToolExecutionCompleteError.to_dict``);
    surface the ``message`` rather than the dict's Python repr (which would leak
    ``"{'message': ..., 'code': ...}"`` into the tool error shown to the
    model / recorded on the span). A bare string passes through; anything else
    yields ``None`` so the caller can fall back to a generic message.
    """
    if isinstance(raw, dict):
        message = raw.get("message")
        if isinstance(message, str) and message:
            return message
    if isinstance(raw, str) and raw:
        return raw
    return None


def _final_message_text(event: Any) -> str:  # type: ignore[explicit-any]
    """Extract the aggregate assistant text from the final ASSISTANT_MESSAGE event."""
    if event is None:
        return ""
    content = _event_data(event).get("content")
    return content if isinstance(content, str) else ""


def _accumulate_usage(acc: dict[str, int], data: dict[str, Any]) -> None:  # type: ignore[explicit-any]
    """Sum the token counts from one ASSISTANT_USAGE event into *acc*.

    Copilot emits one usage event per underlying model call, so a turn with
    tool round-trips reports usage several times; we accumulate. The
    authoritative AI-credit cost (``copilotUsage.totalNanoAiu``, in nano-AIU) is
    summed too and converted to ``cost_usd`` in :func:`_finalize_usage`.
    """
    mapping = {
        "inputTokens": "input_tokens",
        "outputTokens": "output_tokens",
        "cacheReadTokens": "cache_read_input_tokens",
    }
    for wire_key, usage_key in mapping.items():
        value = data.get(wire_key)
        if isinstance(value, (int, float)):
            acc[usage_key] = acc.get(usage_key, 0) + int(value)
    copilot_usage = data.get("copilotUsage")
    if isinstance(copilot_usage, dict):
        nano_aiu = copilot_usage.get("totalNanoAiu")
        if isinstance(nano_aiu, (int, float)):
            acc["_cost_nano_aiu"] = acc.get("_cost_nano_aiu", 0) + int(nano_aiu)


def _finalize_usage(acc: dict[str, int]) -> dict[str, Any] | None:  # type: ignore[explicit-any]
    """Build the TurnComplete usage dict from accumulated counts, or ``None``."""
    if not acc:
        return None
    usage: dict[str, Any] = dict(acc)  # type: ignore[explicit-any]
    nano_aiu = usage.pop("_cost_nano_aiu", 0)
    usage["total_tokens"] = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
    if nano_aiu:
        # 1 AIC = 1e9 nano-AIU = $0.01, so nano-AIU / 1e11 = USD.
        usage["cost_usd"] = nano_aiu / 1e11
    return usage


async def _safe_stop(obj: Any) -> None:  # type: ignore[explicit-any]
    """Best-effort async teardown of a ``copilot`` client / session.

    :class:`copilot.CopilotClient` exposes ``stop()`` (terminates the bundled
    CLI subprocess it launched); :class:`copilot.CopilotSession` exposes
    ``disconnect()``. Prefer ``stop`` then ``disconnect``; a teardown failure
    must not mask the original error or leave the closer raising.
    """
    closer = (
        getattr(obj, "stop", None)
        or getattr(obj, "disconnect", None)
        or getattr(obj, "aclose", None)
        or getattr(obj, "close", None)
    )
    if closer is None:
        return
    try:
        result = closer()
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:  # noqa: BLE001 — best-effort teardown
        logger.debug("CopilotExecutor: stop failed: %s", exc)
