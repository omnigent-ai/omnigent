// Per-tool presentation for ToolCard. A tool that needs its own title, result
// line, or expanded body registers a `ToolView` in TOOL_VIEWS; every part is
// optional, and whatever a view leaves out uses the generic rendering.

import type { ComponentType } from "react";
import type { ToolCall, ToolResultSummary } from "@/lib/toolResultSummary";
import { type ToolTitle, splitToolName } from "@/lib/toolTitle";

export interface ToolBodyProps {
  call: ToolCall;
  /** Elapsed or completed runtime in seconds, when known. */
  duration: number | undefined;
}

export interface ToolView {
  /** Row title; returning `null` falls back to `formatToolTitle`. */
  title?: (args: Record<string, unknown>) => ToolTitle | null;
  /** Line under the row; returning `null` hides it. Replaces `summarizeToolResult`. */
  summary?: (call: ToolCall) => ToolResultSummary | null;
  /** Expanded content, in place of the generic Input / Result fields. */
  Body?: ComponentType<ToolBodyProps>;
}

/**
 * Views keyed by the tool name a harness reports, e.g. `TodoWrite`,
 * `manage_todo_list`, or `mcp__linear__get_issue`. Register one view under
 * every harness's name for the same kind of tool.
 */
const TOOL_VIEWS = new Map<string, ToolView>();

/** The registered view for a tool; Omnigent's `mcp__omnigent__x` tools resolve to `x`. */
export function toolViewFor(
  name: string,
  views: ReadonlyMap<string, ToolView> = TOOL_VIEWS,
): ToolView | undefined {
  const exact = views.get(name);
  if (exact !== undefined) return exact;
  const { server, tool } = splitToolName(name);
  return server === undefined ? views.get(tool) : undefined;
}
