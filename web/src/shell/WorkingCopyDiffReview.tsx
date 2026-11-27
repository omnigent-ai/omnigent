// The session's working-copy changes in the whole-review diff. Files come from
// the changed-files list in the Changes tab's order; each file's diff loads
// through the same cached request the single-file diff uses.

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { Loader2Icon } from "lucide-react";
import { DiffReview, type DiffReviewProps } from "@/components/diff-review/DiffReview";
import {
  DiffContentLoader,
  type DiffContentRequest,
  type DiffSourceText,
} from "@/components/diff-review/diffContentLoader";
import { reviewFile, type DiffReviewFile } from "@/components/diff-review/diffReviewModel";
import { createFileDiffComputer } from "@/components/diff-review/fileDiffComputation";
import type {
  ReviewRevealTarget,
  ReviewScrollAnchor,
} from "@/components/diff-review/reviewNavigation";
import { currentLinesByPath } from "@/components/diff-review/useReviewTextLayer";
import { fetchFileDiff, fileDiffQueryKey, type FileDiffResponse } from "@/hooks/useFileDiff";
import {
  useForgetDiffTextWhenRevoked,
  useWorkingCopyBaseCommands,
  useWorkingCopyChanges,
  type WorkingCopyChanges,
} from "@/hooks/useWorkingCopyChanges";
import type { WorkspaceChangedFile } from "@/hooks/useWorkspaceChangedFiles";
import {
  type ComparisonFileDiffBase,
  comparisonFileDiffQueryKey,
  fetchComparisonFileDiff,
} from "@/lib/branchPointFileDiff";
import {
  type ComparisonEntry,
  ComparisonFileError,
  type EffectiveComparison,
  hostListing,
  isSettledFileReason,
  needsRelist,
  StaleComparisonReadError,
} from "@/lib/workingCopyComparison";
import type { WorkingCopyComparisonOwner } from "@/lib/workingCopyComparisonOwner";
import { useComparisonOwner } from "@/hooks/useComparisonOwner";
import { hasNoTextDiff } from "./codeViewerHelpers";
import { type ChangedSort, groupChangedFiles } from "./FlatFileList";
import { isFilePositionPending } from "./filePositionState";
import type { FileDiffRequest, FilePosition } from "./FileViewerContext";
import { useReviewCommentLayer, type ReviewCommentContext } from "./useReviewCommentLayer";
import { useWorkingCopyBase, workingCopyBaseMenu } from "./workingCopyBase";
import {
  WorkingCopyComparisonPlaceholder,
  WorkingCopyComparisonStatusRow,
} from "./WorkingCopyComparisonStatus";

export const ORIGINAL_CONTENT_UNAVAILABLE = "Original content unavailable";

function isComparisonEntry(entry: WorkspaceChangedFile): entry is ComparisonEntry {
  return "fingerprint" in entry;
}

/** Header notes for a branch-point entry whose kind, state, or content needs saying. */
function comparisonEntryNotes(entry: ComparisonEntry): string[] {
  const notes: string[] = [];
  if (entry.change === "type_changed") notes.push("Type changed");
  if (entry.change === "conflicted") notes.push("Has merge conflicts");
  if (entry.baseKind === "submodule" || entry.workingKind === "submodule") {
    notes.push("Submodule commit changed");
  }
  return notes;
}

/** Whether an entry has no text to load: binary by name, by the listing, or a submodule. */
function hasNoEntryText(entry: WorkspaceChangedFile): boolean {
  if (hasNoTextDiff(entry.path)) return true;
  return isComparisonEntry(entry) && entry.binary;
}

/** The review's file for one changed-files entry. */
export function workingCopyReviewFile(entry: WorkspaceChangedFile): DiffReviewFile {
  const file = reviewFile(
    entry.path,
    entry.status === "created" ? "added" : entry.status === "deleted" ? "deleted" : "modified",
    {
      additions: entry.lines_added,
      deletions: entry.lines_removed,
      isBinary: hasNoEntryText(entry),
    },
  );
  if (!isComparisonEntry(entry)) return file;
  const notes = comparisonEntryNotes(entry);
  return notes.length > 0 ? { ...file, notes } : file;
}

/** Changes whenever the listing suggests the file's content changed. */
export function changedFileFingerprint(entry: WorkspaceChangedFile): string {
  return [
    entry.status,
    entry.bytes,
    entry.modified_at,
    entry.lines_added,
    entry.lines_removed,
  ].join(":");
}

