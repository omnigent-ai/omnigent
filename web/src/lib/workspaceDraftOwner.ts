// The signed-in workspace user, and the drafts and changes that belong to that user.
// Host-neutral: the drafts, notices and writes of each user stay apart.

import { useSyncExternalStore } from "react";

import { getCurrentUserId, subscribeIdentity } from "@/lib/identity";

// Moves on every change of workspace user, even back to one seen before, never on a
// re-check that finds the same user. Separate from the Mac read scope's order.
let workspaceAuthGeneration = 0;
let watchedWorkspaceUser: string | null | undefined;
let stopWatchingWorkspaceUser: (() => void) | null = null;
const workspaceAuthListeners = new Set<() => void>();

const workspaceUserRetirements = new Set<(previousGeneration: number) => void>();

function watchWorkspaceUser(): void {
  if (stopWatchingWorkspaceUser) return;
  watchedWorkspaceUser = getCurrentUserId();
  stopWatchingWorkspaceUser = subscribeIdentity(() => {
    const user = getCurrentUserId();
    if (user === watchedWorkspaceUser) return;
    watchedWorkspaceUser = user;
    workspaceUserChanged();
  });
}

/** Moves the generation, then lets each retirement drop the previous user's changes, then re-renders. */
function workspaceUserChanged(): void {
  const previousGeneration = workspaceAuthGeneration;
  workspaceAuthGeneration += 1;
  for (const retire of workspaceUserRetirements) retire(previousGeneration);
  for (const listener of workspaceAuthListeners) listener();
}

/** Run `retire` with the previous generation whenever the workspace user changes; returns its unsubscribe. */
export function registerWorkspaceUserRetirement(
  retire: (previousGeneration: number) => void,
): () => void {
  workspaceUserRetirements.add(retire);
  return () => {
    workspaceUserRetirements.delete(retire);
  };
}

/** The workspace-user transition count; moves whenever the signed-in user changes. */
export function currentWorkspaceAuthGeneration(): number {
  watchWorkspaceUser();
  return workspaceAuthGeneration;
}

/** Hear every change of workspace user, outside React. */
export function subscribeWorkspaceUser(listener: () => void): () => void {
  watchWorkspaceUser();
  workspaceAuthListeners.add(listener);
  return () => workspaceAuthListeners.delete(listener);
}

/** Re-render when the workspace user changes; returns the user drafts and notices belong to. */
export function useWorkspaceDraftOwner(): string {
  useSyncExternalStore(
    (listener) => {
      watchWorkspaceUser();
      workspaceAuthListeners.add(listener);
      return () => workspaceAuthListeners.delete(listener);
    },
    () => workspaceAuthGeneration,
    () => 0,
  );
  return draftOwner();
}

/** Who drafts belong to now: the workspace user, or a fixed owner before one is known. */
export function draftOwner(): string {
  return getCurrentUserId() ?? "(signed out)";
}

/** Forget the workspace user and its listeners, so a test starts clean. */
export function resetWorkspaceDraftOwnerForTests(): void {
  stopWatchingWorkspaceUser?.();
  stopWatchingWorkspaceUser = null;
  watchedWorkspaceUser = undefined;
  workspaceAuthGeneration = 0;
  workspaceAuthListeners.clear();
}
