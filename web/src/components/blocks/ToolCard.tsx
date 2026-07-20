// Tool-call renderer. Renders each call as a single muted-text trigger
// line (status icon + truncated `name(argsSummary)`); clicking expands an
// indented panel with the parameters JSON and output preview. The big
// border-stripe / badge / pill card shell was removed deliberately — tool
// calls used to dominate the transcript, and the goal is for the
// assistant's prose to read first.

import {
  CheckIcon,
  ChevronRightIcon,
  CircleSlashIcon,
  CopyIcon,
  ExternalLinkIcon,
  Loader2Icon,
  Maximize2Icon,
  Minimize2Icon,
  XCircleIcon,
} from "lucide-react";
import type { ReactNode } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CodeBlock,
  CodeBlockActions,
  CodeBlockHeader,
  CodeBlockTitle,
} from "@/components/ai-elements/code-block";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { RenderItem, ToolState } from "@/lib/renderItems";
import { iconForTool } from "@/lib/toolIcon";
import { type ToolTitle, type ToolTitleBodyKind, formatToolTitle } from "@/lib/toolTitle";
import { useFileViewer } from "@/shell/FileViewerContext";
import {
  TRANSCRIPT_CARD_BODY_CLASS,
  TRANSCRIPT_CARD_META_CLASS,
  TRANSCRIPT_RAIL_CLASS,
} from "./toolSurface";

const OUTPUT_PREVIEW_LINE_LIMIT = 80;
const OUTPUT_PREVIEW_CHAR_LIMIT = 12_000;

/**
 * Tools whose `args.path` field is a workspace file path that the user
 * should be able to click to open in the FileViewer.
 */
const FILE_PATH_TOOLS = new Set(["sys_os_read", "sys_os_write", "sys_os_edit"]);

/**
 * If the string is valid JSON, return its 2-space-indented form.
 * Otherwise return the string verbatim. The code block renders inside a
 * `<pre>`, so a compact one-line JSON payload otherwise becomes a single
 * horizontal-scrolling line.
 */
function prettyPrintIfJson(s: string): string {
  try {
    const parsed: unknown = JSON.parse(s);
    return JSON.stringify(parsed, null, 2);
  } catch {
    return s;
  }
}

export function formatToolDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) {
    return "0ms";
  }

  if (seconds < 1) {
    return `${Math.max(1, Math.round(seconds * 1000))}ms`;
  }

  if (seconds < 10) {
    return `${seconds.toFixed(1)}s`;
  }

  if (seconds < 60) {
    return `${Math.round(seconds)}s`;
  }

  const totalSeconds = Math.round(seconds);
  const minutes = Math.floor(totalSeconds / 60);
  const remainingSeconds = totalSeconds % 60;
  if (totalSeconds < 60 * 60) {
    return `${minutes}m ${remainingSeconds}s`;
  }

  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return `${hours}h ${remainingMinutes}m`;
}

interface OutputPreview {
  text: string;
  isTruncated: boolean;
  lineCount: number;
  charCount: number;
  shownLineCount: number;
  shownCharCount: number;
  hiddenLineCount: number;
  hiddenCharCount: number;
}

export function getOutputPreview(output: string, expanded = false): OutputPreview {
  const lines = output.length === 0 ? [] : output.split("\n");
  const lineCount = lines.length;
  const charCount = output.length;

  if (
    expanded ||
    (lineCount <= OUTPUT_PREVIEW_LINE_LIMIT && charCount <= OUTPUT_PREVIEW_CHAR_LIMIT)
  ) {
    return {
      text: output,
      isTruncated: false,
      lineCount,
      charCount,
      shownLineCount: lineCount,
      shownCharCount: charCount,
      hiddenLineCount: 0,
      hiddenCharCount: 0,
    };
  }

  let text =
    lineCount > OUTPUT_PREVIEW_LINE_LIMIT
      ? lines.slice(0, OUTPUT_PREVIEW_LINE_LIMIT).join("\n")
      : output;

  if (text.length > OUTPUT_PREVIEW_CHAR_LIMIT) {
    text = text.slice(0, OUTPUT_PREVIEW_CHAR_LIMIT).trimEnd();
  }

  const shownLineCount = text.length === 0 ? 0 : text.split("\n").length;
  const shownCharCount = text.length;

  return {
    text,
    isTruncated: shownCharCount < charCount,
    lineCount,
    charCount,
    shownLineCount,
    shownCharCount,
    hiddenLineCount: Math.max(0, lineCount - shownLineCount),
    hiddenCharCount: Math.max(0, charCount - shownCharCount),
  };
}

