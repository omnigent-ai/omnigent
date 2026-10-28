// Tool-call row: icon, title and a one-line result, expanding to labeled input
// and result fields with the raw payload one toggle away. Kept light so the
// assistant's prose reads first; per-tool views plug in through `toolViews.ts`.

import {
  CheckIcon,
  ChevronRightIcon,
  CircleSlashIcon,
  CopyIcon,
  Loader2Icon,
  Maximize2Icon,
  Minimize2Icon,
  WrapTextIcon,
  XCircleIcon,
} from "lucide-react";
import type { CSSProperties, ReactNode } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CodeBlock,
  CodeBlockActions,
  type CodeColorScheme,
  type CodeLanguage,
  CodeBlockContainer,
  CodeBlockContent,
  CodeBlockHeader,
  CodeBlockTitle,
} from "@/components/ai-elements/code-block";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import { useTerminalIsDark } from "@/components/theme/useTerminalIsDark";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { RenderItem, ToolState } from "@/lib/renderItems";
import { dropTrailingPartialEscape, showControlCharacters, stripAnsi } from "@/lib/ansi";
import { type ShellOutputFormat, parseShellToolOutput } from "@/lib/shellToolOutput";
import { terminalSurfaceStyle } from "@/lib/terminalPalette";
import { iconForTool } from "@/lib/toolIcon";
import { mcpServerDisplayName } from "@/lib/mcpServerName";
import {
  type ToolCall,
  type ToolResultSummary,
  summarizeToolResult,
} from "@/lib/toolResultSummary";
import {
  type ToolRunCall,
  type ToolTitle,
  formatToolRunLabel,
  formatToolTitle,
  shellCommandPreview,
  shellToolCall,
  splitEnvPrefix,
} from "@/lib/toolTitle";
import {
  type ParsedToolOutput,
  isRecord,
  parseToolOutput,
  toolErrorMessage,
} from "@/lib/toolValue";
import { resolveChatFilePath } from "@/hooks/useWorkspaceChangedFiles";
import { useFileViewer, useWorkspacePaths } from "@/shell/FileViewerContext";
import { ToolValueView } from "./ToolValueView";
import { type ToolBodyProps, toolViewFor } from "./toolViews";

const OUTPUT_PREVIEW_LINE_LIMIT = 80;
const OUTPUT_PREVIEW_CHAR_LIMIT = 12_000;
// Longer single-line results go in a wrapping code panel instead of prose.
const INLINE_RESULT_MAX_CHARS = 500;

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
  /** Full args dict, rendered as fields in the expanded panel. */
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
  arguments: args,
  output,
  state,
  startedAt,
  duration,
}: ToolCardProps) {
  const view = toolViewFor(name);
  const call = useMemo<ToolCall>(
    () => ({ name, args, output, state }),
    [name, args, output, state],
  );
  const title = useMemo(
    () => view?.title?.(args) ?? formatToolTitle(name, args),
    [view, name, args],
  );
  const summary = useMemo(
    () => (view?.summary === undefined ? summarizeToolResult(call) : view.summary(call)),
    [view, call],
  );
  const elapsedDuration = useElapsedDuration(state === "input-available" ? startedAt : null);
  const displayDuration = duration ?? elapsedDuration;

  // Link a file tool's path to the FileViewer, resolved like chat file links.
  const openFile = useFileViewer();
  const { root, home } = useWorkspacePaths();
  const linkPath =
    title.filePath === undefined
      ? null
      : (resolveChatFilePath(title.filePath, root, home)?.path ?? null);
  const onBodyClick = openFile && linkPath ? () => openFile(linkPath) : undefined;
  const Body = view?.Body ?? DefaultToolBody;

  return (
    <Collapsible defaultOpen={false} className="group not-prose w-full">
      <ToolTriggerRow
        title={title}
        displayPath={linkPath}
        name={name}
        nativeToolType={nativeToolType}
        state={state}
        duration={displayDuration}
        summary={summary}
        onBodyClick={onBodyClick}
      />
      <CollapsibleContent className="mt-1 ml-2 space-y-2 border-l pl-3 py-1 data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=open]:animate-in">
        <Body call={call} duration={displayDuration} />
      </CollapsibleContent>
    </Collapsible>
  );
}

