// Mirrors sdks/python-client/omnigent_client/_stream.py.
//
// Hand-ported. When _stream.py changes, update this file and add or
// extend the matching case in blockStream.test.ts. See ap-web/README.md
// "Reducer parity" for the workflow.
//
// The state-machine logic — flush thresholds, dedup sets, pending-tool
// sweeping, reasoning↔text closure — is the SAME as the Python reducer.
// If a comment looks long, that's because it's transcribed verbatim
// from the Python source so the next maintainer sees the same context.

import {
  type AnyBlock,
  type BlockContext,
  type CompactionBlock,
  type CompactionInProgressBlock,
  type ElicitationBlock,
  type ErrorBlock,
  type FileBlock,
  type NativeToolBlock,
  type PolicyDeniedBlock,
  type ReasoningBlock,
  type ReasoningChunk,
  type ReasoningStartBlock,
  type ResponseEndBlock,
  type ResponseStartBlock,
  type RetryBlock,
  type SlashCommandBlock,
  type TerminalCommandBlock,
  type TextChunk,
  type TextDone,
  type ToolExecution,
  type ToolGroup,
  type ToolResultBlock,
  type UserMessageBlock,
  slashCommandEchoItemId,
  slashCommandEchoText,
} from "./blocks";
import type { StreamEvent } from "./events";
import type { Response } from "./types";

const DEFAULT_FLUSH_THRESHOLD = 30;

/**
 * Format tool arguments for inline display next to the tool name.
 *
 * Public so frontends that re-render historical tool calls can produce
 * the same `argsSummary` string the live stream produced. Without a
 * single source of truth, "this is what you saw originally" diverges
 * from "this is what the renderer chooses now" the moment either side
 * changes.
 */
export function formatToolArgsBrief(name: string, args: Record<string, unknown>): string {
  if (Object.keys(args).length === 0) {
    return "";
  }
  const KEYS: Record<string, string> = {
    Read: "file_path",
    Write: "file_path",
    Edit: "file_path",
    Bash: "command",
    Glob: "pattern",
    Grep: "pattern",
    web_search: "query",
  };
  const key = KEYS[name];
  if (key && key in args) {
    let s = String(args[key]);
    if (key === "file_path" && s.includes("/")) {
      const idx = s.lastIndexOf("/");
      s = s.slice(idx + 1);
    }
    return s.length > 80 ? s.slice(0, 80) + "…" : s;
  }
  let s: string;
  try {
    s = JSON.stringify(args);
  } catch {
    s = String(args);
  }
  return s.length > 80 ? s.slice(0, 80) + "…" : s;
}

export function formatNativeLabel(toolType: string, data: Record<string, unknown>): string {
  if (toolType === "web_search_call") {
    const action = data.action;
    if (action && typeof action === "object" && !Array.isArray(action)) {
      const a = action as Record<string, unknown>;
      const at = String(a.type ?? "");
      if (at === "search") {
        const q = String(a.query ?? "");
        return `web search: ${q.slice(0, 80)}`;
      }
      if (at === "open_page") {
        const u = String(a.url ?? "");
        return `web open: ${u.slice(0, 80)}`;
      }
    }
    return "web search";
  }
  if (toolType === "mcp_call") {
    const n = String(data.name ?? "");
    return n ? `mcp: ${n}` : "mcp call";
  }
  return toolType.replaceAll("_", " ");
}

interface ReducerState {
  flushThreshold: number;

  inReasoning: boolean;
  reasoningText: string;
  summaryText: string;
  // Per-section accumulator for reasoning chunks awaiting a newline /
  // threshold flush. Mirrors `accumulated` for text.
  reasoningAccumulated: string;
  // Set when this reasoning section streamed any ReasoningChunk. The
  // final ReasoningBlock is then suppressed so renderers don't show
  // the same text twice (once live, once as a summary panel).
  reasoningChunksEmitted: boolean;

  inText: boolean;
  accumulated: string;
  fullText: string;

