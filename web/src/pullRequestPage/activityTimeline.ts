// The Overview's activity timeline as a model, before anything draws it: the PR's conversation
// comments, review threads, reviews and pushes in one list, narrowed by kind, thread state,
// author, file and search, in the conversation's sorts, with what each kind counts and how
// sure that is. It holds only what the reads give: where reviews aren't read, a review's body
// is a conversation comment, and pushes exist only where the read brings the PR's history.

import type {
  PullRequestComment,
  PullRequestReview,
  PullRequestReviewState,
  PullRequestTimelineEvent,
} from "@/newPullRequestPage/hooks/usePullRequests";
import type { ReviewThread } from "@/lib/pullRequestReviewThreads";
import {
  type CommentFilter,
  type CommentFilterField,
  type CommentsItem,
  type CommentsSort,
  type CommentsView,
  entryTime,
  filterCounts,
  inView,
  matchesFilters,
  matchesSearch,
  sortItems,
  threadTime,
} from "@/pullRequestComments/pullRequestCommentsModel";

import { commentActivityIds, postedCommentKey, timelineEventActivityId } from "./activityKeys";

/**
 * One entry: a conversation comment, a review thread, a review's verdict with its body as a
 * comment where it has one, a push.
 */
export type ActivityEntry =
  | { kind: "comment"; key: string; comment: PullRequestComment; time: number }
  | { kind: "thread"; key: string; thread: ReviewThread; time: number }
  | {
      kind: "review";
      key: string;
      review: PullRequestReview;
      comment: PullRequestComment | null;
      time: number;
    }
  | { kind: "push"; key: string; event: PullRequestTimelineEvent; time: number };

export type ActivityKind = ActivityEntry["kind"];
/** Which kinds show: every one, or one of them. */
export type ActivityKindFilter = "all" | ActivityKind;
export const ACTIVITY_KIND_FILTERS: readonly { id: ActivityKindFilter; title: string }[] = [
  { id: "all", title: "All" },
  { id: "comment", title: "Comments" },
  { id: "thread", title: "Threads" },
  { id: "review", title: "Reviews" },
  { id: "push", title: "Pushes" },
];

/** What a review's row says its author did, and the words search finds it by. */
export const REVIEW_VERDICTS: Record<PullRequestReviewState, { action: string; words: string }> = {
  APPROVED: { action: "approved these changes", words: "approved approval" },
  CHANGES_REQUESTED: { action: "requested changes", words: "requested changes" },
  COMMENTED: { action: "reviewed", words: "reviewed commented" },
  DISMISSED: { action: "had a review dismissed", words: "dismissed" },
};

/** What the timeline shows: its kind, which threads, the filters and search, its sort. */
export interface ActivityTimelineState {
  kind: ActivityKindFilter;
  /** Which review threads show; it narrows threads only, never comments or pushes. */
  threads: CommentsView;
  filters: readonly CommentFilter[];
  query: string;
  sort: CommentsSort;
}

export const DEFAULT_ACTIVITY_TIMELINE: ActivityTimelineState = {
  kind: "all",
  threads: "all",
  filters: [],
  query: "",
  sort: "newest",
};

/** What was read of the PR's activity, and how much of it there is beyond that. */
export interface ActivityReads {
  comments: readonly PullRequestComment[];
  /** More conversation comments are on the host than were read. */
  commentsPartial: boolean;
  /** Null while review threads aren't read here, as where this Mac can't read them. */
  threads: readonly ReviewThread[] | null;
  /** More threads are on the host than were read. */
  moreThreads: boolean;
  /** Null where the read brings no reviews, as the host's read; their bodies are comments then. */
  reviews: readonly PullRequestReview[] | null;
  /** More reviews were submitted than the read listed. */
  reviewsPartial: boolean;
  /** Null where the read brings no push history, as this Mac's own overview read. */
  events: readonly PullRequestTimelineEvent[] | null;
  /** More pushes happened than the read listed. */
  eventsPartial: boolean;
}

