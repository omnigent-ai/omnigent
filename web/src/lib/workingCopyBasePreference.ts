// What the Changes tab and its review compare a session's working copy with:
// Automatic, the last commit, or where the branch left its default branch. A
// per-device preference; Automatic is the default and follows the app's
// policy rather than a copied choice.

import { useSyncExternalStore } from "react";

/** The base the user chose. */
export type WorkingCopyBaseIntent = "automatic" | "head" | "branch-point";

export const WORKING_COPY_BASE_INTENTS: readonly WorkingCopyBaseIntent[] = [
  "automatic",
  "head",
  "branch-point",
];

const STORAGE_KEY = "omnigent.changes.base";
const CHANGE_EVENT = "omnigent:working-copy-base-change";

function isIntent(value: unknown): value is WorkingCopyBaseIntent {
  return WORKING_COPY_BASE_INTENTS.includes(value as WorkingCopyBaseIntent);
}

/** The stored choice; anything unreadable or unknown means Automatic. */
export function readWorkingCopyBaseIntent(): WorkingCopyBaseIntent {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    return isIntent(stored) ? stored : "automatic";
  } catch {
    return "automatic";
  }
}

/**
 * Store the choice and tell this window's listeners; other windows hear it
 * through the storage event. Automatic is stored as no choice at all.
 *
 * :returns: Whether the choice was stored.
 */
export function writeWorkingCopyBaseIntent(intent: WorkingCopyBaseIntent): boolean {
  try {
    if (intent === "automatic") window.localStorage.removeItem(STORAGE_KEY);
    else window.localStorage.setItem(STORAGE_KEY, intent);
  } catch {
    return false;
  }
  window.dispatchEvent(new Event(CHANGE_EVENT));
  return true;
}

/** Calls `listener` whenever the choice changes, in this window or another. */
export function subscribeWorkingCopyBaseIntent(listener: () => void): () => void {
  const onStorage = (event: StorageEvent) => {
    if (event.key === null || event.key === STORAGE_KEY) listener();
  };
  window.addEventListener(CHANGE_EVENT, listener);
  window.addEventListener("storage", onStorage);
  return () => {
    window.removeEventListener(CHANGE_EVENT, listener);
    window.removeEventListener("storage", onStorage);
  };
}

/** The current choice, kept live across windows. */
export function useWorkingCopyBaseIntent(): WorkingCopyBaseIntent {
  return useSyncExternalStore(
    subscribeWorkingCopyBaseIntent,
    readWorkingCopyBaseIntent,
    () => "automatic",
  );
}

/** Where a review reads and saves its base: the device's preference, or a folder's own. */
export interface BaseIntentSource {
  read: () => WorkingCopyBaseIntent;
  write: (intent: WorkingCopyBaseIntent) => void;
  subscribe: (listener: () => void) => () => void;
}

const WORKSPACE_BASE_INTENT: BaseIntentSource = {
  read: readWorkingCopyBaseIntent,
  write: (intent) => void writeWorkingCopyBaseIntent(intent),
  subscribe: subscribeWorkingCopyBaseIntent,
};

/** Bases chosen in a repository folder's review, such as a worktree's, for this window. */
const folderIntents = new Map<string, WorkingCopyBaseIntent>();
const folderListeners = new Set<() => void>();
const folderSources = new Map<string, BaseIntentSource>();

/**
 * The base source for `repository`, a folder relative to the workspace `workspaceKey` names;
 * empty for the workspace's own preference. A folder follows that preference until a base is
 * chosen in its review, and its choice changes neither the workspace's nor a folder of the
 * same name in another workspace or on another host.
 */
export function baseIntentSource(repository: string, workspaceKey = ""): BaseIntentSource {
  if (!repository) return WORKSPACE_BASE_INTENT;
  const key = `${workspaceKey}\u0000${repository}`;
  let source = folderSources.get(key);
  if (!source) {
    source = {
      read: () => folderIntents.get(key) ?? readWorkingCopyBaseIntent(),
      write: (intent) => {
        folderIntents.set(key, intent);
        folderListeners.forEach((listener) => listener());
      },
      subscribe: (listener) => {
        folderListeners.add(listener);
        const stopWorkspace = subscribeWorkingCopyBaseIntent(listener);
        return () => {
          folderListeners.delete(listener);
          stopWorkspace();
        };
      },
    };
    folderSources.set(key, source);
  }
  return source;
}

/** `repository`'s current base, kept live; the workspace's preference for an empty one. */
export function useBaseIntent(repository: string, workspaceKey = ""): WorkingCopyBaseIntent {
  const source = baseIntentSource(repository, workspaceKey);
  return useSyncExternalStore(source.subscribe, source.read, () => "automatic");
}

/** Forgets the bases chosen in folders, for tests. */
export function resetFolderBaseIntentsForTests(): void {
  folderIntents.clear();
  folderSources.clear();
  folderListeners.forEach((listener) => listener());
}
