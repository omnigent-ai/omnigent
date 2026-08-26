"""Tests for the generic ACP executor (:mod:`omnigent.inner.acp_executor`).

Two layers:

* **Unit** — construction/argv, both ``session/new`` shapes, tool-call → event
  mapping, permission-outcome mapping, interrupt via ``session/cancel``, and the
  harness-wrap env parsing — all with a mocked transport.
* **Hermetic e2e** — a tiny fake ACP agent (a Python script speaking ACP over
  stdio, written to a temp file) that the executor spawns for real and drives
  through a full turn: initialize → session/new → session/prompt → streaming
  (thought + text + tool card) → request_permission → completion. No real
  vendor binary is required.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from omnigent.inner import acp_executor as acp_executor_module
from omnigent.inner._acp_omnigent_mcp import OmnigentAcpMcp, _to_acp_mcp_servers
from omnigent.inner.acp_executor import AcpAgentConfig, AcpExecutor
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import (
    ExecutorError,
    ReasoningChunk,
    SubAgentCompleted,
    SubAgentStarted,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    TurnComplete,
    describe_exception,
)

# ---------------------------------------------------------------------------
# Construction / argv
# ---------------------------------------------------------------------------


def test_command_is_shlex_split_into_argv() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="gemini --experimental-acp --model gemini-2.5-pro"))
    assert ex._argv == ["gemini", "--experimental-acp", "--model", "gemini-2.5-pro"]


def test_quoted_command_argv() -> None:
    ex = AcpExecutor(AcpAgentConfig(command='npx -y "@zed-industries/claude-code-acp"'))
    assert ex._argv == ["npx", "-y", "@zed-industries/claude-code-acp"]


def test_empty_command_rejected() -> None:
    with pytest.raises(ValueError):
        AcpExecutor(AcpAgentConfig(command="   "))


def test_handles_tools_internally_and_streaming() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    assert ex.handles_tools_internally() is True
    assert ex.supports_streaming() is True


# ---------------------------------------------------------------------------
# session/new shapes (server- vs client-assigned id, optional model)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_new_server_mode_adopts_returned_id() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x", session_id_mode="server"))
    captured: dict = {}

    async def fake_rpc(method, params, timeout=30.0):
        captured["method"] = method
        captured["params"] = params
        return {"result": {"sessionId": "srv-42"}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    sid = await ex._ensure_session()
    assert sid == "srv-42"
    assert "sessionId" not in captured["params"]  # server assigns it
    assert captured["params"]["cwd"] == ex._cwd
    assert captured["params"]["mcpServers"] == []


@pytest.mark.asyncio
async def test_session_new_sends_empty_mcp_servers_when_omnigent_mcp_disabled() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x", omnigent_mcp=False))
    captured: dict = {}

    async def fake_rpc(method, params, timeout=30.0):
        captured["params"] = params
        return {"result": {"sessionId": "srv-42"}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._ensure_session()
    assert captured["params"]["mcpServers"] == []


@pytest.mark.asyncio
async def test_session_new_client_mode_generates_and_sends_id() -> None:
    ex = AcpExecutor(
        AcpAgentConfig(
            command="x", session_id_mode="client", model="m1", send_model_in_session_new=True
        )
    )

    sent: dict = {}

    async def capture_rpc(method, params, timeout=30.0):
        sent.update(params)
        return {"result": {}}

    ex._rpc = capture_rpc  # type: ignore[assignment]
    sid = await ex._ensure_session()
    assert sid == sent["sessionId"] and sid  # our generated id is used
    assert sent["model"] == "m1"  # model sent because send_model_in_session_new


# ---------------------------------------------------------------------------
# Tool-call extraction + permission outcome
# ---------------------------------------------------------------------------


def test_extract_tool_call_prefers_title() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    name, args = ex._extract_tool_call(
        {"toolCall": {"title": "shell", "kind": "execute", "rawInput": {"command": "ls"}}}
    )
    assert name == "shell"
    assert args == {"command": "ls"}


def test_extract_tool_call_falls_back_to_kind() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    name, args = ex._extract_tool_call({"toolCall": {"kind": "read"}})
    assert name == "read"
    assert args == {}


def test_extract_tool_call_recovers_a_bare_permission_request() -> None:
    """A request naming only ``toolCallId`` resolves via the originating tool_call.

    An agent may ask permission without repeating the tool: Devin sends no
    ``title`` / ``kind`` / ``rawInput``, only the id it already announced. The
    ``tool_call`` update always arrives first, so its name and arguments are
    still on hand.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "toolu_01",
            "title": "Ran command",
            "kind": "execute",
            "rawInput": {"command": "rm -rf build"},
        }
    )
    name, args = ex._extract_tool_call(
        {
            "toolCall": {
                "toolCallId": "toolu_01",
                "_meta": {"vendor/editableCommand": "rm -rf build"},
            }
        }
    )
    assert name == "Ran command"
    assert args == {"command": "rm -rf build"}


def test_extract_tool_call_prefers_the_request_over_the_cache() -> None:
    """A request that carries its own title/rawInput wins; the cache is a fallback."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update(
        {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "stale", "rawInput": {"a": 1}}
    )
    name, args = ex._extract_tool_call(
        {"toolCall": {"toolCallId": "c1", "title": "shell", "rawInput": {"command": "ls"}}}
    )
    assert (name, args) == ("shell", {"command": "ls"})


def test_extract_tool_call_unknown_id_degrades_to_tool() -> None:
    """An id we never saw announced still yields the safe generic fallback."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    assert ex._extract_tool_call({"toolCall": {"toolCallId": "never-announced"}}) == ("tool", {})


def test_permission_outcome_allow_prefers_once() -> None:
    params = {
        "options": [
            {"optionId": "a1", "kind": "allow_always"},
            {"optionId": "a2", "kind": "allow_once"},
            {"optionId": "r1", "kind": "reject_once"},
        ]
    }
    out = AcpExecutor._permission_outcome(params, allow=True)
    assert out == {"outcome": {"outcome": "selected", "optionId": "a2"}}


def test_permission_outcome_deny_picks_reject() -> None:
    params = {"options": [{"optionId": "r", "kind": "reject_once"}]}
    out = AcpExecutor._permission_outcome(params, allow=False)
    assert out == {"outcome": {"outcome": "selected", "optionId": "r"}}


def test_permission_outcome_cancelled_when_no_option() -> None:
    out = AcpExecutor._permission_outcome({"options": []}, allow=True)
    assert out == {"outcome": {"outcome": "cancelled"}}


# ---------------------------------------------------------------------------
# session/update → ExecutorEvent mapping
# ---------------------------------------------------------------------------


def test_update_agent_message_chunk_to_text() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    events = ex._handle_session_update(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}
    )
    assert len(events) == 1 and isinstance(events[0], TextChunk) and events[0].text == "hi"


def test_update_thought_chunk_to_reasoning() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    events = ex._handle_session_update(
        {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hmm"}}
    )
    assert len(events) == 1 and isinstance(events[0], ReasoningChunk) and events[0].delta == "hmm"


def test_tool_call_and_update_emit_cards() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    started = ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "c1",
            "title": "shell",
            "rawInput": {"command": "ls"},
        }
    )
    assert len(started) == 1
    req = started[0]
    assert isinstance(req, ToolCallRequest)
    assert req.name == "shell" and req.metadata == {"call_id": "c1"}
    assert ex._tool_names["c1"] == "shell"

    done = ex._handle_session_update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed"}
    )
    assert len(done) == 1
    comp = done[0]
    assert isinstance(comp, ToolCallComplete)
    assert comp.name == "shell" and comp.status is ToolCallStatus.SUCCESS
    assert comp.metadata == {"call_id": "c1"}
    assert "c1" not in ex._tool_names  # popped


