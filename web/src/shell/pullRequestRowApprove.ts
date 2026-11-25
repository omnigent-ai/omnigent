// Whether a list row offers Approve: the pull request asks the account the row's change would
// be made as for a review, and this Mac submits reviews as it. That's the list's own login where
// it reads the row's host by name, else gh's active account.

import type { PullRequest } from "@/newPullRequestPage/hooks/usePullRequests";
import {
  macReviewWriteOffers,
  macRouteActsAsReadAs,
  macWriteRoute,
} from "@/lib/desktopPullRequestWrites";
import type { MacNamedReader } from "@/lib/macNamedReads";

import { reviewRequestedFrom } from "./pullRequestListModel";

/**
 * True for an open pull request at `url` that asks the account its review would be made as, not
 * its author, to review. Where the list reads by name and this Mac can't submit as that login, an
 * older shell for one, there's no such account and nothing is offered.
 */
export function rowOffersApprove(
  url: string,
  pr: Pick<PullRequest, "state" | "author" | "reviewers">,
  readAs: MacNamedReader | null,
): boolean {
  if (pr.state?.toUpperCase() !== "OPEN") return false;
  const route = macWriteRoute(url, readAs);
  if (!route || !macReviewWriteOffers(route, "pull_request_review_submit")) return false;
  const login = route.actor.login;
  // A list reading this row's host by name approves only as that login; another host, as gh's.
  if (!macRouteActsAsReadAs(route, readAs)) return false;
  return pr.author?.toLowerCase() !== login.toLowerCase() && reviewRequestedFrom(pr, login);
}
