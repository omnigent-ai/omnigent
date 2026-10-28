// Per-tool presentation for ToolCard. A tool that needs its own title, line
// counts, or expanded body registers a `ToolView` in TOOL_VIEWS; every part is
// optional, and whatever a view leaves out uses the generic rendering.

import type { ComponentType } from "react";
import { FILE_EDIT_TOOL_NAMES } from "@/lib/toolEditDiff";
import { type LineCounts, type ToolCall, fileEditLineCounts } from "@/lib/toolResultSummary";
import { type ToolTitle, splitToolName } from "@/lib/toolTitle";
import { FileEditBody } from "./FileEditBody";

export interface ToolBodyProps {
  call: ToolCall;
  /** Elapsed or completed runtime in seconds, when known. */
  duration: number | undefined;
}

export interface ToolView {
  /** Row title; returning `null` falls back to `formatToolTitle`. */
  title?: (args: Record<string, unknown>) => ToolTitle | null;
  /** Added and removed line counts after the row's title, e.g. "+4 -2"; `null` shows none. */
  lineCounts?: (call: ToolCall) => LineCounts | null;
  /**
   * Expanded content in place of the generic Input / Result fields; Raw stays available.
   * The card adds a state only while `output` is null, so the view must render any output, errors included.
   */
  Body?: ComponentType<ToolBodyProps>;
  /** Name of the `Body` view on the switch to Raw; defaults to "Fields". */
  bodyLabel?: string;
}

/** File edits: added/removed line counts on the row, and the diff when expanded. */
const FILE_EDIT_VIEW: ToolView = {
  lineCounts: fileEditLineCounts,
  Body: FileEditBody,
  bodyLabel: "Diff",
};

/**
 * Views keyed by the tool name a harness reports, e.g. `TodoWrite`,
 * `manage_todo_list`, or `mcp__linear__get_issue`. Register one view under
 * every harness's name for the same kind of tool.
 */
const TOOL_VIEWS = new Map<string, ToolView>(
  FILE_EDIT_TOOL_NAMES.map((name) => [name, FILE_EDIT_VIEW]),
);

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
