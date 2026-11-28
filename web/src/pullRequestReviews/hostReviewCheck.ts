// Check Again for a review sent through the session's host whose answer was lost. The host
// doesn't say which review it made, so this reads the PR's reviewers through the host and settles
// the review only when the account's latest state changed to the review's own since the send.

import type { QueryClient } from "@tanstack/react-query";

import { showToast } from "@/components/ui/toast";
import { invalidateSessionPullRequest } from "@/lib/pullRequestRecord";
import { readPullRequestRpc } from "@/lib/pullRequestRpc";

import { forgetHostReviewBaseline, type HostReviewRoute, hostReviewBaseline } from "./hostReviews";
import {
  EMPTY_REVIEW,
  normalizedBody,
  readReviewSubmit,
  SUBMITTED_STATE,
  sameAttempt,
  updateReviewSubmit,
} from "./reviewSubmit";
import { reportReviewLandedByRead } from "./sendReview";

interface ReviewerState {
  login?: unknown;
  kind?: unknown;
  state?: unknown;
}

/** `login`'s latest review state on the PR as the host reads it now; undefined if unreadable. */
async function hostReviewerState(
  conversationId: string,
  prUrl: string,
  login: string,
): Promise<string | null | undefined> {
  try {
    const info = await readPullRequestRpc<{ pr?: { reviewers?: ReviewerState[] } | null }>(
      conversationId,
      "getPullRequest",
      (client) => client.getPullRequest({ prUrl }),
    );
    const reviewers = info.pr?.reviewers;
    if (!Array.isArray(reviewers)) return undefined;
    const own = reviewers.find(
      (reviewer) =>
        reviewer.kind === "user" &&
        typeof reviewer.login === "string" &&
        reviewer.login.toLowerCase() === login.toLowerCase(),
    );
    return typeof own?.state === "string" ? own.state : null;
  } catch {
    return undefined;
  }
}

export type HostReviewCheck = "landed" | "unreadable" | "not_found" | "ambiguous" | "unidentified";

/**
 * Check Again for the unconfirmed host review in `key`. It never sends the review again: it
 * settles once the account's latest state is the review's and wasn't before the send, and
 * otherwise keeps it held, saying why.
 */
export async function checkHostReviewAgain({
  key,
  route,
  prUrl,
  queryClient,
  showsHere,
}: {
  key: string;
  route: HostReviewRoute;
  prUrl: string;
  queryClient: QueryClient;
  showsHere?: () => boolean;
}): Promise<HostReviewCheck | null> {
  const status = readReviewSubmit(key).status;
  if (status.kind !== "uncertain" || status.checking) return null;
  const { sent } = status;
  const settle = (checked: Exclude<HostReviewCheck, "landed">) =>
    updateReviewSubmit(key, (stored) =>
      stored.status.kind === "uncertain" && sameAttempt(stored.status.sent, sent)
        ? { ...stored, status: { ...stored.status, checking: false, checked } }
        : stored,
    );
  updateReviewSubmit(key, (stored) =>
    stored.status.kind === "uncertain" && sameAttempt(stored.status.sent, sent)
      ? { ...stored, status: { ...stored.status, checking: true } }
      : stored,
  );
  const login = route.actor.login;
  const state = login ? await hostReviewerState(route.conversationId, prUrl, login) : undefined;
  const now = readReviewSubmit(key).status;
  if (now.kind !== "uncertain" || !sameAttempt(now.sent, sent)) return null;
  if (state === undefined) {
    settle("unreadable");
    return "unreadable";
  }
  if (state !== SUBMITTED_STATE[sent.event]) {
    settle("not_found");
    return "not_found";
  }
  const before = hostReviewBaseline(sent);
  // The same state before the send, or unknown since a reload: an earlier review could be it.
  if (before === undefined || before === state) {
    settle("ambiguous");
    return "ambiguous";
  }
  // The host names no review, so a summary can't be matched to this one; keep it held.
  if (normalizedBody(sent.body) !== "") {
    settle("unidentified");
    return "unidentified";
  }
  forgetHostReviewBaseline(sent);
  updateReviewSubmit(key, () => EMPTY_REVIEW);
  reportReviewLandedByRead(key, sent.event);
  if (!showsHere || showsHere()) showToast("Your review is on GitHub.");
  void invalidateSessionPullRequest(queryClient, route.conversationId, prUrl, [
    "pr-page-github-info",
  ]);
  return "landed";
}