/**
 * Fetch one file's before/after text through the shared diff cache. A cached
 * diff is reused only if its request started after the changed-files listing
 * at `listedAt` arrived and nothing invalidated it since. Otherwise the text
 * comes from a request that starts after this call, so it includes every
 * change so far.
 */
export async function fetchWorkingCopyText(
  queryClient: QueryClient,
  conversationId: string,
  path: string,
  {
    fresh,
    listedAt,
    signal,
  }: {
    fresh: boolean;
    listedAt: number;
    /** The loader's lifetime: once aborted, no new read starts and nothing is cached. */
    signal?: AbortSignal;
  },
): Promise<DiffSourceText> {
  const queryKey = fileDiffQueryKey(conversationId, path);
  const queryFn = ({ signal: querySignal }: { signal: AbortSignal }) =>
    fetchFileDiff(conversationId, path, querySignal);
  const stopIfGone = () => {
    if (signal?.aborted) {
      throw signal.reason ?? new DOMException("The review was closed.", "AbortError");
    }
  };
  stopIfGone();
  const query = queryClient.getQueryCache().find<FileDiffResponse>({ queryKey, exact: true });
  const cached = query?.state;
  const reusable =
    !fresh &&
    cached?.status === "success" &&
    cached.fetchStatus === "idle" &&
    !cached.isInvalidated &&
    // When the request started, not when its response arrived: a slow request
    // that began before a change can finish after the listing that reports it.
    (cached.data?.requestedAt ?? 0) >= listedAt;
  let response: FileDiffResponse;
  if (reusable) {
    response = cached.data!;
  } else {
    // A request already in flight (another review or the single-file diff) may
    // predate the change; wait it out, then fetch. Any request that's in flight
    // by then started after this call, so joining it is fresh enough.
    if (cached?.fetchStatus === "fetching") await query?.promise?.catch(() => undefined);
    // The wait may outlast the review, or its permission: then nothing is read or cached.
    stopIfGone();
    response = await queryClient.fetchQuery({ queryKey, queryFn, staleTime: 0 });
  }
  if (response.baseline_state === "unknown") return { unavailable: ORIGINAL_CONTENT_UNAVAILABLE };
  return { before: response.before, after: response.after };
}

/**
 * Fetch one file of a pinned comparison, reusing the cached text for exactly
 * this listing. Refusals that are the file's answer come back as text to show;
 * the cache is seeded only while the comparison and fingerprint are current.
 */
export async function fetchBranchPointText(
  queryClient: QueryClient,
  owner: WorkingCopyComparisonOwner,
  conversationId: string,
  path: string,
  base: ComparisonFileDiffBase,
  signal: AbortSignal,
): Promise<DiffSourceText> {
  const queryKey = comparisonFileDiffQueryKey(conversationId, path, base);
  const cached = queryClient.getQueryData<FileDiffResponse>(queryKey);
  if (cached) return { before: cached.before, after: cached.after };
  let response: FileDiffResponse;
  try {
    response = await fetchComparisonFileDiff(owner, conversationId, path, base, signal);
  } catch (error) {
    if (error instanceof ComparisonFileError && isSettledFileReason(error.reason)) {
      return { unavailable: error.message };
    }
    throw error;
  }
  const current = owner.isCurrent(
    conversationId,
    base.generation,
    base.comparisonId,
    path,
    base.fingerprint,
  );
  // A late answer never brings text back after the comparison moved on.
  if (current && queryClient.getQueryData(queryKey) === undefined) {
    queryClient.setQueryData(queryKey, response);
  }
  return { before: response.before, after: response.after };
}

/** Which comparison a review's loader reads; a new one gets a new loader. */
type LoaderScope =
  { base: "head" } | { base: "branch-point"; comparisonId: string; generation: number };

function loaderScope(comparison: EffectiveComparison, generation: number): LoaderScope | null {
  const listing = hostListing(comparison);
  if (listing) return { base: "branch-point", comparisonId: listing.id, generation };
  return comparison.base === "head" ? { base: "head" } : null;
}

