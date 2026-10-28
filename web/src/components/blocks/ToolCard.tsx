// Tool-call row: icon, title and a one-line result, expanding to labeled input
// and result fields with the raw payload one toggle away. Kept light so the
// assistant's prose reads first; per-tool views plug in through `toolViews.ts`.

import { ChevronRightIcon, CircleSlashIcon, Loader2Icon, XCircleIcon } from "lucide-react";
import type { CSSProperties, ReactNode } from "react";
import { useEffect, useMemo, useState } from "react";
import {
  CodeBlockActions,
  type CodeColorScheme,
  type CodeLanguage,
  CodeBlockContainer,
  CodeBlockContent,
} from "@/components/ai-elements/code-block";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import { useTerminalIsDark } from "@/components/theme/useTerminalIsDark";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
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
  contentBlockText,
  isRecord,
  parseToolOutput,
  toolErrorMessage,
} from "@/lib/toolValue";
import { resolveChatFilePath } from "@/hooks/useWorkspaceChangedFiles";
import { useFileViewer, useWorkspacePaths } from "@/shell/FileViewerContext";
import {
  DIFF_TONE_CLASS,
  CodePanel,
  CopyTextButton,
  OutputSection,
  ToolErrorBox,
  WrapTextButton,
  getOutputPreview,
} from "./ToolCodePanel";
import { ToolValueView } from "./ToolValueView";
import { type ToolBodyProps, type ToolView, toolViewFor } from "./toolViews";

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
        <DefaultToolBody call={call} duration={displayDuration} view={view} />
      </CollapsibleContent>
    </Collapsible>
  );
}

interface ToolCardBodyProps extends ToolBodyProps {
  view: ToolView | undefined;
}

/** Expanded content: a registered view's body, a shell panel, or generic fields. */
function DefaultToolBody({ call, duration, view }: ToolCardBodyProps) {
  const shellCall = useMemo(() => shellToolCall(call.name, call.args), [call.name, call.args]);
  if (shellCall === null || view?.Body !== undefined) {
    return <GenericToolBody call={call} duration={duration} view={view} />;
  }
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
 * The input and result as labeled fields, or a registered view's body, with
 * the exact JSON payloads behind a Raw switch. The switch keeps its place so
 * it keeps keyboard focus. The two field sections share one grid, so their
 * values start at the same offset.
 */
function GenericToolBody({ call, duration, view: toolView }: ToolCardBodyProps) {
  const { args, output, state } = call;
  const ViewBody = toolView?.Body;
  const [view, setView] = useState<ToolBodyView>("fields");
  const inputJson = useMemo(() => JSON.stringify(args, null, 2), [args]);
  const hasInput = Object.keys(args).length > 0;
  // A completed call with no output (provider-native tools) has no result to show.
  const hasResult = output !== null || state !== "output-available";
  const result = <ToolResultView output={output} state={state} duration={duration} />;
  if (ViewBody === undefined && !hasInput && output === null) return result;
  const gridResult = <ToolResultView output={output} state={state} duration={duration} subgrid />;
  return (
    <Tabs
      value={view}
      onValueChange={(value) => setView(value === "raw" ? "raw" : "fields")}
      componentId="diagnostics.tool.body_view"
      className="gap-1"
    >
      <div className="flex min-h-6 items-center gap-2">
        {view === "fields" && ViewBody === undefined && (
          <ToolSectionLabel>{hasInput ? "Input" : "Result"}</ToolSectionLabel>
        )}
        <TabsList
          aria-label="Tool call view"
          className="ml-auto p-0.5 group-data-horizontal/tabs:h-6"
        >
          <TabsTrigger value="fields" className="px-2 text-sm">
            {toolView?.bodyLabel ?? "Fields"}
          </TabsTrigger>
          <TabsTrigger value="raw" className="px-2 text-sm">
            Raw
          </TabsTrigger>
        </TabsList>
      </div>
      <TabsContent
        value="fields"
        className={
          ViewBody === undefined ? "grid grid-cols-[auto_minmax(0,1fr)] gap-y-2" : "space-y-2"
        }
      >
        {ViewBody !== undefined ? (
          <>
            <ViewBody call={call} duration={duration} />
            {/* Views render the output; without one, show the pending, cancelled or failed state. */}
            {output === null && hasResult && result}
          </>
        ) : (
          <>
            {hasInput ? <ToolValueView value={args} subgrid /> : gridResult}
            {hasInput && hasResult && (
              <section className="col-span-2 grid grid-cols-subgrid gap-y-1">
                <ToolSectionLabel className="col-span-2">Result</ToolSectionLabel>
                {gridResult}
              </section>
            )}
          </>
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
  if (error !== null) return { kind: "error", message: error, fields: errorContext(parsed, error) };
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
function errorContext(parsed: ParsedToolOutput, message: string): Record<string, unknown> | null {
  if (parsed.kind !== "json" || !isRecord(parsed.value)) return null;
  const record = parsed.value;
  // Leave out what the error already shows: `{"error": true, "message": ...}`'s
  // message, and a `{"content": [...], "isError": true}` result's text.
  const shown = new Set(record.error === true ? ["error", "message"] : ["error"]);
  if (record.isError === true) shown.add("isError");
  if (contentBlockText(record.content)?.trim() === message) shown.add("content");
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
          <ToolErrorBox className="col-span-2">{presentation.message}</ToolErrorBox>
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
      {summary !== null && !summary.isError && <ToolResultLine summary={summary} />}
    </CollapsibleTrigger>
  );
}

/** The result summary under a trigger row, indented to the title past the icon. */
function ToolResultLine({ summary }: { summary: ToolResultSummary }) {
  const content =
    summary.segments === undefined
      ? summary.text
      : summary.segments.map((segment, index) => (
          <span
            // eslint-disable-next-line react/no-array-index-key
            key={index}
            className={segment.tone && DIFF_TONE_CLASS[segment.tone]}
          >
            {segment.text}
          </span>
        ));
  return (
    <span
      data-testid="tool-result-line"
      className={cn(
        "mt-px block min-w-0 truncate pl-5 text-sm",
        summary.isError && "text-destructive",
      )}
    >
      {content}
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
