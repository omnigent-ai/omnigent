// Pings to requested reviewers, from the registered reviewer pings integration. Without one,
// no reviewer offers a ping.

import {
  pullRequestIntegrations,
  type ReviewerPingAction,
  type ReviewerPingPullRequest,
  type ReviewerPingSurface,
} from "@/lib/pullRequestIntegrations";

export type { ReviewerPingAction, ReviewerPingPullRequest, ReviewerPingSurface };

const NO_PINGS: ReadonlyMap<string, ReviewerPingAction> = new Map();

function useNoReviewerPings(): ReadonlyMap<string, ReviewerPingAction> {
  return NO_PINGS;
}

/** The ping for each pingable reviewer on `pr`, keyed by login in lower case. */
export function useReviewerPingActions(
  pr: ReviewerPingPullRequest | null,
  surface: ReviewerPingSurface,
): ReadonlyMap<string, ReviewerPingAction> {
  const useActions = pullRequestIntegrations().reviewerPings?.useActions ?? useNoReviewerPings;
  return useActions(pr, surface);
}

/** The heading over a menu's pings, from the registered integration. */
export function reviewerPingSectionLabel(): string {
  return pullRequestIntegrations().reviewerPings?.sectionLabel ?? "Ping reviewers";
}

/** ⌘K's ping rows for the pull request in view, while it shows. */
export function ReviewerPingPaletteCommands(props: {
  pr: ReviewerPingPullRequest | null;
  active: boolean;
}) {
  const PaletteCommands = pullRequestIntegrations().reviewerPings?.PaletteCommands;
  return PaletteCommands ? <PaletteCommands {...props} /> : null;
}
