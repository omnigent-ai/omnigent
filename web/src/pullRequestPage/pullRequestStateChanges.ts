// Close and Reopen Pull Request, defined once for every place that offers them: the comment
// box, a list row's menus and ⌘K. Each surface reads its labels, when it applies and why it's
// held from here, so they can't drift.

import type { StateChangeOperation } from "@/lib/pullRequestStateHolds";

import type { StateChainState } from "./pullRequestStateChain";

export interface StateChangeDefinition {
  operation: StateChangeOperation;
  /** The comment box's button, which acts at once. */
  label: string;
  /** A row menu's item; Close asks first, so it ends with an ellipsis. */
  menuLabel: string;
  /** ⌘K's row, which names the pull request, e.g. "Close #12 Fix the build…". */
  paletteLabel: (name: string) => string;
  /** Where this Mac can't make it: the item that opens the pull request on GitHub instead. */
  githubLabel: string;
  /** A row asks before it sends: closing takes it off reviewers' lists. */
  confirms: boolean;
  /** Said while it's on its way to GitHub, e.g. by the header's state pill. */
  running: string;
  /** Said beside the state pill when GitHub refused it or its outcome is unknown. */
  failed: string;
  keywords: readonly string[];
}

export const STATE_CHANGES: Record<StateChangeOperation, StateChangeDefinition> = {
  pull_request_close: {
    operation: "pull_request_close",
    label: "Close Pull Request",
    menuLabel: "Close Pull Request",
    paletteLabel: (name) => `Close ${name}`,
    githubLabel: "Close on GitHub",
    confirms: false,
    running: "Closing…",
    failed: "Couldn’t close the pull request",
    keywords: ["close", "state", "pull request", "pr"],
  },
  pull_request_reopen: {
    operation: "pull_request_reopen",
    label: "Reopen Pull Request",
    menuLabel: "Reopen Pull Request",
    paletteLabel: (name) => `Reopen ${name}`,
    githubLabel: "Reopen on GitHub",
    confirms: false,
    running: "Reopening…",
    failed: "Couldn’t reopen the pull request",
    keywords: ["reopen", "open", "state", "pull request", "pr"],
  },
};

/**
 * The change a pull request in `prState` offers: Close while it's open (drafts included, as
 * GitHub calls a draft open), Reopen once closed; nothing once merged or while unread.
 */
export function stateChangeOperationFor(
  prState: string | null | undefined,
): StateChangeOperation | null {
  const state = prState?.toUpperCase();
  return state === "OPEN"
    ? "pull_request_close"
    : state === "CLOSED"
      ? "pull_request_reopen"
      : null;
}

export const STATE_CHANGE_BUSY_REASON = "Wait for the last change to this pull request to finish.";
export const STATE_CHANGE_UNKNOWN_REASON =
  "GitHub may already have changed this pull request. Open it and choose Check Again.";

/** Why Close or Reopen can't start now, or null: one is running, or an outcome is unknown. */
export function stateChangeHeldReason(
  chain: StateChainState,
  lifecycleHeld: boolean,
): string | null {
  if (chain.notice?.outcome.kind === "unknown") return STATE_CHANGE_UNKNOWN_REASON;
  if (chain.running || lifecycleHeld) return STATE_CHANGE_BUSY_REASON;
  return null;
}
