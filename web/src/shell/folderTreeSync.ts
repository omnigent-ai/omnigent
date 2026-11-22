// Keeps a lazily loaded folder tree in step with its folders' listings as they
// refresh. Each listing decides only its own folder's children; changes are
// computed against what the tree holds, so no operation adds a path twice or
// touches one inside a folder already being removed.

import type { FileTreeBatchOperation } from "@/components/file-tree/vendored";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import { folderTreePath, listingTreePaths } from "./breadcrumbPaths";

/** One folder's latest listing, by its path relative to the tree's root (`""` for the root). */
export interface FolderListing {
  readonly folder: string;
  readonly entries: readonly WorkspaceFile[];
}

export interface FolderTreeSync {
  /** The changes to apply to the tree, in order. */
  readonly operations: FileTreeBatchOperation[];
  /** Every path the tree holds once they apply. */
  readonly present: Set<string>;
  /** Folders whose contents are known: the listed ones and those a chain hint describes. */
  readonly loadedFolders: Set<string>;
  /** Folders that disappeared, with everything below them. */
  readonly removedFolders: string[];
}

/** A tree path's own folder: `"src/app.ts"` and `"src/lib/"` are both in `"src"`. */
function parentOf(treePath: string): string {
  const trimmed = treePath.endsWith("/") ? treePath.slice(0, -1) : treePath;
  const separator = trimmed.lastIndexOf("/");
  return separator < 0 ? "" : trimmed.slice(0, separator);
}

/**
 * The row a tree holding `present` shows for `path`, a file or a folder. A
 * folder whose only child is a folder shows as one row named by the chain's
 * deepest folder; undefined when the tree doesn't hold `path`.
 */
export function visibleTreeRow(present: ReadonlySet<string>, path: string): string | undefined {
  if (present.has(path)) return path;
  let row = folderTreePath(path);
  if (!present.has(row)) return undefined;
  for (;;) {
    const folder = row.slice(0, -1);
    const children = [...present].filter((child) => parentOf(child) === folder);
    if (children.length !== 1 || !children[0].endsWith("/")) return row;
    row = children[0];
  }
}

function depth(folder: string): number {
  return folder === "" ? 0 : folder.split("/").length;
}

function isInside(path: string, folder: string): boolean {
  return path.startsWith(`${folder}/`);
}

/** Adds a path and the folders above it, which the tree creates along with it. */
function addPresent(present: Set<string>, treePath: string) {
  present.add(treePath);
  for (let folder = parentOf(treePath); folder !== ""; folder = parentOf(`${folder}/`)) {
    present.add(`${folder}/`);
  }
}

/**
 * The operations that bring a tree holding `present` up to date with
 * `listings`. Parents apply before their subfolders, and a folder removed by
 * its parent's listing takes its own listing with it.
 */
export function syncFolderListings(
  present: ReadonlySet<string>,
  listings: readonly FolderListing[],
): FolderTreeSync {
  const next = new Set(present);
  const operations: FileTreeBatchOperation[] = [];
  const loadedFolders = new Set<string>();
  const removedFolders: string[] = [];
  const isRemoved = (path: string) =>
    removedFolders.some((gone) => path === gone || isInside(path, gone));

  const ordered = [...listings].sort((left, right) => depth(left.folder) - depth(right.folder));
  // A folder's own listing outranks a chain hint about it, however fresh either is.
  const listedFolders = new Set(listings.map((listing) => listing.folder));
  // A hint may not create a folder where the tree holds a file of that name.
  const crossesFile = (folderPath: string) => {
    for (let folder = folderPath.slice(0, -1); folder !== ""; folder = parentOf(`${folder}/`)) {
      if (next.has(folder)) return true;
    }
    return false;
  };
  for (const { folder, entries } of ordered) {
    // A listing still cached for a folder that is gone says nothing about the tree.
    if (isRemoved(folder) || (folder !== "" && !next.has(`${folder}/`))) continue;
    const listed = listingTreePaths(entries);
    const children = new Set(listed.entries);

    for (const path of [...next]) {
      if (parentOf(path) !== folder || children.has(path)) continue;
      const isFolder = path.endsWith("/");
      operations.push({ type: "remove", path, recursive: isFolder });
      next.delete(path);
      if (isFolder) {
        const gone = path.slice(0, -1);
        removedFolders.push(gone);
        for (const inside of [...next]) if (isInside(inside, gone)) next.delete(inside);
      }
    }
    for (const path of listed.entries) {
      if (next.has(path)) continue;
      operations.push({ type: "add", path });
      addPresent(next, path);
    }
    loadedFolders.add(folder);
    // Chain hints only add: a folder the hint no longer names may still be there.
    for (const chain of listed.chains) {
      if (chain.folders.some((chainFolder) => listedFolders.has(chainFolder))) continue;
      if (crossesFile(chain.path)) continue;
      chain.folders.forEach((chainFolder) => loadedFolders.add(chainFolder));
      if (next.has(chain.path)) continue;
      operations.push({ type: "add", path: chain.path });
      addPresent(next, chain.path);
    }
  }
  for (const folder of [...loadedFolders]) if (isRemoved(folder)) loadedFolders.delete(folder);
  return { operations, present: next, loadedFolders, removedFolders };
}
