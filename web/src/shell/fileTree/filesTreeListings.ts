// The Files tree's view of its folder listings: hidden entries left out when the
// panel hides them, a version that changes when any listing does, and the
// metadata its sort reads.

import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import type { FolderListing } from "../folderTreeSync";
import type { TreeEntryMeta } from "./treeSort";

const isHidden = (name: string) => name.startsWith(".");

/**
 * `entries` as the tree shows them. With hidden files hidden, dot-entries are
 * left out, and a chain hint stops before its first hidden folder.
 */
export function visibleEntries(
  entries: readonly WorkspaceFile[],
  showHidden: boolean,
): WorkspaceFile[] {
  if (showHidden) return [...entries];
  const visible: WorkspaceFile[] = [];
  for (const entry of entries) {
    if (isHidden(entry.name)) continue;
    const leaf = entry.leaf_directory_path;
    if (!leaf?.startsWith(`${entry.path}/`)) {
      visible.push(entry);
      continue;
    }
    const kept: string[] = [];
    for (const name of leaf.slice(entry.path.length + 1).split("/")) {
      if (!name || isHidden(name)) break;
      kept.push(name);
    }
    const { leaf_directory_path: _leaf, ...rest } = entry;
    visible.push(
      kept.length > 0 ? { ...rest, leaf_directory_path: `${entry.path}/${kept.join("/")}` } : rest,
    );
  }
  return visible;
}

const listingIds = new WeakMap<object, number>();
let nextListingId = 1;

/** Changes whenever any listing in `listings` is a new result, refetches included. */
export function listingsVersion(listings: readonly FolderListing[]): string {
  return listings
    .map(({ folder, entries }) => {
      let id = listingIds.get(entries);
      if (id === undefined) {
        id = nextListingId++;
        listingIds.set(entries, id);
      }
      return `${folder}\u0000${id}`;
    })
    .join("\u0001");
}

/** Each listed entry's size and mtime by path, folders without their trailing slash. */
export function treeMetadata(listings: readonly FolderListing[]): Map<string, TreeEntryMeta> {
  const meta = new Map<string, TreeEntryMeta>();
  for (const { entries } of listings) {
    for (const entry of entries) {
      meta.set(entry.path, {
        bytes: entry.type === "file" ? (entry.bytes ?? null) : null,
        modified_at: entry.modified_at ?? null,
      });
    }
  }
  return meta;
}

/** A change the panel knows about, relative to the browsed folder. */
export interface TreeChange {
  path: string;
  name: string;
  status: "created" | "modified" | "deleted";
  bytes: number | null;
  modified_at: number | null;
}

const parentOf = (path: string) => {
  const slash = path.lastIndexOf("/");
  return slash < 0 ? "" : path.slice(0, slash);
};

/**
 * `listings` with changed files the listings don't show, such as deleted ones,
 * added to their folders. A folder that exists only through such files, a
 * deleted folder say, gets a listing of its own, so nobody asks the workspace
 * for it. Folders not loaded yet get theirs when they are.
 */
export function withChangedFiles(
  listings: readonly FolderListing[],
  changes: readonly TreeChange[],
): FolderListing[] {
  const byFolder = new Map<string, WorkspaceFile[]>(
    listings.map(({ folder, entries }) => [folder, [...entries]]),
  );
  const known = new Set<string>();
  for (const entries of byFolder.values()) for (const entry of entries) known.add(entry.path);
  // Folders the changes need that no listing has hold only changed files.
  const ensureFolder = (folder: string): boolean => {
    if (folder === "" || byFolder.has(folder)) return true;
    if (known.has(folder)) return false; // A real folder, not loaded yet.
    const parent = parentOf(folder);
    if (!ensureFolder(parent)) return false;
    byFolder.get(parent)!.push({
      path: folder,
      name: folder.slice(folder.lastIndexOf("/") + 1),
      type: "directory",
      bytes: null,
      modified_at: null,
    });
    known.add(folder);
    byFolder.set(folder, []);
    return true;
  };
  let changed = false;
  for (const change of changes) {
    if (known.has(change.path)) continue;
    const folder = parentOf(change.path);
    if (!ensureFolder(folder)) continue;
    byFolder.get(folder)!.push({
      path: change.path,
      name: change.name,
      type: "file",
      bytes: change.bytes,
      modified_at: change.modified_at,
    });
    known.add(change.path);
    changed = true;
  }
  if (!changed) return [...listings];
  return [...byFolder].map(([folder, entries]) => ({ folder, entries }));
}
