// When a pull request's details read its review threads: only while something shown lists
// them, since each read asks the host for every thread and its first replies.

import type { PullRequestSubtab } from "@/shell/pullRequestPanelView";

export interface ReviewThreadReadInputs {
  /** The details are on screen, not a hidden tab. */
  active: boolean;
  tab: PullRequestSubtab;
  /** The new pull request page, whose Overview and Comments list threads too. */
  newPage: boolean;
  /** The host or this Mac can read them. */
  readable: boolean;
  hasPr: boolean;
}

/** Changes always lists threads; on the new page, so do Overview's activity and Comments. */
export function reviewThreadsWanted({
  active,
  tab,
  newPage,
  readable,
  hasPr,
}: ReviewThreadReadInputs): boolean {
  if (!active || !readable || !hasPr) return false;
  if (tab === "changes") return true;
  return newPage && (tab === "summary" || tab === "comments");
}
