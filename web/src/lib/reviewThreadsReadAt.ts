// When each part of a PR's conversations page was read. Every part the page shows has its own
// entry: the listing, each thread and each comment. A read or write that observed a part dates it
// then; every other part keeps its exact entry; a part no longer shown has none. The page is as
// old as the oldest part it shows.

import type {
  ReviewComment,
  ReviewThread,
  ReviewThreadsPage,
} from "@/lib/pullRequestReviewThreads";

/** One part's read: when it was. */
export interface PartRead {
  at: number;
}
export type PartReads = Readonly<Record<string, PartRead>>;

/** A conversations page with its parts' reads; none where they can't be told. */
export interface TimedThreadsPage extends ReviewThreadsPage {
  reads?: PartReads;
}

/** The observation that these are the conversations, which dates a page that shows none. */
export const LISTING_PART = "listing";
export const threadPart = (threadId: string) => `thread:${threadId}`;
export const commentPart = (threadId: string, commentId: string) =>
  `comment:${threadId}/${commentId}`;

const NO_PARTS: ReadonlySet<string> = new Set();

/** Every part `page` shows: its listing, each thread and each of their comments. */
export function presentParts(page: ReviewThreadsPage): ReadonlySet<string> {
  const parts = new Set([LISTING_PART]);
  for (const thread of page.data) {
    parts.add(threadPart(thread.id));
    for (const comment of thread.comments.data) parts.add(commentPart(thread.id, comment.id));
  }
  return parts;
}

const isReadTime = (time: unknown): time is number =>
  typeof time === "number" && Number.isFinite(time) && time > 0;

/** Whether `page`'s reads date exactly the parts it shows, every one of them. */
export function readsComplete(page: TimedThreadsPage): page is TimedThreadsPage & {
  reads: PartReads;
} {
  const { reads } = page;
  if (!reads) return false;
  const parts = presentParts(page);
  const keys = Object.keys(reads);
  return (
    keys.length === parts.size && keys.every((key) => parts.has(key) && isReadTime(reads[key]?.at))
  );
}

/** The parts age goes by: an empty page's listing, otherwise its threads and comments. */
function countedParts(page: ReviewThreadsPage): string[] {
  const parts = [...presentParts(page)];
  return page.data.length === 0 ? [LISTING_PART] : parts.filter((part) => part !== LISTING_PART);
}

/** When the oldest part `page` shows was read; undefined where its reads can't tell. */
export function oldestReadAt(page: TimedThreadsPage): number | undefined {
  if (!readsComplete(page)) return undefined;
  return Math.min(...countedParts(page).map((part) => page.reads[part]!.at));
}

/**
 * The reads of `page` after a read or write observed `observed` at `now`: those parts were read
 * then; every other part it shows keeps its exact entry from `previous`; parts it no longer shows
 * have none. A part neither observed nor dated before leaves the reads unknown, so nothing dates
 * the page by them.
 */
export function readsAfter(
  previous: PartReads | undefined,
  page: ReviewThreadsPage,
  observed: ReadonlySet<string>,
  now: number,
): PartReads | undefined {
  const reads: Record<string, PartRead> = {};
  for (const part of presentParts(page)) {
    const known = previous?.[part];
    if (observed.has(part)) reads[part] = { at: now };
    else if (known) reads[part] = known;
    else return undefined;
  }
  return reads;
}

/** `next`, changed from `page`, with its reads after observing `observed` at `now`. */
export function pageAfter<Page extends ReviewThreadsPage>(
  page: Pick<TimedThreadsPage, "reads">,
  next: Page,
  observed: ReadonlySet<string> = NO_PARTS,
  now: number = Date.now(),
): Page & TimedThreadsPage {
  const { reads: _previous, ...rest } = next as Page & TimedThreadsPage;
  const reads = readsAfter(page.reads, next, observed, now);
  return (reads ? { ...rest, reads } : rest) as Page & TimedThreadsPage;
}

/** What a first page observed: its threads and comments, and the listing unless it warned. */
export function firstPageObserved(firstPage: ReviewThreadsPage): ReadonlySet<string> {
  const parts = threadsObserved(firstPage.data);
  if (!firstPage.warning) parts.add(LISTING_PART);
  return parts;
}

/** What a read or write that answered with whole threads observed. */
export function threadsObserved(threads: readonly ReviewThread[]): Set<string> {
  const parts = new Set<string>();
  for (const thread of threads) {
    parts.add(threadPart(thread.id));
    for (const comment of thread.comments.data) parts.add(commentPart(thread.id, comment.id));
  }
  return parts;
}

/** What a page of one thread's replies observed. */
export function commentsObserved(
  threadId: string,
  comments: readonly ReviewComment[],
): ReadonlySet<string> {
  return new Set(comments.map((comment) => commentPart(threadId, comment.id)));
}
