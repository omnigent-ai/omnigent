// The Files tree's view of its folder listings: hidden entries left out when the
// panel hides them, a version that changes when any listing does, and the
// metadata its sort reads.

import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import type { FolderListing } from "../folderTreeSync";
import { activeFileTreePath } from "./activeFileTreePath";
import type { TreeEntryMeta } from "./treeSort";

const isHidden = (name: string) => name.startsWith(".");

/** The filter's include and exclude patterns: comma-separated, as the name search takes them. */
export interface TreePatterns {
  include: string;
  exclude: string;
}

export const NO_TREE_PATTERNS: TreePatterns = { include: "", exclude: "" };

/** One pattern, compiled; `folderOnly` when it names a folder, which a file never matches. */
interface PatternMatcher {
  regex: RegExp;
  folderOnly: boolean;
}

/**
 * One pattern as a matcher for tree paths. `*` and `?` stay within a name and `**`
 * crosses folders. A leading or middle `/` anchors the pattern at the browsed folder;
 * without one it matches a name at any depth. A trailing `/` or `/**` names a folder,
 * which also covers everything in it.
 */
function patternMatcher(pattern: string): PatternMatcher | null {
  const written = pattern.trim();
  const trimmed = written.replace(/^\.?\/+/, "");
  let glob = trimmed.replace(/(?:\/\*\*)+\/?$|\/+$/, "");
  if (!glob) return null;
  const folderOnly = glob !== trimmed;
  // As in gitignore, a leading or middle `/` anchors, decided before either end is trimmed:
  // `/src/` and `src/**` are the top-level `src`, while `src/` is any `src` folder.
  const anchored = trimmed !== written || trimmed.replace(/\/+$/, "").includes("/");
  if (!anchored) glob = `**/${glob}`;
  let source = "";
  for (let index = 0; index < glob.length; index++) {
    const char = glob[index];
    if (char === "*" && glob[index + 1] === "*") {
      const crossesFolder = glob[index + 2] === "/";
      source += crossesFolder ? "(?:.*/)?" : ".*";
      index += crossesFolder ? 2 : 1;
    } else if (char === "*") {
      source += "[^/]*";
    } else if (char === "?") {
      source += "[^/]";
    } else {
      source += char.replace(/[.+^${}()|[\]\\]/g, "\\$&");
    }
  }
  return { regex: new RegExp(`^${source}$`), folderOnly };
}

function patternMatchers(list: string): PatternMatcher[] {
  return list.split(",").flatMap((pattern) => patternMatcher(pattern) ?? []);
}

/** Whether `path`, or a folder holding it, matches one of `matchers`. */
function matchesWithin(
  path: string,
  type: WorkspaceFile["type"],
  matchers: readonly PatternMatcher[],
): boolean {
  for (let prefix = path; prefix; prefix = prefix.slice(0, Math.max(0, prefix.lastIndexOf("/")))) {
    // Every prefix but the path itself is a folder holding it.
    const isFolder = prefix !== path || type === "directory";
    if (matchers.some(({ regex, folderOnly }) => (isFolder || !folderOnly) && regex.test(prefix))) {
      return true;
    }
  }
  return false;
}

/**
 * Whether the tree shows a path, under the hidden-files setting and the patterns.
 * An exclude hides what it matches, folders with their contents. An include keeps
 * only the files it matches, and every folder, since a folder's files aren't known
 * until it opens.
 */
export function treePathFilter(
  showHidden: boolean,
  patterns: TreePatterns = NO_TREE_PATTERNS,
): (path: string, type: WorkspaceFile["type"]) => boolean {
  const excluded = patternMatchers(patterns.exclude);
  const included = patternMatchers(patterns.include);
  return (path, type) => {
    if (!showHidden && isHidden(path.slice(path.lastIndexOf("/") + 1))) return false;
    if (matchesWithin(path, type, excluded)) return false;
    return type === "directory" || included.length === 0 || matchesWithin(path, type, included);
  };
}

/** A filter for files deep in the tree: one shows only where every folder above it does too. */
export function treeFilePathFilter(
  showHidden: boolean,
  patterns: TreePatterns = NO_TREE_PATTERNS,
): (path: string) => boolean {
  const shows = treePathFilter(showHidden, patterns);
  return (path) => {
    const parts = path.split("/");
    return parts.every((_, depth) =>
      shows(parts.slice(0, depth + 1).join("/"), depth === parts.length - 1 ? "file" : "directory"),
    );
  };
}

/** Whether `patterns` narrow the tree at all. */
export function hasTreePatterns(patterns: TreePatterns): boolean {
  return (
    patternMatchers(patterns.include).length > 0 || patternMatchers(patterns.exclude).length > 0
  );
}

/**
 * `entries` as the tree shows them. Hidden and excluded entries are left out (see
 * `treePathFilter`), and a chain hint stops before its first such folder.
 */
export function visibleEntries(
  entries: readonly WorkspaceFile[],
  showHidden: boolean,
  patterns: TreePatterns = NO_TREE_PATTERNS,
): WorkspaceFile[] {
  if (showHidden && !hasTreePatterns(patterns)) return [...entries];
  const shows = treePathFilter(showHidden, patterns);
  const visible: WorkspaceFile[] = [];
  for (const entry of entries) {
    if (!shows(entry.path, entry.type)) continue;
    const leaf = entry.leaf_directory_path;
    if (!leaf?.startsWith(`${entry.path}/`)) {
      visible.push(entry);
      continue;
    }
    const kept: string[] = [];
    let chainPath = entry.path;
    for (const name of leaf.slice(entry.path.length + 1).split("/")) {
      chainPath = `${chainPath}/${name}`;
      if (!name || !shows(chainPath, "directory")) break;
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

/**
 * The workspace's changes (workspace-relative paths) as a tree browsing `location`
 * sees them: only those inside it, with paths relative to it.
 */
export function changesWithinFolder(
  changes: readonly TreeChange[],
  location: string,
  workspaceRoot: string | null,
): readonly TreeChange[] {
  if (location === "") return changes;
  return changes.flatMap((change) => {
    const path = activeFileTreePath(change.path, location, workspaceRoot);
    return path === null ? [] : [{ ...change, path }];
  });
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

/** The changes the tree lists, under the hidden-files setting and the patterns. */
export function visibleChangedFiles(
  changes: readonly TreeChange[],
  showHidden: boolean,
  patterns: TreePatterns = NO_TREE_PATTERNS,
): readonly TreeChange[] {
  if (showHidden && !hasTreePatterns(patterns)) return changes;
  const shows = treeFilePathFilter(showHidden, patterns);
  return changes.filter((change) => shows(change.path));
}

/**
 * The rows at the top of the tree: the listing's visible entries and the folders or
 * files that only Git knows of, such as a deleted file.
 */
export function treeRootEntries(
  entries: readonly WorkspaceFile[],
  changes: readonly TreeChange[],
  showHidden: boolean,
  patterns: TreePatterns = NO_TREE_PATTERNS,
): readonly WorkspaceFile[] {
  const root = { folder: "", entries: visibleEntries(entries, showHidden, patterns) };
  const merged = withChangedFiles([root], visibleChangedFiles(changes, showHidden, patterns));
  return merged.find((listing) => listing.folder === "")?.entries ?? root.entries;
}