interface ToolCardProps {
  /** Display name for the tool. For native tools, this is the friendly label. */
  name: string;
  /**
   * Set for native (provider-managed) tools — the underlying type
   * (e.g. "web_search_call"). Used to pick the category icon.
   */
  nativeToolType?: string;
  /** Brief one-line summary of arguments shown next to the name. */
  argsSummary?: string;
  /** Full args dict, rendered as JSON in the expanded panel. */
  arguments: Record<string, unknown>;
  /** Tool output, or null if not yet available / never produced. */
  output: string | null;
  state: ToolState;
  /** Seconds from the page's performance clock when the tool call rendered. */
  startedAt?: number | null;
  /** Completed runtime in seconds. Undefined when historical data lacks timing. */
  duration?: number;
}

export function ToolCard({
  name,
  nativeToolType,
  argsSummary,
  arguments: args,
  output,
  state,
  startedAt,
  duration,
}: ToolCardProps) {
  const title = useMemo(() => formatToolTitle(name, args, argsSummary), [name, args, argsSummary]);
  const inputJson = useMemo(() => JSON.stringify(args, null, 2), [args]);
  const formattedOutput = useMemo(
    () => (output === null ? null : prettyPrintIfJson(output)),
    [output],
  );
  const elapsedDuration = useElapsedDuration(state === "input-available" ? startedAt : null);
  const displayDuration = duration ?? elapsedDuration;

  // When this is a file-path tool and we're inside AppShell, make the path
  // in the trigger row a clickable link that opens the FileViewer.
  const openFile = useFileViewer();
  const rawPath =
    FILE_PATH_TOOLS.has(name) &&
    typeof args.path === "string" &&
    args.path.length > 0 &&
    !args.path.startsWith("/") // FileViewer rejects absolute paths
      ? args.path
      : null;
  const onBodyClick = openFile && rawPath ? () => openFile(rawPath) : undefined;

  return (
    <Collapsible defaultOpen={false} className="tool-call-row group not-prose w-full">
      <ToolTriggerRow
        title={title}
        name={name}
        nativeToolType={nativeToolType}
        state={state}
        duration={displayDuration}
        onBodyClick={onBodyClick}
      />
      <CollapsibleContent
        className={cn(
          "mt-1 ml-2.5 space-y-2 py-1 data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=open]:animate-in",
          TRANSCRIPT_RAIL_CLASS,
        )}
      >
        <CodePanel
          title="Parameters"
          text={inputJson}
          copyText={inputJson}
          copyLabel="Copy parameters"
        />
        {formattedOutput !== null && <OutputSection output={formattedOutput} />}
        {formattedOutput === null && state === "input-available" && (
          <ToolPendingOutput duration={displayDuration} />
        )}
        {formattedOutput === null &&
          (state === "output-error" || state === "cancelled" || state === "no-output") && (
            <EmptyOutputState state={state} />
          )}
      </CollapsibleContent>
    </Collapsible>
  );
}

/**
 * Render a contiguous run of tool calls as one muted "See N steps" line.
 * Clicking expands to show each tool as its own (also-collapsible)
 * trigger. `BlockRenderer` decides which tools fold here (older tools
 * once a streaming-tail of the most recent ones has been peeled off,
 * or all completed tools once streaming finishes).
 */
