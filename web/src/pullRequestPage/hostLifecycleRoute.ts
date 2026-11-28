// Which of the page's state changes the session's host makes, where this Mac has no route:
// each is offered only where the host lists its method for this caller, so a change the
// caller's level doesn't allow is hidden rather than offered and then refused.

import type { LifecycleStoreOperation } from "@/lib/pullRequestStateHolds";
import type { Method } from "@/lib/workspaceApi";

import {
  type HostChangeRoute,
  hostChangeRoute,
  hostChangeRouteCurrent,
} from "./pullRequestPageRoute";

/**
 * The host methods each state change needs; batch auto-merge stays on this Mac. Turning
 * auto-merge on also reads the merge options, for the methods its dialog offers.
 */
const HOST_METHODS: Partial<Record<LifecycleStoreOperation, readonly Method[]>> = {
  pull_request_close: ["setPullRequestState"],
  pull_request_reopen: ["setPullRequestState"],
  pull_request_convert_to_draft: ["setPullRequestDraft"],
  pull_request_ready_for_review: ["setPullRequestDraft"],
  pull_request_auto_merge_enable: ["enableAutoMerge", "getMergeOptions"],
  pull_request_auto_merge_disable: ["disableAutoMerge"],
};

/** Where `conversationId`'s host makes `operation` now; null where it doesn't serve every method. */
export function hostLifecycleRoute(
  conversationId: string | undefined,
  operation: LifecycleStoreOperation,
): HostChangeRoute | null {
  const methods = HOST_METHODS[operation];
  if (!conversationId || !methods) return null;
  const [first, ...rest] = methods;
  const route = first ? hostChangeRoute(conversationId, first) : null;
  return route && rest.every((method) => hostChangeRoute(conversationId, method)) ? route : null;
}

/** Whether a change captured for `route` would still go there. */
export function hostLifecycleRouteCurrent(
  route: HostChangeRoute,
  operation: LifecycleStoreOperation,
): boolean {
  const methods = HOST_METHODS[operation];
  return !!methods && methods.every((method) => hostChangeRouteCurrent(route, method));
}
