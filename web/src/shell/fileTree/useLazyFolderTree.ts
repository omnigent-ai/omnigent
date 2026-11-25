// Keeps a file tree model in step with lazily loaded folder listings, and
// says which folders it still needs: the ones a person expanded that nobody has
// listed yet, and the ones a chain hint stopped describing. Where listings come
// from is the caller's business, so the Files panel and the breadcrumb tree
// share the model logic while loading their folders their own way.

import { useCallback, useEffect, useRef, useState } from "react";
import type { FileTreeModel } from "@/components/file-tree/vendored";
import { folderTreePath } from "../breadcrumbPaths";
import { type FolderListing, type FolderTreeSync, syncFolderListings } from "../folderTreeSync";

export interface LazyFolderTreeState {
  /** Folders the tree needs listed beyond the caller's own: expanded or no longer chained. */
  wantedFolders: string[];
  /** Whether the tree holds nothing at all. */
  isEmpty: boolean;
  /** Folders whose contents the tree knows, as of the last sync. */
  loadedFolders: ReadonlySet<string>;
  /** Every path the tree holds, as of the last sync. */
  present: ReadonlySet<string>;
  /** Every path the tree holds right now, for work done in the same commit as a sync. */
  currentPresent: () => ReadonlySet<string>;
}

/** The model's starting state for `listings`, before any sync. */
export function initialFolderTree(listings: readonly FolderListing[]): FolderTreeSync {
  return syncFolderListings(new Set(), listings);
}

/**
 * Applies `listings` to `model` whenever `listingVersion` changes. `required`
 * folders are the caller's own and are never reported as wanted.
 */
export function useLazyFolderTree(
  model: FileTreeModel,
  initial: FolderTreeSync,
  listings: readonly FolderListing[],
  listingVersion: string,
  required: readonly string[],
): LazyFolderTreeState {
  const present = useRef<Set<string>>(initial.present);
  const loadedFolders = useRef<Set<string>>(initial.loadedFolders);
  const [wantedFolders, setWantedFolders] = useState<string[]>([]);
  const [snapshot, setSnapshot] = useState({
    isEmpty: initial.present.size === 0,
    loadedFolders: initial.loadedFolders as ReadonlySet<string>,
    present: initial.present as ReadonlySet<string>,
  });
  const latest = useRef({ listings, required });
  latest.current = { listings, required };

  useEffect(() => {
    const { listings: listingsNow, required: requiredNow } = latest.current;
    const sync = syncFolderListings(present.current, listingsNow);
    if (sync.operations.length > 0) model.batch(sync.operations);
    // A folder a chain hint no longer describes may have gained entries: list it.
    const relisted = [...loadedFolders.current].filter(
      (folder) =>
        !sync.loadedFolders.has(folder) &&
        sync.present.has(folderTreePath(folder)) &&
        !requiredNow.includes(folder),
    );
    present.current = sync.present;
    loadedFolders.current = sync.loadedFolders;
    const removed = (path: string) =>
      sync.removedFolders.some((gone) => path === gone || path.startsWith(`${gone}/`));
    setWantedFolders((current) => {
      const kept =
        sync.removedFolders.length > 0 ? current.filter((path) => !removed(path)) : current;
      const added = relisted.filter((folder) => !kept.includes(folder));
      return kept.length === current.length && added.length === 0 ? current : [...kept, ...added];
    });
    setSnapshot({
      isEmpty: sync.present.size === 0,
      loadedFolders: sync.loadedFolders,
      present: sync.present,
    });
  }, [model, listingVersion]);

  // Expanding a folder that hasn't been listed yet asks for its contents.
  useEffect(() => {
    const askForExpanded = () => {
      const unlisted: string[] = [];
      for (const row of model.getVisibleRows(0, model.getVisibleCount())) {
        if (row.kind !== "directory" || !row.isExpanded) continue;
        const folder = row.path.replace(/\/$/, "");
        if (!loadedFolders.current.has(folder)) unlisted.push(folder);
      }
      if (unlisted.length === 0) return;
      setWantedFolders((current) => {
        const added = unlisted.filter((folder) => !current.includes(folder));
        return added.length > 0 ? [...current, ...added] : current;
      });
    };
    askForExpanded();
    return model.subscribe(askForExpanded);
  }, [model]);

  const currentPresent = useCallback((): ReadonlySet<string> => present.current, []);
  return { wantedFolders, ...snapshot, currentPresent };
}
