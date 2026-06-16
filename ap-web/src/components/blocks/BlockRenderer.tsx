// Dispatch from a `RenderItem` to the right component. Pure switch on
// `item.kind`. Compaction renders as a standalone `Bubble` in
// `ChatPage`, not as an inline render item — no case for it here.
//
// Tool-call collapsing: within a contiguous run of tool / native_tool
// items, older tools fold into a single "See N steps" line (rendered
// by `ToolGroupSummary`). The trailing `STREAMING_TAIL` tools (any
// state) stay outside the group ONLY when (a) the session is still
// running and (b) the very last item in the transcript is a tool —
// meaning the agent hasn't produced any text/reasoning after this
// run yet, so these tools are the live activity. Once the agent
// emits anything else after a tool run (or once the session is
// idle), the run collapses entirely except for still-in-progress
// spinners.

import type { ReactNode } from "react";
import { useMemo } from "react";
import type React from "react";
import { defaultRemarkPlugins } from "streamdown";
import remarkBreaks from "remark-breaks";
import { MessageResponse } from "@/components/ai-elements/message";
import { useThrottledValue } from "@/hooks/useThrottledValue";
import type { RenderItem } from "@/lib/renderItems";
import type { SessionStatus } from "@/lib/types";
import { cn } from "@/lib/utils";
import {
  useFileViewer,
  useFileViewerConversationId,
  useIsChangedPath,
  useWorkspacePaths,
} from "@/shell/FileViewerContext";
import { toWorkspaceRelativePath, useWorkspaceFileExists } from "@/hooks/useWorkspaceChangedFiles";
import { ApprovalCard } from "./ApprovalCard";
import { ReasoningView } from "./ReasoningView";
import { SlashCommandCard } from "./SlashCommandCard";
import { TerminalCommandCard } from "./TerminalCommandCard";
import { ErrorBanner, PolicyDeniedBanner, RetryIndicator } from "./StatusBlocks";
import { ToolCard, ToolGroupSummary } from "./ToolCard";

/**
 * Inline-`code` renderer that turns workspace file paths (e.g.
 * `` `src/components/App.tsx` ``) into clickable links opening the FileViewer.
 *
 * The span's text is first collapsed to a workspace-relative path: an
 * absolute (`/home/u/ws/foo.md`) or home-relative (`~/ws/foo.md`) path under
 * the workspace root is stripped down to its relative form so it matches the
 * changed-files list and the filesystem API (both speak relative paths);
 * absolute/`~` paths outside the root resolve to null and never linkify.
 *
 * That relative path is then linkified when it is either (a) a known
 * agent-changed file — resolved synchronously, the fast path, and the only
 * path that may be an uncommitted/deleted file — or (b) a path-shaped string
 * that the filesystem API confirms points at a real file in the workspace.
 * Everything else (prose-y inline code, non-existent paths) falls back to a
 * styled `<code>` matching Streamdown's default inline appearance. The span
 * always *displays* the original text the agent wrote; only the link target
 * uses the resolved relative path.
 *
 * Rendered by Streamdown as a real component (via the `inlineCode` slot), so
 * it may call hooks: the existence query re-renders this span when it settles,
 * independent of whether `MessageResponse` re-renders its parent.
 */