function useDiffContentLoader(
  conversationId: string,
  ignoreWhitespace: boolean,
  listedAt: number,
  scope: LoaderScope | null,
) {
  const queryClient = useQueryClient();
  const owner = useComparisonOwner();
  const latestListedAt = useRef(listedAt);
  useEffect(() => {
    latestListedAt.current = listedAt;
  }, [listedAt]);
  const [loader, setLoader] = useState<{ key: string; loader: DiffContentLoader } | null>(null);
  // A loader belongs to one session and comparison; later whitespace changes go through setIgnoreWhitespace.
  const initialIgnoreWhitespace = useRef(ignoreWhitespace);
  const scopeKey = scope ? JSON.stringify([conversationId, scope]) : null;
  useEffect(() => {
    if (!scopeKey) return;
    const [, scoped] = JSON.parse(scopeKey) as [string, LoaderScope];
    const created = new DiffContentLoader({
      computer: createFileDiffComputer(),
      // A file whose comparison changed twice in one chain waits for Retry.
      holdsFailure: (cause) => cause instanceof ComparisonFileError && needsRelist(cause.reason),
      isSuperseded: (cause) => cause instanceof StaleComparisonReadError,
      ignoreWhitespace: initialIgnoreWhitespace.current,
      fetchText: (path, { fresh, fingerprint, signal }) =>
        scoped.base === "branch-point"
          ? fetchBranchPointText(
              queryClient,
              owner,
              conversationId,
              path,
              {
                comparisonId: scoped.comparisonId,
                generation: scoped.generation,
                fingerprint,
              },
              signal,
            )
          : fetchWorkingCopyText(queryClient, conversationId, path, {
              fresh,
              listedAt: latestListedAt.current,
              signal,
            }),
    });
    setLoader({ key: scopeKey, loader: created });
    return () => created.dispose();
  }, [conversationId, queryClient, scopeKey, owner]);
  const current = loader && loader.key === scopeKey ? loader.loader : null;
  useEffect(() => current?.setIgnoreWhitespace(ignoreWhitespace), [current, ignoreWhitespace]);
  return current;
}

/** The note on a branch-point listing's files whose names can't be shown; null when there are none. */
export function unrepresentableNote(count: number): string | null {
  if (count <= 0) return null;
  return count === 1
    ? "1 file with a name that can't be shown"
    : `${count.toLocaleString()} files with names that can't be shown`;
}

const NO_CONTENTS = new Map();

export interface WorkingCopyDiffReviewProps {
  conversationId: string;
  /** Commenting on the review's files, shared with the file viewer. */
  comments?: ReviewCommentContext;
  /** A citation in the opened file; revealed until the reader takes over. */
  position?: FilePosition;
  /** The file the viewer has open; the review scrolls to it. */
  revealPath: string;
  /** A request that opened `revealPath` in the review; its first mount reveals the file. */
  revealRequest?: FileDiffRequest;
  sort: ChangedSort;
  layout: "unified" | "split";
  onLayoutChange: (layout: "unified" | "split") => void;
  wrapLines: boolean;
  onWrapLinesChange: (wrap: boolean) => void;
  hideWhitespace: boolean;
  onHideWhitespaceChange: (hide: boolean) => void;
  findOpen: boolean;
  findFocusRequest: number;
  onFindClose: () => void;
  /** Per-file actions for each file header's menu. */
  fileActions?: DiffReviewProps["fileActions"];
  folderActions?: DiffReviewProps["folderActions"];
  fileActionsInPalette?: DiffReviewProps["fileActionsInPalette"];
  onOpenFileInNewTab?: DiffReviewProps["onOpenFileInNewTab"];
  /** False while the viewer is mounted but hidden; the review's ⌘K commands are withdrawn. */
  active?: boolean;
}

/** A line the review was asked to reveal, and the citation it came from, if any. */
interface RevealRequest {
  target: ReviewRevealTarget;
  position?: FilePosition;
}

// The reader's place per conversation and opened file, for reopening the review.
const scrollAnchors = new Map<string, ReviewScrollAnchor>();
// Requests that already placed a review; a remount for the same one restores the reader's place.
const placedRevealRequests = new WeakSet<FileDiffRequest>();

/**
 * The session's changes as the file viewer shows them: this review owns the
 * session's comparison registration and names its base in the toolbar.
 */
export function WorkingCopyDiffReview(props: WorkingCopyDiffReviewProps) {
  const { conversationId, active = true } = props;
  // Only a review on screen keeps the comparison current; a hidden one just reads it.
  const changedFiles = useWorkingCopyChanges(conversationId, {
    kind: active ? "surface" : "file",
  });
  useForgetDiffTextWhenRevoked(conversationId, changedFiles.permitted);
  const { comparison } = changedFiles;
  const headBase = useWorkingCopyBase(conversationId);
  const baseCommands = useWorkingCopyBaseCommands(conversationId);
  const base = useMemo(
    () => workingCopyBaseMenu(comparison, headBase, baseCommands),
    [comparison, headBase, baseCommands],
  );
  return <WorkingCopyDiffReviewBody {...props} changedFiles={changedFiles} base={base} />;
}

