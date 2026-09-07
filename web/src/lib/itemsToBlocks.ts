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
  type AskUserQuestionPayload,
  castAskUserQuestionPayload,
  exitPlanModePlan,
} from "./askUserQuestion";
import {
  type AnyBlock,
  type BlockContext,
  type CompactionBlock,
  type ElicitationBlock,
  type ErrorBlock,
  type NativeToolBlock,
  type ReasoningBlock,
  type RoutingDecisionBlock,
  type SlashCommandBlock,
  type TerminalCommandBlock,
  type TextDone,
  type ToolExecution,
  type ToolGroup,
  type ToolResultBlock,
  type UserMessageBlock,
  answeredElicitationItemId,
  slashCommandEchoItemId,
  slashCommandEchoText,
  structuredErrorFields,
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
  type ReasoningItem,
  type RoutingDecisionItem,
  type SlashCommandItem,
  type TerminalCommandItem,
  isCompactionItem,
  isErrorItem,
  isFunctionCallItem,
  isFunctionCallOutputItem,
  isMessageItem,
  isNativeToolItem,
  isReasoningItem,
  isRoutingDecisionItem,
  isSlashCommandItem,
  isTerminalCommandItem,
} from "./conversationItems";
import { nativePolicyNameForAgentName } from "./nativeCodingAgents";
import { routingExtrasFromWire } from "./routingDecision";

// Claude built-ins whose call is a question TO the user rather than work
// the agent did on its own. The elicitation that carried the card is
// never persisted, so history rebuilds it from the call and its result.
const ASK_USER_QUESTION_TOOL = "AskUserQuestion";
const EXIT_PLAN_MODE_TOOL = "ExitPlanMode";

// Claude Code's ExitPlanMode result opens with this when the user
// approved; a rejection returns the feedback they typed instead.
const PLAN_APPROVED_PREFIX = "User has approved your plan";

// Each answer in an AskUserQuestion result is quoted against its question:
// `Your questions have been answered: "<question>"="<answer>", ...`.
const ANSWER_SEPARATOR = '"="';

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
  const outputs = toolOutputsByCallId(items);
  for (const item of items) {
    if (!item.response_id) continue;
    if (isSlashCommandItem(item)) {
      const echo = skillEchoBlock(item);
      if (echo !== null) blocks.push(echo);
    }
    if (isFunctionCallItem(item)) {
      const card = answeredElicitationBlock(item, outputs.get(item.call_id));
      if (card !== null) blocks.push(card);
    }
    const block = itemToBlock(item);
    if (block !== null) blocks.push(block);
  }
  return blocks;
}

function toolOutputsByCallId(items: ConversationItem[]): Map<string, string> {
  const outputs = new Map<string, string>();
  for (const item of items) {
    if (isFunctionCallOutputItem(item)) outputs.set(item.call_id, item.output);
  }
  return outputs;
}

/**
 * Rebuild the answered question / plan card for a gated Claude built-in.
 *
 * The card the user answered rode an elicitation, which is never
 * persisted — so on reload the only trace of the exchange is the tool
 * call and the result carrying what the user picked. Reconstructing a
 * responded card from that pair puts the question and the answer back
 * in the transcript, alongside the tool row the live stream also shows.
 *
 * @param item - The persisted ``AskUserQuestion`` / ``ExitPlanMode`` call.
 * @param output - The paired tool result, or undefined when this batch of
 *   items doesn't hold it (still outstanding, or split across history
 *   pages). Without it the answer is unknown, so no card is rebuilt.
 * @returns The responded card, or ``null`` when this isn't a gated
 *   question / plan call or its answer can't be recovered.
 */
