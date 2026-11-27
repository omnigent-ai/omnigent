// Which comparison owner the views below read a session's changes from: the
// document's shared one, unless a review shows something of its own, such as a
// commit or a worktree inside the workspace, without changing what the session
// compares everywhere else.

import { createContext, useCallback, useContext, useSyncExternalStore } from "react";

import type { WorkingCopyComparisonOwner } from "@/lib/workingCopyComparisonOwner";
import {
  sessionChoiceCount,
  subscribeSessionComparisons,
  workingCopyComparisons,
  workingCopyComparisonsAt,
} from "@/lib/workingCopyComparisons";

export const ComparisonOwnerContext = createContext<WorkingCopyComparisonOwner | null>(null);

/** The owner views here register with and read from. */
export function useComparisonOwner(): WorkingCopyComparisonOwner {
  return useContext(ComparisonOwnerContext) ?? workingCopyComparisons();
}

/** The repository folder the views here review, relative to the workspace; empty for it. */
export function useComparisonRepository(): string {
  return useContext(ComparisonOwnerContext)?.repository ?? "";
}

/** Which host and workspace the folder the views here review lies in; empty for the workspace. */
export function useComparisonWorkspaceKey(): string {
  return useContext(ComparisonOwnerContext)?.workspaceKey ?? "";
}

/**
 * The owner a view's base choices change: the one reviewing its repository folder, or the
 * session's shared one, even under a commit's own owner.
 */
export function useChoosingComparisonOwner(): WorkingCopyComparisonOwner {
  return workingCopyComparisonsAt(useComparisonRepository(), useComparisonWorkspaceKey());
}

/** How many comparisons the person has chosen for the session, in any folder's review. */
export function useSessionChoiceCount(sessionId: string): number {
  const subscribe = useCallback(
    (listener: () => void) => subscribeSessionComparisons(sessionId, listener),
    [sessionId],
  );
  return useSyncExternalStore(subscribe, () => sessionChoiceCount(sessionId));
}
