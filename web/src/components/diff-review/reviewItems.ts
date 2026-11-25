// Builds what CodeView renders and what find searches from the review's files
// and their loaded diffs. Items keep their identity while nothing about them
// changes, so a refresh that touches one file never makes CodeView lay out the rest.

import type { CodeViewItem, DiffLineAnnotation, FileDiffMetadata } from "@pierre/diffs";
import { reviewItemId, type DiffReviewFile } from "./diffReviewModel";
import type { ReviewSearchLine } from "./findMatches";

/** Where one file's diff stands. */
export type DiffReviewContent =
  | { status: "loading" }
  /** `refreshError` says the latest version failed to load and this one is older. */
  | { status: "ready"; fileDiff: FileDiffMetadata; refreshError?: string }
  | { status: "failed"; message: string }
  /**
   * The source can't produce a diff, e.g. the original content is gone. `revision`
   * identifies the file's content when the source knows it, so it can be marked viewed.
   */
  | { status: "unavailable"; message: string; revision?: string };

export interface ReviewItemsInput<A = undefined> {
  files: readonly DiffReviewFile[];
  contents: ReadonlyMap<string, DiffReviewContent>;
  /** Explicit expand/collapse choices by path; absent paths use the default. */
  collapsed: ReadonlyMap<string, boolean>;
  /** Files the reader marked viewed, which start collapsed. */
  viewed?: ReadonlySet<string>;
  /** Rows a source shows under diff lines, by path; items keep identity while these do. */
  annotations?: ReadonlyMap<string, readonly DiffLineAnnotation<A>[]>;
}

/** Whether a file starts collapsed: generated files and files with no diff to show. */
export function collapsesByDefault(file: DiffReviewFile, content: DiffReviewContent | undefined) {
  return file.isBinary || file.isGenerated || content?.status !== "ready";
}

/** Whether the person can expand the file to read its diff. */
export function isExpandable(file: DiffReviewFile, content: DiffReviewContent | undefined) {
  return !file.isBinary && content?.status === "ready";
}

// CodeView only re-reads an item whose version changed, so every newly built
// item gets a version no earlier item used; reused items keep theirs.
let lastItemVersion = 0;
function nextItemVersion(): number {
  lastItemVersion += 1;
  return lastItemVersion;
}

const placeholderCache = new Map<string, { label: string; item: CodeViewItem<undefined> }>();

function placeholderItem(file: DiffReviewFile, label: string): CodeViewItem<undefined> {
  const cached = placeholderCache.get(file.path);
  if (cached?.label === label) return cached.item;
  const item: CodeViewItem<undefined> = {
    id: reviewItemId(file.path),
    type: "file",
    file: { name: file.path, contents: label, lang: "text", cacheKey: `${file.path}:${label}` },
    collapsed: true,
    version: nextItemVersion(),
  };
  placeholderCache.set(file.path, { label, item });
  return item;
}

/** Why a file has no diff to show yet, shown in its header. */
export function placeholderLabel(
  file: DiffReviewFile,
  content: DiffReviewContent | undefined,
): string {
  if (file.isBinary) return "Binary file";
  switch (content?.status) {
    case "failed":
    case "unavailable":
      return content.message;
    default:
      return "Loading diff…";
  }
}

interface CachedDiffItem {
  path: string;
  collapsed: boolean;
  annotations: readonly unknown[] | undefined;
  item: CodeViewItem<unknown>;
}

const itemCache = new WeakMap<FileDiffMetadata, CachedDiffItem[]>();

/** Diff items cached per diff and path; a file's newest few are enough while annotations change. */
const CACHED_ITEMS_PER_DIFF = 4;

function diffItem<A>(
  path: string,
  fileDiff: FileDiffMetadata,
  collapsed: boolean,
  annotations: readonly DiffLineAnnotation<A>[] | undefined,
): CodeViewItem<A> {
  const entries = itemCache.get(fileDiff) ?? [];
  const cached = entries.find(
    (entry) =>
      entry.path === path && entry.collapsed === collapsed && entry.annotations === annotations,
  );
  if (cached) return cached.item as CodeViewItem<A>;
  const item: CodeViewItem<A> = {
    id: reviewItemId(path),
    type: "diff",
    fileDiff,
    collapsed,
    ...(annotations && { annotations: [...annotations] }),
    version: nextItemVersion(),
  };
  itemCache.set(fileDiff, [
    ...entries.slice(-(CACHED_ITEMS_PER_DIFF - 1)),
    { path, collapsed, annotations, item: item as CodeViewItem<unknown> },
  ]);
  return item;
}