function WorkspacePathInlineCode({
  children: codeChildren,
  className,
  ...codeProps
}: React.ComponentPropsWithoutRef<"code">) {
  const openFile = useFileViewer();
  const isChangedPath = useIsChangedPath();
  const conversationId = useFileViewerConversationId();
  const { root, home } = useWorkspacePaths();
  const text = typeof codeChildren === "string" ? codeChildren : "";

  // Collapse absolute / "~"-relative forms onto a workspace-relative path so
  // they match the changed-files list and the filesystem API. null = absolute
  // or "~" path outside the workspace (or the root itself) → never a link.
  const linkPath = text ? toWorkspaceRelativePath(text, root, home) : null;
  // "Trusted" means we resolved an absolute/"~" form against the root, so the
  // result is known workspace-relative even if it's a bare basename (no
  // interior slash) that the existence check's path-shape heuristic rejects.
  const trusted = linkPath !== null && linkPath !== text;

  const isChanged = !!linkPath && isChangedPath(linkPath);
  // Only hit the filesystem for path-shaped spans that aren't already known
  // changes; passing null disables the query (keeps hook order stable).
  const existsOnDisk = useWorkspaceFileExists(
    conversationId,
    openFile && linkPath && !isChanged ? linkPath : null,
    trusted,
  );

  if (openFile && linkPath && (isChanged || existsOnDisk)) {
    // Rendered as an inline <code> (not a <button>): a button is laid out as
    // an atomic inline-block, so a long path can't break across lines and
    // drops below the list marker as a whole unit. An inline <code> flows and
    // wraps like the surrounding text; role/tabIndex/keydown restore the
    // button semantics.
    return (
      <code
        role="button"
        tabIndex={0}
        data-streamdown="inline-code"
        // Keep the base inline-code class/props (merge, don't replace) so the
        // link only adds the underline affordance on top of Streamdown's
        // styling and any caller-provided attributes survive.
        className={cn(
          "font-mono text-sm underline decoration-dotted underline-offset-2 hover:text-foreground transition-colors cursor-pointer",
          className,
        )}
        onClick={() => openFile(linkPath)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            openFile(linkPath);
          }
        }}
        {...codeProps}
      >
        {codeChildren}
      </code>
    );
  }
  // Match Streamdown's default inline-code styling so non-path inline code
  // looks unchanged.
  return (
    <code
      className={cn("rounded bg-muted px-1.5 py-0.5 font-mono text-sm", className)}
      data-streamdown="inline-code"
      {...codeProps}
    >
      {codeChildren}
    </code>
  );
}

// Stable module-level override map so MessageResponse's memo (which ignores
// `components` changes) never sees a new identity.
const FILE_PATH_AWARE_COMPONENTS = { inlineCode: WorkspacePathInlineCode };

// How often the live (growing) assistant bubble re-parses its markdown. The
// store pump commits a new, longer text up to once per animation frame (~60/s);
// without this the whole accumulated message is re-parsed on every commit. ~10/s
// is smooth to read and cuts the per-frame parse cost. Trailing-edge, so the
// final text still appears within this window of the last token.
const STREAM_MARKDOWN_THROTTLE_MS = 100;

// Defense-in-depth against a pathological text block locking the tab.
// A user message whose text is a ~50KB unbroken base64 data URL
// — e.g. an image block accidentally serialized into the text stream — both
// jams the full markdown pipeline (Shiki/KaTeX/mermaid + rehype) on the main
// thread AND forces the browser to lay out one ~50K-char line with no break
// opportunities. Either heuristic below routes such a block to plain,
// break-anywhere rendering that bypasses markdown entirely.
//
// `MAX_MARKDOWN_TEXT_LENGTH`: total size above which we never run markdown.
// `MAX_UNBROKEN_TOKEN_LENGTH`: longest run of non-whitespace chars above which
//   layout becomes pathological regardless of total size (base64, long URLs).
// `MAX_PLAINTEXT_DISPLAY_LENGTH`: hard cap on what we paint even as plain text,
//   so a multi-MB payload can't blow up the DOM; the rest is elided.
const MAX_MARKDOWN_TEXT_LENGTH = 50_000;
const MAX_UNBROKEN_TOKEN_LENGTH = 5_000;
const MAX_PLAINTEXT_DISPLAY_LENGTH = 200_000;

/**
 * Longest run of consecutive non-whitespace characters in `text`. ASCII
 * whitespace (space, tab, CR, LF, FF, VT) resets the run — those are the
 * break opportunities the layout engine can use. O(n), single pass.
 */
