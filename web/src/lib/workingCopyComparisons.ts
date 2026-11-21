// The document's one owner of branch-point comparisons, wired to the session's
// host, the saved base, and the signed-in user, plus the one way to say a
// session's changes may be out of date.

import type { QueryClient } from "@tanstack/react-query";

import {
  clearCustomComparison,
  readCustomComparison,
  subscribeCustomComparisons,
  writeCustomComparison,
} from "@/lib/customComparison";
import { BRANCH_COMMITS_KEY } from "@/lib/branchCommits";
import { getCurrentUserId, subscribeIdentity } from "@/lib/identity";
import {
  fetchBranchPointListing,
  forgetListedComparisons,
  type WorkspaceGitRoute,
} from "@/lib/workingCopyComparison";
import {
  type ComparisonOwnerDependencies,
  WorkingCopyComparisonOwner,
} from "@/lib/workingCopyComparisonOwner";
import {
  readWorkingCopyBaseIntent,
  subscribeWorkingCopyBaseIntent,
} from "@/lib/workingCopyBasePreference";
import { workspaceSidebarSource } from "@/lib/workspaceSidebarSource";

/** The runner's last-commit list for a session; branch-point lists add a suffix. */
export const WORKING_COPY_CHANGES_KEY = "workspace-changed-files";

function subscribeAttention(listener: () => void): () => void {
  const onVisibility = () => listener();
  window.addEventListener("focus", listener);
  document.addEventListener("visibilitychange", onVisibility);
  return () => {
    window.removeEventListener("focus", listener);
    document.removeEventListener("visibilitychange", onVisibility);
  };
}

/**
 * Asks whether `sessionId`'s host serves comparisons, and again on `ask`.
 * `onRoute` hears each answer; an answer to an older ask is never reported.
 */
export function watchWorkspaceGitRoute(
  { sessionId }: { sessionId: string },
  onRoute: (route: WorkspaceGitRoute) => void,
): { stop: () => void; ask: () => void } {
  let current: AbortController | null = null;
  const ask = () => {
    current?.abort();
    const controller = new AbortController();
    current = controller;
    workspaceSidebarSource()
      .servesGitComparisons(sessionId, controller.signal)
      .then(
        (served) => {
          if (current === controller) onRoute({ state: served ? "served" : "unserved" });
        },
        (error: unknown) => {
          if (current !== controller || controller.signal.aborted) return;
          const message = error instanceof Error && error.message ? error.message : "";
          onRoute({
            state: "unreachable",
            message: message || "Couldn't ask this session's host.",
          });
        },
      );
  };
  ask();
  return {
    stop: () => {
      current?.abort();
      current = null;
    },
    ask,
  };
}

/**
 * An owner wired to the session's host, whose typed comparisons come from
 * `customComparisons`. It follows the saved base, unless `onlyTyped` keeps it to
 * its typed comparison and lists nothing else. Another signed-in user never sees
 * its comparisons or choices; `dispose` stops it and its identity watch.
 */
export function createComparisonOwner({
  customComparisons,
  onlyTyped = false,
}: {
  customComparisons: ComparisonOwnerDependencies["customComparisons"];
  onlyTyped?: boolean;
}): { owner: WorkingCopyComparisonOwner; dispose: () => void } {
  const owner = new WorkingCopyComparisonOwner({
    fetchListing: (sessionId, options) => fetchBranchPointListing(sessionId, options),
    forgetListings: forgetListedComparisons,
    watchRoute: watchWorkspaceGitRoute,
    // Without a typed comparison, "head" lists nothing from the host.
    readIntent: onlyTyped ? () => "head" : readWorkingCopyBaseIntent,
    subscribeIntent: onlyTyped ? () => () => {} : subscribeWorkingCopyBaseIntent,
    now: () => Date.now(),
    setTimeout: (callback, ms) => window.setTimeout(callback, ms),
    clearTimeout: (handle) => window.clearTimeout(handle as number | undefined),
    documentVisible: () => document.visibilityState !== "hidden",
    subscribeAttention,
    customComparisons,
  });
  let userId = getCurrentUserId();
  const stopIdentity = subscribeIdentity(() => {
    const next = getCurrentUserId();
    if (next === userId) return;
    userId = next;
    owner.reset();
  });
  return {
    owner,
    dispose: () => {
      stopIdentity();
      owner.dispose();
    },
  };
}

function createOwner(): WorkingCopyComparisonOwner {
  return createComparisonOwner({
    customComparisons: {
      read: readCustomComparison,
      write: (sessionId, workspace, spec) => void writeCustomComparison(sessionId, workspace, spec),
      clear: clearCustomComparison,
      subscribe: subscribeCustomComparisons,
    },
  }).owner;
}

let sharedOwner: WorkingCopyComparisonOwner | null = null;

/** The document's one comparison owner, created on first use. */
export function workingCopyComparisons(): WorkingCopyComparisonOwner {
  sharedOwner ??= createOwner();
  return sharedOwner;
}

/** Replaces the shared owner, for tests; `null` makes the next use create a real one. */
export function setWorkingCopyComparisonsForTests(owner: WorkingCopyComparisonOwner | null): void {
  sharedOwner?.dispose();
  sharedOwner = owner;
}

/**
 * The session's files may have changed: refetch its last-commit list and branch commits, and tell
 * the comparison owner, which lists the branch point again if anything shows it.
 */
export function invalidateWorkingCopyChanges(
  queryClient: QueryClient | null | undefined,
  sessionId: string,
): void {
  void queryClient?.invalidateQueries({ queryKey: [WORKING_COPY_CHANGES_KEY, sessionId] });
  void queryClient?.invalidateQueries({ queryKey: [BRANCH_COMMITS_KEY, sessionId] });
  workingCopyComparisons().invalidate(sessionId);
}
