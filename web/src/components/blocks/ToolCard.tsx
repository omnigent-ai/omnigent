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
  type CodeLanguage,
  CodeBlockContainer,
  CodeBlockContent,
  CodeBlockHeader,
  CodeBlockTitle,
} from "@/components/ai-elements/code-block";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { RenderItem, ToolState } from "@/lib/renderItems";
import { dropTrailingPartialEscape, showControlCharacters, stripAnsi } from "@/lib/ansi";
import { type ShellOutputFormat, parseShellToolOutput } from "@/lib/shellToolOutput";
import { iconForTool } from "@/lib/toolIcon";
import {
  type ToolRunCall,
  type ToolTitle,
  formatToolRunLabel,
  formatToolTitle,
  shellToolCall,
  splitEnvPrefix,
} from "@/lib/toolTitle";
import { resolveChatFilePath } from "@/hooks/useWorkspaceChangedFiles";
import { useFileViewer, useWorkspacePaths } from "@/shell/FileViewerContext";

const OUTPUT_PREVIEW_LINE_LIMIT = 80;
const OUTPUT_PREVIEW_CHAR_LIMIT = 12_000;

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
  const shellCall = useMemo(() => shellToolCall(name, args), [name, args]);
  const inputJson = useMemo(() => JSON.stringify(args, null, 2), [args]);
  const formattedOutput = useMemo(
    () => (output === null ? null : prettyPrintIfJson(output)),
    [output],
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
        name={name}
        nativeToolType={nativeToolType}
        state={state}
        duration={displayDuration}
        onBodyClick={onBodyClick}
      />
      <CollapsibleContent className="mt-1 ml-2 space-y-2 border-l pl-3 py-1 data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=open]:animate-in">
        {shellCall !== null ? (
          <ShellTerminalPanel
            command={shellCall.command}
            outputFormat={shellCall.outputFormat}
            argsCwd={typeof args.cwd === "string" && args.cwd.length > 0 ? args.cwd : null}
            inputJson={inputJson}
            output={output}
            state={state}
          />
        ) : (
          <>
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
          </>
        )}
        {shellCall === null &&
          output === null &&
          (state === "output-error" || state === "cancelled" || state === "no-output") && (
            <EmptyOutputState state={state} />
          )}
      </CollapsibleContent>
    </Collapsible>
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
      <CollapsibleTrigger className="flex cursor-pointer items-center gap-1.5 py-0.5 text-left text-muted-foreground text-chat transition-colors hover:text-foreground">
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

/** Tool name + args used to categorize a run item for the summary label. */
function toolRunCall(item: RenderItem): ToolRunCall {
  if (item.kind === "tool") {
    return { name: item.execution.name, args: item.execution.arguments };
  }
  if (item.kind === "native_tool") return { name: item.toolType, args: item.data };
  return { name: "" };
}

/**
 * Single muted-text trigger line for a tool call. Status/category icon
 * at left, title (verb bold + dynamic body) in the middle truncated to
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
  /** When set, the body text (e.g. file path) is rendered as a clickable link. */
  onBodyClick?: () => void;
}) {
  const tooltip =
    title.verb && title.body ? `${title.verb} ${title.body}` : (title.verb ?? title.body);
  return (
    <CollapsibleTrigger
      title={tooltip}
      className="flex w-full cursor-pointer items-center gap-1.5 py-0.5 text-left text-muted-foreground text-chat transition-colors hover:text-foreground"
    >
      <StatusIcon name={name} nativeToolType={nativeToolType} state={state} />
      <span className="min-w-0 flex-1 truncate">
        {title.verb !== null && <span className="font-semibold text-foreground">{title.verb}</span>}
        {title.verb !== null && title.body.length > 0 && " "}
        <ToolTitleBody title={title} onBodyClick={onBodyClick} />
      </span>
      {duration !== undefined && (
        <span className="shrink-0 tabular-nums opacity-70">{formatToolDuration(duration)}</span>
      )}
      <ChevronRightIcon className="size-3.5 shrink-0 transition-transform group-data-[state=open]:rotate-90" />
    </CollapsibleTrigger>
  );
}

/** The dynamic part of a trigger row: a shell command, a linked file path, or plain text. */
function ToolTitleBody({ title, onBodyClick }: { title: ToolTitle; onBodyClick?: () => void }) {
  if (title.isShellCommand === true) return <ShellCommandLine command={title.body} />;
  if (!onBodyClick) return title.body;
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
      {title.body}
    </span>
  );
}

/** Collapsed-row command, styled like a terminal prompt line. */
function ShellCommandLine({ command }: { command: string }) {
  const { env, rest } = splitEnvPrefix(command);
  return (
    <span className="font-mono text-[0.92em]">
      {/* Decorative prompt: keep it out of the row's accessible name. */}
      <span aria-hidden="true" className="font-semibold text-foreground">
        ${" "}
      </span>
      {env.length > 0 && <span className="opacity-60">{env}</span>}
      <span>{rest}</span>
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
  inputJson,
  output,
  state,
}: {
  command: string;
  outputFormat: ShellOutputFormat;
  /** Working directory from the call's arguments (Codex sends one). */
  argsCwd: string | null;
  inputJson: string;
  output: string | null;
  state: ToolState;
}) {
  const [wrap, setWrap] = useState(true);
  const [showRaw, setShowRaw] = useState(false);
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
      <Button
        aria-pressed={showRaw}
        className={cn("h-6 px-1.5 text-muted-foreground", showRaw && "text-foreground")}
        onClick={() => setShowRaw((value) => !value)}
        size="xs"
        type="button"
        variant="ghost"
        componentId="diagnostics.tool.toggle_raw_parameters"
      >
        Raw
      </Button>
    </CodeBlockActions>
  );
  return (
    <div className="space-y-2">
      <CodeBlockContainer
        language="bash"
        data-testid="shell-tool-terminal"
        className="@container/shell"
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
        <div
          className="[&_code>span:first-child]:before:font-semibold [&_code>span:first-child]:before:text-muted-foreground [&_code>span:first-child]:before:content-[var(--shell-prompt)]"
          style={SHELL_PROMPT_STYLE}
        >
          <CodeBlockContent
            code={command}
            language="bash"
            wrap={wrap}
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
          <CodePanel
            title="Parameters"
            text={inputJson}
            copyText={inputJson}
            copyLabel="Copy parameters"
          />
          {rawOutput !== null && (
            <OutputSection
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
  title: string;
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
          <span className="truncate font-medium uppercase tracking-wide">{title}</span>
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
  renderPanel,
  language,
}: {
  output: string;
  /** Renders the (possibly truncated) text; defaults to a titled Output panel. */
  renderPanel?: (text: string) => ReactNode;
  /** Highlighting for the default Output panel; JSON unless set. */
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
            title="Output"
            text={preview.text}
            copyText={output}
            copyLabel="Copy output"
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