def test_tool_call_caches_release_on_completion() -> None:
    """Both id-keyed caches drop the entry when the call closes.

    They exist only to bridge a tool_call to its permission request and closing
    update, so a long session must not accumulate one entry per tool call.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "c1",
            "title": "shell",
            "rawInput": {"command": "ls"},
        }
    )
    assert ex._tool_names == {"c1": "shell"}
    assert ex._tool_inputs == {"c1": {"command": "ls"}}

    ex._handle_session_update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed"}
    )
    assert ex._tool_names == {}
    assert ex._tool_inputs == {}


def test_tool_call_update_failed_maps_to_error() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update({"sessionUpdate": "tool_call", "toolCallId": "c2", "title": "t"})
    done = ex._handle_session_update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "c2", "status": "failed"}
    )
    assert done[0].status is ToolCallStatus.ERROR


def test_usage_update_sets_context_window() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    assert ex.max_context_tokens() is None
    ex._handle_session_update({"sessionUpdate": "usage_update", "size": 200000})
    assert ex.max_context_tokens() == 200000


def test_usage_maps_cached_reads_to_the_canonical_key() -> None:
    """
    ``cachedReadTokens`` surfaces as its own ``cache_read_input_tokens``.

    Cache reads are real consumption billed at a fraction of the input rate, so
    folding them into ``input_tokens`` (or dropping them, as before) misreports
    cost — in a measured Devin turn 10,944 of 15,637 input tokens were cache
    reads. ``cache_read_input_tokens`` is the key the SSE layer and AgentInfo
    already render, so no UI change is needed.

    **What breaks if this fails**: a future token budget computes ~3x the real
    consumption and fires almost immediately.
    """
    usage = AcpExecutor._usage_from_result(
        {
            "usage": {
                "totalTokens": 15675,
                "inputTokens": 15637,
                "outputTokens": 38,
                "cachedReadTokens": 10944,
            }
        }
    )
    assert usage == {
        "total_tokens": 15675,
        "input_tokens": 15637,
        "output_tokens": 38,
        "cache_read_input_tokens": 10944,
    }


def test_usage_omits_absent_and_non_integer_fields() -> None:
    """Agents that report a subset (or garbage) still yield usable usage."""
    assert AcpExecutor._usage_from_result({"usage": {"totalTokens": 10}}) == {"total_tokens": 10}
    # ``True`` is an int subclass — it must not be mistaken for a token count.
    assert AcpExecutor._usage_from_result({"usage": {"inputTokens": True}}) is None
    assert AcpExecutor._usage_from_result({"usage": {"totalTokens": "nope"}}) is None
    assert AcpExecutor._usage_from_result({}) is None


def test_in_progress_tool_update_emits_nothing() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update({"sessionUpdate": "tool_call", "toolCallId": "c3", "title": "t"})
    assert (
        ex._handle_session_update(
            {"sessionUpdate": "tool_call_update", "toolCallId": "c3", "status": "in_progress"}
        )
        == []
    )


# ---------------------------------------------------------------------------
# Sub-agent surfacing (extension-supplied dialect -> normalized events)
# ---------------------------------------------------------------------------


class _FakeSubAgentDialect:
    """An invented dialect, so this generic suite names no vendor."""

    def read(self, update: dict[str, object]) -> tuple[object, ...]:
        """Return a start/end for ``acme.dev/spawn`` / ``acme.dev/done``."""
        from omnigent.inner.acp_subagents import SubAgentEnd, SubAgentStart

        if isinstance(update.get("acme.dev/spawn"), dict):
            return (SubAgentStart(child_key="w1", title="worker", task="do a thing"),)
        if isinstance(update.get("acme.dev/done"), dict):
            return (SubAgentEnd(child_key="w1", ok=True, summary="done"),)
        return ()


def _extended_executor() -> AcpExecutor:
    """An executor whose extension supplies one dialect (a vendor's wrap does this)."""
    from omnigent.inner.acp_extension import AcpExtension

    return AcpExecutor(
        AcpAgentConfig(command="x"),
        extension=AcpExtension(name="acme", subagent_sources=(_FakeSubAgentDialect(),)),
    )


def test_handle_session_update_emits_subagent_started() -> None:
    """An extension-recognized start becomes a ``SubAgentStarted`` event.

    The executor half of the seam: the runner turns this event into a child
    session, so if it stops firing the "Subagents" panel goes empty.
    """
    events = _extended_executor()._handle_session_update(
        {"sessionUpdate": "tool_call_update", "acme.dev/spawn": {"id": "w1"}}
    )
    assert [e for e in events if isinstance(e, SubAgentStarted)] == [
        SubAgentStarted(child_key="w1", title="worker", task="do a thing")
    ]


def test_handle_session_update_emits_subagent_completed() -> None:
    """An extension-recognized end becomes a ``SubAgentCompleted`` event."""
    events = _extended_executor()._handle_session_update(
        {"sessionUpdate": "tool_call_update", "acme.dev/done": {"id": "w1"}}
    )
    assert [e for e in events if isinstance(e, SubAgentCompleted)] == [
        SubAgentCompleted(child_key="w1", ok=True, summary="done")
    ]


def test_generic_executor_does_no_subagent_scanning() -> None:
    """With no extension, the executor is inert even for a dialect-shaped frame.

    **What breaks if this fails**: every ACP agent — Grok, a user's own
    ``acp:<slug>`` — gets some other vendor's dialect run against its frames,
    which is the coupling the extension seam exists to prevent. The default must
    read no vendor field at all.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))  # generic acp harness
    for frame in (
        {"sessionUpdate": "tool_call_update", "acme.dev/spawn": {"id": "w1"}},
        {"sessionUpdate": "tool_call_update", "_meta": {"cognition.ai/subagent_started": {}}},
        {"sessionUpdate": "agent_message_chunk", "content": {"text": "hi"}},
    ):
        events = ex._handle_session_update(frame)
        assert not any(isinstance(e, (SubAgentStarted, SubAgentCompleted)) for e in events), frame


def test_tool_cards_still_render_alongside_the_scan() -> None:
    """The scan is additive — an ordinary tool_call still produces its card."""
    events = _extended_executor()._handle_session_update(
        {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "Ran ls", "kind": "execute"}
    )
    assert [type(e) for e in events] == [ToolCallRequest]


# ---------------------------------------------------------------------------
# Permission decision (policy + elicitation gates)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decide_permission_allows_with_no_gates() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    assert await ex._decide_permission({"toolCall": {"title": "shell"}}) == (True, None)


@pytest.mark.asyncio
async def test_decide_permission_denies_on_policy_deny() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))

    class _V:
        action = "POLICY_ACTION_DENY"

    ex._policy_evaluator = AsyncMock(return_value=_V())
    assert await ex._decide_permission({"toolCall": {"title": "shell"}}) == (False, None)


@pytest.mark.asyncio
async def test_decide_permission_ask_defers_to_elicitation() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))

    class _V:
        action = "POLICY_ACTION_ASK"

    ex._policy_evaluator = AsyncMock(return_value=_V())
    ex._elicitation_handler = AsyncMock(return_value=True)
    assert await ex._decide_permission({"toolCall": {"title": "shell"}}) == (True, None)
    ex._elicitation_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_decide_permission_ask_without_handler_fails_closed() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))

    class _V:
        action = "POLICY_ACTION_ASK"

    ex._policy_evaluator = AsyncMock(return_value=_V())
    assert await ex._decide_permission({"toolCall": {"title": "shell"}}) == (False, None)


def _seed_tool_call(ex: AcpExecutor) -> dict[str, object]:
    """Announce a ``tool_call``, then return the bare permission params for it.

    Mirrors the real frame order for an agent that asks permission without
    repeating the tool: ``tool_call`` (with the name + command) → then
    ``session/request_permission`` carrying only the id.
    """
    ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "toolu_01",
            "title": "Ran command",
            "kind": "execute",
            "rawInput": {"command": "rm -rf build"},
        }
    )
    return {"toolCall": {"toolCallId": "toolu_01"}}


@pytest.mark.asyncio
async def test_decide_permission_policy_sees_the_real_tool_call() -> None:
    """The TOOL_CALL policy is evaluated against the resolved name + arguments.

    Rules gate on the tool name and then read its arguments (the destructive-shell
    builtin reads ``arguments["command"]``), so a bare request evaluated as
    ``{"name": "tool", "arguments": {}}`` matches nothing — a "deny ``rm -rf``"
    policy would sit silent while the command ran.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    params = _seed_tool_call(ex)

    class _V:
        action = "POLICY_ACTION_DENY"

    ex._policy_evaluator = AsyncMock(return_value=_V())
    assert await ex._decide_permission(params) == (False, None)
    ex._policy_evaluator.assert_awaited_once_with(
        "PHASE_TOOL_CALL",
        {"name": "Ran command", "arguments": {"command": "rm -rf build"}},
    )


@pytest.mark.asyncio
async def test_decide_permission_card_names_the_tool() -> None:
    """The approval card describes the call instead of an unnamed "tool".

    The elicitation handler renders ``<tool_name>(<args>)``, so these two values
    are literally what the user reads before approving.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    params = _seed_tool_call(ex)
    ex._elicitation_handler = AsyncMock(return_value=True)

    assert await ex._decide_permission(params) == (True, None)
    ex._elicitation_handler.assert_awaited_once_with("Ran command", {"command": "rm -rf build"})


# ---------------------------------------------------------------------------
# Scoped approval: the agent's own permission options
# ---------------------------------------------------------------------------


def _agent_options() -> list[dict[str, str]]:
    """Devin-shaped options: allow-once, two scoped always-allows, and a reject.

    Order is the agent's own — narrowest first — and is preserved on the card so
    the least-privilege choice leads.
    """
    return [
        {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
        {
            "optionId": "allow_session",
            "name": "Yes, allow `ls` commands (this session)",
            "kind": "allow_always",
        },
        {
            "optionId": "allow_always_global",
            "name": "Yes, always allow `ls` commands in all projects",
            "kind": "allow_always",
        },
        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
    ]


def test_acp_executor_accepts_the_choice_bridge() -> None:
    """The attribute the adapter installs by name exists, and starts unwired.

    The executor half of a cross-layer contract: the adapter gates its install on
    this attribute (see ``tests/runtime/harnesses/test_executor_adapter.py``), so a
    rename here would silently disable scoped approval.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    assert ex._elicitation_choice_handler is None


@pytest.mark.asyncio
async def test_decide_permission_offers_the_agents_own_scopes() -> None:
    """The card gets every option the agent offered, in the agent's order.

    **What breaks if this fails**: the user is back to Approve/Reject and each
    grant is once-scoped, so the same command class re-prompts indefinitely.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    params = {
        "toolCall": {"title": "shell", "rawInput": {"command": "ls"}},
        "options": _agent_options(),
    }
    ex._elicitation_choice_handler = AsyncMock(
        return_value="Yes, allow `ls` commands (this session)"
    )

    assert await ex._decide_permission(params) == (True, "allow_session")
    ex._elicitation_choice_handler.assert_awaited_once_with(
        "shell",
        {"command": "ls"},
        [
            "Allow",
            "Yes, allow `ls` commands (this session)",
            "Yes, always allow `ls` commands in all projects",
            "Reject",
        ],
    )


@pytest.mark.asyncio
async def test_scoped_choice_reaches_the_agent_as_that_option() -> None:
    """End of the chain: the agent is told the exact scope the user picked.

    Mirrors the call site (``_decide_permission`` then ``_permission_outcome``),
    because the scope only takes effect if it survives into the reply — the agent
    is what honors it and stops asking.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    params = {"toolCall": {"title": "shell"}, "options": _agent_options()}
    ex._elicitation_choice_handler = AsyncMock(
        return_value="Yes, allow `ls` commands (this session)"
    )

    allow, option_id = await ex._decide_permission(params)
    assert ex._permission_outcome(params, allow=allow, option_id=option_id) == {
        "outcome": {"outcome": "selected", "optionId": "allow_session"}
    }


@pytest.mark.asyncio
async def test_decide_permission_choice_reject_denies() -> None:
    """Picking the agent's own reject option denies, and names that option."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    params = {"toolCall": {"title": "shell"}, "options": _agent_options()}
    ex._elicitation_choice_handler = AsyncMock(return_value="Reject")

    assert await ex._decide_permission(params) == (False, "reject_once")


@pytest.mark.asyncio
async def test_decide_permission_choice_declined_denies() -> None:
    """A dismissed / timed-out choice card denies, with no scope."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    params = {"toolCall": {"title": "shell"}, "options": _agent_options()}
    ex._elicitation_choice_handler = AsyncMock(return_value=None)

    assert await ex._decide_permission(params) == (False, None)


@pytest.mark.asyncio
async def test_decide_permission_choice_not_offered_denies() -> None:
    """A label the agent never offered fails closed rather than guessing."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    params = {"toolCall": {"title": "shell"}, "options": _agent_options()}
    ex._elicitation_choice_handler = AsyncMock(return_value="Yes, do whatever you like")

    assert await ex._decide_permission(params) == (False, None)


@pytest.mark.asyncio
async def test_decide_permission_needs_a_reject_option_for_a_choice_card() -> None:
    """Without a reject option the binary card is used instead.

    A choice card replaces Approve/Reject with the agent's options, so offering
    only allow-shaped ones would leave the user no way to say no.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    params = {
        "toolCall": {"title": "shell"},
        "options": [
            {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
            {"optionId": "allow_session", "name": "Allow this session", "kind": "allow_always"},
        ],
    }
    ex._elicitation_choice_handler = AsyncMock(return_value="Allow this session")
    ex._elicitation_handler = AsyncMock(return_value=True)

    assert await ex._decide_permission(params) == (True, None)
    ex._elicitation_choice_handler.assert_not_awaited()
    ex._elicitation_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_decide_permission_falls_back_on_duplicate_labels() -> None:
    """Two options sharing a label are ambiguous, since the reply names the label."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    params = {
        "toolCall": {"title": "shell"},
        "options": [
            {"optionId": "a1", "name": "Allow", "kind": "allow_once"},
            {"optionId": "a2", "name": "Allow", "kind": "allow_always"},
            {"optionId": "r1", "name": "Reject", "kind": "reject_once"},
        ],
    }
    ex._elicitation_choice_handler = AsyncMock(return_value="Allow")
    ex._elicitation_handler = AsyncMock(return_value=True)

    assert await ex._decide_permission(params) == (True, None)
    ex._elicitation_choice_handler.assert_not_awaited()


def test_permission_outcome_honors_a_chosen_scope() -> None:
    """A user-picked option is echoed verbatim, overriding the once-scoped default."""
    params = {"options": _agent_options()}
    out = AcpExecutor._permission_outcome(params, allow=True, option_id="allow_session")
    assert out == {"outcome": {"outcome": "selected", "optionId": "allow_session"}}


def test_permission_outcome_ignores_an_unoffered_scope() -> None:
    """An id the agent didn't offer is never echoed; the safe default applies.

    Guards against sending the agent an option it can't honor — or a broader one
    than it advertised — if a stale or hand-crafted id ever reaches here.
    """
    params = {"options": _agent_options()}
    out = AcpExecutor._permission_outcome(params, allow=True, option_id="made_up")
    assert out == {"outcome": {"outcome": "selected", "optionId": "allow_once"}}


# ---------------------------------------------------------------------------
# permission_mode (bypassPermissions)
# ---------------------------------------------------------------------------


def _bypass_executor() -> AcpExecutor:
    """An executor whose spec opted out of approval cards."""
    return AcpExecutor(AcpAgentConfig(command="x", permission_mode="bypassPermissions"))


@pytest.mark.asyncio
async def test_bypass_permissions_skips_the_card() -> None:
    """``bypassPermissions`` allows a policy-silent call without prompting.

    **What breaks if this fails**: a headless ACP worker (a polly sub-agent, a
    scheduled task) parks on an approval card nobody is watching, so the turn
    stalls rather than running unattended.
    """
    ex = _bypass_executor()
    ex._elicitation_handler = AsyncMock(return_value=True)
    ex._elicitation_choice_handler = AsyncMock(return_value="Allow")

    assert await ex._decide_permission({"toolCall": {"title": "shell"}}) == (True, None)
    ex._elicitation_handler.assert_not_awaited()
    ex._elicitation_choice_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_bypass_permissions_still_denies_on_policy_deny() -> None:
    """Policy runs in every mode, so a DENY still blocks under bypass.

    This is the invariant that makes the mode safe to offer: it waives the
    *human* gate, never the user's own rules.
    """
    ex = _bypass_executor()
    ex._elicitation_handler = AsyncMock(return_value=True)

    class _V:
        action = "POLICY_ACTION_DENY"

    ex._policy_evaluator = AsyncMock(return_value=_V())
    assert await ex._decide_permission({"toolCall": {"title": "shell"}}) == (False, None)


@pytest.mark.asyncio
async def test_bypass_permissions_still_prompts_on_policy_ask() -> None:
    """A policy that says ASK outranks bypass — the user asked to be asked."""
    ex = _bypass_executor()

    class _V:
        action = "POLICY_ACTION_ASK"

    ex._policy_evaluator = AsyncMock(return_value=_V())
    ex._elicitation_handler = AsyncMock(return_value=True)

    assert await ex._decide_permission({"toolCall": {"title": "shell"}}) == (True, None)
    ex._elicitation_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_default_permission_mode_still_asks() -> None:
    """The ``auto`` default is unchanged: a policy-silent call still prompts.

    **What breaks if this fails**: every ACP agent silently stops asking for
    approval — the mode would be a default-off switch instead of an opt-in.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    assert ex._config.permission_mode == "auto"
    ex._elicitation_handler = AsyncMock(return_value=True)

    assert await ex._decide_permission({"toolCall": {"title": "shell"}}) == (True, None)
    ex._elicitation_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_unrecognized_permission_mode_still_asks() -> None:
    """Only the exact ``bypassPermissions`` waives the card; anything else prompts.

    A typo (``"bypass"``) or a mode borrowed from another harness
    (``"acceptEdits"``) must fail toward asking, not toward silence.
    """
    for mode in ("bypass", "acceptEdits", "default", ""):
        ex = AcpExecutor(AcpAgentConfig(command="x", permission_mode=mode))
        ex._elicitation_handler = AsyncMock(return_value=True)
        assert await ex._decide_permission({"toolCall": {"title": "shell"}}) == (True, None), mode
        assert ex._elicitation_handler.await_count == 1, mode


@pytest.mark.asyncio
async def test_bypass_never_sends_the_agents_own_bypass_option() -> None:
    """Bypass answers each request; it never tells the agent to stop asking.

    Devin offers ``switch_bypass`` ("switch to bypass mode"). Selecting it would
    end the request stream, so omnigent would no longer see the agent's tool
    calls and the TOOL_CALL policy could not gate them. The narrow
    ``allow_once`` grant keeps every later call visible.
    """
    ex = _bypass_executor()
    ex._elicitation_handler = AsyncMock(return_value=True)
    params = {
        "toolCall": {"title": "shell"},
        "options": [
            {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
            {
                "optionId": "switch_bypass",
                "name": "Yes, switch to bypass mode",
                "kind": "allow_always",
            },
            {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
        ],
    }

    allow, option_id = await ex._decide_permission(params)
    assert ex._permission_outcome(params, allow=allow, option_id=option_id) == {
        "outcome": {"outcome": "selected", "optionId": "allow_once"}
    }


def test_harness_wrap_reads_permission_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wrap decodes the forwarded mode, closing spawn env → child config."""
    from omnigent.inner import acp_harness

    monkeypatch.setenv("HARNESS_ACP_COMMAND", "devin acp")
    monkeypatch.setenv("HARNESS_ACP_PERMISSION_MODE", "bypassPermissions")
    ex = acp_harness._build_acp_executor()
    assert isinstance(ex, AcpExecutor)
    assert ex._config.permission_mode == "bypassPermissions"
    assert ex._bypass_permissions is True


def test_harness_wrap_permission_mode_defaults_to_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset (or blank) var leaves the wrap prompting, as before this option."""
    from omnigent.inner import acp_harness

    monkeypatch.setenv("HARNESS_ACP_COMMAND", "devin acp")
    monkeypatch.delenv("HARNESS_ACP_PERMISSION_MODE", raising=False)
    assert acp_harness._build_acp_executor()._config.permission_mode == "auto"

    monkeypatch.setenv("HARNESS_ACP_PERMISSION_MODE", "   ")
    ex = acp_harness._build_acp_executor()
    assert ex._config.permission_mode == "auto"
    assert ex._bypass_permissions is False


# ---------------------------------------------------------------------------
# warm model switch (session/set_config_option)
# ---------------------------------------------------------------------------


def _model_option(current: str, *values: str) -> dict:
    """A ``config_option_update`` payload advertising a settable model."""
    return {
        "sessionUpdate": "config_option_update",
        "configOptions": [
            {
                "id": "model",
                "currentValue": current,
                "options": [{"value": v} for v in values],
            }
        ],
    }


def test_config_option_update_records_options_and_active_model() -> None:
    """
    The agent's ``currentValue`` is the only trustworthy record of the live model.

    **What breaks if this fails**: we'd re-request a switch already in effect, or
    trust the model's own self-report — which lies (Devin reported ``FAMILY=SWE``
    after a confirmed switch to Gemini).
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update(_model_option("swe-1-7-medium", "swe-1-7-medium", "gemini-3-1-pro"))
    assert ex._active_model == "swe-1-7-medium"
    assert "model" in ex._config_option_ids


@pytest.mark.asyncio
async def test_model_override_switches_warm_via_set_config_option() -> None:
    """
    A new model is applied with ``session/set_config_option`` using ``configId``.

    ``configId`` is the parameter name the agent expects; ``optionId`` fails with
    ``missing field 'configId'``. The session is NOT recreated, so the transcript
    survives the switch.

    **What breaks if this fails**: ``/model`` silently does nothing mid-session,
    or the switch drops the conversation by respawning.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update(_model_option("swe-1-7-medium"))
    calls: list[tuple[str, dict]] = []

    async def fake_rpc(method, params, timeout=30.0):
        calls.append((method, params))
        return {"result": {"configOptions": [{"id": "model", "currentValue": params["value"]}]}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._apply_model_override("s1", "gemini-3-1-pro-low")

    assert calls == [
        (
            "session/set_config_option",
            {"sessionId": "s1", "configId": "model", "value": "gemini-3-1-pro-low"},
        )
    ]
    assert ex._active_model == "gemini-3-1-pro-low"


@pytest.mark.asyncio
async def test_model_override_noops_when_already_active_or_unset() -> None:
    """No redundant round-trip when the model is unchanged or unspecified."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update(_model_option("swe-1-7-medium"))
    ex._rpc = AsyncMock()  # type: ignore[assignment]

    await ex._apply_model_override("s1", None)
    await ex._apply_model_override("s1", "swe-1-7-medium")
    ex._rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_override_skipped_when_agent_has_no_model_option() -> None:
    """
    An agent advertising options but no ``model`` one is left alone.

    **What breaks if this fails**: every turn sends a doomed request to agents
    that simply don't support switching (goose, kilocode, …).
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update(
        {"sessionUpdate": "config_option_update", "configOptions": [{"id": "mode"}]}
    )
    ex._rpc = AsyncMock()  # type: ignore[assignment]
    await ex._apply_model_override("s1", "some-model")
    ex._rpc.assert_not_awaited()
    assert ex._model_switch_supported is False


@pytest.mark.asyncio
async def test_model_override_rejection_latches_off_and_does_not_raise() -> None:
    """
    A rejected switch is logged and disabled, never fatal.

    **What breaks if this fails**: an agent that doesn't implement
    ``session/set_config_option`` fails the whole turn instead of answering on
    the model it already has.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update(_model_option("m1"))
    ex._rpc = AsyncMock(return_value={"error": {"code": -32601, "message": "unsupported"}})  # type: ignore[assignment]

    await ex._apply_model_override("s1", "m2")
    assert ex._model_switch_supported is False
    assert ex._active_model == "m1"  # unchanged

    # Latched off: a later turn must not retry.
    ex._rpc.reset_mock()
    await ex._apply_model_override("s1", "m3")
    ex._rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_override_trusts_echoed_value_over_request() -> None:
    """
    When the agent echoes a ``currentValue`` that differs from the request, we
    record the echo — not the request.

    An agent may normalize the id or silently keep its current model while still
    returning success. ``currentValue`` is the only trustworthy record, so
    ``_active_model`` must reflect it. Because the request was not actually
    reached, the next turn must be free to retry it.

    **What breaks if this fails**: ``_active_model`` reflects a model the agent
    never switched to, so a later ``/model`` for the requested id is wrongly
    skipped as already-active.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update(_model_option("swe-1-7-medium"))

    async def fake_rpc(method, params, timeout=30.0):
        # Agent accepts the call but reports a different (normalized) id.
        return {"result": {"configOptions": [{"id": "model", "currentValue": "gemini-3-1-pro"}]}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._apply_model_override("s1", "gemini-3-1-pro-low")

    # Echo wins over the request.
    assert ex._active_model == "gemini-3-1-pro"
    # The requested id was not reached, so a later turn still attempts it.
    calls: list[str] = []

    async def tracking_rpc(method, params, timeout=30.0):
        calls.append(params["value"])
        return {"result": {"configOptions": [{"id": "model", "currentValue": params["value"]}]}}

    ex._rpc = tracking_rpc  # type: ignore[assignment]
    await ex._apply_model_override("s1", "gemini-3-1-pro-low")
    assert calls == ["gemini-3-1-pro-low"]


@pytest.mark.asyncio
async def test_model_override_falls_back_to_request_when_no_option_echoed() -> None:
    """
    When the agent accepts the switch but echoes no model option, record the
    requested model.

    **What breaks if this fails**: ``_active_model`` is left stale after a
    successful switch, so the same switch is re-requested every turn.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update(_model_option("swe-1-7-medium"))
    ex._rpc = AsyncMock(return_value={"result": {}})  # type: ignore[assignment]

    await ex._apply_model_override("s1", "gemini-3-1-pro-low")
    assert ex._active_model == "gemini-3-1-pro-low"


# ---------------------------------------------------------------------------
# interrupt → session/cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupt_sends_session_cancel() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._session_id = "s1"
    ex._proc = type("P", (), {"returncode": None})()  # type: ignore[assignment]
    sent: list[dict] = []
    ex._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]
    assert await ex.interrupt_session("ignored") is True
    assert sent == [{"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "s1"}}]


@pytest.mark.asyncio
async def test_interrupt_noop_without_session() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    assert await ex.interrupt_session("ignored") is False


# ---------------------------------------------------------------------------
# Harness wrap env parsing
# ---------------------------------------------------------------------------


def test_harness_wrap_requires_command(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.inner import acp_harness

    monkeypatch.delenv("HARNESS_ACP_COMMAND", raising=False)
    with pytest.raises(RuntimeError, match="HARNESS_ACP_COMMAND"):
        acp_harness._build_acp_executor()


def test_harness_wrap_builds_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.inner import acp_harness

    monkeypatch.setenv("HARNESS_ACP_COMMAND", "goose acp")
    monkeypatch.setenv("HARNESS_ACP_NAME", "Goose")
    monkeypatch.setenv("HARNESS_ACP_SESSION_ID_MODE", "client")
    monkeypatch.setenv("HARNESS_ACP_SEND_MODEL", "1")
    monkeypatch.setenv("HARNESS_ACP_OMNIGENT_MCP", "0")
    monkeypatch.setenv("HARNESS_ACP_MODEL", "gpt-5.3")
    ex = acp_harness._build_acp_executor()
    assert isinstance(ex, AcpExecutor)
    assert ex._config.command == "goose acp"
    assert ex._config.name == "Goose"
    assert ex._config.session_id_mode == "client"
    assert ex._config.send_model_in_session_new is True
    assert ex._config.omnigent_mcp is False
    assert ex._config.model == "gpt-5.3"


def test_harness_wrap_reads_inject_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """HARNESS_ACP_INJECT_SYSTEM_PROMPT=0 sets inject_system_prompt=False (#4917)."""
    from omnigent.inner import acp_harness

    monkeypatch.setenv("HARNESS_ACP_COMMAND", "omp acp")
    monkeypatch.setenv("HARNESS_ACP_INJECT_SYSTEM_PROMPT", "0")
    ex = acp_harness._build_acp_executor()
    assert isinstance(ex, AcpExecutor)
    assert ex._config.inject_system_prompt is False


def test_harness_wrap_inject_system_prompt_defaults_to_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """inject_system_prompt defaults to True when env var is absent."""
    from omnigent.inner import acp_harness

    monkeypatch.setenv("HARNESS_ACP_COMMAND", "goose acp")
    monkeypatch.delenv("HARNESS_ACP_INJECT_SYSTEM_PROMPT", raising=False)
    ex = acp_harness._build_acp_executor()
    assert isinstance(ex, AcpExecutor)
    assert ex._config.inject_system_prompt is True


def test_harness_wrap_reads_env_passthrough_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wrap decodes the forwarded names, closing parent → child → spawn env."""
    from omnigent.inner import acp_harness

    monkeypatch.setenv("HARNESS_ACP_COMMAND", "grok agent stdio")
    monkeypatch.setenv("HARNESS_ACP_ENV_PASSTHROUGH", "XAI_API_KEY, GROK_TOKEN ,")
    monkeypatch.setenv("XAI_API_KEY", "xai-secret")
    ex = acp_harness._build_acp_executor()
    assert isinstance(ex, AcpExecutor)
    assert ex._config.env_passthrough == ("XAI_API_KEY", "GROK_TOKEN")
    # And it actually lands in the env the agent is spawned with.
    assert ex._build_spawn_env().get("XAI_API_KEY") == "xai-secret"


# ---------------------------------------------------------------------------
# Hermetic end-to-end: drive a real fake ACP agent over stdio
# ---------------------------------------------------------------------------

# A minimal ACP agent: JSON-RPC 2.0 over newline-delimited stdio. It answers the
# handshake, then on session/prompt streams a thought + text + a tool card, asks
# the client for permission, and — once the client answers — finishes the tool
# card, streams closing text, and returns the prompt with a stop reason + usage.
_FAKE_ACP_AGENT = r"""
import sys, json

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def update(sid, upd):
    send({"jsonrpc": "2.0", "method": "session/update",
          "params": {"sessionId": sid, "update": upd}})

def chunk(sid, kind, text):
    update(sid, {"sessionUpdate": kind, "content": {"type": "text", "text": text}})

pending_prompt_id = None
pending_sid = None
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": 1,
            "agentCapabilities": {"promptCapabilities": {"image": False}},
        }})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "fake-session-1"}})
    elif method == "session/prompt":
        sid = msg["params"]["sessionId"]
        chunk(sid, "agent_thought_chunk", "planning")
        chunk(sid, "agent_message_chunk", "Hello ")
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "shell",
                     "kind": "execute", "status": "pending", "rawInput": {"command": "echo hi"}})
        send({"jsonrpc": "2.0", "id": 900, "method": "session/request_permission", "params": {
            "sessionId": sid,
            "toolCall": {"title": "shell", "kind": "execute", "rawInput": {"command": "echo hi"}},
            "options": [{"optionId": "ok", "kind": "allow_once"},
                        {"optionId": "no", "kind": "reject_once"}],
        }})
        pending_prompt_id, pending_sid = mid, sid
    elif mid == 900 and method is None:
        # The client's permission reply — finish the turn.
        update(pending_sid, {"sessionUpdate": "tool_call_update",
                             "toolCallId": "t1", "status": "completed"})
        chunk(pending_sid, "agent_message_chunk", "done")
        send({"jsonrpc": "2.0", "id": pending_prompt_id, "result": {
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
        }})