  pendingTools: Map<string, ToolExecution>;
  // Long-lived metadata for every tool call rendered in the current
  // response. Unlike `pendingTools`, this survives text deltas so a
  // delayed function_call_output can still render with the original
  // tool name/arguments instead of being dropped.
  toolExecutionsByCallId: Map<string, ToolExecution>;
  // Dedup set: every `callId` we've yielded a ToolGroup for in the
  // CURRENT response. Survives `pendingTools` clears (which fire on
  // each TextDelta to flush ToolResultBlock for completed tools), so
  // the post-stream action_required event for an MCP tool whose
  // inline observed event already rendered doesn't silently re-render
  // once the intervening text deltas cleared `pendingTools`.
  //
  // SCOPE: per-response, not session-wide. Cleared on every
  // `response_created` because the SDK can legitimately reuse a
  // `tool_use_id`-shaped call id across separate tasks; a session-
  // wide set would silently drop a legitimate second-turn tool call.
  // See migration plan §4.4.
  seenCallIds: Set<string>;
  // Dedup set: every result we've yielded a ToolResultBlock for,
  // keyed `${responseId}:${callId}` (the event's own id when stamped,
  // else the active one) — rid-scoped so a backdated cross-turn
  // result for a REUSED callId can't suppress the live turn's real
  // result. Same survives-clear semantics as
  // `seenCallIds` for the call-line side, but on the result side.
  // Necessary because every action_required tool's result fires
  // TWICE on the SSE stream: once inline from
  // `_dispatch_action_required` (the moment the dispatch returns)
  // and once from the `response.completed` flush in
  // `_translate_omnigent_event`. Without this, the result panel renders
  // twice. Per-response scope, same rationale as `seenCallIds`.
  seenResultCallIds: Set<string>;

  agent: string | null;
  turn: number;
  started: boolean;
  // Active server-assigned response id, set on each `response.created`.
  // Stamped onto every emitted block's `ctx.responseId`.
  responseId: string;
}

function createState(flushThreshold: number): ReducerState {
  return {
    flushThreshold,
    inReasoning: false,
    reasoningText: "",
    summaryText: "",
    reasoningAccumulated: "",
    reasoningChunksEmitted: false,
    inText: false,
    accumulated: "",
    fullText: "",
    pendingTools: new Map(),
    toolExecutionsByCallId: new Map(),
    seenCallIds: new Set(),
    seenResultCallIds: new Set(),
    agent: null,
    turn: 0,
    started: false,
    responseId: "",
  };
}

function ctx(
  state: ReducerState,
  itemId: string | null = null,
  responseId: string | null = null,
): BlockContext {
  const agent = state.agent;
  const depth = agent ? (agent.match(/\./g)?.length ?? 0) : 0;
  return {
    agent,
    depth,
    turn: state.turn,
    timestamp: performance.now() / 1000,
    // `responseId` overrides for out-of-band output items (a relay-backdated
    // tool output, a skill receipt's synthetic turn id) so the block renders
    // under the item's true id without moving the reducer's active id.
    responseId: responseId || state.responseId,
    itemId,
  };
}

/**
 * Close any open reasoning section, flushing trailing accumulated text
 * as a chunk and emitting the summary block when no chunks have been
 * emitted yet. Mirrors the Python `_stream.py` blocks at lines
 * 266–281, 352–366, 441–455, 501–515 (each terminal-ish branch
 * repeats the same close).
 */
function* closeReasoning(state: ReducerState): Generator<AnyBlock> {
  if (!state.inReasoning) return;
  state.inReasoning = false;
  if (state.reasoningAccumulated) {
    yield {
      type: "reasoning_chunk",
      ctx: ctx(state),
      text: state.reasoningAccumulated,
    } satisfies ReasoningChunk;
    state.reasoningChunksEmitted = true;
    state.reasoningAccumulated = "";
  }
  if (!state.reasoningChunksEmitted) {
    yield {
      type: "reasoning_block",
      ctx: ctx(state),
      reasoningText: state.reasoningText,
      summaryText: state.summaryText,
    } satisfies ReasoningBlock;
  }
}

