// Which of the page's state changes the session's host makes, where this Mac has no route:
// each is offered only where the host lists its method for this caller, so a change the
// caller's level doesn't allow is hidden rather than offered and then refused.

import type { LifecycleStoreOperation } from "@/lib/desktopPullRequestWrites";
import type { Method } from "@/lib/workspaceApi";

import {
  type HostChangeRoute,
  hostChangeRoute,
  hostChangeRouteCurrent,
} from "./pullRequestPageRoute";

/** The host's method for each state change; batch auto-merge stays on this Mac. */
const HOST_METHODS: Partial<Record<LifecycleStoreOperation, Method>> = {
  pull_request_close: "setPullRequestState",
  pull_request_reopen: "setPullRequestState",
  pull_request_convert_to_draft: "setPullRequestDraft",
  pull_request_ready_for_review: "setPullRequestDraft",
  pull_request_auto_merge_enable: "enableAutoMerge",
  pull_request_auto_merge_disable: "disableAutoMerge",
};

/** Where `conversationId`'s host makes `operation` now; null where it doesn't. */
export function hostLifecycleRoute(
  conversationId: string | undefined,
  operation: LifecycleStoreOperation,
): HostChangeRoute | null {
  const method = HOST_METHODS[operation];
  return conversationId && method ? hostChangeRoute(conversationId, method) : null;
}

/** Whether a change captured for `route` would still go there. */
export function hostLifecycleRouteCurrent(
  route: HostChangeRoute,
  operation: LifecycleStoreOperation,
): boolean {
  const method = HOST_METHODS[operation];
  return !!method && hostChangeRouteCurrent(route, method);
}