/** Expanded content for tools without a registered view: shell or generic. */
function DefaultToolBody({ call, duration }: ToolBodyProps) {
  const shellCall = useMemo(() => shellToolCall(call.name, call.args), [call.name, call.args]);
  if (shellCall === null) return <GenericToolBody call={call} duration={duration} />;
  const { cwd } = call.args;
  return (
    <ShellTerminalPanel
      command={shellCall.command}
      outputFormat={shellCall.outputFormat}
      argsCwd={typeof cwd === "string" && cwd.length > 0 ? cwd : null}
      args={call.args}
      output={call.output}
      state={call.state}
    />
  );
}

type ToolBodyView = "fields" | "raw";

/**
 * The input and result as labeled fields, with the exact JSON payloads behind
 * a Fields | Raw switch. The switch keeps its place so it keeps keyboard focus.
 * Both sections share one grid, so their values start at the same offset.
 */
function GenericToolBody({ call, duration }: ToolBodyProps) {
  const { args, output, state } = call;
  const [view, setView] = useState<ToolBodyView>("fields");
  const inputJson = useMemo(() => JSON.stringify(args, null, 2), [args]);
  const hasInput = Object.keys(args).length > 0;
  // A completed call with no output (provider-native tools) has no result to show.
  const hasResult = output !== null || state !== "output-available";
  const result = <ToolResultView output={output} state={state} duration={duration} />;
  if (!hasInput && output === null) return result;
  const gridResult = <ToolResultView output={output} state={state} duration={duration} subgrid />;
  return (
    <Tabs
      value={view}
      onValueChange={(value) => setView(value === "raw" ? "raw" : "fields")}
      componentId="diagnostics.tool.body_view"
      className="gap-1"
    >
      <div className="flex min-h-6 items-center gap-2">
        {view === "fields" && <ToolSectionLabel>{hasInput ? "Input" : "Result"}</ToolSectionLabel>}
        <TabsList
          aria-label="Tool call view"
          className="ml-auto p-0.5 group-data-horizontal/tabs:h-6"
        >
          <TabsTrigger value="fields" className="px-2 text-sm">
            Fields
          </TabsTrigger>
          <TabsTrigger value="raw" className="px-2 text-sm">
            Raw
          </TabsTrigger>
        </TabsList>
      </div>
      <TabsContent value="fields" className="grid grid-cols-[auto_minmax(0,1fr)] gap-y-2">
        {hasInput ? <ToolValueView value={args} subgrid /> : gridResult}
        {hasInput && hasResult && (
          <section className="col-span-2 grid grid-cols-subgrid gap-y-1">
            <ToolSectionLabel className="col-span-2">Result</ToolSectionLabel>
            {gridResult}
          </section>
        )}
      </TabsContent>
      <TabsContent value="raw" className="space-y-2">
        <CodePanel title="Input" text={inputJson} copyText={inputJson} copyLabel="Copy input" />
        {output === null ? (
          result
        ) : (
          <OutputSection title="Result" output={prettyPrintIfJson(output)} />
        )}
      </TabsContent>
    </Tabs>
  );
}

function ToolSectionLabel({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cn("font-medium text-muted-foreground text-sm", className)}>{children}</div>
  );
}

type ResultPresentation =
  | { kind: "error"; message: string; fields: Record<string, unknown> | null }
  | { kind: "fields"; value: unknown }
  | { kind: "inline"; text: string }
  | { kind: "panel"; text: string; language: CodeLanguage };

/**
 * How to show a tool's output: a reported failure, labeled fields for JSON
 * that fits without truncation, short text inline, and anything longer in
 * the preview panel that can expand to the full output.
 */
function presentToolResult(output: string): ResultPresentation {
  const parsed = parseToolOutput(output);
  const error = toolErrorMessage(parsed);
  if (error !== null) return { kind: "error", message: error, fields: errorContext(parsed) };
  if (parsed.kind === "json") {
    const pretty = prettyJson(parsed.value);
    if (pretty === null) return { kind: "panel", text: parsed.text, language: "text" };
    if (!getOutputPreview(pretty).isTruncated) return { kind: "fields", value: parsed.value };
    return { kind: "panel", text: pretty, language: "json" };
  }
  const text = stripAnsi(parsed.text);
  const trimmed = text.trim();
  if (!trimmed.includes("\n") && trimmed.length <= INLINE_RESULT_MAX_CHARS) {
    return { kind: "inline", text: trimmed };
  }
  return { kind: "panel", text, language: "text" };
}