function longestUnbrokenRun(text: string): number {
  let max = 0;
  let current = 0;
  for (let i = 0; i < text.length; i += 1) {
    const code = text.charCodeAt(i);
    // 32 = space; 9..13 = tab, LF, VT, FF, CR.
    if (code === 32 || (code >= 9 && code <= 13)) {
      current = 0;
    } else {
      current += 1;
      if (current > max) max = current;
    }
  }
  return max;
}

/**
 * Whether `text` should bypass the markdown pipeline because rendering it
 * there would risk locking the tab. See the constants above for the why.
 */
function isPathologicalText(text: string): boolean {
  return (
    text.length > MAX_MARKDOWN_TEXT_LENGTH || longestUnbrokenRun(text) > MAX_UNBROKEN_TOKEN_LENGTH
  );
}

/**
 * Plain, break-anywhere fallback for a pathological text block — no markdown.
 * `whitespace-pre-wrap` keeps newlines; `break-all` gives the layout engine a
 * break opportunity inside an otherwise unbreakable token. Over-long payloads
 * are elided so the DOM node itself can't grow without bound.
 */
function PlainTextFallback({ text }: { text: string }) {
  const truncated = text.length > MAX_PLAINTEXT_DISPLAY_LENGTH;
  const shown = truncated ? text.slice(0, MAX_PLAINTEXT_DISPLAY_LENGTH) : text;
  return (
    <div className="whitespace-pre-wrap break-all font-mono text-xs">
      {shown}
      {truncated && (
        <span className="text-muted-foreground">
          {`\n… [${text.length - MAX_PLAINTEXT_DISPLAY_LENGTH} more characters not shown]`}
        </span>
      )}
    </div>
  );
}

/**
 * Wraps `MessageResponse` with {@link WorkspacePathInlineCode} via Streamdown's
 * `inlineCode` slot — NOT `code` — so fenced code blocks keep their default
 * `<pre>` wrapper and Shiki highlighting. Overriding `code` here would replace
 * block rendering too, stripping `<pre>` and collapsing whitespace.
 *
 * When `breaks` is set, single newlines render as `<br>` (remark-breaks)
 * instead of collapsing to spaces per CommonMark. Used for user bubbles,
 * where people type multi-line messages without blank-line paragraph
 * separators and expect their line breaks preserved. NOTE: Streamdown's
 * `remarkPlugins` prop *replaces* its defaults rather than merging, so we
 * extend `defaultRemarkPlugins` (which carries remark-gfm) — passing
 * `[remarkBreaks]` alone would silently drop GFM tables / strikethrough.
 */
export function FilePathAwareMessageResponse({
  children,
  breaks = false,
  ...props
}: React.ComponentProps<typeof MessageResponse> & { breaks?: boolean }) {
  const components = FILE_PATH_AWARE_COMPONENTS;

  // Extend (don't replace) Streamdown's defaults so remark-gfm survives;
  // append remark-breaks only when `breaks` is requested. When `breaks` is
  // false we pass `undefined` so Streamdown uses its own defaults unchanged.
  const remarkPlugins = useMemo(
    () => (breaks ? [...Object.values(defaultRemarkPlugins), remarkBreaks] : undefined),
    [breaks],
  );

  // Throttle the markdown so the live (still-growing) bubble re-parses a few
  // times per second instead of on every store commit. `children` is a string
  // at both call sites (a text RenderItem and the user bubble); finalized/static
  // text changes once, which emits immediately, so this is a no-op off the
  // streaming path. The hook must be called unconditionally (rules of hooks), so
  // non-string children (none today) pass an inert "" and bypass the result.
  const isString = typeof children === "string";
  const throttledText = useThrottledValue(
    isString ? (children as string) : "",
    STREAM_MARKDOWN_THROTTLE_MS,
  );

  // Defense-in-depth: a string child that is huge or carries a
  // giant unbroken token (e.g. a base64 data URL serialized into the text
  // stream) would lock the tab in the markdown pipeline + layout. Render it as
  // plain break-anywhere text instead. Both call sites (assistant text blocks
  // and the user bubble) flow through here, so this one guard covers both.
  const pathological = useMemo(
    () => isString && isPathologicalText(children as string),
    [isString, children],
  );
  if (pathological) {
    return <PlainTextFallback text={children as string} />;
  }

  return (
    <MessageResponse {...props} components={components} remarkPlugins={remarkPlugins}>
      {isString ? throttledText : children}
    </MessageResponse>
  );
}