export function ToolGroupSummary({
  tools,
  count,
  className,
}: {
  tools: RenderItem[];
  count?: number;
  className?: string;
}) {
  // Label the FULL contiguous run, not just the folded tools — during
  // streaming the most-recent tools render as a visible tail outside this
  // group, so counting only `tools` would undercount ("See 2 steps" when
  // there are more visible). `count` defaults to the folded length for
  // fully-collapsed runs (reload / idle), where they're equal.
  const n = count ?? tools.length;
  const label = `See ${n} step${n === 1 ? "" : "s"}`;
  return (
    // Named `group/tool-summary` so this collapsible only rotates its
    // OWN chevron (line 296 in `ToolTriggerRow` uses an unnamed
    // `group-data-[state=open]:rotate-90` that would otherwise match
    // any ancestor `.group[data-state=open]` and incorrectly rotate
    // chevrons of inner tool cards when this outer group is open).
    // `peer` lets `BlockRenderer`'s trailing tail react to this
    // collapsible's open/closed state for the border-join effect.
    <Collapsible
      defaultOpen={false}
      className={cn("group/tool-summary peer not-prose mb-1.5 w-full", className)}
    >
      <CollapsibleTrigger className="tool-group-summary-trigger inline-flex max-w-full cursor-pointer items-center gap-1.5 py-[3px] text-left text-sm font-normal leading-[1.4] text-muted-foreground transition-colors hover:text-foreground">
        <span>{label}</span>
        <ChevronRightIcon className="size-3 shrink-0 opacity-75 transition-transform group-data-[state=open]/tool-summary:rotate-90" />
      </CollapsibleTrigger>
      <CollapsibleContent
        className={cn(
          "tool-group-timeline mt-1.5 space-y-1 pt-0.5 pb-0 data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=open]:animate-in",
          TRANSCRIPT_RAIL_CLASS,
        )}
      >
        {tools.map((item) => {
          if (item.kind === "tool") {
            return (
              <ToolCard
                key={`tool:${item.execution.callId}`}
                name={item.execution.name}
                argsSummary={item.execution.argsSummary}
                arguments={item.execution.arguments}
                output={item.output}
                state={item.state}
                startedAt={item.startedAt}
                duration={item.duration}
              />
            );
          }
          if (item.kind === "native_tool") {
            return (
              <ToolCard
                key={`native:${item.itemId ?? item.label}`}
                name={item.label}
                nativeToolType={item.toolType}
                arguments={item.data}
                output={null}
                state="output-available"
              />
            );
          }
          return null;
        })}
      </CollapsibleContent>
    </Collapsible>
  );
}

/**
 * Single muted-text trigger line for a tool call. Status/category icon
 * at left, title (neutral action + subtly emphasized dynamic body) in the middle truncated to
 * one line, optional duration on the right, chevron at the far right.
 */
function ToolTriggerRow({
  title,
  name,
  nativeToolType,
  state,
  duration,
  onBodyClick,
}: {
  title: ToolTitle;
  name: string;
  nativeToolType: string | undefined;
  state: ToolState;
  duration: number | undefined;
  /** When set, a sibling action opens the referenced workspace file. */
  onBodyClick?: () => void;
}) {
  const tooltip =
    title.verb && title.body ? `${title.verb} ${title.body}` : (title.verb ?? title.body);
  return (
    <div className="tool-call-trigger-row inline-flex max-w-full items-center gap-1">
      <CollapsibleTrigger
        title={tooltip}
        className="tool-call-trigger inline-flex min-h-11 min-w-0 max-w-full cursor-pointer items-center gap-2.5 py-[3px] text-left text-13 font-normal text-muted-foreground transition-opacity hover:opacity-80 md:min-h-0"
      >
        <StatusIcon name={name} nativeToolType={nativeToolType} state={state} />
        <span className="min-w-0 truncate">
          {title.verb !== null && <span className="font-normal text-foreground">{title.verb}</span>}
          {title.verb !== null && title.body.length > 0 && " "}
          <span className={toolBodyClassName(title.bodyKind)}>{title.body}</span>
        </span>
        {duration !== undefined && (
          <span
            className={cn("shrink-0 font-mono tabular-nums opacity-70", TRANSCRIPT_CARD_META_CLASS)}
          >
            {formatToolDuration(duration)}
          </span>
        )}
        <ChevronRightIcon className="size-3 shrink-0 opacity-75 transition-transform group-data-[state=open]:rotate-90" />
      </CollapsibleTrigger>
      {onBodyClick && (
        <button
          type="button"
          aria-label={`Open ${title.body}`}
          title={`Open ${title.body}`}
          className="grid size-11 shrink-0 cursor-pointer place-items-center md:size-6 rounded-[var(--radius-otto-xs)] text-file-reference opacity-70 transition-[background-color,opacity] hover:bg-muted hover:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
          onClick={onBodyClick}
        >
          <ExternalLinkIcon className="size-3" />
        </button>
      )}
    </div>
  );
}

