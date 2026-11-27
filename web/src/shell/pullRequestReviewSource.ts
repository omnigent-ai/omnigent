// A pull request as the shared diff review shows it: its changed files and the
// diffs parsed from its patch, and its review conversations and new-conversation
// drafts as rows under the lines they're about.

import type { DiffLineAnnotation, FileDiffMetadata } from "@pierre/diffs";
import { useMemo, useRef } from "react";
import { reviewFile, type DiffReviewFile } from "@/components/diff-review/diffReviewModel";
import type { DiffReviewContent } from "@/components/diff-review/reviewItems";
import type { PullRequestChangedFile } from "@/newPullRequestPage/hooks/usePullRequests";
import {
  reviewThreadAnnotations,
  type ReviewThread,
  type ReviewThreadAnchor,
} from "@/lib/pullRequestReviewThreads";
import { hasNoTextDiff } from "./codeViewerHelpers";

export const RENAMED_WITHOUT_CHANGES = "Renamed without changes";
export const NO_TEXT_DIFF = "No text diff available for this file.";

/** What one row under a diff line holds. */
export interface PullRequestReviewAnnotation {
  threads: readonly ReviewThread[];
  /** Above the file's first hunk: conversations and drafts no line of the diff shows. */
  fileLevel: boolean;
  /** The new-conversation composer opens in this row. */
  newThread: boolean;
  /** Why the lines the reader picked can't start a conversation. */
  selectionError?: string;
}

export type PullRequestReviewAnnotations = ReadonlyMap<
  string,
  readonly DiffLineAnnotation<PullRequestReviewAnnotation>[]
>;

/** A file's new-conversation draft, and whether the pull request changed since it was anchored. */
export interface ReviewDraftPlacement {
  anchor: ReviewThreadAnchor;
  stale: boolean;
}

/**
 * Whether the patch has text to show for a file. A binary patch parses to no
 * hunks; a textual patch shows even for a file that previews as an image, like an SVG.
 */
export function hasTextualPatch(fileDiff: FileDiffMetadata | undefined): boolean {
  return !!fileDiff && fileDiff.type !== "rename-pure" && fileDiff.hunks.length > 0;
}

export function pullRequestReviewFile(
  file: PullRequestChangedFile,
  fileDiff: FileDiffMetadata | undefined,
): DiffReviewFile {
  const change =
    file.status === "created" ? "added" : file.status === "deleted" ? "deleted" : "modified";
  const binaryPatch = !!fileDiff && fileDiff.type !== "rename-pure" && fileDiff.hunks.length === 0;
  // The list names a rename's old path; an older host's leaves it to the patch.
  const previousPath = file.previous_path ?? fileDiff?.prevName ?? null;
  return {
    ...reviewFile(file.path, change, {
      additions: file.lines_added,
      deletions: file.lines_removed,
      isBinary: binaryPatch || (!fileDiff && hasNoTextDiff(file.path)),
    }),
    previousPath: previousPath && previousPath !== file.path ? previousPath : null,
  };
}

/** The blobs a patch's `index` line names, which change whenever the content does. */
function patchObjectRevision(fileDiff: FileDiffMetadata | undefined): string | undefined {
  if (!fileDiff?.prevObjectId && !fileDiff?.newObjectId) return undefined;
  return `${fileDiff.prevObjectId ?? ""}..${fileDiff.newObjectId ?? ""}`;
}

/** Where the PR's one patch is: still being read, failed, or read and parsed into `filesByPath`. */
export type PullRequestPatchState =
  { status: "loading" } | { status: "failed"; message: string } | { status: "ready" };

const PATCH_READY: PullRequestPatchState = { status: "ready" };

/**
 * Each file's diff from the parsed patch, or why the patch has none to show. Until the patch is
 * read every listed file is loading, and if reading it failed every file says why.
 */
export function pullRequestReviewContents(
  files: readonly PullRequestChangedFile[],
  filesByPath: ReadonlyMap<string, FileDiffMetadata>,
  patch: PullRequestPatchState = PATCH_READY,
): Map<string, DiffReviewContent> {
  const contents = new Map<string, DiffReviewContent>();
  if (patch.status !== "ready") {
    const content: DiffReviewContent =
      patch.status === "loading"
        ? { status: "loading" }
        : { status: "failed", message: patch.message };
    for (const file of files) contents.set(file.path, content);
    return contents;
  }
  for (const file of files) {
    const fileDiff = filesByPath.get(file.path);
    const revision = patchObjectRevision(fileDiff);
    if (fileDiff?.type === "rename-pure") {
      contents.set(file.path, {
        status: "unavailable",
        message: RENAMED_WITHOUT_CHANGES,
        revision: revision ?? `${fileDiff.prevName ?? ""}->${fileDiff.name}`,
      });
    } else if (hasTextualPatch(fileDiff)) {
      contents.set(file.path, { status: "ready", fileDiff: fileDiff! });
    } else {
      contents.set(file.path, { status: "unavailable", message: NO_TEXT_DIFF, revision });
    }
  }
  return contents;
}

