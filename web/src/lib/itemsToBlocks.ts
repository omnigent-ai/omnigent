// Translate persisted `ConversationItem`s into a flat `AnyBlock[]` —
// the same shape the live `BlockStream` reducer produces during
// streaming. The store holds a single block list regardless of source
// (history vs live), and the renderer walks that list.
//
// Every emitted block carries `ctx.responseId` + `ctx.itemId` so the
// renderer can group by response for bubble layout and key by item id
// for stable React keys. User-message items become `UserMessageBlock`s
// in the same flat list (so the bubble walker doesn't special-case
// "where did the user input come from").

import {
  type AnyBlock,
  type BlockContext,
  type CompactionBlock,
  type ErrorBlock,
  type NativeToolBlock,
  type RoutingDecisionBlock,
  type SlashCommandBlock,
  type TerminalCommandBlock,
  type TextDone,
  type ToolExecution,
  type ToolGroup,
  type ToolResultBlock,
  type UserMessageBlock,
  slashCommandEchoItemId,
  slashCommandEchoText,
} from "./blocks";
import { formatNativeLabel, formatToolArgsBrief } from "./blockStream";
import {
  type CompactionItem,
  type ConversationItem,
  type ErrorItem,
  type FunctionCallItem,
  type FunctionCallOutputItem,
  type MessageItem,
  type NativeToolItem,
  type RoutingDecisionItem,
  type SlashCommandItem,
  type TerminalCommandItem,
  isCompactionItem,
  isErrorItem,
  isFunctionCallItem,
  isFunctionCallOutputItem,
  isMessageItem,
  isNativeToolItem,
  isRoutingDecisionItem,
  isSlashCommandItem,
  isTerminalCommandItem,
} from "./conversationItems";

/**
 * Walk persisted items in arrival order and emit a flat block list.
 *
 * Preserves item order. User-message items become `UserMessageBlock`s
 * carrying their full `content` (text + image + file) — attachment
 * rendering can read the content directly without going through a
 * pre-flattened `userText` string.
 *
 * Items without a `response_id` are silently skipped (defensive — the
 * server's persistence path always sets one).
 */
export function itemsToBlocks(items: ConversationItem[]): AnyBlock[] {
  const blocks: AnyBlock[] = [];
  for (const item of items) {
    if (!item.response_id) continue;
    if (isSlashCommandItem(item)) {
      const echo = skillEchoBlock(item);
      if (echo !== null) blocks.push(echo);
    }
    const block = itemToBlock(item);
    if (block !== null) blocks.push(block);
  }
  return blocks;
}

function itemToBlock(item: ConversationItem): AnyBlock | null {
  if (isMessageItem(item) && item.is_meta === true) {
    return null;
  }
  if (isMessageItem(item) && item.role === "user") {
    // Hide the compaction summary message injected by Claude Code
    // after /compact. It starts with a distinctive prefix and is
    // part of the model's context (needed for resume) but should
    // not be shown as a chat bubble in the web UI.
    if (isCompactionSummaryMessage(item)) {
      return null;
    }
    return userMessageToBlock(item);
  }
  if (isMessageItem(item) && item.role === "assistant") {
    return assistantMessageToBlock(item);
  }
  if (isFunctionCallItem(item)) {
    return functionCallToBlock(item);
  }
  if (isFunctionCallOutputItem(item)) {
    return functionCallOutputToBlock(item);
  }
  if (isErrorItem(item)) {
    return errorToBlock(item);
  }
  if (isNativeToolItem(item)) {
    return nativeToolToBlock(item);
  }
  if (isCompactionItem(item)) {
    return compactionToBlock(item);
  }
  if (isSlashCommandItem(item)) {
    return slashCommandToBlock(item);
  }
  if (isRoutingDecisionItem(item)) {
    return routingDecisionToBlock(item);
  }
  if (isTerminalCommandItem(item)) {
    return terminalCommandToBlock(item);
  }
  // Unknown future item types — skip silently so the page still renders.
  return null;
}

/**
 * Detect the compaction summary message injected by Claude Code after
 * `/compact`. The message carries the conversation summary for the
 * model's context but should not render as a user chat bubble.
 */
function isCompactionSummaryMessage(item: MessageItem): boolean {
  const prefix = "This session is being continued from a previous conversation";
  for (const block of item.content) {
    if (block.type === "input_text" && typeof block.text === "string") {
      if (block.text.startsWith(prefix)) {
        return true;
      }
    }
  }
  return false;
}

function userMessageToBlock(item: MessageItem): UserMessageBlock {
  return {
    type: "user_message",
    ctx: ctxFor(item),
    // Forward the full content array verbatim so the renderer can
    // pluck text, images, and files without the translator imposing
    // an interpretation. Cast restricts to the user-input subset
    // (input_text / input_image / input_file); output_text would only
    // appear on assistant messages and is excluded here.
    content: item.content.filter(
      (
        c,
      ): c is Extract<
        MessageItem["content"][number],
        { type: "input_text" | "input_image" | "input_file" }
      > => c.type === "input_text" || c.type === "input_image" || c.type === "input_file",
    ),
  };
}

