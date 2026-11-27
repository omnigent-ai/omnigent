// Reviews sent through the session's host, where this Mac can't send them: a comment review at
// EDIT, approve and request changes for the owner. The host answers without the review's id, so
// each send keeps the account's state before it, for Check Again (`hostReviewCheck.ts`).

import { type CodeHostPullRequest, parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import { hostWrite } from "@/lib/hostPullRequestWrites";

import type { ReviewEvent, SentReview } from "./reviewSubmit";

/** The session's host as the account a review is sent as. */
export interface HostReviewRoute {
  conversationId: string;
  target: CodeHostPullRequest;
  /** The account the host reports for the PR; an empty login where it names none. */
  actor: { login: string; generation: 0 };
}

/** The host route for `prUrl`, or null for a link that isn't a GitHub pull request. */
export function hostReviewRoute(
  conversationId: string,
  prUrl: string,
  account: string | null | undefined,
): HostReviewRoute | null {
  const target = parseCodeHostPullRequestUrl(prUrl);
  if (target?.provider !== "github") return null;
  return { conversationId, target, actor: { login: account ?? "", generation: 0 } };
}

/** What a host submit said it did, naming the account where the host reported one. */
export function hostReviewSubmittedMessage(
  event: ReviewEvent,
  where: string,
  login: string,
): string {
  const as = login ? ` as @${login}` : "";
  if (event === "APPROVE") return `Approved ${where}${as}.`;
  if (event === "REQUEST_CHANGES") return `Requested changes on ${where}${as}.`;
  return `Submitted your review of ${where}${as}.`;
}

const parseReviewAnswer = (answer: unknown) =>
  typeof answer === "object" &&
  answer !== null &&
  (answer as { object?: unknown }).object === "session.github.pr_review"
    ? answer
    : null;

/**
 * One review through the host, pinned to `headSha`: a comment review or a verdict, or the
 * pending review `pendingReviewId` finished with either.
 */
export function submitReviewOnHost(
  conversationId: string,
  review: {
    prUrl: string;
    event: ReviewEvent;
    body: string;
    headSha: string;
    pendingReviewId?: string;
  },
): Promise<unknown> {
  const request = {
    prUrl: review.prUrl,
    event: review.event,
    body: review.body,
    commitOid: review.headSha,
  };
  const { pendingReviewId } = review;
  return hostWrite(
    conversationId,
    pendingReviewId
      ? {
          method: "submitPendingReview",
          call: (client) => client.submitPendingReview({ ...request, pendingReviewId }),
        }
      : review.event === "COMMENT"
        ? { method: "submitReview", call: (client) => client.submitReview(request) }
        : {
            method: "submitReviewVerdict",
            call: (client) => client.submitReviewVerdict(request),
          },
    parseReviewAnswer,
  );
}

/** The account's state before each unconfirmed send, by attempt; gone after a reload. */
const baselines = new Map<string, string | null>();

export function rememberHostReviewBaseline(sent: SentReview, priorState: string | null): void {
  if (sent.attemptId) baselines.set(sent.attemptId, priorState);
}

/** The state recorded before `sent`, or undefined where none was (as after a reload). */
export function hostReviewBaseline(sent: SentReview): string | null | undefined {
  return sent.attemptId ? baselines.get(sent.attemptId) : undefined;
}

export function forgetHostReviewBaseline(sent: SentReview): void {
  if (sent.attemptId) baselines.delete(sent.attemptId);
}

export function resetHostReviewsForTests(): void {
  baselines.clear();
}