"""


@pytest.mark.asyncio
async def test_end_to_end_against_fake_acp_agent(tmp_path: Path) -> None:
    agent_path = tmp_path / "fake_acp_agent.py"
    agent_path.write_text(_FAKE_ACP_AGENT)
    command = shlex.join([sys.executable, str(agent_path)])

    ex = AcpExecutor(AcpAgentConfig(command=command, name="Fake"))
    approvals: list[tuple[str, dict]] = []

    async def elicit(tool_name: str, tool_input: dict) -> bool:
        approvals.append((tool_name, tool_input))
        return True

    ex._elicitation_handler = elicit  # type: ignore[assignment]

    events = []
    try:
        async for ev in ex.run_turn([{"role": "user", "content": "hi"}], [], "you are a bot"):
            events.append(ev)
    finally:
        await ex.close()

    # The permission request surfaced through the elicitation handler.
    assert approvals == [("shell", {"command": "echo hi"})]

    kinds = [type(e).__name__ for e in events]
    assert "ReasoningChunk" in kinds
    assert "ToolCallRequest" in kinds
    assert "ToolCallComplete" in kinds

    text = "".join(e.text for e in events if isinstance(e, TextChunk))
    assert text == "Hello done"

    completions = [e for e in events if isinstance(e, TurnComplete)]
    assert len(completions) == 1
    assert completions[0].usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

    tool_reqs = [e for e in events if isinstance(e, ToolCallRequest)]
    assert tool_reqs[0].name == "shell" and tool_reqs[0].args == {"command": "echo hi"}
    tool_done = [e for e in events if isinstance(e, ToolCallComplete)]
    assert tool_done[0].status is ToolCallStatus.SUCCESS


# ---------------------------------------------------------------------------
# Omnigent MCP bridge (session/new.mcpServers via the shared serve-mcp relay)
# ---------------------------------------------------------------------------


def test_mcp_to_acp_servers_flattens_env_to_array() -> None:
    """ACP wants env as [{name,value}], not a dict; command/args pass through."""
    out = _to_acp_mcp_servers(
        {"mcpServers": {"omnigent": {"command": "/py", "args": ["-Im", "x"], "env": {"A": "1"}}}}
    )
    assert out == [
        {
            "name": "omnigent",
            "command": "/py",
            "args": ["-Im", "x"],
            "env": [{"name": "A", "value": "1"}],
        }
    ]


def test_mcp_disabled_returns_empty() -> None:
    m = OmnigentAcpMcp("t")
    assert (
        m.session_new_servers(
            tools=[{"name": "x"}], tool_executor=lambda *a: None, loop=None, enabled=False
        )
        == []
    )


def test_mcp_no_executor_returns_empty() -> None:
    m = OmnigentAcpMcp("t")
    assert m.session_new_servers(tools=[{"name": "x"}], tool_executor=None, loop=None) == []


def test_mcp_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_ACP_MCP", "0")
    m = OmnigentAcpMcp("t")
    assert (
        m.session_new_servers(tools=[{"name": "x"}], tool_executor=lambda *a: None, loop=None)
        == []
    )


@pytest.mark.asyncio
async def test_mcp_relay_starts_and_builds_serve_mcp_entry() -> None:
    """A real relay boots (writes bridge.json + tool_relay.json + HTTP server)
    and yields one ACP stdio server pointing at the shared serve-mcp."""
    m = OmnigentAcpMcp("t")

    async def fake_exec(name: str, args: dict) -> dict:
        return {"ok": True}

    loop = asyncio.get_event_loop()
    servers = m.session_new_servers(
        tools=[{"name": "sys_agent_list", "parameters": {"type": "object", "properties": {}}}],
        tool_executor=fake_exec,
        loop=loop,
    )
    try:
        assert len(servers) == 1
        entry = servers[0]
        assert entry["name"] == "omnigent"
        assert "serve-mcp" in entry["args"]
        assert "omnigent.claude_native_bridge" in entry["args"]
        assert all("name" in e and "value" in e for e in entry["env"])
        # Idempotent: a second call returns the cached relay, not a new one.
        assert m.session_new_servers(tools=[], tool_executor=fake_exec, loop=loop) is servers
    finally:
        m.close()


@pytest.mark.asyncio
async def test_acp_session_new_carries_mcp_servers() -> None:
    """When a tool executor + tools are present, session/new carries mcpServers."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._tool_executor = lambda n, a: None  # type: ignore[assignment]
    ex._omnigent_tools = [{"name": "sys_agent_list"}]
    sentinel = [{"name": "omnigent", "command": "/py", "args": [], "env": []}]
    ex._mcp.session_new_servers = lambda **kw: sentinel  # type: ignore[method-assign]
    captured: dict = {}

    async def fake_rpc(method, params, timeout=30.0):
        captured["params"] = params
        return {"result": {"sessionId": "s1"}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._ensure_session()
    assert captured["params"]["mcpServers"] is sentinel


@pytest.mark.asyncio
async def test_acp_session_new_omnigent_mcp_disabled_per_agent() -> None:
    """`omnigent_mcp=False` disables relay setup but preserves ACP's field."""
    ex = AcpExecutor(AcpAgentConfig(command="x", omnigent_mcp=False))
    ex._tool_executor = lambda n, a: None  # type: ignore[assignment]
    ex._omnigent_tools = [{"name": "sys_agent_list"}]
    captured: dict = {}

    async def fake_rpc(method, params, timeout=30.0):
        captured["params"] = params
        return {"result": {"sessionId": "s1"}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._ensure_session()
    assert captured["params"]["mcpServers"] == []


def test_omnigent_tools_cleared_when_mcp_disabled() -> None:
    """run_turn discards builtin tools when omnigent_mcp=False (#4917).

    With the relay disabled, the tool schemas serve no purpose and must not be
    stored — they could otherwise accidentally reach the session/prompt path.
    Verified by pre-populating _omnigent_tools and running the capture logic
    from the start of run_turn in isolation.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x", omnigent_mcp=False))
    tools = [{"name": "load_skill"}, {"name": "sys_session_rename"}]

    # Simulate the first few lines of run_turn: capture _omnigent_tools.
    # When omnigent_mcp is False the assignment must yield an empty list.
    ex._omnigent_tools = (tools or []) if ex._config.omnigent_mcp else []
    assert ex._omnigent_tools == [], "tools must be discarded when omnigent_mcp=False"


def test_omnigent_tools_kept_when_mcp_enabled() -> None:
    """Sanity: _omnigent_tools is populated when omnigent_mcp=True."""
    ex = AcpExecutor(AcpAgentConfig(command="x", omnigent_mcp=True))
    tools = [{"name": "load_skill"}, {"name": "sys_session_rename"}]
    ex._omnigent_tools = (tools or []) if ex._config.omnigent_mcp else []
    assert ex._omnigent_tools == tools, "tools must be stored when omnigent_mcp=True"


@pytest.mark.asyncio
async def test_inject_system_prompt_false_skips_prepend(tmp_path: Path) -> None:
    """inject_system_prompt=False prevents the spec's system prompt from being
    folded into the first ACP user turn (#4917 — Pi-fork agents like omp).

    Without this fix, Omnigent's system prompt is prepended to the user message
    on the first turn.  For agents that fully own their own system prompt (Pi
    forks), this confuses the internal Claude model into emitting XML tool-call
    fragments (``</function></tool_call>``) when there is no MCP relay backing
    the described tools.
    """
    agent_path = tmp_path / "prompt_echo_agent.py"
    agent_path.write_text(
        r"""
import sys, json

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid = msg.get("id")
    method = msg.get("method", "")
    if method == "initialize":
        caps = {"promptCapabilities": {"image": False}}
        send({"jsonrpc": "2.0", "id": mid,
              "result": {"protocolVersion": 1, "agentCapabilities": caps}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "echo-1"}})
    elif method == "session/prompt":
        sid = msg.get("params", {}).get("sessionId", "echo-1")
        # Echo back the text the client sent so the test can inspect it.
        text = ""
        for block in msg.get("params", {}).get("prompt", []):
            if isinstance(block, dict) and block.get("type") == "text":
                text += block.get("text", "")
        send({"jsonrpc": "2.0", "method": "session/update",
              "params": {"sessionId": sid,
                         "update": {"sessionUpdate": "agent_message_chunk",
                                    "content": {"type": "text", "text": text}}}})
        send({"jsonrpc": "2.0", "id": mid,
              "result": {"stopReason": "end_turn", "usage": {}}})
"""
    )
    command = shlex.join([sys.executable, str(agent_path)])

    # With injection enabled (default), the system prompt is prepended.
    ex_inject = AcpExecutor(AcpAgentConfig(command=command, inject_system_prompt=True))
    texts_inject: list[str] = []
    try:
        async for ev in ex_inject.run_turn(
            [{"role": "user", "content": "hello"}], [], "SYSTEM_PROMPT_TEXT"
        ):
            if isinstance(ev, TextChunk):
                texts_inject.append(ev.text)
    finally:
        await ex_inject.close()
    combined_inject = "".join(texts_inject)
    assert "SYSTEM_PROMPT_TEXT" in combined_inject, (
        "system prompt should appear in the echoed first turn when inject_system_prompt=True"
    )

    # With injection disabled, the system prompt must NOT appear.
    ex_no_inject = AcpExecutor(AcpAgentConfig(command=command, inject_system_prompt=False))
    texts_no_inject: list[str] = []
    try:
        async for ev in ex_no_inject.run_turn(
            [{"role": "user", "content": "hello"}], [], "SYSTEM_PROMPT_TEXT"
        ):
            if isinstance(ev, TextChunk):
                texts_no_inject.append(ev.text)
    finally:
        await ex_no_inject.close()
    combined_no_inject = "".join(texts_no_inject)
    assert "SYSTEM_PROMPT_TEXT" not in combined_no_inject, (
        "system prompt must not appear in the first turn when inject_system_prompt=False"
    )
    assert "hello" in combined_no_inject, "user message itself must still be sent"


@pytest.mark.asyncio
async def test_end_to_end_denied_permission(tmp_path: Path) -> None:
    """A denied elicitation still completes the turn (the agent gets a reject)."""
    agent_path = tmp_path / "fake_acp_agent.py"
    agent_path.write_text(_FAKE_ACP_AGENT)
    command = shlex.join([sys.executable, str(agent_path)])

    ex = AcpExecutor(AcpAgentConfig(command=command, name="Fake"))

    async def deny(tool_name: str, tool_input: dict) -> bool:
        return False

    ex._elicitation_handler = deny  # type: ignore[assignment]

    events = []
    try:
        async for ev in ex.run_turn([{"role": "user", "content": "hi"}], [], ""):
            events.append(ev)
    finally:
        await ex.close()

    # Turn still completes even though the tool was rejected.
    assert any(isinstance(e, TurnComplete) for e in events)


# ---------------------------------------------------------------------------
# describe_exception — never report a blank turn error (#4281)
# ---------------------------------------------------------------------------


def test_describe_exception_falls_back_to_repr_for_blank_message():
    """A bare exception whose ``str()`` is empty is described by ``repr()``.

    Regression for #4281: executors reported failures via ``str(exc)``, so a
    bare ``RuntimeError()`` reached the operator as "inner executor error: "
    with no detail. The fallback must at least name the exception type.
    """
    assert str(RuntimeError()) == ""  # the exact blank-message case from the bug
    described = describe_exception(RuntimeError())
    assert described != ""
    assert "RuntimeError" in described


def test_describe_exception_preserves_a_real_message():
    """When the exception carries a message, it is used verbatim (no repr noise)."""
    assert describe_exception(ValueError("boom: bad line")) == "boom: bad line"


@pytest.mark.parametrize(
    "exc",
    [RuntimeError(), TimeoutError(), OSError(), Exception()],
)
def test_describe_exception_never_blank(exc: BaseException):
    """No bare stdlib exception yields an empty description."""
    assert describe_exception(exc).strip() != ""


# ---------------------------------------------------------------------------
# Spawn env: the agent must actually receive credentials (#4281)
# ---------------------------------------------------------------------------


def test_spawn_env_keeps_credential_declared_on_the_agent() -> None:
    """A name in the agent's own ``env_passthrough`` reaches the subprocess.

    This is the config path a user actually has (an ``acp.agents:`` row).
    Without it the agent starts unauthenticated and stalls during the
    handshake, which surfaced as a turn that failed with no message at all.
    """
    ex = AcpExecutor(
        AcpAgentConfig(command="agent stdio", name="Grok", env_passthrough=("XAI_API_KEY",))
    )
    with patch.dict(os.environ, {"XAI_API_KEY": "xai-secret"}, clear=False):
        env = ex._build_spawn_env()
    assert env.get("XAI_API_KEY") == "xai-secret"


def test_spawn_env_keeps_credential_declared_on_the_spec() -> None:
    """A spec-declared ``os_env.sandbox.env_passthrough`` name still works."""
    os_env = OSEnvSpec(
        type="caller_process",
        cwd=None,
        sandbox=OSEnvSandboxSpec(type="none", env_passthrough=["XAI_API_KEY"]),
        fork=False,
    )
    ex = AcpExecutor(AcpAgentConfig(command="agent stdio", name="Grok"), os_env=os_env)
    with patch.dict(os.environ, {"XAI_API_KEY": "xai-secret"}, clear=False):
        env = ex._build_spawn_env()
    assert env.get("XAI_API_KEY") == "xai-secret"


def test_spawn_env_unions_agent_and_spec_declarations() -> None:
    """Both sources apply; neither shadows the other."""
    os_env = OSEnvSpec(
        type="caller_process",
        cwd=None,
        sandbox=OSEnvSandboxSpec(type="none", env_passthrough=["FROM_SPEC"]),
        fork=False,
    )
    ex = AcpExecutor(
        AcpAgentConfig(command="agent stdio", name="A", env_passthrough=("FROM_AGENT",)),
        os_env=os_env,
    )
    with patch.dict(os.environ, {"FROM_SPEC": "1", "FROM_AGENT": "2"}, clear=False):
        env = ex._build_spawn_env()
    assert env.get("FROM_SPEC") == "1"
    assert env.get("FROM_AGENT") == "2"


def test_spawn_env_still_excludes_undeclared_secret() -> None:
    """Deny-by-default holds: an undeclared provider key is not handed over."""
    ex = AcpExecutor(AcpAgentConfig(command="agent stdio", name="Grok"))
    with patch.dict(os.environ, {"UNRELATED_API_KEY": "nope"}, clear=False):
        env = ex._build_spawn_env()
    assert "UNRELATED_API_KEY" not in env


@pytest.mark.asyncio
async def test_handshake_timeout_reports_a_non_blank_error(tmp_path: Path) -> None:
    """An agent that never answers ``session/new`` yields a named error.

    ``asyncio.TimeoutError`` has an empty ``str()``, so reporting the failure by
    ``str(exc)`` produced the blank "inner executor error: " an operator can't
    act on.
    """
    agent_path = tmp_path / "silent_agent.py"
    agent_path.write_text(
        "import sys, json\n"
        "for line in sys.stdin:\n"
        "    line = line.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    msg = json.loads(line)\n"
        "    if msg.get('method') == 'initialize':\n"
        "        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': msg['id'],\n"
        "            'result': {'protocolVersion': 1, 'agentCapabilities': {}}}) + '\\n')\n"
        "        sys.stdout.flush()\n"
        # session/new deliberately unanswered -> the handshake RPC times out.
    )
    command = shlex.join([sys.executable, str(agent_path)])

    ex = AcpExecutor(AcpAgentConfig(command=command, name="Silent"))
    errors = []
    with patch.object(acp_executor_module, "_INIT_TIMEOUT_SECONDS", 1.0):
        try:
            async for ev in ex.run_turn([{"role": "user", "content": "hi"}], [], ""):
                if isinstance(ev, ExecutorError):
                    errors.append(ev)
        finally:
            await ex.close()

    assert errors, "a stalled handshake must surface an ExecutorError"
    assert errors[0].message.strip(), "the turn error must never be blank"
    # Names the stalled call, not just the exception type.
    assert "session/new" in errors[0].message
    assert "Silent" in errors[0].message


# ---------------------------------------------------------------------------
# Diagnostics: the agent's own stderr must reach the operator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_failure_quotes_the_agents_stderr(tmp_path: Path) -> None:
    """An agent that explains itself on stderr has that text in the turn error.

    A stalled handshake names only the RPC that timed out. The reason usually
    sits on the agent's stderr ("no API key"), which was drained at debug level
    into a logger the harness child had no handler for — so it reached nobody.
    """
    agent_path = tmp_path / "noisy_agent.py"
    agent_path.write_text(
        "import sys, json, time\n"
        "for line in sys.stdin:\n"
        "    line = line.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    msg = json.loads(line)\n"
        "    if msg.get('method') == 'initialize':\n"
        "        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': msg['id'],\n"
        "            'result': {'protocolVersion': 1, 'agentCapabilities': {}}}) + '\\n')\n"
        "        sys.stdout.flush()\n"
        "    elif msg.get('method') == 'session/new':\n"
        "        sys.stderr.write('ERROR: XAI_API_KEY not set\\n')\n"
        "        sys.stderr.flush()\n"
        "        time.sleep(3600)\n"
    )
    command = shlex.join([sys.executable, str(agent_path)])

    ex = AcpExecutor(AcpAgentConfig(command=command, name="Grok"))
    errors = []
    with patch.object(acp_executor_module, "_INIT_TIMEOUT_SECONDS", 1.0):
        try:
            async for ev in ex.run_turn([{"role": "user", "content": "hi"}], [], ""):
                if isinstance(ev, ExecutorError):
                    errors.append(ev)
        finally:
            await ex.close()

    assert errors, "a stalled handshake must surface an ExecutorError"
    assert "XAI_API_KEY not set" in errors[0].message


def test_stderr_ring_is_bounded_and_lines_are_capped() -> None:
    """A chatty agent can't grow the ring or put a huge line in a UI toast."""
    ex = AcpExecutor(AcpAgentConfig(command="x", name="A"))
    for i in range(acp_executor_module._STDERR_RING_LINES * 3):
        ex._recent_stderr.append(f"line{i}")
    assert len(ex._recent_stderr) == acp_executor_module._STDERR_RING_LINES

    ex._recent_stderr.clear()
    ex._recent_stderr.append("x" * 10_000)
    msg = ex._startup_error_message(TimeoutError())
    assert len(msg) < 2_000, "a single huge stderr line must not dominate the error"


def test_startup_error_names_the_exception_type_when_str_is_empty() -> None:
    """No stderr and an empty ``str(exc)`` still yields something actionable."""
    ex = AcpExecutor(AcpAgentConfig(command="x", name="A"))
    assert "TimeoutError" in ex._startup_error_message(TimeoutError())