function assistantMessageToBlock(item: MessageItem): TextDone {
  const text = item.content
    .filter((b): b is { type: "output_text"; text: string } => b.type === "output_text")
    .map((b) => b.text)
    .join("");
  return {
    type: "text_done",
    ctx: ctxFor(item),
    fullText: text,
    hasCodeBlocks: text.includes("```"),
    ...(item.interrupted === true ? { interrupted: true } : {}),
  };
}

function functionCallToBlock(item: FunctionCallItem): ToolGroup {
  let args: Record<string, unknown> = {};
  try {
    args = JSON.parse(item.arguments) as Record<string, unknown>;
  } catch {
    args = {};
  }
  const execution: ToolExecution = {
    name: item.name,
    arguments: args,
    argsSummary: formatToolArgsBrief(item.name, args),
    callId: item.call_id,
    agentName: item.model ?? "",
    executedBy: "server",
    output: null,
  };
  return {
    type: "tool_group",
    ctx: ctxFor(item),
    executions: [execution],
    iteration: 0,
  };
}

function functionCallOutputToBlock(item: FunctionCallOutputItem): ToolResultBlock {
  const ctx = ctxFor(item);
  return {
    type: "tool_result",
    ctx,
    name: "",
    callId: item.call_id,
    agentName: ctx.agent ?? "",
    output: item.output,
  };
}

function errorToBlock(item: ErrorItem): ErrorBlock {
  return {
    type: "error",
    ctx: ctxFor(item),
    source: item.source,
    code: item.code,
    message: item.message,
  };
}

function nativeToolToBlock(item: NativeToolItem): NativeToolBlock {
  // Native tool items carry provider-specific fields directly on the
  // item. Forward the whole record as `data` so the renderer can pluck
  // out provider-specific fields. The `formatNativeLabel` helper
  // matches the live reducer's output for consistency.
  const data = item as unknown as Record<string, unknown>;
  return {
    type: "native_tool",
    ctx: ctxFor(item),
    toolType: item.type,
    label: formatNativeLabel(item.type, data),
    data,
  };
}

function compactionToBlock(item: CompactionItem): CompactionBlock {
  return {
    type: "compaction",
    ctx: ctxFor(item),
  };
}

/**
 * Re-materialize a skill receipt's typed text as a user bubble. The
 * skill `slash_command` item is the only transcript record of the
 * user's send, so without this echo the message vanishes behind the
 * Skill indicator. Command receipts (`/effort`, `/model`, …) stay
 * indicator-only — they are state changes, not prose. A missing
 * ``kind`` defaults to skill, matching ``slashCommandToBlock``.
 */
function skillEchoBlock(item: SlashCommandItem): UserMessageBlock | null {
  if (item.kind === "command") return null;
  return {
    type: "user_message",
    ctx: { ...ctxFor(item), itemId: slashCommandEchoItemId(item.id) },
    content: [{ type: "input_text", text: slashCommandEchoText(item.name, item.arguments) }],
  };
}

function slashCommandToBlock(item: SlashCommandItem): SlashCommandBlock {
  // Coerce a missing ``output`` (server-side exclude_none) to null.
  // Default ``kind`` to ``"skill"`` for items persisted before the
  // field was added — matches the SSE shim in ``sse.ts``.
  return {
    type: "slash_command",
    ctx: ctxFor(item),
    kind: item.kind === "command" ? "command" : "skill",
    name: item.name,
    arguments: item.arguments,
    output: typeof item.output === "string" ? item.output : null,
  };
}

function routingDecisionToBlock(item: RoutingDecisionItem): RoutingDecisionBlock {
  return {
    type: "routing_decision",
    ctx: ctxFor(item),
    model: item.model,
    applied: item.applied,
    rationale: typeof item.rationale === "string" ? item.rationale : "",
    ...(item.agent !== undefined && { agent: item.agent }),
  };
}

function terminalCommandToBlock(item: TerminalCommandItem): TerminalCommandBlock {
  return {
    type: "terminal_command",
    ctx: ctxFor(item),
    kind: item.kind,
    input: item.input ?? null,
    stdout: item.stdout ?? null,
    stderr: item.stderr ?? null,
  };
}

function ctxFor(item: ConversationItem): BlockContext {
  // Items don't carry timestamps in the API surface — use 0 as a
  // stable sentinel. `BlockContext.timestamp` is only meaningful for
  // live streaming (drives the reasoning timer); historical blocks
  // render without that affordance. `agent` comes from the item's
  // `model` field when present.
  const agent = "model" in item && typeof item.model === "string" ? item.model : null;
  const depth = agent ? (agent.match(/\./g)?.length ?? 0) : 0;
  // Preserve human authorship only when the server sent it.
  const createdBy =
    "created_by" in item && typeof item.created_by === "string" ? item.created_by : undefined;
  return {
    agent,
    depth,
    turn: 0,
    timestamp: 0,
    responseId: item.response_id,
    itemId: item.id,
    ...(createdBy !== undefined ? { createdBy } : {}),
  };
}