const STREAMING_TAIL = 3;

interface BlockRendererProps {
  items: RenderItem[];
  sessionStatus: SessionStatus;
}

export function BlockRenderer({ items, sessionStatus }: BlockRendererProps) {
  const rendered: ReactNode[] = [];
  const isAgentActive = sessionStatus === "running" || sessionStatus === "waiting";
  const streamingRunStart = isAgentActive ? findStreamingRunStart(items) : -1;
  // Reasoning is "currently streaming" iff the agent is live AND this
  // reasoning is the very last item in the bubble. Mirrors the
  // `streamingRunStart` rule for tool runs: the trailing live edge stays
  // expanded; once anything else lands after it, it collapses.
  const lastIdx = items.length - 1;
  const reasoningStreamingIdx =
    isAgentActive && lastIdx >= 0 && items[lastIdx]!.kind === "reasoning" ? lastIdx : -1;

  for (let i = 0; i < items.length; i += 1) {
    const item = items[i]!;

    if (isToolItem(item)) {
      // Consume contiguous run of tool / native_tool items.
      const runStart = i;
      while (i < items.length && isToolItem(items[i]!)) i += 1;
      const run = items.slice(runStart, i);
      i -= 1; // outer loop will i += 1

      // Only the run at `streamingRunStart` (when set) is treated as
      // "currently streaming". Earlier runs, and any run followed by
      // assistant text/reasoning, collapse the same way they would
      // when idle.
      const { grouped, standalone } = partitionToolRun(run, runStart === streamingRunStart);

      if (grouped.length > 0) {
        // Wrap (group + trailing tail) in a single MessageContent child
        // so the message column's `gap-2` only applies AROUND this
        // pair, not BETWEEN them — the tail's `peer-data-[state=open]:mt-0`
        // can then truly bring the two bordered blocks flush when the
        // group is expanded.
        rendered.push(
          <div key={`tool-group-with-tail:${runStart}`}>
            <ToolGroupSummary tools={grouped} count={run.length} />
            {standalone.length > 0 && (
              <div className="mt-1 ml-2 space-y-1 border-l pl-3 py-1 peer-data-[state=open]:mt-0">
                {standalone.map((tool, idx) => renderItem(tool, runStart + idx, false))}
              </div>
            )}
          </div>,
        );
      } else {
        for (const tool of standalone) {
          rendered.push(renderItem(tool, runStart, false));
        }
      }
      continue;
    }

    rendered.push(renderItem(item, i, i === reasoningStreamingIdx));
  }

  return <>{rendered}</>;
}

/**
 * Split a contiguous tool run into the part that folds into the
 * "See N steps" group versus the part rendered individually.
 *
 * For the live-streaming run, the trailing `STREAMING_TAIL` tools
 * (regardless of state) stay outside the group so the user can watch
 * the most recent activity. For any other run — older runs in the
 * transcript, or any run once the loop is idle — only still-in-progress
 * tools stay outside; everything else folds.
 */
function partitionToolRun(
  run: RenderItem[],
  isStreamingRun: boolean,
): { grouped: RenderItem[]; standalone: RenderItem[] } {
  if (isStreamingRun) {
    const tailStart = Math.max(0, run.length - STREAMING_TAIL);
    return { grouped: run.slice(0, tailStart), standalone: run.slice(tailStart) };
  }
  return {
    grouped: run.filter((t) => !isInProgressTool(t)),
    standalone: run.filter(isInProgressTool),
  };
}

function isToolItem(item: RenderItem): boolean {
  return item.kind === "tool" || item.kind === "native_tool";
}

