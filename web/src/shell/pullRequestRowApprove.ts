// Approve on a list row: a pull request that asks the account the session's host reviews as,
// not its author, is approved at once, with no comment, through the host's SubmitReviewVerdict.

import type { QueryClient } from "@tanstack/react-query";

import type { PullRequestActionOrigin } from "@/lib/pullRequestActionOrigin";
import type { PullRequest } from "@/newPullRequestPage/hooks/usePullRequests";
import { hostReviewRoute } from "@/pullRequestReviews/hostReviews";
import { reviewSubmitKey } from "@/pullRequestReviews/reviewSubmit";
import { sendReviewOnHost } from "@/pullRequestReviews/sendReview";

import { reviewRequestedFrom } from "./pullRequestListModel";

/**
 * The login a row's Approve goes as: the host's account for an open PR at a known head that
 * asks it, not its author, for a review. Null where the row offers no Approve.
 */
export function rowApproveLogin(
  pr: Pick<PullRequest, "state" | "author" | "reviewers" | "head_sha">,
  hostLogin: string | null | undefined,
): string | null {
  if (!hostLogin || pr.state?.toUpperCase() !== "OPEN" || !pr.head_sha) return null;
  if (pr.author?.toLowerCase() === hostLogin.toLowerCase()) return null;
  return reviewRequestedFrom(pr, hostLogin) ? hostLogin : null;
}

/** Approve `prUrl` at `headSha` through the session's host; false where nothing was sent. */
export function approveRowOnHost(options: {
  conversationId: string;
  owner: string;
  prUrl: string;
  headSha: string;
  login: string;
  origin: PullRequestActionOrigin | null;
  queryClient: QueryClient;
  onRefused: (message: string) => void;
}): boolean {
  const route = hostReviewRoute(options.conversationId, options.prUrl, options.login);
  if (!route) return false;
  return sendReviewOnHost({
    key: reviewSubmitKey(options.owner, options.prUrl, options.login),
    route,
    prUrl: options.prUrl,
    event: "APPROVE",
    body: "",
    headSha: options.headSha,
    priorState: null,
    origin: options.origin,
    queryClient: options.queryClient,
    onRefused: (_code, message) => options.onRefused(message),
  });
}