/**
 * Close any open text section, flushing the trailing accumulated text
 * and emitting `TextDone`. Mirrors the closure at Python lines
 * 367–377, 456–466, 516–526.
 *
 * `itemId` is the server-assigned message item id from the event that
 * finalized the text (`MessageDone.itemId`). Stamped on the resulting
 * `TextDone.ctx.itemId` so renderers can correlate the streamed text
 * back to its persisted item. `null` when the closure is fired by a
 * non-message boundary (tool call, terminal event without a preceding
 * message_done).
 */
function* closeText(state: ReducerState, itemId: string | null = null): Generator<AnyBlock> {
  if (!state.inText) return;
  if (state.accumulated) {
    yield {
      type: "text_chunk",
      ctx: ctx(state),
      text: state.accumulated,
    } satisfies TextChunk;
    state.accumulated = "";
  }
  yield {
    type: "text_done",
    ctx: ctx(state, itemId),
    fullText: state.fullText,
    hasCodeBlocks: state.fullText.includes("```"),
  } satisfies TextDone;
  state.inText = false;
  state.fullText = "";
}

function outputTextFromMessageContent(content: Array<Record<string, unknown>>): string {
  let text = "";
  for (const block of content) {
    if (block.type !== "output_text") continue;
    if (typeof block.text === "string") text += block.text;
  }
  return text;
}

/**
 * Adopt an output item's response id only when no response is active
 * yet (fresh pump, e.g. terminal-observed items with no lifecycle
 * events). Once a response is active, an out-of-band id — a
 * relay-backdated tool output or a skill receipt's synthetic
 * `turn_<uuid>` — must NOT re-stamp `state.responseId`: subsequent
 * runner events carry no response_id, so the rest of the streaming
 * turn would render under the wrong id (splitting the live bubble),
 * and wiping the dedup sets mid-turn breaks the MCP double-ToolCall
 * dedup. Out-of-band blocks carry their own id via the `ctx()`
 * override instead.
 */
function adoptResponseIdIfUnset(state: ReducerState, responseId: string): void {
  if (!responseId || state.responseId !== "") return;
  state.responseId = responseId;
}

/**
 * Begin a response: finalize prior-turn blocks, reset per-response state,
 * stamp the id, emit ResponseStartBlock. Shared by `response_created` and
 * `response_in_progress` (the runner suppresses the former from the stream).
 */
function* beginResponse(state: ReducerState, response: Response): Generator<AnyBlock> {
  // Finalize a prior turn whose terminal event never arrived (fenced Stop)
  // so its partial doesn't bleed into this bubble; no-op if already closed.
  yield* closeReasoning(state);
  yield* closeText(state);
  // Emit result-only groups for tools that completed between turns.
  for (const ex of Array.from(state.pendingTools.values())) {
    if (ex.output !== null) {
      yield {
        type: "tool_result",
        ctx: ctx(state),
        name: ex.name,
        callId: ex.callId,
        agentName: ex.agentName,
        output: ex.output,
      } satisfies ToolResultBlock;
    }
  }
  state.pendingTools.clear();
  state.toolExecutionsByCallId.clear();
  state.agent = response.model;
  // Reset per-response dedup at the task boundary — cross-task call_id
  // reuse must render independently (see migration plan §4.4).
  state.seenCallIds.clear();
  state.seenResultCallIds.clear();
  // Bump `turn` after the first task so blocks carry their task index.
  if (state.started) state.turn += 1;
  state.started = true;
  // Stamp the id before emitting so every block of this turn carries it.
  state.responseId = response.id;
  yield {
    type: "response_start",
    ctx: ctx(state),
    model: state.agent,
    responseId: response.id,
    // Header carries conversation.id → navigate to /c/:id immediately.
    conversationId: response.conversation?.id ?? null,
  } satisfies ResponseStartBlock;
}