/**
 * If the transcript ends in a contiguous tool run, return its start
 * index — that run is the live activity and should keep its
 * streaming tail. Otherwise return -1: the agent has spoken (or
 * reasoned) after the most recent tools, so they're no longer
 * "current".
 */
function findStreamingRunStart(items: RenderItem[]): number {
  if (items.length === 0) return -1;
  if (!isToolItem(items[items.length - 1]!)) return -1;
  let i = items.length - 1;
  while (i > 0 && isToolItem(items[i - 1]!)) i -= 1;
  return i;
}

/**
 * A tool item is in-progress only when it's a `tool` (not a
 * `native_tool` — those are provider-managed and always arrive
 * completed) and its derived UI state is `input-available`.
 */
function isInProgressTool(item: RenderItem): boolean {
  return item.kind === "tool" && item.state === "input-available";
}

function renderItem(item: RenderItem, index: number, isReasoningStreaming: boolean): ReactNode {
  const key = keyFor(item, index);
  switch (item.kind) {
    case "text":
      return <FilePathAwareMessageResponse key={key}>{item.text}</FilePathAwareMessageResponse>;
    case "reasoning":
      return (
        <ReasoningView
          key={key}
          text={item.text}
          isStreaming={isReasoningStreaming}
          duration={item.duration}
        />
      );
    case "tool":
      return (
        <ToolCard
          key={key}
          name={item.execution.name}
          argsSummary={item.execution.argsSummary}
          arguments={item.execution.arguments}
          output={item.output}
          state={item.state}
          startedAt={item.startedAt}
          duration={item.duration}
        />
      );
    case "native_tool":
      // Reuse the same tool card. Native tools are server-side
      // (provider-managed) so they're always "completed" by the
      // time we see them; render the raw provider data as input.
      return (
        <ToolCard
          key={key}
          name={item.label}
          nativeToolType={item.toolType}
          arguments={item.data}
          output={null}
          state="output-available"
        />
      );
    case "slash_command":
      return (
        <SlashCommandCard
          key={key}
          kind={item.slashKind}
          name={item.name}
          arguments={item.arguments}
          output={item.output}
        />
      );
    case "terminal_command":
      return (
        <TerminalCommandCard
          key={key}
          kind={item.terminalKind}
          input={item.input}
          stdout={item.stdout}
          stderr={item.stderr}
        />
      );
    case "error":
      return <ErrorBanner key={key} message={item.message} source={item.source} code={item.code} />;
    case "policy_denied":
      return <PolicyDeniedBanner key={key} reason={item.reason} phase={item.phase} />;
    case "retry":
      return (
        <RetryIndicator
          key={key}
          source={item.source}
          attempt={item.attempt}
          maxAttempts={item.maxAttempts}
          delaySeconds={item.delaySeconds}
        />
      );
    case "elicitation":
      return (
        <ApprovalCard
          key={key}
          elicitationId={item.elicitationId}
          message={item.message}
          phase={item.phase}
          policyName={item.policyName}
          contentPreview={item.contentPreview}
          requestedSchema={item.requestedSchema}
          url={item.url}
          status={item.status}
          response={item.response}
          askUserQuestion={item.askUserQuestion}
          exitPlanMode={item.exitPlanMode}
          codexCommand={item.codexCommand}
          allowAllEdits={item.allowAllEdits}
        />
      );
  }
}

/**
 * Stable key for each render item. Prefer the server-assigned item id;
 * fall back to call_id for tools (unique within a response) or to
 * position for pre-finalization fragments that don't carry an item id
 * yet (text/reasoning chunks emitted before their `output_item.done`).
 */
function keyFor(item: RenderItem, index: number): string {
  if (item.itemId) return `${item.kind}:${item.itemId}`;
  if (item.kind === "tool") return `tool:${item.execution.callId}`;
  if (item.kind === "elicitation") return `elicitation:${item.elicitationId}`;
  return `${item.kind}:${index}`;
}