export interface WorkingCopyDiffReviewBodyProps extends WorkingCopyDiffReviewProps {
  /** The session's changes from the one view that registered for them. */
  changedFiles: WorkingCopyChanges;
  /** What the toolbar names the comparison; null shows no base. */
  base: DiffReviewProps["base"];
  /** Another design's chrome for the review; absent renders the file viewer's. */
  presentation?: DiffReviewProps["presentation"];
  /**
   * Whether this body shows the comparison's status row and its note on files left
   * out, above the review. False where the caller shows them itself, under its toolbar.
   */
  showComparisonNotes?: boolean;
  /** Where viewed marks and the reader's place are kept; the session's changes unless given. */
  reviewScope?: string;
}

/**
 * The review of a session's changes, given changes some owner already reads.
 * That owner also forgets the session's cached diff text when access is lost,
 * since this body may not be mounted to see it happen.
 * It loads each file's text under the listing's generation, so a read for an
 * older comparison or another session never lands here.
 */
export function WorkingCopyDiffReviewBody({
  conversationId,
  changedFiles,
  base,
  presentation,
  showComparisonNotes = true,
  comments,
  position,
  revealPath,
  revealRequest,
  sort,
  layout,
  onLayoutChange,
  wrapLines,
  onWrapLinesChange,
  hideWhitespace,
  onHideWhitespaceChange,
  findOpen,
  findFocusRequest,
  onFindClose,
  fileActions,
  folderActions,
  fileActionsInPalette,
  onOpenFileInNewTab,
  active = true,
  reviewScope = `changes:${conversationId}`,
}: WorkingCopyDiffReviewBodyProps) {
  const owner = useComparisonOwner();
  const { comparison } = changedFiles;
  // No list, as after a lost grant, means no loader: its reads stop with it.
  const scope = changedFiles.data ? loaderScope(comparison, changedFiles.generation) : null;
  const loader = useDiffContentLoader(
    conversationId,
    hideWhitespace,
    changedFiles.dataUpdatedAt,
    scope,
  );
  const entries = useMemo(
    () => groupChangedFiles(changedFiles.data?.data ?? [], sort).flatMap((group) => group.files),
    [changedFiles.data?.data, sort],
  );
  const files = useMemo(() => entries.map(workingCopyReviewFile), [entries]);

  // Runs on every successful listing refresh, even an identical one, so files
  // whose last load failed are tried again.
  const listedAt = changedFiles.isSuccess ? changedFiles.dataUpdatedAt : 0;
  useEffect(() => {
    if (!loader) return;
    const requests: DiffContentRequest[] = entries
      .filter((entry) => !hasNoEntryText(entry))
      .map((entry) => ({
        path: entry.path,
        fingerprint: isComparisonEntry(entry) ? entry.fingerprint : changedFileFingerprint(entry),
      }));
    loader.setFiles(requests, revealPath);
  }, [loader, entries, revealPath, listedAt]);

  // While the workspace can't be reached, loaded files stay and nothing new is read.
  useEffect(() => loader?.setPaused(!changedFiles.readable), [loader, changedFiles.readable]);

  // Access was verified again: files refused meanwhile load, and loaded ones stay as they are.
  const lastAccessEpoch = useRef(changedFiles.accessEpoch);
  useEffect(() => {
    if (lastAccessEpoch.current === changedFiles.accessEpoch) return;
    lastAccessEpoch.current = changedFiles.accessEpoch;
    loader?.retryFailures();
  }, [loader, changedFiles.accessEpoch]);

  const contents = useSyncExternalStore(
    (listener) => loader?.subscribe(listener) ?? (() => {}),
    () => loader?.contents ?? NO_CONTENTS,
  );

  const currentLines = useMemo(() => currentLinesByPath(contents), [contents]);
  const commentLayer = useReviewCommentLayer({
    conversationId,
    currentPath: revealPath,
    context: comments,
    currentLines,
  });

  // The latest request wins: a citation, or a comment made active elsewhere.
  // A request belongs to the opened file and, for a citation, to that position.
  const [request, setRequest] = useState<RevealRequest | null>(null);
  const [requestPath, setRequestPath] = useState(revealPath);
  if (requestPath !== revealPath) {
    // Navigation retires the previous file's request, whether or not it was revealed yet.
    setRequestPath(revealPath);
    setRequest(null);
  }
  useEffect(() => {
    if (commentLayer.revealTarget) setRequest({ target: commentLayer.revealTarget });
  }, [commentLayer.revealTarget]);
  useEffect(() => {
    if (!position || !isFilePositionPending(position)) return;
    setRequest({
      target: {
        path: revealPath,
        line: position.line,
        column: position.column,
        endLine: position.endLine,
        align: "center",
        persistent: () => isFilePositionPending(position),
      },
      position,
    });
  }, [position, revealPath]);
  const revealTarget =
    request &&
    request.target.path === revealPath &&
    (request.position === undefined || request.position === position)
      ? request.target
      : null;
  const markedLines = useMemo(
    () =>
      position?.endLine ? { path: revealPath, start: position.line, end: position.endLine } : null,
    [position, revealPath],
  );

  const anchorKey = `${reviewScope}:${revealPath}`;
  // A fresh request, like clicking the file in Changes, shows the file rather than where the reader was.
  const [initialScrollAnchor] = useState(() =>
    isFilePositionPending(position) ||
    comments?.activeSelection ||
    (revealRequest && !placedRevealRequests.has(revealRequest))
      ? undefined
      : scrollAnchors.get(anchorKey),
  );
  useEffect(() => {
    if (revealRequest) placedRevealRequests.add(revealRequest);
  }, [revealRequest]);
  const latestAnchorKey = useRef(anchorKey);
  latestAnchorKey.current = anchorKey;
  const saveScrollAnchor = useCallback((anchor: ReviewScrollAnchor) => {
    scrollAnchors.set(latestAnchorKey.current, anchor);
  }, []);

  const placeholder = (
    <WorkingCopyComparisonPlaceholder sessionId={conversationId} comparison={comparison} />
  );
  if (comparison.base === "pending" || comparison.base === "unavailable") return placeholder;
  if (changedFiles.isError && !changedFiles.data) {
    return (
      <div
        role="alert"
        className="flex h-full items-center justify-center p-8 text-ui text-destructive"
      >
        Couldn&apos;t load the changed files.{" "}
        {changedFiles.error instanceof Error ? changedFiles.error.message : ""}
      </div>
    );
  }
  if (!changedFiles.permitted) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-ui text-muted-foreground">
        You don&apos;t have access to this session&apos;s files.
      </div>
    );
  }
  if (!changedFiles.data || !loader) {
    return (
      <div className="flex h-full items-center justify-center gap-2 p-8 text-ui text-muted-foreground">
        <Loader2Icon className="size-4 animate-spin" />
        Loading changes…
      </div>
    );
  }
  const listing = hostListing(comparison);
  const footer =
    showComparisonNotes && listing ? unrepresentableNote(listing.unrepresentableNames) : null;
  return (
    <>
      {showComparisonNotes && (
        <WorkingCopyComparisonStatusRow sessionId={conversationId} comparison={comparison} />
      )}
      {footer && (
        <p className="shrink-0 border-b border-border px-3 py-1 text-xs text-muted-foreground">
          {footer}
        </p>
      )}
      <DiffReview
        diagnosticsView="working_copy"
        files={files}
        base={base}
        contents={contents}
        onRetry={(path) => {
          // The person asked again, so the file gets another automatic re-list.
          if (listing) {
            owner.allowRecovery(conversationId, path);
          }
          loader.retry(path);
        }}
        revealPath={revealPath}
        layout={layout}
        onLayoutChange={onLayoutChange}
        wrapLines={wrapLines}
        onWrapLinesChange={onWrapLinesChange}
        ignoreWhitespace={hideWhitespace}
        onIgnoreWhitespaceChange={onHideWhitespaceChange}
        commandHeading="Changes"
        commandsActive={active}
        findOpen={findOpen}
        findFocusRequest={findFocusRequest}
        onFindClose={onFindClose}
        emptyMessage="Files the agent creates, edits, or deletes in this session appear here."
        highlights={commentLayer.highlights}
        onTextEvent={commentLayer.onTextEvent}
        revealTarget={revealTarget}
        fileActions={fileActions}
        folderActions={folderActions}
        fileActionsInPalette={fileActionsInPalette}
        onOpenFileInNewTab={onOpenFileInNewTab}
        markedLines={markedLines}
        initialScrollAnchor={initialScrollAnchor}
        onScrollAnchorChange={saveScrollAnchor}
        reviewScope={reviewScope}
        presentation={presentation}
      />
      {commentLayer.actions}
    </>
  );
}
