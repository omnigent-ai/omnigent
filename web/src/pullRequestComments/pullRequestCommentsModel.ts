// What Activity's conversation comments and review threads share: their thread views, the
// filters, search and sorts, and how a thread's place and lines read. It works on what has
// loaded; the timeline says how sure its counts are.

import type { PullRequestComment } from "@/newPullRequestPage/hooks/usePullRequests";
import type { ReviewThread } from "@/lib/pullRequestReviewThreads";

export type CommentsView = "all" | "unresolved" | "resolved";

export type CommentFilterField = "author" | "file" | "state";
export const COMMENT_FILTER_FIELDS: readonly CommentFilterField[] = ["author", "file", "state"];
export const COMMENT_FILTER_TITLES: Record<CommentFilterField, string> = {
  author: "Author",
  file: "File",
  state: "State",
};
export interface CommentFilter {
  field: CommentFilterField;
  values: string[];
}

export type CommentsSort = "oldest" | "newest" | "file";
export const COMMENTS_SORT_IDS: readonly CommentsSort[] = ["oldest", "newest", "file"];
export const COMMENTS_SORTS: readonly { id: CommentsSort; title: string }[] = [
  { id: "newest", title: "Newest First" },
  { id: "oldest", title: "Oldest First" },
  { id: "file", title: "By File" },
];

const STATE_TITLES: Record<string, string> = { current: "Current", outdated: "Outdated" };

/** A review thread or a conversation comment, as filters, search and sorting read it. */
export type CommentsItem =
  | { kind: "thread"; key: string; thread: ReviewThread; time: number }
  | { kind: "comment"; key: string; comment: PullRequestComment; time: number };

/** The time of an entry with no readable creation time, which lists after every dated one. */
export const UNDATED = Number.MAX_SAFE_INTEGER;

/** An entry's place in time from its ISO creation time, or {@link UNDATED}. */
export function entryTime(value: string | null | undefined): number {
  const parsed = value ? Date.parse(value) : NaN;
  return Number.isFinite(parsed) ? parsed : UNDATED;
}

/** A thread placed by its first published comment, so a pending reply doesn't move it. */
export function threadTime(thread: ReviewThread): number {
  const comments = thread.comments.data;
  return entryTime((comments.find((comment) => !comment.pending) ?? comments[0])?.created_at);
}

/** What sorting reads of an entry: when it was written, and a review thread's place. */
export interface SortableItem {
  kind: string;
  time: number;
  /** Set on a review thread's entry. */
  thread?: ReviewThread;
}

/** A thread only its author can see yet: every comment in it is a pending draft. */
export function isDraftThread(item: SortableItem): boolean {
  if (item.kind !== "thread" || !item.thread) return false;
  const comments = item.thread.comments.data;
  return comments.length > 0 && comments.every((comment) => comment.pending === true);
}

function itemAuthors(item: CommentsItem): string[] {
  if (item.kind === "comment") return item.comment.author ? [item.comment.author] : [];
  return [
    ...new Set(
      item.thread.comments.data.flatMap((comment) => (comment.author ? [comment.author] : [])),
    ),
  ];
}

function itemFile(item: SortableItem): string | undefined {
  return item.kind === "thread" ? item.thread?.path : undefined;
}

function fieldValues(item: CommentsItem, field: CommentFilterField): string[] {
  switch (field) {
    case "author":
      return itemAuthors(item);
    case "file": {
      const file = itemFile(item);
      return file ? [file] : [];
    }
    case "state":
      return [item.kind === "thread" && item.thread.outdated ? "outdated" : "current"];
  }
}

export function inView(item: CommentsItem, view: CommentsView): boolean {
  switch (view) {
    case "all":
      return true;
    case "unresolved":
      return item.kind === "thread" && !item.thread.resolved;
    case "resolved":
      return item.kind === "thread" && item.thread.resolved;
  }
}

export function matchesFilters(item: CommentsItem, filters: readonly CommentFilter[]): boolean {
  return filters.every(
    (filter) =>
      filter.values.length === 0 ||
      fieldValues(item, filter.field).some((value) => filter.values.includes(value)),
  );
}

function searchableText(item: CommentsItem, revealed: ReadonlySet<string>): string {
  if (item.kind === "comment") return `${item.comment.author ?? ""}\n${item.comment.body}`;
  return [
    item.thread.path,
    // A hidden comment stays out of search until someone shows it, as on GitHub.
    ...item.thread.comments.data.flatMap((comment) =>
      comment.minimized && !revealed.has(comment.id)
        ? [comment.author ?? ""]
        : [comment.author ?? "", comment.body],
    ),
  ].join("\n");
}

const NONE_REVEALED: ReadonlySet<string> = new Set();

/** Every word of the query appears in what an entry says, who said it, or where. */
export function matchesSearch(
  item: CommentsItem,
  query: string,
  revealed: ReadonlySet<string> = NONE_REVEALED,
): boolean {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return true;
  const text = searchableText(item, revealed).toLowerCase();
  return words.every((word) => text.includes(word));
}

/** How many entries carry each value of each field, for the Filter menu. */
export function filterCounts(
  items: readonly CommentsItem[],
): Record<CommentFilterField, Map<string, number>> {
  const counts: Record<CommentFilterField, Map<string, number>> = {
    author: new Map(),
    file: new Map(),
    state: new Map(),
  };
  for (const item of items) {
    for (const field of COMMENT_FILTER_FIELDS) {
      for (const value of fieldValues(item, field)) {
        counts[field].set(value, (counts[field].get(value) ?? 0) + 1);
      }
    }
  }
  return counts;
}