/** One CodeView item per file, in file order. */
export function buildReviewItems<A = undefined>({
  files,
  contents,
  collapsed,
  viewed,
  annotations,
}: ReviewItemsInput<A>): CodeViewItem<A>[] {
  return files.map((file) => {
    const content = contents.get(file.path);
    if (file.isBinary || content?.status !== "ready") {
      // Placeholders carry no annotations, so they fit any annotation type.
      return placeholderItem(file, placeholderLabel(file, content)) as CodeViewItem<A>;
    }
    const isCollapsed =
      collapsed.get(file.path) ?? (collapsesByDefault(file, content) || !!viewed?.has(file.path));
    return diffItem(file.path, content.fileDiff, isCollapsed, annotations?.get(file.path));
  });
}

/** Added and deleted line counts a diff shows. */
export function changedLineCounts(fileDiff: FileDiffMetadata): {
  additions: number;
  deletions: number;
} {
  let additions = 0;
  let deletions = 0;
  for (const hunk of fileDiff.hunks) {
    for (const content of hunk.hunkContent) {
      if (content.type !== "change") continue;
      additions += content.additions;
      deletions += content.deletions;
    }
  }
  return { additions, deletions };
}

/** A file's counts from its diff once loaded, else what the source reported. */
export function fileLineCounts(
  file: DiffReviewFile,
  content: DiffReviewContent | undefined,
): { additions: number; deletions: number } {
  if (content?.status === "ready") return changedLineCounts(content.fileDiff);
  return { additions: file.additions ?? 0, deletions: file.deletions ?? 0 };
}

/**
 * The lines find searches in one diff: the whole new version (unchanged lines
 * included, so collapsed context is found too) plus the deleted lines.
 */
export function searchLinesForDiff(id: string, fileDiff: FileDiffMetadata): ReviewSearchLine[] {
  const lines: ReviewSearchLine[] = [];
  if (!fileDiff.isPartial) {
    fileDiff.additionLines.forEach((text, index) =>
      lines.push({ id, text, lineNumber: index + 1, side: "additions" }),
    );
  }
  for (const hunk of fileDiff.hunks) {
    let additionLine = hunk.additionStart;
    let deletionLine = hunk.deletionStart;
    for (const content of hunk.hunkContent) {
      if (content.type === "context") {
        for (let offset = 0; fileDiff.isPartial && offset < content.lines; offset += 1) {
          lines.push({
            id,
            text: fileDiff.additionLines[content.additionLineIndex + offset] ?? "",
            lineNumber: additionLine + offset,
            side: "additions",
          });
        }
        additionLine += content.lines;
        deletionLine += content.lines;
        continue;
      }
      for (let offset = 0; offset < content.deletions; offset += 1) {
        lines.push({
          id,
          text: fileDiff.deletionLines[content.deletionLineIndex + offset] ?? "",
          lineNumber: deletionLine + offset,
          side: "deletions",
        });
      }
      for (let offset = 0; fileDiff.isPartial && offset < content.additions; offset += 1) {
        lines.push({
          id,
          text: fileDiff.additionLines[content.additionLineIndex + offset] ?? "",
          lineNumber: additionLine + offset,
          side: "additions",
        });
      }
      additionLine += content.additions;
      deletionLine += content.deletions;
    }
  }
  return lines;
}

// Keyed by diff, and checked against its text: a diff can be filled in where it is.
const searchLineCache = new WeakMap<
  FileDiffMetadata,
  { additionLines: readonly string[]; isPartial: boolean; lines: ReviewSearchLine[] }
>();

/** Every searchable line in the shown files: their paths, then their diffs. */
export function reviewSearchLines(
  files: readonly DiffReviewFile[],
  contents: ReadonlyMap<string, DiffReviewContent>,
): ReviewSearchLine[] {
  const lines: ReviewSearchLine[] = [];
  for (const file of files) {
    const id = reviewItemId(file.path);
    lines.push({ id, text: file.path, lineNumber: 1, target: "currentPath" });
    const content = contents.get(file.path);
    if (file.isBinary || content?.status !== "ready") continue;
    const { fileDiff } = content;
    let cached = searchLineCache.get(fileDiff);
    if (
      !cached ||
      cached.isPartial !== fileDiff.isPartial ||
      cached.additionLines !== fileDiff.additionLines
    ) {
      cached = {
        additionLines: fileDiff.additionLines,
        isPartial: fileDiff.isPartial,
        lines: searchLinesForDiff(id, fileDiff),
      };
      searchLineCache.set(fileDiff, cached);
    }
    for (const line of cached.lines) lines.push(line);
  }
  return lines;
}
