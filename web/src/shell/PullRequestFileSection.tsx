// One file's section in the pull request Changes tab, plus the small pieces the
// Changes tab and the panel share with it (status icons, diffstat, IconButton).

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDownIcon,
  ChevronRightIcon,
  FileDiffIcon,
  FileMinusIcon,
  FilePlusIcon,
  FileSymlinkIcon,
  Loader2Icon,
  MessageSquareIcon,
  type LucideIcon,
  PlusIcon,
  XIcon,
} from "lucide-react";
import { FileDiff } from "@pierre/diffs/react";
import type { DiffLineAnnotation, FileDiffMetadata, SelectedLineRange } from "@pierre/diffs";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  usePullRequestReviewThreadComposer,
  type PullRequestChangedFile,
} from "@/hooks/usePullRequests";
import {
  reviewThreadAnnotations,
  reviewSelection,
  type ReviewThread,
} from "@/lib/pullRequestReviewThreads";
import { cn } from "@/lib/utils";
import { PullRequestCommentForm } from "./PullRequestCommentComposer";
import { PullRequestReviewThread, type ReviewThreadContext } from "./PullRequestReviewThread";
import { highlightsSupported } from "./previewSearch";

/** A ghost icon button with a tooltip; the toolbar's shared control element.
 *  No-delay is set by the surrounding TooltipProvider. */
export function IconButton({
  label,
  onClick,
  disabled,
  className,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="ghost"
          size="icon-xs"
          aria-label={label}
          onClick={onClick}
          disabled={disabled}
          className={cn("shrink-0", className)}
        >
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}

// GitHub-style status icons (a file glyph carrying the change kind) instead of
// bare A/M/D/R letters — quicker to recognize at a glance.
export const STATUS_META: Record<
  PullRequestChangedFile["status"],
  { label: string; className: string; Icon: LucideIcon }
> = {
  created: { label: "Added", className: "text-green-600 dark:text-green-400", Icon: FilePlusIcon },
  modified: {
    label: "Modified",
    className: "text-amber-600 dark:text-amber-400",
    Icon: FileDiffIcon,
  },
  deleted: { label: "Deleted", className: "text-red-600 dark:text-red-400", Icon: FileMinusIcon },
  renamed: {
    label: "Renamed",
    className: "text-blue-600 dark:text-blue-400",
    Icon: FileSymlinkIcon,
  },
};

/** Diffstat (+adds −removes) shared by the sidebar row and the section header. */
export function DiffStat({ file }: { file: PullRequestChangedFile }) {
  if (file.lines_added === null && file.lines_removed === null) return null;
  return (
    <span className="shrink-0 font-mono text-xs tabular-nums">
      {file.lines_added !== null && (
        <span className="text-green-600 dark:text-green-400">+{file.lines_added}</span>
      )}{" "}
      {file.lines_removed !== null && (
        <span className="text-red-600 dark:text-red-400">−{file.lines_removed}</span>
      )}
    </span>
  );
}

/** Split a path into its directory prefix (with trailing slash) and basename. */
function splitPath(path: string): { dir: string; name: string } {
  const i = path.lastIndexOf("/");
  return i === -1
    ? { dir: "", name: path }
    : { dir: path.slice(0, i + 1), name: path.slice(i + 1) };
}

/** Conversations and a draft can share the same diff annotation. */
interface ReviewAnnotation {
  threads: ReviewThread[];
  newThread?: boolean;
}
export type DiffOptions = React.ComponentProps<typeof FileDiff<ReviewAnnotation>>["options"];

/**
 * Files with more unified diff lines than this wait for "Show diff": FileDiff
 * tokenizes on the main thread (disableWorkerPool), so a huge file freezes the app.
 */
export const LARGE_DIFF_THRESHOLD = 2000;

/** The active find match on one line, as character offsets into that line's text. */
export interface FindHighlight {
  side: "additions" | "deletions";
  line: number;
  start: number;
  end: number;
}

/** Styled by the `::highlight()` rule in the diff's unsafeCSS. */
const FIND_HIGHLIGHT = "pr-find-active";
/** Frames to wait for a diff row that is still rendering. */
const ROW_RETRY_FRAMES = 30;

/** The diff's rows live in its open shadow root once @pierre/diffs has rendered. */
function diffRoot(section: HTMLElement): ParentNode {
  return section.querySelector("diffs-container")?.shadowRoot ?? section;
}

function findDiffRow({ side, line }: FindHighlight, section: HTMLElement): HTMLElement | null {
  const root = diffRoot(section);
  // Split views give each side its own column; unified rows use one number per line.
  const unified =
    side === "deletions"
      ? '[data-line-type="change-deletion"]'
      : ':not([data-line-type="change-deletion"])';
  return (
    root.querySelector<HTMLElement>(`[data-${side}] [data-line="${line}"]`) ??
    root.querySelector<HTMLElement>(`[data-line="${line}"]${unified}`)
  );
}

/** A DOM range over `start`..`end` of the row's text, which may span several token spans. */
function rowRange(row: HTMLElement, start: number, end: number): Range | null {
  const walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  let offset = 0;
  let started = false;
  for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
    const length = node.data.length;
    if (!started && start < offset + length) {
      range.setStart(node, start - offset);
      started = true;
    }
    if (started && end <= offset + length) {
      range.setEnd(node, end - offset);
      return range;
    }
    offset += length;
  }
  return null;
}