export function toggleFilter(
  filters: readonly CommentFilter[],
  field: CommentFilterField,
  value: string,
): CommentFilter[] {
  const existing = filters.find((filter) => filter.field === field);
  if (!existing) return [...filters, { field, values: [value] }];
  const values = existing.values.includes(value)
    ? existing.values.filter((candidate) => candidate !== value)
    : [...existing.values, value];
  return values.length === 0
    ? filters.filter((filter) => filter.field !== field)
    : filters.map((filter) => (filter.field === field ? { field, values } : filter));
}

/** A filter value as the menu and chips show it: files by their name, states titled. */
export function filterValueTitle(field: CommentFilterField, value: string): string {
  if (field === "state") return STATE_TITLES[value] ?? value;
  if (field === "file") return value.slice(value.lastIndexOf("/") + 1);
  return value;
}

function threadLine(thread: ReviewThread): number {
  return thread.line ?? thread.original_line ?? 0;
}

/**
 * Top-level entries in `sort`'s order; a thread's replies keep reading order inside it.
 * Drafts are the newest: first in Newest First, last in Oldest First. Undated entries
 * follow the dated ones either way; equal times keep the loaded order.
 */
export function sortItems<Item extends SortableItem>(
  items: readonly Item[],
  sort: CommentsSort,
): Item[] {
  if (sort !== "file") {
    const drafts = items.filter(isDraftThread);
    const published = items.filter((item) => !isDraftThread(item));
    const dated = published.filter((item) => item.time !== UNDATED);
    const undated = published.filter((item) => item.time === UNDATED);
    if (sort === "oldest") {
      return [...dated.sort((left, right) => left.time - right.time), ...undated, ...drafts];
    }
    return [...drafts, ...dated.sort((left, right) => right.time - left.time), ...undated];
  }
  // By file, in path order and by line within a file; the conversation comes first.
  const line = (item: SortableItem) =>
    item.kind === "thread" && item.thread ? threadLine(item.thread) : 0;
  return [...items].sort(
    (left, right) =>
      (itemFile(left) ?? "").localeCompare(itemFile(right) ?? "") ||
      line(left) - line(right) ||
      left.time - right.time,
  );
}

/**
 * Whether search and filters saw everything: no threads, replies or conversation comments
 * are still on the host. Counts can be complete while a thread's later replies aren't.
 */
export function searchCoverage({
  moreThreads,
  commentsPartial,
  threads,
}: {
  moreThreads: boolean;
  commentsPartial: boolean;
  threads: readonly ReviewThread[];
}): { complete: boolean; threadsWithMoreReplies: ReviewThread[] } {
  const threadsWithMoreReplies = threads.filter((thread) => thread.comments.has_more);
  return {
    complete: !moreThreads && !commentsPartial && threadsWithMoreReplies.length === 0,
    threadsWithMoreReplies,
  };
}

/** A count as a chip shows it: a lower bound, such as "11+", while more are still on the host. */
export function countLabel(count: number, lowerBound: boolean): string {
  return lowerBound ? `${count}+` : String(count);
}

/**
 * Which lines a thread is about: its current lines, or for an outdated thread the lines it
 * was written on; a file-level thread names the file.
 */
export function threadLineLabel(thread: ReviewThread): string {
  if (thread.subject_type === "FILE") return "File";
  const current = thread.line !== null && thread.line !== undefined;
  const line = current ? thread.line : thread.original_line;
  const start = current ? thread.start_line : thread.original_start_line;
  if (line === null || line === undefined) return "Outdated";
  return start !== null && start !== undefined && start !== line
    ? `Lines ${start}–${line}`
    : `Line ${line}`;
}

/** How many lines the thread spans, for marking them in its hunk. */
export function threadSpan(thread: ReviewThread): number {
  const current = thread.line !== null && thread.line !== undefined;
  const line = current ? thread.line : thread.original_line;
  const start = current ? thread.start_line : thread.original_start_line;
  if (line === null || line === undefined || start === null || start === undefined) return 1;
  return Math.max(1, line - start + 1);
}

export interface HunkLine {
  kind: "context" | "addition" | "deletion";
  text: string;
  oldLine?: number;
  newLine?: number;
}

/** Reads GitHub's `diff_hunk`: a hunk header and the lines down to the commented one. */
export function parseDiffHunk(hunk: string): HunkLine[] {
  const lines = hunk.replace(/\r\n?/g, "\n").split("\n");
  const header = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(lines[0] ?? "");
  let oldLine = header ? Number(header[1]) : 1;
  let newLine = header ? Number(header[2]) : 1;
  const result: HunkLine[] = [];
  for (const line of header ? lines.slice(1) : lines) {
    if (line.startsWith("\\")) continue;
    const marker = line[0];
    const text = line.slice(1);
    if (marker === "+") result.push({ kind: "addition", text, newLine: newLine++ });
    else if (marker === "-") result.push({ kind: "deletion", text, oldLine: oldLine++ });
    else {
      result.push({
        kind: "context",
        text: marker === " " ? text : line,
        oldLine: oldLine++,
        newLine: newLine++,
      });
    }
  }
  return result;
}

/** How many of a hunk's lines show before "Show N more lines": the span and three above it. */
export function shownHunkLines(lineCount: number, span: number): number {
  return Math.min(lineCount, Math.max(4, span + 3));
}