/** A failed record's other fields, shown under its error; `null` when it has none. */
function errorContext(parsed: ParsedToolOutput): Record<string, unknown> | null {
  if (parsed.kind !== "json" || !isRecord(parsed.value)) return null;
  const record = parsed.value;
  // `{"error": true, "message": ...}` already shows its message as the error.
  const shown = new Set(record.error === true ? ["error", "message"] : ["error"]);
  const rest = Object.entries(record).filter(([key]) => !shown.has(key));
  return rest.length > 0 ? Object.fromEntries(rest) : null;
}

/** Two-space JSON, or `null` when the value nests too deeply to serialize. */
function prettyJson(value: unknown): string | null {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return null;
  }
}

/** `subgrid` places the result in the fields grid shared with the input. */
function ToolResultView({
  output,
  state,
  duration,
  subgrid = false,
}: {
  output: string | null;
  state: ToolState;
  duration: number | undefined;
  subgrid?: boolean;
}) {
  const presentation = useMemo(
    () => (output === null ? null : presentToolResult(output)),
    [output],
  );
  // Results that are not labeled fields span both grid columns.
  const spanned = (node: ReactNode) =>
    subgrid ? <div className="col-span-2 min-w-0">{node}</div> : node;
  if (presentation === null) {
    if (state === "input-available") return spanned(<ToolPendingOutput duration={duration} />);
    if (state === "output-error" || state === "cancelled" || state === "no-output") {
      return spanned(<EmptyOutputState state={state} />);
    }
    return null;
  }
  switch (presentation.kind) {
    case "error":
      return (
        <div className={subgrid ? "col-span-2 grid grid-cols-subgrid gap-y-2" : "space-y-2"}>
          <div className="col-span-2 max-h-60 overflow-auto whitespace-pre-wrap break-words rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-destructive text-sm">
            {presentation.message}
          </div>
          {presentation.fields !== null && (
            <ToolValueView value={presentation.fields} subgrid={subgrid} />
          )}
        </div>
      );
    case "fields":
      return <ToolValueView value={presentation.value} subgrid={subgrid} />;
    case "inline":
      return spanned(
        presentation.text.length === 0 ? (
          <p className="text-muted-foreground text-sm">No output</p>
        ) : (
          <p className="break-words text-sm">{presentation.text}</p>
        ),
      );
    case "panel":
      return spanned(<OutputSection output={presentation.text} language={presentation.language} />);
  }
}

function RawToggleButton({ pressed, onToggle }: { pressed: boolean; onToggle: () => void }) {
  return (
    <Button
      aria-pressed={pressed}
      className={cn("h-6 px-1.5 text-muted-foreground", pressed && "text-foreground")}
      onClick={onToggle}
      size="xs"
      type="button"
      variant="ghost"
      componentId="diagnostics.tool.toggle_raw_parameters"
    >
      Raw
    </Button>
  );
}

/**
 * Render the folded (older) part of a contiguous tool run as one muted
 * summary line ("Read 2 files"). Clicking expands to show each tool as
 * its own (also-collapsible) trigger. `BlockRenderer` decides which
 * tools fold here: older tools once the visible tail of the most
 * recent ones has been peeled off.
 */