function* processEvent(state: ReducerState, event: StreamEvent): Generator<AnyBlock> {
  switch (event.type) {
    // ── Response lifecycle ──────────────────────────
    case "response_created": {
      yield* beginResponse(state, event.response);
      return;
    }

    case "response_queued":
      return;

    case "response_in_progress": {
      // Runner suppresses response.created, so in_progress is the only live
      // turn header — adopt its id (idempotent if already begun), else the
      // opening text/reasoning render under an empty id and split off.
      if (event.response.id && event.response.id !== state.responseId) {
        yield* beginResponse(state, event.response);
      }
      return;
    }

    // ── Reasoning ───────────────────────────────────
    case "reasoning_started": {
      // Already reasoning = a new summarized-thinking block in the same
      // section. Flush this block's tail + a separator so consecutive
      // thought items don't render run-together ("…item one.Item two").
      if (state.inReasoning) {
        yield {
          type: "reasoning_chunk",
          ctx: ctx(state),
          text: state.reasoningAccumulated + "\n\n",
        } satisfies ReasoningChunk;
        state.reasoningAccumulated = "";
        state.reasoningChunksEmitted = true;
        return;
      }
      // Entering reasoning closes open text (symmetric with text_delta
      // closing reasoning) — else interleaved think→speak→think orphans
      // the pre-reasoning text and concatenates segments.
      yield* closeText(state);
      state.inReasoning = true;
      state.reasoningText = "";
      state.summaryText = "";
      state.reasoningAccumulated = "";
      state.reasoningChunksEmitted = false;
      yield {
        type: "reasoning_start",
        ctx: ctx(state),
      } satisfies ReasoningStartBlock;
      return;
    }

    case "reasoning_delta":
    case "reasoning_summary_delta": {
      // An out-of-order delta (no preceding ReasoningStarted) marks an
      // implicit start — Codex's bridged events arrive this way. Emit
      // the start block once so the formatter has its "thinking…" anchor.
      if (!state.inReasoning) {
        // Close any open text first — same boundary as reasoning_started.
        yield* closeText(state);
        state.inReasoning = true;
        state.reasoningText = "";
        state.summaryText = "";
        state.reasoningAccumulated = "";
        state.reasoningChunksEmitted = false;
        yield {
          type: "reasoning_start",
          ctx: ctx(state),
        } satisfies ReasoningStartBlock;
      }
      if (event.type === "reasoning_delta") {
        state.reasoningText += event.delta;
      } else {
        state.summaryText += event.delta;
      }
      // Stream the delta as a ReasoningChunk so the renderer can paint
      // mid-flight. Mirrors the TextDelta line/threshold flush so chunks
      // land on natural breaks.
      state.reasoningAccumulated += event.delta;
      while (state.reasoningAccumulated.includes("\n")) {
        const idx = state.reasoningAccumulated.indexOf("\n");
        const line = state.reasoningAccumulated.slice(0, idx);
        state.reasoningAccumulated = state.reasoningAccumulated.slice(idx + 1);
        yield {
          type: "reasoning_chunk",
          ctx: ctx(state),
          text: line + "\n",
        } satisfies ReasoningChunk;
        state.reasoningChunksEmitted = true;
      }
      if (state.reasoningAccumulated.length >= state.flushThreshold) {
        const lastSpace = state.reasoningAccumulated.lastIndexOf(" ");
        if (lastSpace > 0) {
          yield {
            type: "reasoning_chunk",
            ctx: ctx(state),
            text: state.reasoningAccumulated.slice(0, lastSpace + 1),
          } satisfies ReasoningChunk;
          state.reasoningAccumulated = state.reasoningAccumulated.slice(lastSpace + 1);
          state.reasoningChunksEmitted = true;
        }
      }
      return;
    }

    // ── Text ────────────────────────────────────────
    case "text_delta": {
      yield* closeReasoning(state);
      // Emit results for tools that completed.
      for (const ex of Array.from(state.pendingTools.values())) {
        if (ex.output !== null) {
          yield {
            type: "tool_result",
            ctx: ctx(state),
            name: ex.name,
            callId: ex.callId,
            agentName: ex.agentName,
            output: ex.output,
          } satisfies ToolResultBlock;
        }
      }
      state.pendingTools.clear();

      state.inText = true;
      state.accumulated += event.delta;
      state.fullText += event.delta;

      while (state.accumulated.includes("\n")) {
        const idx = state.accumulated.indexOf("\n");
        const line = state.accumulated.slice(0, idx);
        state.accumulated = state.accumulated.slice(idx + 1);
        yield {
          type: "text_chunk",
          ctx: ctx(state),
          text: line + "\n",
        } satisfies TextChunk;
      }

      if (state.accumulated.length >= state.flushThreshold) {
        const lastSpace = state.accumulated.lastIndexOf(" ");
        if (lastSpace > 0) {
          yield {
            type: "text_chunk",
            ctx: ctx(state),
            text: state.accumulated.slice(0, lastSpace + 1),
          } satisfies TextChunk;
          state.accumulated = state.accumulated.slice(lastSpace + 1);
        }
      }
      return;
    }

    // ── Tool calls ──────────────────────────────────
    case "tool_call": {
      adoptResponseIdIfUnset(state, event.responseId);
      // Dedupe by callId. Under the claude-sdk harness's MCP path, a
      // tool call surfaces as TWO ToolCall events with correlated
      // call ids: an inline observed event (status="completed")
      // emitted as the inner SDK parses the `tool_use` block, and a
      // post-stream action_required event emitted when the SDK
      // invokes the MCP-server handler. The adapter
      // (omnigent/runtime/harnesses/_executor_adapter.py) threads
      // the SDK's `tool_use_id` through both so they share a callId;
      // this block keeps the first occurrence (the inline render)
      // and drops the second so the renderer doesn't draw the call
      // line twice. See designs/RUN_OMNIGENT_REPL_PARITY.md.
      //
      // Non-MCP paths emit exactly one ToolCall per callId, so the
      // second-arrival branch never fires for them.
      if (state.seenCallIds.has(event.callId)) {
        // Already rendered the call line for this callId. Re-register
        // in pendingTools so the eventual `ToolResult` can pair by
        // callId — the prior pending entry may have been cleared by
        // the `pendingTools.clear()` that fires on every `text_delta`
        // between observed and action_required (or between
        // action_required and the post-PATCH function_call_output).
        const execution: ToolExecution = state.toolExecutionsByCallId.get(event.callId) ?? {
          name: event.name,
          arguments: event.arguments,
          argsSummary: formatToolArgsBrief(event.name, event.arguments),
          callId: event.callId,
          agentName: event.agentName,
          executedBy: "server",
          output: null,
        };
        state.toolExecutionsByCallId.set(event.callId, execution);
        state.pendingTools.set(event.callId, execution);
        return;
      }
      state.seenCallIds.add(event.callId);

      yield* closeReasoning(state);
      yield* closeText(state);

      const execution: ToolExecution = {
        name: event.name,
        arguments: event.arguments,
        argsSummary: formatToolArgsBrief(event.name, event.arguments),
        callId: event.callId,
        agentName: event.agentName,
        executedBy: "server",
        output: null,
      };
      state.pendingTools.set(event.callId, execution);
      state.toolExecutionsByCallId.set(event.callId, execution);
      // Yield immediately so the user sees the tool call before
      // execution. output=null means the renderer shows the call
      // line but no result panel.
      yield {
        type: "tool_group",
        ctx: ctx(state, event.itemId || null, event.responseId || null),
        executions: [execution],
        iteration: 0,
      } satisfies ToolGroup;
      return;
    }

    case "tool_result": {
      adoptResponseIdIfUnset(state, event.responseId);
      // Out-of-band = stamped with another turn's id; rid-less runner results are current.
      const isCurrentResponse = !event.responseId || event.responseId === state.responseId;
      // Rid-scoped key: a backdated result for a reused callId must not eat the live turn's result.
      const resultKey = `${event.responseId || state.responseId}:${event.callId}`;
      if (state.seenResultCallIds.has(resultKey)) {
        // Already rendered the result panel. The late
        // `response.completed` flush emission is redundant; drop it
        // so a subsequent `response_created` doesn't yield it again
        // via the pendingTools sweep.
        return;
      }
      // An out-of-band result never touches pending state — a same-callId entry there is the CURRENT turn's.
      const ex = isCurrentResponse
        ? (state.pendingTools.get(event.callId) ?? state.toolExecutionsByCallId.get(event.callId))
        : undefined;
      if (ex !== undefined) {
        ex.output = event.output;
        ex.executedBy = "client";
      }
      // `ex === undefined` happens when the call's metadata is gone or
      // out-of-band — a relay-backdated cross-turn output (maps reset
      // at the next turn's beginResponse) or a call hydrated from
      // history into a fresh pump. Emit anyway with the persisted-path
      // convention (empty name, like itemsToBlocks'
      // function_call_output) — the renderer pairs results to their
      // call card by callId.
      //
      // Yield the result panel IMMEDIATELY so multi-tool turns don't
      // bunch result rendering at end-of-turn.
      state.seenResultCallIds.add(resultKey);
      yield {
        type: "tool_result",
        ctx: ctx(state, event.itemId || null, event.responseId || null),
        name: ex?.name ?? "",
        callId: event.callId,
        agentName: ex?.agentName ?? "",
        output: event.output,
      } satisfies ToolResultBlock;
      // Drop from `pendingTools` (current turn only — the live tool is
      // still pending) so the next text_delta / response_created sweep
      // doesn't re-yield this result.
      if (isCurrentResponse) state.pendingTools.delete(event.callId);
      return;
    }

    // ── Native tools ────────────────────────────────
    case "native_tool_call": {
      adoptResponseIdIfUnset(state, event.responseId);
      yield {
        type: "native_tool",
        ctx: ctx(state, event.itemId || null, event.responseId || null),
        toolType: event.toolType,
        label: formatNativeLabel(event.toolType, event.data),
        data: event.data,
      } satisfies NativeToolBlock;
      return;
    }

    // ── Slash command (Claude Code TUI) ─────────────
    case "slash_command": {
      adoptResponseIdIfUnset(state, event.responseId);
      if (event.kind === "skill") {
        // A skill receipt is the only record of the user's send — echo
        // the typed `/name args` as a user bubble so the message doesn't
        // vanish behind the Skill indicator (mirrors `skillEchoBlock` in
        // itemsToBlocks, sharing the derived item id for dedup).
        yield {
          type: "user_message",
          ctx: {
            ...ctx(
              state,
              event.itemId ? slashCommandEchoItemId(event.itemId) : null,
              event.responseId || null,
            ),
            ...(event.createdBy !== undefined ? { createdBy: event.createdBy } : {}),
          },
          content: [
            { type: "input_text", text: slashCommandEchoText(event.name, event.arguments) },
          ],
        } satisfies UserMessageBlock;
      }
      yield {
        type: "slash_command",
        ctx: ctx(state, event.itemId || null, event.responseId || null),
        kind: event.kind,
        name: event.name,
        arguments: event.arguments,
        output: event.output,
      } satisfies SlashCommandBlock;
      return;
    }

    // ── Terminal command (!cmd) ──────────────────────
    case "terminal_command": {
      adoptResponseIdIfUnset(state, event.responseId);
      yield {
        type: "terminal_command",
        ctx: ctx(state, event.itemId || null, event.responseId || null),
        kind: event.kind,
        input: event.input,
        stdout: event.stdout,
        stderr: event.stderr,
      } satisfies TerminalCommandBlock;
      return;
    }

    // ── Message done ────────────────────────────────
    case "message_done": {
      const isResponseSwitch = !!event.responseId && event.responseId !== state.responseId;
      const hadOpenText = state.inText;
      // Snapshot accumulated text BEFORE closeText resets state.fullText —
      // used by the content-equality dedup below to handle the session-stream
      // race where ``response.created`` was lost (subscribe registered after
      // the workflow already emitted it). In that race, state.responseId is
      // either empty or carries the previous turn's id, so isResponseSwitch
      // spuriously fires for an in-fact same-response message_done.
      const accumulatedFromDeltas = state.fullText;

      // Close prior blocks under the OLD responseId before stamping the new one.
      yield* closeReasoning(state);
      if (hadOpenText) {
        // On a response switch, event.itemId belongs to the new message — don't
        // attach it to the old text block being closed.
        yield* closeText(state, isResponseSwitch ? null : event.itemId || null);
      }

      // A message_done with a new id is a genuine turn transition (the
      // turn's response.in_progress was missed) — unlike backdated tool
      // outputs / skill receipts, adopt it and reset per-response dedup,
      // mirroring beginResponse.
      if (isResponseSwitch) {
        state.responseId = event.responseId;
        state.pendingTools.clear();
        state.toolExecutionsByCallId.clear();
        state.seenCallIds.clear();
        state.seenResultCallIds.clear();
      }

      // Same-response: deltas already produced the text; skip event.content to
      // avoid duplication. Response-switch: emit event.content as the new body.
      if (hadOpenText && !isResponseSwitch) return;

      const text = outputTextFromMessageContent(event.content);

      // Race-safe dedup: even on a perceived response switch, if the deltas
      // that just closed accumulated EXACTLY the text in ``event.content``,
      // both belong to the same response and emitting a second ``text_done``
      // would duplicate the assistant message in the UI. Triggered when
      // ``response.created`` is lost on the session stream (the turn-1
      // race against fresh subscribe registration).
      if (hadOpenText && text === accumulatedFromDeltas) return;

      if (text) {
        yield {
          type: "text_done",
          ctx: ctx(state, event.itemId || null),
          fullText: text,
          hasCodeBlocks: text.includes("```"),
        } satisfies TextDone;
      }
      return;
    }

    // ── Status events ───────────────────────────────
    case "compaction_in_progress": {
      // Spinner placeholder; replaced by CompactionBlock when
      // `compaction_completed` arrives (see renderItems.buildBubbles).
      yield {
        type: "compaction_loading",
        ctx: ctx(state),
      } satisfies CompactionInProgressBlock;
      return;
    }

    case "compaction_completed": {
      yield {
        type: "compaction",
        ctx: ctx(state),
      } satisfies CompactionBlock;
      return;
    }

    case "retry": {
      yield {
        type: "retry",
        ctx: ctx(state),
        source: event.source,
        attempt: event.attempt,
        maxAttempts: event.maxAttempts,
        delaySeconds: event.delaySeconds,
      } satisfies RetryBlock;
      return;
    }

    case "error": {
      // Pass `code` through too — renderers need it as a fallback
      // label when `message` is empty (otherwise the error panel
      // shows just `[llm]` with no hint as to what went wrong).
      yield {
        type: "error",
        ctx: ctx(state, event.itemId ?? null, event.responseId ?? null),
        message: event.error.message,
        source: event.source,
        code: event.error.code,
      } satisfies ErrorBlock;
      return;
    }

    case "output_file_done": {
      yield {
        type: "file",
        ctx: ctx(state),
        fileId: event.fileId,
        filename: event.filename,
      } satisfies FileBlock;
      return;
    }

    // ── Terminal events ─────────────────────────────
    case "response_completed":
    case "response_failed":
    case "response_incomplete":
    case "response_cancelled": {
      yield* closeReasoning(state);
      yield* closeText(state);
      // Surface the error message from a failed response as an
      // inline ErrorBlock so the conversation view renders it.
      // Without this, a policy DENY that fires before any text
      // delta leaves the user staring at an empty bubble.
      if (event.type === "response_failed" && event.response.error) {
        yield {
          type: "error",
          ctx: ctx(state),
          message: event.response.error.message ?? "",
          source: "",
          code: event.response.error.code ?? "response_failed",
        } satisfies ErrorBlock;
      }
      yield {
        type: "response_end",
        ctx: ctx(state),
        status: event.response.status,
        response: event.response,
      } satisfies ResponseEndBlock;
      return;
    }

    // ── Elicitation ─────────────────────────────────
    case "elicitation_request": {
      yield* closeReasoning(state);
      yield* closeText(state);
      // Surface the MCP-shape elicitation as an inline approval
      // block. Tool / message rendering before the elicitation
      // remains intact (the block lands at its arrival position
      // in the stream); the user accepts/rejects via the approval
      // card, which posts back through
      // `POST /v1/sessions/{id}/events {type: "approval"}`.
      yield {
        type: "elicitation",
        // Stamp the card with its OWN response id so it forms a standalone
        // bubble whenever there is no active turn to anchor it to:
        //   • a REQUEST-phase ASK gates the user's prompt BEFORE any turn is
        //     forwarded, so `state.responseId` still holds the PREVIOUS turn's
        //     id; and
        //   • a terminal-driven harness (e.g. cursor-native) never emits
        //     `response_created`, so `state.responseId` is still "".
        // Left as-is, bubble grouping would fold the card into a prior bubble
        // (or group it under the empty id), defeating the ChatPage reorder that
        // keeps the gated user message above its card. Other phases WITH an
        // active turn (tool_call) keep that turn's id so the card renders
        // inline with the turn that triggered it.
        ctx:
          event.phase === "request" || state.responseId === ""
            ? ctx(state, null, `elicit_${event.elicitationId}`)
            : ctx(state),
        elicitationId: event.elicitationId,
        targetSessionId: event.targetSessionId,
        message: event.message,
        phase: event.phase,
        policyName: event.policyName,
        contentPreview: event.contentPreview,
        requestedSchema: event.requestedSchema,
        url: event.url,
        status: "pending",
        response: null,
        askUserQuestion: event.askUserQuestion,
        exitPlanMode: event.exitPlanMode,
        codexCommand: event.codexCommand,
        allowAllEdits: event.allowAllEdits,
        rememberScope: event.rememberScope,
      } satisfies ElicitationBlock;
      return;
    }

    // ── Policy denied ────────────────────────────────
    case "policy_denied": {
      yield* closeReasoning(state);
      yield* closeText(state);
      yield {
        type: "policy_denied",
        ctx: ctx(state),
        reason: event.reason ?? "Denied by policy",
        phase: event.phase ?? "",
      } satisfies PolicyDeniedBlock;
      return;
    }

    // ── Interrupt (Stop) ─────────────────────────────
    case "session_interrupted": {
      // Stop fences the turn (no response.cancelled), so seal the partial
      // HERE in stream order — deferring to the next response_created lets
      // a followup user message land first and split the stopped bubble.
      // Live block only; the server still never persists the stopped turn.
      yield* closeReasoning(state);
      yield* closeText(state);
      return;
    }

    // Events the reducer intentionally ignores, listed so a new event
    // type surfaces loudly. `session.*` are store concerns (consumed off
    // the raw stream); `compaction_failed` is a store side effect.
    case "compaction_failed":
    case "client_task_cancel":
    case "session_status":
    case "session_usage":
    case "session_todos":
    case "session_terminal_pending":
    case "session_sandbox_status":
    case "session_input_consumed":
    case "session_created":
    // Mutates an existing block in the chat-store; see
    // `handleSessionEvent`.
    case "elicitation_resolved":
      return;
  }
}

