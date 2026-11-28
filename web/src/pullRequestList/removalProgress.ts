// A session's removal of linked PRs while it's under way, and what every entry point that can't
// remove meanwhile says: the group header, a row's menu, the selection bar, ⌘K and the PR's menu.

import { pullRequestKey } from "@/lib/pullRequestReference";

import { stopBulkRun } from "./bulkRuns";

/**
 * The removal a session is waiting on: the one running, or else the first one queued behind
 * another session's. Absent once every removal for the session has settled.
 */
export interface RemovalProgress {
  /** `waiting` while it's queued behind another removal and hasn't sent anything yet. */
  phase: "removing" | "waiting";
  /** The viewed session's rows it removes, by {@link pullRequestKey}. */
  urls: ReadonlySet<string>;
  /** How many of its PRs have ended, whatever their outcome; null while it's waiting. */
  done: number | null;
  total: number;
  /** Whether it can be stopped now, was already asked to stop, or has no Stop at all. */
  stop: "available" | "requested" | "none";
  /** The run that Stop stops, while it's running as one. */
  runId: string | null;
}

/** What a busy Remove control says it does while the removal under way can be stopped. */
export const STOP_REMOVING_LABEL = "Stop removing";

/** Why removing can't start now, where nothing more is known about the removal under way. */
export const REMOVAL_PENDING_REASON = "Wait for the pull request being removed to finish.";

const pullRequests = (count: number) => `${count} pull request${count === 1 ? "" : "s"}`;

/** Whether `url` is one of the PRs the removal under way is removing, not just waiting on. */
export function isBeingRemoved(progress: RemovalProgress | null, url: string): boolean {
  return progress?.phase === "removing" && progress.urls.has(pullRequestKey(url));
}

/** Whether any of `urls` is in the removal, running or queued. */
export function removalIncludes(progress: RemovalProgress | null, urls: readonly string[]) {
  return !!progress && urls.some((url) => progress.urls.has(pullRequestKey(url)));
}

/**
 * Why removing can't start while `progress` is under way. `inRun` says whether what the user
 * would remove is part of it, which says how far it got rather than asking them to wait.
 */
export function removalPendingReason(progress: RemovalProgress | null, inRun: boolean): string {
  if (!progress) return REMOVAL_PENDING_REASON;
  if (progress.phase === "waiting") return "Waiting for another removal to finish.";
  if (progress.stop === "requested") return "Stopping after the requests already sent…";
  if (inRun) {
    return progress.total === 1
      ? "Removing this pull request…"
      : `Removing ${progress.done ?? 0} of ${pullRequests(progress.total)}…`;
  }
  return progress.total === 1
    ? "Wait for the pull request being removed to finish."
    : `Wait for the ${pullRequests(progress.total)} being removed to finish.`;
}

/** Stop the removal under way, where it can be stopped now; null where it can't. */
export function removalStopper(progress: RemovalProgress | null): (() => void) | null {
  const runId = progress?.runId ?? null;
  if (progress?.phase !== "removing" || progress.stop !== "available" || runId === null) {
    return null;
  }
  return () => stopBulkRun(runId);
}

/** A busy control's own short label, such as "Removing 3 of 8", where it stood for Remove. */
export function removalBusyLabel(progress: RemovalProgress): string {
  if (progress.phase === "waiting") return "Waiting…";
  if (progress.stop === "requested") return "Stopping…";
  return progress.total === 1 ? "Removing…" : `Removing ${progress.done ?? 0} of ${progress.total}`;
}