export function ToolGroupSummary({ tools }: { tools: RenderItem[] }) {
  const label = formatToolRunLabel(tools.map(toolRunCall));
  return (
    // Named `group/tool-summary` so this collapsible only rotates its
    // OWN chevron (line 296 in `ToolTriggerRow` uses an unnamed
    // `group-data-[state=open]:rotate-90` that would otherwise match
    // any ancestor `.group[data-state=open]` and incorrectly rotate
    // chevrons of inner tool cards when this outer group is open).
    <Collapsible defaultOpen={false} className="group/tool-summary not-prose w-full">
      <CollapsibleTrigger className="flex cursor-pointer items-center gap-1.5 py-0.5 text-left text-muted-foreground text-ui transition-colors hover:text-foreground">
        <span>{label}</span>
        <ChevronRightIcon className="size-3.5 shrink-0 transition-transform group-data-[state=open]/tool-summary:rotate-90" />
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-1 ml-2 space-y-1 border-l pl-3 pt-1 pb-0 data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=open]:animate-in">
        {tools.map((item) => {
          if (item.kind === "tool") {
            return (
              <ToolCard
                key={`tool:${item.execution.callId}`}
                name={item.execution.name}
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

/** Tool name + args used to categorize a run item for the summary label. */
function toolRunCall(item: RenderItem): ToolRunCall {
  if (item.kind === "tool") {
    return { name: item.execution.name, args: item.execution.arguments };
  }
  if (item.kind === "native_tool") return { name: item.toolType, args: item.data };
  return { name: "" };
}

/**
 * Muted trigger for a tool call. Status/category icon at left, title (verb
 * medium + dynamic body) truncated to one line, MCP server tag, optional
 * duration, and chevron; the result summary sits on a second line.
 */
function ToolTriggerRow({
  title,
  displayPath,
  name,
  nativeToolType,
  state,
  duration,
  summary,
  onBodyClick,
}: {
  title: ToolTitle;
  /** The file path as the FileViewer resolves it (workspace-relative when inside). */
  displayPath: string | null;
  name: string;
  nativeToolType: string | undefined;
  state: ToolState;
  duration: number | undefined;
  summary: ToolResultSummary | null;
  /** When set, the body text (e.g. file path) is rendered as a clickable link. */
  onBodyClick?: () => void;
}) {
  const label =
    title.verb && title.body ? `${title.verb} ${title.body}` : (title.verb ?? title.body);
  const tooltip = title.toolName === undefined ? label : `${label} (${title.toolName})`;
  return (
    <CollapsibleTrigger
      title={tooltip}
      className="block w-full cursor-pointer py-0.5 text-left text-muted-foreground text-ui transition-colors hover:text-foreground"
    >
      <span className="flex w-full items-center gap-1.5">
        <StatusIcon name={name} nativeToolType={nativeToolType} state={state} />
        <span className="min-w-0 flex-1 truncate">
          {title.verb !== null && <span className="font-medium text-foreground">{title.verb}</span>}
          {title.verb !== null && title.body.length > 0 && " "}
          <ToolTitleBody title={title} displayPath={displayPath} onBodyClick={onBodyClick} />
        </span>
        {title.server !== undefined && (
          <Badge
            variant="outline"
            className="h-[1.15rem] shrink-0 rounded px-1 font-normal text-muted-foreground text-sm"
          >
            {mcpServerDisplayName(title.server)}
          </Badge>
        )}
        {duration !== undefined && (
          <span className="shrink-0 tabular-nums opacity-70">{formatToolDuration(duration)}</span>
        )}
        <ChevronRightIcon className="size-3.5 shrink-0 transition-transform group-data-[state=open]:rotate-90" />
      </span>
      {summary !== null && <ToolResultLine summary={summary} />}
    </CollapsibleTrigger>
  );
}

/** The result summary under a trigger row, indented to the title past the icon. */
function ToolResultLine({ summary }: { summary: ToolResultSummary }) {
  return (
    <span
      data-testid="tool-result-line"
      className={cn(
        "mt-px block min-w-0 truncate pl-5 text-sm",
        summary.isError && "text-destructive",
      )}
    >
      {summary.text}
    </span>
  );
}

/** The dynamic part of a trigger row: a shell command, a linked file path, or plain text. */
function ToolTitleBody({
  title,
  displayPath,
  onBodyClick,
}: {
  title: ToolTitle;
  displayPath: string | null;
  onBodyClick?: () => void;
}) {
  if (title.isShellCommand === true) return <ShellCommandLine command={title.body} />;
  if (!onBodyClick) return displayPath ?? title.body;
  return (
    // Use <span role="link"> instead of <button> to avoid nesting
    // interactive elements — CollapsibleTrigger already renders as
    // a <button>, and nested buttons are invalid HTML.
    <span
      role="link"
      tabIndex={0}
      className="underline decoration-dotted underline-offset-2 hover:text-foreground transition-colors cursor-pointer"
      onClick={(e) => {
        e.stopPropagation();
        onBodyClick();
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault(); // prevent Space from triggering parent button's click via keyup
          e.stopPropagation();
          onBodyClick();
        }
      }}
    >
      {displayPath ?? title.body}
    </span>
  );
}

/** Collapsed-row command: a script's first line, and a count of the rest. */
function ShellCommandLine({ command }: { command: string }) {
  const { firstLine, moreLines } = shellCommandPreview(command);
  const { env, rest } = splitEnvPrefix(firstLine);
  return (
    // Only the command truncates, so the line count stays visible.
    <span className="inline-flex max-w-full items-baseline font-mono text-[0.92em]">
      <span className="min-w-0 truncate">
        {env.length > 0 && <span className="opacity-60">{env}</span>}
        <span>{rest}</span>
      </span>
      {moreLines > 0 && (
        <>
          {" "}
          <span className="ml-1.5 shrink-0 font-sans opacity-60">
            +{moreLines} {moreLines === 1 ? "line" : "lines"}
          </span>
        </>
      )}
    </span>
  );
}

/**
 * Expanded view for shell tools: the working directory, `$ command`, and
 * the output in one terminal-like block, with exit status in the footer.
 * The raw argument JSON stays one click away for debugging.
 */
function ShellTerminalPanel({
  command,
  outputFormat,
  argsCwd,
  args,
  output,
  state,
}: {
  command: string;
  outputFormat: ShellOutputFormat;
  /** Working directory from the call's arguments (Codex sends one). */
  argsCwd: string | null;
  args: Record<string, unknown>;
  output: string | null;
  state: ToolState;
}) {
  const [wrap, setWrap] = useState(true);
  const [showRaw, setShowRaw] = useState(false);
  const inputJson = useMemo(() => JSON.stringify(args, null, 2), [args]);
  const terminalIsDark = useTerminalIsDark();
  const appIsDark = useResolvedThemeMode() === "dark";
  // The terminal theme setting can pin the scheme the app is not in; repaint
  // just this block then, and otherwise keep the app's own palette.
  let pinnedScheme: CodeColorScheme | undefined;
  if (terminalIsDark !== appIsDark) pinnedScheme = terminalIsDark ? "dark" : "light";
  const parsed = useMemo(
    () => (output === null ? null : parseShellToolOutput(output, outputFormat)),
    [output, outputFormat],
  );
  const rawOutput = useMemo(
    () => (output === null ? null : showControlCharacters(prettyPrintIfJson(output))),
    [output],
  );
  const hasOutput = parsed !== null && parsed.text.length > 0;
  // sys_os_shell reports its working directory in the result instead.
  const cwd = argsCwd ?? parsed?.cwd ?? null;
  // With no cwd line the actions sit with the command: floated so only its
  // first line wraps around them, or on a sticky row when lines do not wrap or
  // the card is too narrow to share a line. Only classes change, so focus stays.
  const actionsBesideCommand = cwd === null;
  const actions = (
    <CodeBlockActions className="my-0 mr-0 ml-auto shrink-0 gap-1 @max-[480px]/shell:-ml-1">
      <WrapTextButton onToggle={() => setWrap((value) => !value)} wrap={wrap} />
      <CopyTextButton label="Copy command" caption="command" text={command} />
      {hasOutput && (
        <CopyTextButton label="Copy output" caption="output" text={stripAnsi(parsed.text)} />
      )}
      <RawToggleButton pressed={showRaw} onToggle={() => setShowRaw((value) => !value)} />
    </CodeBlockActions>
  );
  return (
    <div className="space-y-2">
      <CodeBlockContainer
        language="bash"
        data-testid="shell-tool-terminal"
        data-terminal-theme={terminalIsDark ? "dark" : "light"}
        className="@container/shell"
        style={pinnedScheme === undefined ? undefined : terminalSurfaceStyle(terminalIsDark)}
      >
        {cwd !== null && (
          // On a narrow card the folder takes its own line and the actions wrap below
          // it, left-aligned with the text.
          <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1 px-3 pt-1.5">
            <span
              className="min-w-0 truncate font-mono text-muted-foreground text-xs @max-[480px]/shell:basis-full"
              title={cwd}
            >
              {cwd}
            </span>
            {actions}
          </div>
        )}
        {/* The `$` prompt is a pseudo-element so selecting or copying the command skips it. */}
        <div className={SHELL_COMMAND_LINES_CLASS} style={SHELL_PROMPT_STYLE}>
          <CodeBlockContent
            code={command}
            language="bash"
            wrap={wrap}
            colorScheme={pinnedScheme}
            className={cn(
              TERMINAL_PRE_CLASS,
              actionsBesideCommand && wrap ? "pt-2 pb-1 @max-[480px]/shell:pt-1" : "pt-1 pb-1",
            )}
            leading={
              actionsBesideCommand ? (
                <div
                  className={
                    wrap
                      ? "float-right mt-1.5 mr-3 -mb-1 ml-2 @max-[480px]/shell:float-none @max-[480px]/shell:m-0 @max-[480px]/shell:flex @max-[480px]/shell:px-3 @max-[480px]/shell:pt-1.5"
                      : "sticky left-0 flex justify-end px-3 pt-1.5 @max-[480px]/shell:justify-start"
                  }
                >
                  {actions}
                </div>
              ) : undefined
            }
          />
        </div>
        {hasOutput && (
          <OutputSection
            output={parsed.text}
            renderPanel={(text) => (
              <CodeBlockContent
                code={dropTrailingPartialEscape(text)}
                language="ansi"
                wrap={wrap}
                colorScheme={pinnedScheme}
                className={cn(TERMINAL_PRE_CLASS, "pt-0 pb-2")}
              />
            )}
          />
        )}
        {parsed !== null && !hasOutput && (
          <div className="px-3 pb-2 font-mono text-muted-foreground text-xs">(no output)</div>
        )}
        <ShellStatus
          state={state}
          exitCode={parsed?.exitCode ?? null}
          timedOut={parsed?.timedOut ?? false}
          outputRecorded={output !== null}
        />
      </CodeBlockContainer>
      {showRaw && (
        <>
          <CodePanel title="Input" text={inputJson} copyText={inputJson} copyLabel="Copy input" />
          {rawOutput !== null && (
            <OutputSection
              title="Result"
              output={rawOutput}
              language={outputFormat === "sys-os-json" ? "json" : "text"}
            />
          )}
        </>
      )}
    </div>
  );
}

// A non-breaking space keeps the prompt on the same line as the first word.
const SHELL_PROMPT_STYLE = { "--shell-prompt": '"$\u00a0"' } as CSSProperties;

// One prompt for the whole script, like a pasted block in a terminal. Wrapped
// text indents past it so it doesn't read as a new line.
const SHELL_COMMAND_LINES_CLASS =
  "[&_code>span]:pl-[2ch] [&_code>span]:-indent-[2ch] [&_code>span:first-child]:before:font-semibold [&_code>span:first-child]:before:text-muted-foreground [&_code>span:first-child]:before:content-[var(--shell-prompt)]";

// Command and output share the panel's own background so the block reads as
// one continuous terminal surface instead of stacked code panels.
const TERMINAL_PRE_CLASS = "px-3 !bg-transparent dark:!bg-transparent";

/** Status line under the output; omitted when there is nothing to report. */
function ShellStatus({
  state,
  exitCode,
  timedOut,
  outputRecorded,
}: {
  state: ToolState;
  exitCode: number | null;
  timedOut: boolean;
  /** Whether the call recorded any output string, even an empty one. */
  outputRecorded: boolean;
}) {
  let status: ReactNode;
  if (state === "input-available") {
    status = (
      <span className="flex items-center gap-1.5 text-muted-foreground">
        <Loader2Icon className="size-3 animate-spin text-info" />
        running
      </span>
    );
  } else if (timedOut) {
    status = <span className="text-destructive">timed out</span>;
  } else if (exitCode !== null) {
    status = (
      <span className={exitCode === 0 ? "text-success" : "text-destructive"}>exit {exitCode}</span>
    );
  } else if (state === "output-error") {
    status = <span className="text-destructive">failed</span>;
  } else if (state === "cancelled") {
    status = <span className="text-muted-foreground">cancelled</span>;
  } else if (state === "no-output" && !outputRecorded) {
    status = <span className="text-muted-foreground">no output recorded</span>;
  } else {
    return null;
  }
  return <div className="px-3 pb-2 font-mono text-xs tabular-nums">{status}</div>;
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
    return <Loader2Icon className="size-3.5 shrink-0 animate-spin text-info" />;
  }
  if (state === "output-error") {
    return <XCircleIcon className="size-3.5 shrink-0 text-destructive" />;
  }
  if (state === "cancelled" || state === "no-output") {
    // Turn over, no output recorded — muted slash, not the error icon.
    return <CircleSlashIcon className="size-3.5 shrink-0" />;
  }
  const Icon = iconForTool(name, nativeToolType);
  return <Icon className="size-3.5 shrink-0" />;
}

function CodePanel({
  title,
  text,
  copyText,
  copyLabel,
  language = "json",
}: {
  title?: string;
  text: string;
  copyText: string;
  copyLabel: string;
  language?: CodeLanguage;
}) {
  // Soft-wrap long lines by default so the panel never needs horizontal
  // scrolling to read; the toggle restores the scrolling view for when
  // column alignment matters (same affordance as chat code blocks).
  const [wrap, setWrap] = useState(true);
  return (
    <CodeBlock code={text} language={language} wrap={wrap}>
      <CodeBlockHeader>
        <CodeBlockTitle className="min-w-0">
          {title !== undefined && <span className="truncate font-medium">{title}</span>}
        </CodeBlockTitle>
        <CodeBlockActions>
          <WrapTextButton onToggle={() => setWrap((value) => !value)} wrap={wrap} />
          <CopyTextButton label={copyLabel} text={copyText} />
        </CodeBlockActions>
      </CodeBlockHeader>
    </CodeBlock>
  );
}

function WrapTextButton({ wrap, onToggle }: { wrap: boolean; onToggle: () => void }) {
  const label = wrap ? "Disable word wrap" : "Enable word wrap";
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          aria-label={label}
          aria-pressed={wrap}
          className={cn("size-6 text-muted-foreground", wrap && "text-foreground")}
          onClick={onToggle}
          size="icon-xs"
          type="button"
          variant="ghost"
          componentId="diagnostics.tool.toggle_wrap"
        >
          <WrapTextIcon className="size-3.5" />
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}

function OutputSection({
  output,
  title,
  renderPanel,
  language,
}: {
  output: string;
  /** The default panel's title; the Fields view leaves it out, since its section label names it. */
  title?: string;
  /** Renders the (possibly truncated) text; defaults to a code panel. */
  renderPanel?: (text: string) => ReactNode;
  /** Highlighting for the default panel; JSON unless set. */
  language?: CodeLanguage;
}) {
  const [isExpanded, setIsExpanded] = useState(false);
  useEffect(() => setIsExpanded(false), [output]);

  const collapsedPreview = useMemo(() => getOutputPreview(output), [output]);
  const preview = useMemo(() => getOutputPreview(output, isExpanded), [output, isExpanded]);
  const canExpand = collapsedPreview.isTruncated;

  return (
    <div className="space-y-2">
      <div
        className={cn(
          "relative rounded-md",
          canExpand && !isExpanded && "max-h-80 overflow-hidden",
          // overflow-auto (vs overflow-y-auto) keeps long single-line output from blowing out the bubble width.
          (!canExpand || isExpanded) && "max-h-[36rem] overflow-auto",
        )}
      >
        {renderPanel ? (
          renderPanel(preview.text)
        ) : (
          <CodePanel
            title={title}
            text={preview.text}
            copyText={output}
            copyLabel="Copy result"
            language={language}
          />
        )}
        {canExpand && !isExpanded && (
          <div className="pointer-events-none absolute inset-x-px bottom-px h-16 rounded-b-md bg-gradient-to-t from-background to-transparent" />
        )}
      </div>
      {canExpand && (
        <div className="flex flex-col gap-2 rounded-md border bg-muted/30 px-3 py-2 text-muted-foreground text-sm sm:flex-row sm:items-center sm:justify-between">
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
            componentId="diagnostics.tool.toggle_expanded"
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
    <div className="rounded-md border border-dashed bg-muted/30 p-3">
      <div className="flex items-center gap-2 text-muted-foreground text-ui">
        <Loader2Icon className="size-4 animate-spin text-info" />
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
    <div className="rounded-md border border-dashed bg-muted/30 px-3 py-2 text-muted-foreground text-ui">
      {message}
    </div>
  );
}

interface CopyTextButtonProps {
  text: string;
  label: string;
  /** Visible text next to the icon, for when several copy buttons sit together. */
  caption?: string;
}

function CopyTextButton({ text, label, caption }: CopyTextButtonProps) {
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
          className={cn("text-muted-foreground", caption ? "h-6 gap-1 px-1.5" : "size-6")}
          onClick={copyToClipboard}
          size={caption ? "xs" : "icon-xs"}
          type="button"
          variant="ghost"
          componentId="diagnostics.tool.copy"
        >
          <Icon className="size-3.5" />
          {caption}
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
