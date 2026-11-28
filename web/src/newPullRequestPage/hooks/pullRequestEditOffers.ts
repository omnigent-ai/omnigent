// Which of a PR's optional edits a view offers: its read says GitHub allows the edit, and the
// session's host serves the methods it needs, the read that fills its control and the write.
// An older or partial host that serves the page but not these offers none of them.

import { useMemo } from "react";

import { usePullRequestMethodServed } from "@/pullRequestPage/pullRequestPageRoute";

import { type PullRequestCapabilities, pullRequestEditCapabilities } from "./usePullRequests";

/** Whether the session's host serves each optional edit: labels, reviewers and merging. */
export interface PullRequestEditsServed {
  labels: boolean;
  reviewers: boolean;
  merging: boolean;
}

/** Which optional edits `sessionId`'s host serves every method of. */
export function usePullRequestEditsServed(sessionId: string): PullRequestEditsServed {
  // Each method is asked for on every render, so the hooks keep their order.
  const listsLabels = usePullRequestMethodServed(sessionId, "listLabels");
  const updatesLabels = usePullRequestMethodServed(sessionId, "updateLabels");
  const listsCandidates = usePullRequestMethodServed(sessionId, "listReviewerCandidates");
  const updatesReviewers = usePullRequestMethodServed(sessionId, "updateReviewers");
  const readsMergeOptions = usePullRequestMethodServed(sessionId, "getMergeOptions");
  const merges = usePullRequestMethodServed(sessionId, "mergePullRequest");
  const labels = listsLabels && updatesLabels;
  const reviewers = listsCandidates && updatesReviewers;
  const merging = readsMergeOptions && merges;
  // One identity while the answers hold, so callbacks built on it stay put.
  return useMemo(() => ({ labels, reviewers, merging }), [labels, reviewers, merging]);
}

/** The edits a PR's read allows, less those its session's host doesn't serve. */
export function offeredEditCapabilities(
  info: { capabilities: PullRequestCapabilities },
  served: PullRequestEditsServed,
): ReturnType<typeof pullRequestEditCapabilities> {
  const allowed = pullRequestEditCapabilities(info);
  return {
    ...allowed,
    labels: allowed.labels && served.labels,
    reviewers: allowed.reviewers && served.reviewers,
    merging: allowed.merging && served.merging,
  };
}