/** A kind's count, as sure as the reads let it be. */
export type ActivityCount =
  | { known: true; count: number; lowerBound: boolean }
  /** Nothing of this kind was read here: a count would claim none. */
  | { known: false };

export interface ActivityTimeline {
  /** What shows, in the state's sort. */
  entries: ActivityEntry[];
  /** Each kind's count with the same filters and search, whatever kind is chosen. */
  counts: Record<ActivityKindFilter, ActivityCount>;
  /** The threads' counts by state, with the same filters and search, and how sure each is. */
  threadCounts: Record<CommentsView, ActivityCount>;
}

/**
 * The reviews that say something: a comment-only review without a body only gathers its
 * threads, which list on their own.
 */
function listedReviews(reviews: readonly PullRequestReview[] | null): PullRequestReview[] {
  return (reviews ?? []).filter((review) => review.state !== "COMMENTED" || review.body.trim());
}

/** A review's body as the conversation comment its row shows, linking to the review. */
function reviewComment(review: PullRequestReview): PullRequestComment | null {
  if (!review.body.trim()) return null;
  return {
    author: review.author,
    body: review.body,
    created_at: review.submitted_at,
    url: review.url,
    review_id: review.id,
  };
}

/**
 * Every read entry, each once, with a key that stays its own: comments by link, time and author,
 * kept apart where several share a link (every review's body links to the PR), threads by id,
 * reviews and pushes by their own ids. A review's body shows in its review's row, not again as
 * a comment. In time order; equal times keep this order.
 */
export function activityEntries(reads: ActivityReads): ActivityEntry[] {
  const commentIds = commentActivityIds(reads.comments);
  const readReviews = new Set((reads.reviews ?? []).map((review) => review.id));
  return [
    ...reads.comments.flatMap((comment, index): ActivityEntry[] =>
      comment.review_id && readReviews.has(comment.review_id)
        ? []
        : [
            {
              kind: "comment",
              key: commentIds[index]!,
              comment,
              time: entryTime(comment.created_at),
            },
          ],
    ),
    ...(reads.threads ?? []).map((thread): ActivityEntry => ({
      kind: "thread",
      key: `thread:${thread.id}`,
      thread,
      time: threadTime(thread),
    })),
    ...listedReviews(reads.reviews).map((review): ActivityEntry => ({
      kind: "review",
      key: `review:${review.id}`,
      review,
      comment: reviewComment(review),
      time: entryTime(review.submitted_at),
    })),
    ...(reads.events ?? []).map((event): ActivityEntry => ({
      kind: "push",
      key: timelineEventActivityId(event),
      event,
      time: entryTime(event.created_at),
    })),
  ];
}

/** A push matches a filter on its author; it has no file or state, so those leave it out. */
function pushMatchesFilters(
  event: PullRequestTimelineEvent,
  filters: readonly CommentFilter[],
): boolean {
  return filters.every((filter) => {
    if (filter.values.length === 0) return true;
    if (filter.field === "author") return !!event.author && filter.values.includes(event.author);
    return false;
  });
}

function pushMatchesSearch(event: PullRequestTimelineEvent, query: string): boolean {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const text = [event.author ?? "", event.message ?? "", event.sha ?? ""].join("\n").toLowerCase();
  return words.every((word) => text.includes(word));
}

/** A review as the conversation comment it reads like: its author, and its body if any. */
function reviewItem(
  entry: Extract<ActivityEntry, { kind: "review" }>,
): Extract<CommentsItem, { kind: "comment" }> {
  const { review } = entry;
  const comment = entry.comment ?? {
    author: review.author,
    body: "",
    created_at: review.submitted_at,
    url: review.url,
  };
  return { kind: "comment", key: entry.key, comment, time: entry.time };
}

function entryMatchesFilters(entry: ActivityEntry, filters: readonly CommentFilter[]): boolean {
  if (entry.kind === "push") return pushMatchesFilters(entry.event, filters);
  return matchesFilters(entry.kind === "review" ? reviewItem(entry) : entry, filters);
}

