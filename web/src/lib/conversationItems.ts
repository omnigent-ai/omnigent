// TypeScript types mirroring the server's `ConversationItem` discriminated
// union, plus fetch helpers for cursor-paginated history loading.
//
// The server flattens the union — each item carries its type-
// specific fields directly alongside the common ones (`id`, `type`,
// `response_id`, `status`), with no nested `{type, data}` wrapper.
//
// Source of truth for the shape: `omnigent/entities/conversation.py`
// + `omnigent/server/routes/conversations.py:54-67` (`to_api_dict`).
//
// We model only the fields the renderer needs. Unknown future types are
// passed through silently as `BaseItem & Record<string, unknown>` so the
// translator can skip them without crashing.

import type { MessageContentBlock } from "./blocks";

export interface BaseItem {
  id: string;
  type: string;
  response_id: string;
  status: string;
}

export interface MessageItem extends BaseItem {
  type: "message";
  role: "user" | "assistant";
  content: MessageContentBlock[];
  /** Agent name; assistant-only. Server alias for `agent`. */
  model?: string;
  /** Human author email; omitted for agent/tool/system items. */
  created_by?: string;
  /** Hidden durable context such as injected skill instructions. */
  is_meta?: boolean;
  /** Assistant-only marker for durable partial text from an interrupted turn. */
  interrupted?: boolean;
}

export interface FunctionCallItem extends BaseItem {
  type: "function_call";
  name: string;
  /** JSON string per OpenAI's Responses API spec; parse before use. */
  arguments: string;
  call_id: string;
  model?: string;
}

export interface FunctionCallOutputItem extends BaseItem {
  type: "function_call_output";
  call_id: string;
  output: string;
}

/**
 * Persisted error banner. Mirrors `response.error` so historical
 * hydration can render the same destructive banner as the live stream.
 */
export interface ErrorItem extends BaseItem {
  type: "error";
  source: string;
  code: string;
  message: string;
}

/** The provider-native tool item types the runtime persists today. */
export const NATIVE_TOOL_ITEM_TYPES = new Set<string>([
  "web_search_call",
  "file_search_call",
  "code_interpreter_call",
  "computer_call",
  "image_generation_call",
  "mcp_call",
  "mcp_list_tools",
]);

/**
 * Native tool items carry provider-specific fields directly on the item
 * (no nested `data` slot — the whole item IS the data). The translator
 * forwards the whole record into `NativeToolBlock.data`.
 */
export type NativeToolItem = BaseItem & {
  type:
    | "web_search_call"
    | "file_search_call"
    | "code_interpreter_call"
    | "computer_call"
    | "image_generation_call"
    | "mcp_call"
    | "mcp_list_tools";
} & Record<string, unknown>;

export interface CompactionItem extends BaseItem {
  type: "compaction";
  summary?: string;
  last_item_id?: string;
  model?: string;
  token_count?: number;
}

/**
 * A Claude Code slash-command invocation from the embedded TUI's
 * JSONL transcript. Lives in NON_CONTENT_ITEM_TYPES server-side so
 * downstream LLMs don't see a phantom tool call.
 */
export interface SlashCommandItem extends BaseItem {
  type: "slash_command";
  /**
   * `"skill"` for plugin/Skill invocations, `"command"` for surfaced
   * CLI built-ins. Absent on items persisted before the field was
   * added — translator coerces missing/unknown values to `"skill"`.
   */
  kind?: "skill" | "command";
  /** Command name with leading `/` stripped, e.g. `dev-productivity:simplify`. */
  name: string;
  /** Raw `<command-args>` text; empty when invoked with no args. */
  arguments: string;
  /** `<local-command-stdout>` text; absent when no stdout (server strips via exclude_none). */
  output?: string;
  /** Harness/agent name — server alias for the `agent` field. */
  model?: string;
}

/**
 * A runner-side terminal command (`!cmd`) observed in a Claude Code
 * embedded TUI transcript. Two items per invocation: one `kind="input"`
 * (the command text) and one `kind="output"` (stdout + stderr).
 */
export interface TerminalCommandItem extends BaseItem {
  type: "terminal_command";
  /** `"input"` for the command text; `"output"` for stdout/stderr. */
  kind: "input" | "output";
  /** The raw command string; present when `kind="input"`. */
  input?: string;
  /** Captured stdout; present when `kind="output"`. */
  stdout?: string;
  /** Captured stderr; present when `kind="output"`. */
  stderr?: string;
}

/**
 * An intelligent-model-router decision item. Display-only (server-side
 * NON_CONTENT_ITEM_TYPES), so the model never sees it; the web UI renders
 * it as a muted chip at its transcript position.
 */
export interface RoutingDecisionItem extends BaseItem {
  type: "routing_decision";
  /** Model id the router chose, e.g. `databricks-claude-opus-4-8`. */
  model: string;
  /** `true` when the brain ran on `model`; `false` = "would have picked". */
  applied: boolean;
  /** The router's one-line rationale. */
  rationale: string;
  /** Sub-agent name when mirrored into the parent session; undefined otherwise. */
  agent?: string;
}

export type ConversationItem =
  | MessageItem
  | FunctionCallItem
  | FunctionCallOutputItem
  | ErrorItem
  | NativeToolItem
  | CompactionItem
  | SlashCommandItem
  | RoutingDecisionItem
  | TerminalCommandItem
  | (BaseItem & Record<string, unknown>);

export function isMessageItem(item: ConversationItem): item is MessageItem {
  return item.type === "message";
}

export function isFunctionCallItem(item: ConversationItem): item is FunctionCallItem {
  return item.type === "function_call";
}

export function isFunctionCallOutputItem(item: ConversationItem): item is FunctionCallOutputItem {
  return item.type === "function_call_output";
}

export function isErrorItem(item: ConversationItem): item is ErrorItem {
  return item.type === "error";
}

export function isNativeToolItem(item: ConversationItem): item is NativeToolItem {
  return NATIVE_TOOL_ITEM_TYPES.has(item.type);
}

export function isCompactionItem(item: ConversationItem): item is CompactionItem {
  return item.type === "compaction";
}

export function isSlashCommandItem(item: ConversationItem): item is SlashCommandItem {
  return item.type === "slash_command";
}

export function isRoutingDecisionItem(item: ConversationItem): item is RoutingDecisionItem {
  return item.type === "routing_decision";
}

export function isTerminalCommandItem(item: ConversationItem): item is TerminalCommandItem {
  return item.type === "terminal_command";
}

// Cursor-paginated history fetching lives in `sessionsApi.ts`
// (`fetchSessionItemsPage`) so it shares the authenticated fetch
// wrapper and the windowed-load contract with the initial bind.
