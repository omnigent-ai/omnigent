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
import { baseIntentSource, resetFolderBaseIntentsForTests } from "@/lib/workingCopyBasePreference";
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
 * its typed comparison and lists nothing else. With `repository`, a folder relative to the
 * workspace, it reviews that repository in place of the workspace. Another signed-in user
 * never sees its comparisons or choices; `dispose` stops it and its identity watch.
 */
export function createComparisonOwner({
  customComparisons,
  onlyTyped = false,
  repository = "",
  workspaceKey = "",
}: {
  customComparisons: ComparisonOwnerDependencies["customComparisons"];
  onlyTyped?: boolean;
  repository?: string;
  workspaceKey?: string;
}): { owner: WorkingCopyComparisonOwner; dispose: () => void } {
  const intents = baseIntentSource(repository, workspaceKey);
  const owner = new WorkingCopyComparisonOwner({
    fetchListing: (sessionId, options) =>
      fetchBranchPointListing(sessionId, { ...options, repository }),
    repository,
    workspaceKey,
    forgetListings: forgetListedComparisons,
    watchRoute: watchWorkspaceGitRoute,
    // Without a typed comparison, "head" lists nothing from the host.
    readIntent: onlyTyped ? () => "head" : intents.read,
    subscribeIntent: onlyTyped ? () => () => {} : intents.subscribe,
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

function createOwner(repository = "", workspaceKey = ""): WorkingCopyComparisonOwner {
  return createComparisonOwner({
    customComparisons: {
      read: readCustomComparison,
      write: (sessionId, workspace, spec) => void writeCustomComparison(sessionId, workspace, spec),
      clear: clearCustomComparison,
      subscribe: subscribeCustomComparisons,
    },
    repository,
    workspaceKey,
  }).owner;
}

let sharedOwner: WorkingCopyComparisonOwner | null = null;
/** Reviews of repository folders inside sessions' workspaces, such as worktrees, one per folder. */
const repositoryOwners = new Map<string, WorkingCopyComparisonOwner>();

/** A session's comparison listener, with what stops it on each owner it was attached to. */
interface SessionSubscription {
  sessionId: string;
  listener: () => void;
  stops: (() => void)[];
}
/** Listeners from `subscribeSessionComparisons`, attached to each folder owner as it's made. */
const sessionSubscriptions = new Set<SessionSubscription>();

/** The document's one comparison owner, created on first use. */
export function workingCopyComparisons(): WorkingCopyComparisonOwner {
  sharedOwner ??= createOwner();
  return sharedOwner;
}

/** Names a session's host and workspace root, so same-named folders elsewhere stay apart. */
export function workspaceKeyOf(hostId: string | null, workspaceRoot: string): string {
  return `${hostId ?? ""}\n${workspaceRoot.replace(/\/+$/, "")}`;
}

/**
 * The owner of reviews of `repository`, a folder relative to the workspace `workspaceKey`
 * names, created on first use. It lists and chooses apart from the workspace's, so the
 * sidebar's count and the workspace's review never show another repository's changes.
 */
export function workingCopyComparisonsAt(
  repository: string,
  workspaceKey = "",
): WorkingCopyComparisonOwner {
  if (!repository) return workingCopyComparisons();
  const key = `${workspaceKey}\u0000${repository}`;
  let owner = repositoryOwners.get(key);
  if (!owner) {
    owner = createOwner(repository, workspaceKey);
    repositoryOwners.set(key, owner);
    for (const subscription of sessionSubscriptions) {
      subscription.stops.push(owner.subscribe(subscription.sessionId, subscription.listener));
    }
  }
  return owner;
}

/**
 * How many comparisons the person has chosen for the session, in its workspace's review and
 * in any folder's, so a choice in either tells a tab's landing it is older than the choice.
 */
export function sessionChoiceCount(sessionId: string): number {
  let count = workingCopyComparisons().choiceCount(sessionId);
  for (const owner of repositoryOwners.values()) count += owner.choiceCount(sessionId);
  return count;
}

/**
 * Calls `listener` when the session's comparisons change in the workspace's review or in a
 * folder's, including a folder whose review opens later.
 */
export function subscribeSessionComparisons(sessionId: string, listener: () => void): () => void {
  const subscription: SessionSubscription = {
    sessionId,
    listener,
    stops: [workingCopyComparisons(), ...repositoryOwners.values()].map((owner) =>
      owner.subscribe(sessionId, listener),
    ),
  };
  sessionSubscriptions.add(subscription);
  return () => {
    sessionSubscriptions.delete(subscription);
    subscription.stops.forEach((stop) => stop());
  };
}

/** Replaces the shared owner, for tests; `null` makes the next use create a real one. */
export function setWorkingCopyComparisonsForTests(owner: WorkingCopyComparisonOwner | null): void {
  sharedOwner?.dispose();
  sharedOwner = owner;
  for (const repositoryOwner of repositoryOwners.values()) repositoryOwner.dispose();
  repositoryOwners.clear();
  resetFolderBaseIntentsForTests();
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
  for (const owner of repositoryOwners.values()) owner.invalidate(sessionId);
}