function toolBodyClassName(kind: ToolTitleBodyKind): string | undefined {
  switch (kind) {
    case "command":
      return "inline-block max-w-full truncate rounded-[var(--radius-otto-xs)] bg-foreground/[0.065] px-1 py-px align-bottom font-mono text-[0.88em] leading-[1.35] text-foreground/85 dark:bg-foreground/[0.1]";
    case "path":
      return "font-mono text-[0.92em] font-normal text-file-reference";
    case "identifier":
      return "font-mono text-[0.92em] text-foreground/75";
    case "metric":
      return "font-mono text-[0.92em] tabular-nums text-foreground/75";
    case "query":
      return "text-foreground/75";
    case "url":
      return "font-mono text-[0.9em] text-foreground/70";
    case "plain":
      return undefined;
  }
}

/**
 * Icon shown at the start of a tool-call row. The transient states
 * (running / errored / cancelled) take priority so the user sees an
 * unambiguous progress signal; once the tool has completed cleanly we
 * fall back to a category icon picked from the tool name.
 */
function StatusIcon({
  name,
  nativeToolType,
  state,
}: {
  name: string;
  nativeToolType: string | undefined;
  state: ToolState;
}): ReactNode {
  if (state === "input-available") {
    // Slightly larger and tinted so the running indicator is the one
    // thing in the row that actively draws the eye.
    return (
      <span className="tool-step-icon-node">
        <Loader2Icon className="animate-spin text-info-foreground" />
      </span>
    );
  }
  if (state === "output-error") {
    return (
      <span className="tool-step-icon-node">
        <XCircleIcon className="text-destructive" />
      </span>
    );
  }
  if (state === "cancelled" || state === "no-output") {
    // Turn over, no output recorded — muted slash, not the error icon.
    return (
      <span className="tool-step-icon-node">
        <CircleSlashIcon />
      </span>
    );
  }
  const Icon = iconForTool(name, nativeToolType);
  return (
    <span className="tool-step-icon-node">
      <Icon />
    </span>
  );
}

function CodePanel({
  title,
  text,
  copyText,
  copyLabel,
}: {
  title: string;
  text: string;
  copyText: string;
  copyLabel: string;
}) {
  return (
    <CodeBlock code={text} language="json">
      <CodeBlockHeader>
        <CodeBlockTitle className="min-w-0">
          <span className="truncate font-medium uppercase tracking-wide">{title}</span>
        </CodeBlockTitle>
        <CodeBlockActions>
          <CopyTextButton label={copyLabel} text={copyText} />
        </CodeBlockActions>
      </CodeBlockHeader>
    </CodeBlock>
  );
}

function OutputSection({ output }: { output: string }) {
  const [isExpanded, setIsExpanded] = useState(false);
  useEffect(() => setIsExpanded(false), [output]);

  const collapsedPreview = useMemo(() => getOutputPreview(output), [output]);
  const preview = useMemo(() => getOutputPreview(output, isExpanded), [output, isExpanded]);
  const canExpand = collapsedPreview.isTruncated;

  return (
    <div className="space-y-2">
      <div
        className={cn(
          "relative rounded-[var(--radius-otto-sm)]",
          canExpand && !isExpanded && "max-h-80 overflow-hidden",
          // overflow-auto (vs overflow-y-auto) keeps long single-line output from blowing out the bubble width.
          (!canExpand || isExpanded) && "max-h-[36rem] overflow-auto",
        )}
      >
        <CodePanel title="Output" text={preview.text} copyText={output} copyLabel="Copy output" />
        {canExpand && !isExpanded && (
          <div className="pointer-events-none absolute inset-x-px bottom-px h-16 rounded-b-[var(--radius-otto-sm)] bg-gradient-to-t from-background to-transparent" />
        )}
      </div>
      {canExpand && (
        <div className="flex flex-col gap-2 rounded-[var(--radius-otto-sm)] border border-[var(--border-otto-hairline)] bg-muted/30 px-3 py-2 text-muted-foreground text-xs sm:flex-row sm:items-center sm:justify-between">
          <span className="min-w-0">
            {isExpanded ? "Showing full output" : "Previewing output"} (
            {formatOutputStats(isExpanded ? preview : collapsedPreview)})
          </span>
          <Button
            className="w-fit"
            onClick={() => setIsExpanded((value) => !value)}
            size="xs"
            type="button"
            variant="outline"
          >
            {isExpanded ? (
              <Minimize2Icon className="size-3" />
            ) : (
              <Maximize2Icon className="size-3" />
            )}
            {isExpanded ? "Collapse" : "Expand"}
          </Button>
        </div>
      )}
    </div>
  );
}