/** The side whose line 0 sits above a file's first hunk; a deleted file shows only old lines. */
function aboveFileSide(fileDiff: FileDiffMetadata): "additions" | "deletions" {
  return fileDiff.hunks.every((hunk) => hunk.additionCount === 0) ? "deletions" : "additions";
}

/**
 * Conversations by the changed file they belong to, matched by current or
 * previous path. The rest, such as those on files the pull request no longer
 * changes, are listed after the files.
 */
export function groupReviewThreads(
  threads: readonly ReviewThread[],
  files: readonly PullRequestChangedFile[],
  filesByPath: ReadonlyMap<string, FileDiffMetadata>,
): { byFile: Map<string, ReviewThread[]>; elsewhere: ReviewThread[] } {
  const byFile = new Map<string, ReviewThread[]>();
  const elsewhere: ReviewThread[] = [];
  for (const thread of threads) {
    const file = files.find(
      (entry) =>
        entry.path === thread.path || filesByPath.get(entry.path)?.prevName === thread.path,
    );
    if (file) byFile.set(file.path, [...(byFile.get(file.path) ?? []), thread]);
    else elsewhere.push(thread);
  }
  return { byFile, elsewhere };
}

/** Conversations as `groupReviewThreads` groups them. */
export interface ReviewThreadGroups {
  byFile: ReadonlyMap<string, readonly ReviewThread[]>;
  elsewhere: readonly ReviewThread[];
}

const sameThreads = (left: readonly ReviewThread[], right: readonly ReviewThread[]) =>
  left.length === right.length && left.every((thread, index) => thread === right[index]);

/**
 * `next`, keeping each of `previous`'s lists that holds the same thread objects in the same
 * order, and `previous` itself when nothing changed: a file's diff then sees its conversations
 * unchanged and keeps its rows, rather than drawing them again on every render.
 */
export function keepUnchangedThreadGroups(
  previous: ReviewThreadGroups | null,
  next: ReviewThreadGroups,
): ReviewThreadGroups {
  let changed = !previous || previous.byFile.size !== next.byFile.size;
  const byFile = new Map<string, readonly ReviewThread[]>();
  for (const [path, threads] of next.byFile) {
    const kept = previous?.byFile.get(path);
    if (kept && sameThreads(kept, threads)) byFile.set(path, kept);
    else {
      byFile.set(path, threads);
      changed = true;
    }
  }
  const keptElsewhere = previous && sameThreads(previous.elsewhere, next.elsewhere);
  if (!keptElsewhere) changed = true;
  if (!changed && previous) return previous;
  return { byFile, elsewhere: keptElsewhere ? previous.elsewhere : next.elsewhere };
}

/**
 * The conversations grouped by file, each file's list keeping its identity until its own
 * conversations change, so a render for anything else leaves every diff's rows as they are.
 */
export function useReviewThreadGroups(
  threads: readonly ReviewThread[],
  files: readonly PullRequestChangedFile[],
  filesByPath: ReadonlyMap<string, FileDiffMetadata>,
): ReviewThreadGroups {
  const grouped = useMemo(
    () => groupReviewThreads(threads, files, filesByPath),
    [threads, files, filesByPath],
  );
  const previous = useRef<ReviewThreadGroups | null>(null);
  const groups = useMemo(() => keepUnchangedThreadGroups(previous.current, grouped), [grouped]);
  previous.current = groups;
  return groups;
}

/**
 * Rows for every file with a diff: conversations on the lines they anchor to,
 * a draft on the line it was started from, and, above the first hunk, what no
 * line shows. Files without a diff can't show rows; their conversations come back
 * in `withoutDiff`, and the paths of their drafts in `draftsWithoutDiff`. A file still
 * loading has neither yet.
 */
