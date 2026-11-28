// Where a change to a pull request was started from, captured when it's sent. The Pull
// Requests page provides one, so the changes made there, and only those, update its lists
// and its Recently reviewed history, even if they finish after their control unmounted.

import { createContext, useContext } from "react";

/**
 * A change that takes a pull request out of the page's lists once GitHub made it, or, for
 * `reopened`, brings back one the user closed.
 */
export type PullRequestOwnAction =
  | "approved"
  | "changes_requested"
  | "commented"
  | "removed_you"
  | "merged"
  | "closed"
  | "reopened"
  | "converted_to_draft"
  | "marked_ready";

/** How a sent change reports its outcome, bound to the page and account it was sent from. */
export interface PullRequestActionReport {
  /**
   * GitHub made it. Ignored unless the page, workspace user and Mac account generation it
   * was sent as are still the current ones, so a late answer can't change a later view.
   */
  succeeded: (action: PullRequestOwnAction) => void;
  /**
   * A fresh read as the same login, in the same page scope, showed GitHub made it, after its
   * send couldn't say. That login may have signed in again since; this grants nothing to send.
   */
  confirmedByRead?: (action: PullRequestOwnAction) => void;
}

export interface PullRequestActionOrigin {
  /**
   * A change that may take `prUrl` out of the page's lists is being sent as `actor`; a review
   * names the head it was submitted against.
   */
  begin: (
    prUrl: string,
    actor: { login: string; generation: number },
    sent?: PullRequestActionSent,
  ) => PullRequestActionReport;
}

export interface PullRequestActionSent {
  headSha?: string | null;
  /** This PR's own change, from its row or its open view, so the page shows where its row went. */
  landing?: PullRequestOwnAction;
}

/** `origin` for one PR's own `action`, whose row's landing the page shows; bulk runs don't. */
export function withRowLanding(
  origin: PullRequestActionOrigin | null,
  action: PullRequestOwnAction,
): PullRequestActionOrigin | null {
  if (!origin) return null;
  return {
    begin: (prUrl, actor, sent) => origin.begin(prUrl, actor, { ...sent, landing: action }),
  };
}

export const PullRequestActionOriginContext = createContext<PullRequestActionOrigin | null>(null);

/** The page's origin where a control renders on it; null elsewhere, e.g. in a workspace. */
export function usePullRequestActionOrigin(): PullRequestActionOrigin | null {
  return useContext(PullRequestActionOriginContext);
}