function paintFindMatch(row: HTMLElement, { start, end }: FindHighlight) {
  const range = rowRange(row, start, end);
  if (range && highlightsSupported()) CSS.highlights.set(FIND_HIGHLIGHT, new Highlight(range));
}

function clearFindMatch() {
  if (highlightsSupported()) CSS.highlights.delete(FIND_HIGHLIGHT);
}

/**
 * One file's section in the stacked diff: a sticky grey header (chevron + status
 * + path + diffstat) and the file's rendered diff. Clicking the header toggles
 * the diff open/closed. The diff mounts lazily once the section nears the
 * viewport (a big PR doesn't build every diff at once).
 */
export function PullRequestFileSection({
  file,
  fileDiff,
  options,
  registerRef,
  collapsed: collapsedProp,
  onToggleCollapsed,
  threads,
  threadContext,
  matchingRevision,
  headerActions,
  forceExpanded = false,
  forceMount = false,
  bypassLargeDiffGate = false,
  highlight = null,
}: {
  file: PullRequestChangedFile;
  /** Parsed per-file diff from the whole-PR patch; absent for binary/unparsed. */
  fileDiff: FileDiffMetadata | undefined;
  options: DiffOptions;
  registerRef: (path: string, el: HTMLElement | null) => void;
  /** Whether this file's diff is hidden. Lifted so the toolbar can drive all. */
  collapsed: boolean;
  onToggleCollapsed: () => void;
  threads: ReviewThread[];
  threadContext: ReviewThreadContext;
  matchingRevision: boolean;
  /** Controls beside the header's toggle button. */
  headerActions?: React.ReactNode;
  /** Show the diff even while `collapsed` (a temporary reveal). */
  forceExpanded?: boolean;
  /** Mount the diff without waiting for the section to near the viewport. */
  forceMount?: boolean;
  /** Render a large diff without waiting for "Show diff". */
  bypassLargeDiffGate?: boolean;
  /** The active find match; painted and centred, apart from the reader's selection. */
  highlight?: FindHighlight | null;
}) {
  const ref = useRef<HTMLDivElement>(null);
  // The force flags latch, so a section stays mounted and ungated after the reader moves on.
  // Latching during render mounts the diff in the same commit as the reveal.
  const [forcedMount, setForcedMount] = useState(false);
  if (forceMount && !forcedMount) setForcedMount(true);
  const [forcedUngate, setForcedUngate] = useState(false);
  if (bypassLargeDiffGate && !forcedUngate) setForcedUngate(true);
  const [seenInViewport, setSeen] = useState(false);
  const seen = seenInViewport || forcedMount;
  const [showLargeDiffClicked, setShowLargeDiff] = useState(false);
  const showLargeDiff = showLargeDiffClicked || forcedUngate;
  const collapsed = collapsedProp && !forceExpanded;
  const composer = usePullRequestReviewThreadComposer(
    threadContext.conversationId,
    threadContext.prUrl,
    file.path,
    threadContext.headSha ?? "",
    threadContext.baseSha ?? "",
  );
  const revision = `${threadContext.baseSha}:${threadContext.headSha}`;
  const [selection, setSelection] = useState<{
    revision: string;
    range: SelectedLineRange | null;
  }>();
  // Restore the committed range when a pointer gesture is canceled.
  const [selectionPreview, setSelectionPreview] = useState<typeof selection>();
  const selectedLines = selection?.revision === revision ? selection.range : null;
  const readerLines =
    selectionPreview?.revision === revision ? selectionPreview.range : selectedLines;
  const previewSelection = useCallback(
    (range: SelectedLineRange | null) => setSelectionPreview({ revision, range }),
    [revision],
  );
  const commitSelection = useCallback(
    (range: SelectedLineRange | null) => {
      setSelection({ revision, range });
      setSelectionPreview(undefined);
    },
    [revision],
  );
  const [selectionError, setSelectionError] = useState<string>();
  const startConversation = (range: SelectedLineRange) => {
    if (!threadContext.canCreate || !fileDiff || composer.draft.pending) return;
    const lines = reviewSelection(range, fileDiff);
    if (!lines) {
      setSelectionError("Select lines within one diff hunk, on the same side.");
      return;
    }
    setSelectionError(undefined);
    composer.selectLines(lines);
  };

  useEffect(() => {
    registerRef(file.path, ref.current);
    return () => registerRef(file.path, null);
  }, [file.path, registerRef]);

  useEffect(() => {
    const el = ref.current;
    if (!el || seen) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) setSeen(true);
      },
      { rootMargin: "400px 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [seen]);

  const meta = STATUS_META[file.status];
  const StatusIcon = meta.Icon;
  const { dir, name } = splitPath(file.path);
  const ToggleIcon = collapsed ? ChevronRightIcon : ChevronDownIcon;
  // A rename carries its old path in the parsed patch; a pure rename (100%
  // similarity) has no hunks, so show a note instead of an empty diff.
  const renamedFrom =
    fileDiff?.prevName && fileDiff.prevName !== file.path ? fileDiff.prevName : null;
  const pureRename = fileDiff?.type === "rename-pure";
  const renderingDiff =
    !!fileDiff &&
    !pureRename &&
    seen &&
    (fileDiff.unifiedLineCount <= LARGE_DIFF_THRESHOLD || showLargeDiff);
  const anchor = composer.draft.anchor;
  const showGutterUtility = !!threadContext.canCreate && (!anchor || composer.stale);
  const showDraftInline = !!anchor && !composer.stale && renderingDiff;
  const { annotations, unplaced } = useMemo(() => {
    const { annotations: threadAnnotations, unplaced: unplacedThreads } = reviewThreadAnnotations(
      threads,
      renderingDiff ? fileDiff : undefined,
      matchingRevision,
    );
    const inlineAnnotations: DiffLineAnnotation<ReviewAnnotation>[] = threadAnnotations.map(
      (annotation) => ({ ...annotation, metadata: { threads: annotation.metadata } }),
    );
    if (showDraftInline) {
      const side = anchor.side === "LEFT" ? "deletions" : "additions";
      const existing = inlineAnnotations.find(
        (item) => item.side === side && item.lineNumber === anchor.line,
      );
      if (existing) existing.metadata.newThread = true;
      else
        inlineAnnotations.push({
          side,
          lineNumber: anchor.line,
          metadata: { threads: [], newThread: true },
        });
    }
    return { annotations: inlineAnnotations, unplaced: unplacedThreads };
  }, [threads, renderingDiff, fileDiff, matchingRevision, anchor, showDraftInline]);

  // Centre the active find match once its row renders. Diff re-renders replace the rows,
  // so onPostRender repaints the range without scrolling again.
  useEffect(() => {
    const section = ref.current;
    if (!highlight || !section || collapsed || !renderingDiff) return;
    let frame = 0;
    let frames = 0;
    const centre = () => {
      const row = findDiffRow(highlight, section);
      if (!row) {
        if (frames++ < ROW_RETRY_FRAMES) frame = requestAnimationFrame(centre);
        return;
      }
      paintFindMatch(row, highlight);
      row.scrollIntoView({ block: "center" });
    };
    centre();
    return () => {
      cancelAnimationFrame(frame);
      clearFindMatch();
    };
  }, [highlight, collapsed, renderingDiff]);
  // Stable across selection drags: the diff re-renders when a callback option changes identity.
  const repaintHighlight = useCallback(() => {
    const section = ref.current;
    const row = highlight && section ? findDiffRow(highlight, section) : null;
    if (highlight && row) paintFindMatch(row, highlight);
  }, [highlight]);
  const newConversation = anchor && (
    <section
      aria-label="New review conversation"
      className="pull-request-comment-surface my-2 min-w-0 rounded-md border text-foreground font-sans text-ui [white-space:normal]"
    >
      <div className="flex items-center justify-between gap-2 px-3 py-2">
        <span>
          New conversation · {anchor.side === "LEFT" ? "Old" : "New"}{" "}
          {anchor.start_line ? `lines ${anchor.start_line}–${anchor.line}` : `line ${anchor.line}`}
        </span>
        <IconButton
          label="Close comment composer"
          disabled={composer.draft.pending}
          onClick={composer.cancel}
        >
          <XIcon className="size-4" aria-hidden="true" />
        </IconButton>
      </div>
      {composer.stale && (
        <p role="status" className="px-3 pb-2">
          The pull request changed. Your draft is saved; select its lines again before posting.
        </p>
      )}
      {!threadContext.canCreate && (
        <p role="status" className="px-3 pb-2">
          Your draft is saved. Commenting is currently unavailable.
        </p>
      )}
      <PullRequestCommentForm
        {...composer}
        account={threadContext.account}
        inline
        newThread
        disabled={composer.stale || !threadContext.canCreate}
      />
    </section>
  );

  return (
    <div ref={ref} data-github-file={file.path}>
      <div className="sticky top-0 z-10 flex w-full items-center border-b border-border bg-secondary dark:bg-muted">
        <button
          type="button"
          onClick={onToggleCollapsed}
          aria-expanded={!collapsed}
          title={renamedFrom ? `${renamedFrom} → ${file.path}` : file.path}
          className="group flex min-w-0 flex-1 cursor-pointer items-center gap-2 px-3 py-1.5 text-left"
        >
          <ToggleIcon
            aria-hidden="true"
            className="size-3.5 shrink-0 text-muted-foreground transition-colors group-hover:text-foreground"
          />
          <StatusIcon aria-hidden className={cn("size-3.5 shrink-0", meta.className)} />
          <span className="min-w-0 flex-1 truncate text-ui">
            {renamedFrom ? (
              <>
                <span className="text-muted-foreground">{renamedFrom}</span>
                <span className="text-muted-foreground"> → </span>
                <span className="font-medium">{file.path}</span>
              </>
            ) : (
              <>
                {dir && <span className="text-muted-foreground">{dir}</span>}
                <span className="font-medium">{name}</span>
              </>
            )}
          </span>
          <DiffStat file={file} />
          {threads.length > 0 && (
            <span
              className="flex shrink-0 items-center gap-1 text-xs text-muted-foreground"
              title={`${threads.length} conversation${threads.length === 1 ? "" : "s"}`}
            >
              <MessageSquareIcon className="size-3" />
              {threads.length}
            </span>
          )}
        </button>
        {headerActions && (
          <div className="flex shrink-0 items-center gap-1 pr-2">{headerActions}</div>
        )}
      </div>
      {!collapsed && (
        <>
          {selectionError && (
            <p role="alert" className="px-3 py-2 text-ui">
              {selectionError}
            </p>
          )}
          {!showDraftInline && newConversation && <div className="px-3">{newConversation}</div>}
        </>
      )}
      {!collapsed && unplaced.length > 0 && (
        <div className="px-3">
          {unplaced.map((thread) => (
            <PullRequestReviewThread
              key={thread.id}
              thread={thread}
              context={threadContext}
              fileDiff={matchingRevision ? fileDiff : undefined}
              showContext
            />
          ))}
        </div>
      )}
      {collapsed ? null : pureRename ? (
        <div className="p-4 text-ui text-muted-foreground">File renamed without changes.</div>
      ) : !seen ? (
        <div className="flex items-center justify-center gap-2 p-6 text-ui text-muted-foreground">
          <Loader2Icon className="size-4 animate-spin" />
          Loading diff…
        </div>
      ) : fileDiff && fileDiff.unifiedLineCount > LARGE_DIFF_THRESHOLD && !showLargeDiff ? (
        <div className="flex items-center gap-3 p-4 text-ui text-muted-foreground">
          Large diff — {fileDiff.unifiedLineCount.toLocaleString()} lines
          <button
            type="button"
            className="text-foreground underline-offset-2 hover:underline"
            onClick={() => setShowLargeDiff(true)}
          >
            Show diff
          </button>
        </div>
      ) : fileDiff ? (
        <div className="overflow-x-auto" onPointerCancel={() => setSelectionPreview(undefined)}>
          <div
            style={{
              minWidth: annotations.length && options?.diffStyle === "split" ? 720 : undefined,
            }}
          >
            <FileDiff
              fileDiff={fileDiff}
              options={{
                ...options,
                enableLineSelection: !!threadContext.canCreate,
                enableGutterUtility: showGutterUtility,
                onLineSelectionStart: previewSelection,
                onLineSelectionChange: previewSelection,
                onLineSelectionEnd: commitSelection,
                onPostRender: repaintHighlight,
              }}
              selectedLines={readerLines}
              disableWorkerPool
              renderGutterUtility={
                showGutterUtility
                  ? (getHoveredLine) => (
                      <button
                        type="button"
                        aria-label="Start a new review conversation"
                        title="Start a new review conversation"
                        className="absolute -right-px top-1/2 z-10 flex size-5 -translate-y-1/2 translate-x-1/2 cursor-pointer items-center justify-center rounded bg-primary text-primary-foreground"
                        disabled={composer.draft.pending}
                        onClick={() => {
                          const hovered = getHoveredLine();
                          const withinSelection =
                            hovered &&
                            selectedLines &&
                            selectedLines.side === hovered.side &&
                            (!selectedLines.endSide || selectedLines.endSide === hovered.side) &&
                            hovered.lineNumber >=
                              Math.min(selectedLines.start, selectedLines.end) &&
                            hovered.lineNumber <= Math.max(selectedLines.start, selectedLines.end);
                          const range = withinSelection
                            ? selectedLines
                            : hovered && {
                                start: hovered.lineNumber,
                                end: hovered.lineNumber,
                                side: hovered.side,
                              };
                          if (range) startConversation(range);
                        }}
                      >
                        <PlusIcon className="size-4" />
                      </button>
                    )
                  : undefined
              }
              lineAnnotations={annotations}
              renderAnnotation={(annotation) => (
                <div className="min-w-0 px-3">
                  {annotation.metadata.newThread && newConversation}
                  {annotation.metadata.threads.map((thread) => (
                    <PullRequestReviewThread
                      key={thread.id}
                      thread={thread}
                      context={threadContext}
                      fileDiff={matchingRevision ? fileDiff : undefined}
                    />
                  ))}
                </div>
              )}
            />
          </div>
        </div>
      ) : (
        <div className="p-4 text-ui text-muted-foreground">
          No text diff available for this file.
        </div>
      )}
    </div>
  );
}