function answeredElicitationBlock(
  item: FunctionCallItem,
  output: string | undefined,
): ElicitationBlock | null {
  if (output === undefined) return null;
  if (item.name !== ASK_USER_QUESTION_TOOL && item.name !== EXIT_PLAN_MODE_TOOL) return null;
  let args: Record<string, unknown>;
  try {
    args = JSON.parse(item.arguments) as Record<string, unknown>;
  } catch {
    return null;
  }
  if (item.name === EXIT_PLAN_MODE_TOOL) {
    if (exitPlanModePlan(args) === null) return null;
    return {
      ...elicitationShell(item),
      exitPlanMode: args,
      response: output.startsWith(PLAN_APPROVED_PREFIX)
        ? { action: "accept" }
        : { action: "decline", content: { feedback: output } },
    };
  }
  const payload = castAskUserQuestionPayload(args);
  if (payload === null) return null;
  const answers = answersFromToolResult(payload, output);
  if (Object.keys(answers).length === 0) return null;
  return {
    ...elicitationShell(item),
    askUserQuestion: args,
    response: { action: "accept", content: answers },
  };
}

/**
 * The fields a rebuilt card shares regardless of which built-in it came
 * from. The elicitation id is deliberately unresolvable — a responded
 * card renders no buttons, and the live id it was answered through is
 * long gone. ``policyName`` names the vendor so the card reads "Claude
 * Code" like the live one did.
 */
function elicitationShell(item: FunctionCallItem): Omit<ElicitationBlock, "response"> {
  const ctx = ctxFor(item);
  return {
    type: "elicitation",
    ctx: { ...ctx, itemId: answeredElicitationItemId(item.id) },
    elicitationId: `answered:${item.call_id}`,
    message: "",
    phase: "pre_tool_use",
    policyName: nativePolicyNameForAgentName(ctx.agent),
    contentPreview: "",
    requestedSchema: {},
    status: "responded",
  };
}

/**
 * Recover the answer the user picked for each question from Claude
 * Code's ``AskUserQuestion`` result text.
 *
 * The result quotes every answer against its question —
 * ``"<question>"="<answer>"`` — but neither side is escaped, so a
 * question containing a quote defeats generic parsing. Searching for
 * each known question verbatim sidesteps that: the questions come from
 * the call's own arguments, so they match exactly.
 *
 * @returns Question text → answer, keyed the way ``ApprovalCard``
 *   reads submitted answers. Empty when the result names no question.
 */
function answersFromToolResult(
  payload: AskUserQuestionPayload,
  output: string,
): Record<string, string> {
  const answers: Record<string, string> = {};
  for (const question of payload.questions) {
    const at = output.indexOf(`"${question.question}${ANSWER_SEPARATOR}`);
    if (at === -1) continue;
    const from = at + question.question.length + 1 + ANSWER_SEPARATOR.length;
    const end = output.indexOf('"', from);
    answers[question.question] = end === -1 ? output.slice(from) : output.slice(from, end);
  }
  return answers;
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
    if (isCompactionSummaryMessage(item) || isClaudeTaskNotificationMessage(item)) {
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
  if (isReasoningItem(item)) {
    return reasoningToBlock(item);
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

// Hide legacy Claude Task notifications persisted before the bridge marked them meta.
function isClaudeTaskNotificationMessage(item: MessageItem): boolean {
  const requiredMarkers = ["<task-notification>", "<task-id>", "</task-notification>"];
  for (const block of item.content) {
    if (block.type === "input_text" && typeof block.text === "string") {
      const text = block.text.trimStart();
      if (
        text.startsWith("<task-notification>") &&
        requiredMarkers.every((marker) => text.includes(marker))
      ) {
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
  let args: Record<string, unknown>;
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
    ...(item.level ? { level: item.level } : {}),
    ...structuredErrorFields(item),
  };
}

function reasoningToBlock(item: ReasoningItem): ReasoningBlock {
  return {
    type: "reasoning_block",
    ctx: ctxFor(item),
    reasoningText: (item.content ?? []).map((block) => block.text).join("\n\n"),
    summaryText: item.summary.map((block) => block.text).join("\n\n"),
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
    routing: routingExtrasFromWire(item as unknown as Record<string, unknown>),
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
  // `BlockContext.timestamp` is the page-relative live-streaming clock —
  // use 0 as a stable sentinel for historical items so timer affordances
  // stay live-only. The server's epoch `created_at` travels separately as
  // `createdAtS` (drives the completed-turn "Worked for" duration).
  // `agent` comes from the item's `model` field when present.
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
    ...(typeof item.created_at === "number" && item.created_at > 0
      ? { createdAtS: item.created_at }
      : {}),
  };
}
