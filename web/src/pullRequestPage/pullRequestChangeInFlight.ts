// Which state change GitHub is making to a pull request now, wherever it was asked for: read
// from the two stores that run them (close and reopen; draft, ready and auto-merge), so a row,
// a bulk action and the PR page all show the same thing, and it outlives what started it.

import type { LifecycleStoreOperation } from "@/lib/pullRequestStateHolds";

import { LIFECYCLE_ACTIONS, lifecycleActionState } from "./pullRequestLifecycleActions";
import { stateChainState } from "./pullRequestStateChain";

/** A change in flight, and what a row says while it runs. */
export interface PullRequestChangeInFlight {
  operation: LifecycleStoreOperation;
  /** e.g. "Closing…", shown beside the title where motion is reduced. */
  label: string;
}

const LABELS: Record<LifecycleStoreOperation, string> = {
  pull_request_close: "Closing…",
  pull_request_reopen: "Reopening…",
  pull_request_ready_for_review: "Marking ready…",
  pull_request_convert_to_draft: "Converting…",
  pull_request_auto_merge_enable: "Turning on auto-merge…",
  pull_request_auto_merge_enable_many: "Turning on auto-merge…",
  pull_request_auto_merge_disable: "Turning off auto-merge…",
};

/** The change running for `key` (`stateChainKey(owner, prUrl)`), or null where none is. */
export function pullRequestChangeInFlight(key: string): PullRequestChangeInFlight | null {
  const chain = stateChainState(key);
  if (chain.running) return { operation: chain.running, label: LABELS[chain.running] };
  const running = lifecycleActionState(key).running;
  if (!running) return null;
  const operation = LIFECYCLE_ACTIONS[running].operation;
  return { operation, label: LABELS[operation] };
}