/**
 * Consumes a stream of typed events and emits semantic stream blocks.
 *
 * Mirrors `omnigent_client._stream.BlockStream`. The reducer is a
 * stateful state machine — flush thresholds for incremental text and
 * reasoning, dedup sets for tool calls and results, pending-tool
 * sweeping, and reasoning↔text closure on tool calls and terminal
 * events.
 */
export class BlockStream {
  private flushThreshold: number;

  constructor(opts?: { textFlushThreshold?: number }) {
    this.flushThreshold = opts?.textFlushThreshold ?? DEFAULT_FLUSH_THRESHOLD;
  }

  /**
   * Reduce an async stream of events into an async stream of blocks.
   * Use this in the streaming hook (Phase 1).
   */
  async *reduce(events: AsyncIterable<StreamEvent>): AsyncIterable<AnyBlock> {
    const state = createState(this.flushThreshold);
    for await (const event of events) {
      yield* processEvent(state, event);
    }
  }

  /**
   * Reduce an array of events into an array of blocks. Use this in
   * tests where the entire event sequence is known up front.
   */
  reduceSync(events: StreamEvent[]): AnyBlock[] {
    const state = createState(this.flushThreshold);
    const out: AnyBlock[] = [];
    for (const event of events) {
      for (const block of processEvent(state, event)) {
        out.push(block);
      }
    }
    return out;
  }
}
