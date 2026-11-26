/**
 * Where a file link opens while a diff review is on screen, in either workspace design: it
 * stays in the review, at the file and line, when the review includes the file; otherwise it
 * opens the file itself, at the line.
 */

/** The files a review on screen shows, once its comparison is known. */
export type ShownReviewFiles =
  | { readonly state: "listed"; readonly paths: readonly string[] }
  /** Its comparison is still being worked out; the files follow. */
  | { readonly state: "pending" }
  /** It can't compare, so it shows no files. */
  | { readonly state: "unavailable" };

/** Whether a review includes a file; unknown until its comparison is known. */
export type ReviewMembership = "included" | "excluded" | "unknown";

/** A session's changed files as a review shows them; pending until its comparison and list load. */
export function changedReviewFiles(changes: {
  comparison: { base: string };
  data?: { data: readonly { path: string }[] };
  isError?: boolean;
}): ShownReviewFiles {
  const base = changes.comparison.base;
  if (base === "unavailable") return { state: "unavailable" };
  if (changes.data) return { state: "listed", paths: changes.data.data.map((file) => file.path) };
  // A list that failed to load shows no files; one still loading follows.
  if (changes.isError && base !== "pending") return { state: "unavailable" };
  return { state: "pending" };
}

/** Whether `review` includes `path`. */
export function reviewMembership(review: ShownReviewFiles, path: string): ReviewMembership {
  if (review.state === "pending") return "unknown";
  if (review.state === "unavailable") return "excluded";
  return review.paths.includes(path) ? "included" : "excluded";
}

type Listener = () => void;
// The review each session's workspace shows right now, published while it's on screen.
const shownReviews = new Map<string, { owner: object; files: ShownReviewFiles }>();
const listeners = new Set<Listener>();

// A review replaces its publication by withdrawing it and publishing again in one commit,
// so listeners hear once that commit is done, never about the gap between.
let notifying = false;
function changed() {
  if (notifying) return;
  notifying = true;
  queueMicrotask(() => {
    notifying = false;
    for (const listener of [...listeners]) listener();
  });
}

/** Publish `files` as the review `owner` shows for `sessionId`; returns its withdrawal. */
export function publishShownReview(
  sessionId: string,
  owner: object,
  files: ShownReviewFiles,
): () => void {
  shownReviews.set(sessionId, { owner, files });
  changed();
  return () => {
    if (shownReviews.get(sessionId)?.owner !== owner) return;
    shownReviews.delete(sessionId);
    changed();
  };
}

/** The review on screen for `sessionId`, or null when none is. */
export function shownReview(sessionId: string): ShownReviewFiles | null {
  return shownReviews.get(sessionId)?.files ?? null;
}

/** How long a link waits for a review's comparison before it opens the file itself. */
export const REVIEW_SETTLE_TIMEOUT_MS = 15_000;

/**
 * The review on screen for `sessionId` once its comparison is known: at once when it is,
 * otherwise when it settles or leaves the screen, and null after `timeoutMs`.
 */
export function settledShownReview(
  sessionId: string,
  timeoutMs = REVIEW_SETTLE_TIMEOUT_MS,
): Promise<ShownReviewFiles | null> {
  const current = shownReview(sessionId);
  if (current?.state !== "pending") return Promise.resolve(current);
  return new Promise((resolve) => {
    const finish = (review: ShownReviewFiles | null) => {
      listeners.delete(check);
      clearTimeout(timer);
      resolve(review);
    };
    const check = () => {
      const review = shownReview(sessionId);
      if (review?.state !== "pending") finish(review);
    };
    const timer = setTimeout(() => finish(null), timeoutMs);
    listeners.add(check);
  });
}

/** Forget every published review, between tests. */
export function resetShownReviewsForTests() {
  shownReviews.clear();
  listeners.clear();
  notifying = false;
}