export function pullRequestReviewAnnotations({
  threadsByFile,
  contents,
  matchingRevision,
  drafts,
  selectionErrors,
}: {
  threadsByFile: ReadonlyMap<string, readonly ReviewThread[]>;
  contents: ReadonlyMap<string, DiffReviewContent>;
  /** Whether the conversations were read at the revision the diff shows. */
  matchingRevision: boolean;
  drafts: ReadonlyMap<string, ReviewDraftPlacement>;
  selectionErrors: ReadonlyMap<string, string>;
}): {
  annotations: Map<string, DiffLineAnnotation<PullRequestReviewAnnotation>[]>;
  withoutDiff: ReviewThread[];
  draftsWithoutDiff: string[];
} {
  const annotations = new Map<string, DiffLineAnnotation<PullRequestReviewAnnotation>[]>();
  const withoutDiff: ReviewThread[] = [];
  const draftsWithoutDiff: string[] = [];
  const paths = new Set([...threadsByFile.keys(), ...drafts.keys(), ...selectionErrors.keys()]);
  for (const path of paths) {
    const content = contents.get(path);
    const threads = threadsByFile.get(path) ?? [];
    // A file whose diff is still loading places its rows once it arrives.
    if (content?.status === "loading") continue;
    if (content?.status !== "ready") {
      withoutDiff.push(...threads);
      if (drafts.has(path)) draftsWithoutDiff.push(path);
      continue;
    }
    const placed = reviewThreadAnnotations([...threads], content.fileDiff, matchingRevision);
    const rows: DiffLineAnnotation<PullRequestReviewAnnotation>[] = placed.annotations.map(
      ({ side, lineNumber, metadata }) => ({
        side,
        lineNumber,
        metadata: { threads: metadata, fileLevel: false, newThread: false },
      }),
    );
    const draft = drafts.get(path);
    let draftAboveFile = !!draft;
    if (draft && !draft.stale) {
      draftAboveFile = false;
      const side = draft.anchor.side === "LEFT" ? "deletions" : "additions";
      const index = rows.findIndex(
        (row) => row.side === side && row.lineNumber === draft.anchor.line,
      );
      if (index >= 0)
        rows[index] = { ...rows[index]!, metadata: { ...rows[index]!.metadata, newThread: true } };
      else {
        rows.push({
          side,
          lineNumber: draft.anchor.line,
          metadata: { threads: [], fileLevel: false, newThread: true },
        });
      }
    }
    const selectionError = selectionErrors.get(path);
    if (placed.unplaced.length > 0 || draftAboveFile || selectionError) {
      rows.unshift({
        side: aboveFileSide(content.fileDiff),
        lineNumber: 0,
        metadata: {
          threads: placed.unplaced,
          fileLevel: true,
          newThread: draftAboveFile,
          ...(selectionError && { selectionError }),
        },
      });
    }
    if (rows.length > 0) annotations.set(path, rows);
  }
  return { annotations, withoutDiff, draftsWithoutDiff };
}

function sameRow(
  left: DiffLineAnnotation<PullRequestReviewAnnotation>,
  right: DiffLineAnnotation<PullRequestReviewAnnotation>,
): boolean {
  return (
    left.side === right.side &&
    left.lineNumber === right.lineNumber &&
    left.metadata.fileLevel === right.metadata.fileLevel &&
    left.metadata.newThread === right.metadata.newThread &&
    left.metadata.selectionError === right.metadata.selectionError &&
    left.metadata.threads.length === right.metadata.threads.length &&
    left.metadata.threads.every((thread, index) => thread === right.metadata.threads[index])
  );
}

/**
 * `next`, keeping each file's rows from `previous` when nothing in them changed,
 * so the review re-lays out only the files whose conversations did.
 */
export function keepUnchangedAnnotations(
  previous: PullRequestReviewAnnotations,
  next: Map<string, DiffLineAnnotation<PullRequestReviewAnnotation>[]>,
): PullRequestReviewAnnotations {
  let changed = previous.size !== next.size;
  const kept = new Map<string, readonly DiffLineAnnotation<PullRequestReviewAnnotation>[]>();
  for (const [path, rows] of next) {
    const earlier = previous.get(path);
    const same =
      !!earlier &&
      earlier.length === rows.length &&
      earlier.every((row, index) => sameRow(row, rows[index]!));
    kept.set(path, same ? earlier : rows);
    if (!same) changed = true;
  }
  return changed ? kept : previous;
}

/** Where the review shows a conversation: its line, or for one above the file, the first hunk's. */
export interface ReviewThreadLocation {
  path: string;
  line: number;
  side: "additions" | "deletions";
  fileLevel: boolean;
}

export function reviewThreadLocation(
  annotations: PullRequestReviewAnnotations,
  contents: ReadonlyMap<string, DiffReviewContent>,
  threadId: string,
): ReviewThreadLocation | undefined {
  for (const [path, rows] of annotations) {
    const row = rows.find((candidate) =>
      candidate.metadata.threads.some((thread) => thread.id === threadId),
    );
    if (!row) continue;
    if (!row.metadata.fileLevel) {
      return { path, line: row.lineNumber, side: row.side, fileLevel: false };
    }
    // Above the file: reveal the first line on the side that row renders on.
    const content = contents.get(path);
    const firstHunk = content?.status === "ready" ? content.fileDiff.hunks[0] : undefined;
    const line =
      (row.side === "deletions" ? firstHunk?.deletionStart : firstHunk?.additionStart) ?? 1;
    return { path, line: Math.max(line, 1), side: row.side, fileLevel: true };
  }
  return undefined;
}