function entryMatchesSearch(
  entry: ActivityEntry,
  query: string,
  revealed: ReadonlySet<string>,
): boolean {
  if (entry.kind === "push") return pushMatchesSearch(entry.event, query);
  if (entry.kind === "review") {
    const item = reviewItem(entry);
    const words = REVIEW_VERDICTS[entry.review.state].words;
    const comment = { ...item.comment, body: `${words}\n${item.comment.body}` };
    return matchesSearch({ ...item, comment }, query, revealed);
  }
  return matchesSearch(entry, query, revealed);
}

/** Whether an entry passes the filters and search, whatever its kind and thread state. */
function narrowed(
  entry: ActivityEntry,
  state: ActivityTimelineState,
  revealed: ReadonlySet<string>,
): boolean {
  return (
    entryMatchesFilters(entry, state.filters) && entryMatchesSearch(entry, state.query, revealed)
  );
}

const NONE_REVEALED: ReadonlySet<string> = new Set();

/**
 * What the timeline shows for `state`, and what each kind would count. A kind the reads didn't
 * bring counts as unknown, never as none; one with more on the host counts as a lower bound.
 */
export function activityTimeline(
  reads: ActivityReads,
  state: ActivityTimelineState,
  /** Hidden comments someone chose to show, which search reads like any other. */
  revealed: ReadonlySet<string> = NONE_REVEALED,
): ActivityTimeline {
  const entries = activityEntries(reads);
  const passing = entries.filter((entry) => narrowed(entry, state, revealed));
  const count = (kind: ActivityKind) => passing.filter((entry) => entry.kind === kind).length;
  // Search and the author filter read replies: a thread that doesn't match yet may still
  // match in replies not read yet. Its file and state are its own, so those filters can't.
  const readsReplies =
    state.query.trim() !== "" ||
    state.filters.some((filter) => filter.field === "author" && filter.values.length > 0);
  const passingKeys = new Set(passing.map((entry) => entry.key));
  const mayMatchLater = (view: CommentsView) =>
    readsReplies &&
    entries.some(
      (entry) =>
        entry.kind === "thread" &&
        entry.thread.comments.has_more &&
        !passingKeys.has(entry.key) &&
        inView(entry, view),
    );
  const counts: Record<ActivityKind, ActivityCount> = {
    comment: { known: true, count: count("comment"), lowerBound: reads.commentsPartial },
    thread:
      reads.threads === null
        ? { known: false }
        : {
            known: true,
            count: count("thread"),
            lowerBound: reads.moreThreads || mayMatchLater("all"),
          },
    review:
      reads.reviews === null
        ? { known: false }
        : { known: true, count: count("review"), lowerBound: reads.reviewsPartial },
    push:
      reads.events === null
        ? { known: false }
        : { known: true, count: count("push"), lowerBound: reads.eventsPartial },
  };
  // Unread reviews leave nothing out: their bodies are comments then, and a bare verdict
  // isn't an entry where reviews aren't a kind.
  const { review: reviewCount, ...listed } = counts;
  const kinds =
    reads.reviews === null ? Object.values(listed) : [...Object.values(listed), reviewCount];
  const known = kinds.filter((counted) => counted.known);
  const all: ActivityCount = {
    known: true,
    count: known.reduce((sum, counted) => sum + counted.count, 0),
    // All is as sure as its least sure kind: unknown or partial kinds make it a lower bound.
    lowerBound: known.length < kinds.length || known.some((counted) => counted.lowerBound),
  };
  const threadCount = (view: CommentsView): ActivityCount =>
    reads.threads === null
      ? { known: false }
      : {
          known: true,
          count: passing.filter((entry) => entry.kind === "thread" && inView(entry, view)).length,
          lowerBound: reads.moreThreads || mayMatchLater(view),
        };
  const threadCounts = {
    all: threadCount("all"),
    unresolved: threadCount("unresolved"),
    resolved: threadCount("resolved"),
  };
  const shown = passing.filter(
    (entry) =>
      (state.kind === "all" || entry.kind === state.kind) &&
      (entry.kind !== "thread" || inView(entry, state.threads)),
  );
  return { entries: sortItems(shown, state.sort), counts: { all, ...counts }, threadCounts };
}