function ToolPendingOutput({ duration }: { duration: number | undefined }) {
  return (
    <div className="rounded-[var(--radius-otto-sm)] border border-dashed bg-muted/30 p-3">
      <div
        className={cn("flex items-center gap-2 text-muted-foreground", TRANSCRIPT_CARD_BODY_CLASS)}
      >
        <Loader2Icon className="size-4 animate-spin text-info-foreground" />
        <span>
          Waiting for output
          {duration !== undefined ? ` for ${formatToolDuration(duration)}` : ""}
        </span>
      </div>
      <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-muted">
        <div className="h-full w-1/3 animate-pulse rounded-full bg-info/70" />
      </div>
    </div>
  );
}

function EmptyOutputState({ state }: { state: "output-error" | "cancelled" | "no-output" }) {
  let message: string;
  if (state === "cancelled") {
    message = "Tool was cancelled before output arrived.";
  } else if (state === "no-output") {
    message = "No output was recorded for this tool call.";
  } else {
    message = "Tool did not return output before the response failed.";
  }
  return (
    <div
      className={cn(
        "rounded-[var(--radius-otto-sm)] border border-dashed bg-muted/30 px-3 py-2 text-muted-foreground",
        TRANSCRIPT_CARD_BODY_CLASS,
      )}
    >
      {message}
    </div>
  );
}

interface CopyTextButtonProps {
  text: string;
  label: string;
}

function CopyTextButton({ text, label }: CopyTextButtonProps) {
  const [isCopied, setIsCopied] = useState(false);
  const timeoutRef = useRef<number | null>(null);

  const copyToClipboard = useCallback(async () => {
    if (typeof navigator === "undefined" || !navigator.clipboard?.writeText) {
      return;
    }

    try {
      await navigator.clipboard.writeText(text);
    } catch {
      return;
    }

    setIsCopied(true);
    if (timeoutRef.current !== null) {
      window.clearTimeout(timeoutRef.current);
    }
    timeoutRef.current = window.setTimeout(() => setIsCopied(false), 2000);
  }, [text]);

  useEffect(
    () => () => {
      if (timeoutRef.current !== null) {
        window.clearTimeout(timeoutRef.current);
      }
    },
    [],
  );

  const Icon = isCopied ? CheckIcon : CopyIcon;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          aria-label={isCopied ? "Copied" : label}
          className="size-6 text-muted-foreground"
          onClick={copyToClipboard}
          size="icon-xs"
          type="button"
          variant="ghost"
        >
          <Icon className="size-3.5" />
        </Button>
      </TooltipTrigger>
      <TooltipContent>{isCopied ? "Copied" : label}</TooltipContent>
    </Tooltip>
  );
}

function useElapsedDuration(startedAt: number | null | undefined): number | undefined {
  const [now, setNow] = useState(() => getNowSeconds());

  useEffect(() => {
    if (startedAt === null || startedAt === undefined) {
      return;
    }

    setNow(getNowSeconds());
    const interval = window.setInterval(() => setNow(getNowSeconds()), 500);
    return () => window.clearInterval(interval);
  }, [startedAt]);

  if (startedAt === null || startedAt === undefined) {
    return undefined;
  }

  return Math.max(0, now - startedAt);
}

function getNowSeconds(): number {
  if (typeof performance !== "undefined") {
    return performance.now() / 1000;
  }
  return Date.now() / 1000;
}

function formatOutputStats(preview: OutputPreview): string {
  if (!preview.isTruncated) {
    return `${formatCount(preview.lineCount, "line")} / ${formatCount(preview.charCount, "char")}`;
  }

  const hidden: string[] = [];
  if (preview.hiddenLineCount > 0) {
    hidden.push(`${formatCount(preview.hiddenLineCount, "line")} hidden`);
  }
  if (preview.hiddenCharCount > 0) {
    hidden.push(`${formatCount(preview.hiddenCharCount, "char")} hidden`);
  }

  return `${formatCount(preview.shownLineCount, "line")} / ${formatCount(
    preview.shownCharCount,
    "char",
  )} shown; ${hidden.join(", ")}`;
}

function formatCount(count: number, unit: string): string {
  return `${count.toLocaleString()} ${unit}${count === 1 ? "" : "s"}`;
}
