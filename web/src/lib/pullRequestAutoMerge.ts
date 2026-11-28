// Whether a pull request's read says its auto-merge is on, off, or can't say: a read from an
// older shell or host, or one GitHub answered without the field, is unknown, never off.

import type { PullRequestAutoMerge } from "@/newPullRequestPage/hooks/usePullRequests";

export type AutoMergeKnown = "on" | "off" | "unknown";

/** What `pr`'s own read says of its auto-merge; anything but null or an object is unknown. */
export function autoMergeKnown(
  pr: { auto_merge?: PullRequestAutoMerge | null } | null | undefined,
): AutoMergeKnown {
  if (!pr || !Object.hasOwn(pr, "auto_merge")) return "unknown";
  const autoMerge: unknown = pr.auto_merge;
  if (autoMerge === null) return "off";
  return typeof autoMerge === "object" && !Array.isArray(autoMerge) ? "on" : "unknown";
}