/** What a reveal cleared so its entry could show: a filter's field, or the search. */
export type ActivityRevealCleared = CommentFilterField | "search";

export interface ActivityReveal {
  state: ActivityTimelineState;
  cleared: ActivityRevealCleared[];
}

/**
 * The state that shows the entry `key`, changing as little as it can: a kind or thread state
 * that hides it widens to all, and each filter and the search that leave it out are cleared.
 * Null when nothing read has that key, as before a posted comment's read arrives.
 */
export function revealActivityEntry(
  reads: ActivityReads,
  state: ActivityTimelineState,
  key: string,
  revealed: ReadonlySet<string> = NONE_REVEALED,
): ActivityReveal | null {
  const entry = activityEntries(reads).find((candidate) => candidate.key === key);
  if (!entry) return null;
  const kind = state.kind === "all" || state.kind === entry.kind ? state.kind : "all";
  const threads = entry.kind === "thread" && !inView(entry, state.threads) ? "all" : state.threads;
  const passes = (filter: CommentFilter) => entryMatchesFilters(entry, [filter]);
  const kept = state.filters.filter(passes);
  const cleared: ActivityRevealCleared[] = state.filters
    .filter((filter) => !passes(filter))
    .map((filter) => filter.field);
  const searchHides =
    state.query.trim() !== "" && !entryMatchesSearch(entry, state.query, revealed);
  if (searchHides) cleared.push("search");
  return {
    state: { ...state, kind, threads, filters: kept, query: searchHides ? "" : state.query },
    cleared,
  };
}

/** The entry key of a comment posted from this page, once the read includes it. */
export function postedActivityKey(reads: ActivityReads, posted: string): string | null {
  const index = reads.comments.findIndex((comment) => postedCommentKey(comment) === posted);
  return index < 0 ? null : commentActivityIds(reads.comments)[index]!;
}

/** How many entries the Overview shows before "Show N earlier". */
export const ACTIVITY_WINDOW = 10;

export interface ActivityWindow {
  shown: ActivityEntry[];
  /** How many are left out, beyond the window. */
  hidden: number;
  /** Where they'd go: oldest first keeps the newest at the end, so earlier ones go first. */
  hiddenAt: "start" | "end";
}

/**
 * The newest `size` entries in the timeline's order. Newest first and by file cut the end;
 * oldest first cuts the start, so the newest stay beside the comment box either way.
 */
export function activityWindow(
  entries: readonly ActivityEntry[],
  sort: CommentsSort,
  size: number,
): ActivityWindow {
  const hidden = Math.max(0, entries.length - size);
  if (sort === "oldest") return { shown: entries.slice(hidden), hidden, hiddenAt: "start" };
  return { shown: entries.slice(0, entries.length - hidden), hidden, hiddenAt: "end" };
}

/** The window size that includes the entry `key`, at least `size`. */
export function windowIncluding(
  entries: readonly ActivityEntry[],
  sort: CommentsSort,
  size: number,
  key: string,
): number {
  const index = entries.findIndex((entry) => entry.key === key);
  if (index < 0) return size;
  const needed = sort === "oldest" ? entries.length - index : index + 1;
  return Math.max(size, needed);
}

/** The ⋯ menu's filters: kinds and thread states are the pills, so they aren't here. */
export const ACTIVITY_FILTER_FIELDS = [
  "author",
  "file",
  "state",
] as const satisfies readonly CommentFilterField[];

/** How many entries carry each filter value; a push counts under its author. */
export function activityFilterCounts(
  reads: ActivityReads,
): Record<CommentFilterField, Map<string, number>> {
  const entries = activityEntries(reads);
  const counts = filterCounts(
    entries.flatMap((entry): CommentsItem[] => {
      if (entry.kind === "push") return [];
      return [entry.kind === "review" ? reviewItem(entry) : entry];
    }),
  );
  for (const entry of entries) {
    if (entry.kind !== "push" || !entry.event.author) continue;
    counts.author.set(entry.event.author, (counts.author.get(entry.event.author) ?? 0) + 1);
  }
  return counts;
}
