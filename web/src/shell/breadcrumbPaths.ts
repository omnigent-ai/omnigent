// Path logic behind the file viewer's breadcrumbs: the segments a path shows
// and the sibling listing each one opens.

import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";

/** One clickable piece of the path, e.g. the `agents` in `.github/agents/config.yaml`. */
export interface BreadcrumbSegment {
  readonly label: string;
  /** The segment's own path, in the same form as the file's: relative, or absolute. */
  readonly path: string;
  readonly isFile: boolean;
}

/**
 * Splits a workspace-relative or absolute file path into breadcrumb segments.
 * Windows separators split too; segment paths always use forward slashes.
 */
export function breadcrumbSegments(filePath: string): BreadcrumbSegment[] {
  const absolute = filePath.startsWith("/");
  const labels = filePath.split(/[\\/]/).filter(Boolean);
  let current = absolute ? "/" : "";
  return labels.map((label, index) => {
    current = current === "" ? label : current === "/" ? `/${label}` : `${current}/${label}`;
    return { label, path: current, isFile: index === labels.length - 1 };
  });
}

/** The folder holding `path`: `""` for the workspace root, `"/"` for the host root. */
export function parentFolder(path: string): string {
  const separator = path.lastIndexOf("/");
  if (separator < 0) return "";
  return separator === 0 ? "/" : path.slice(0, separator);
}

/** The folder a segment's tree shows: the one holding the segment, so it lists its siblings. */
export function segmentFolder(segment: BreadcrumbSegment): string {
  return parentFolder(segment.path);
}

/** `path` relative to `folder`, which must contain it. */
export function pathWithinFolder(path: string, folder: string): string {
  if (folder === "") return path;
  const prefix = folder === "/" ? "/" : `${folder}/`;
  return path.startsWith(prefix) ? path.slice(prefix.length) : path;
}

/** The folders, relative to the tree's root, from the root down to `revealPath`'s parent. */
export function revealedFolders(revealPath: string): string[] {
  const folders = [""];
  const parts = revealPath.split("/").filter(Boolean).slice(0, -1);
  for (let index = 0; index < parts.length; index++) {
    folders.push(parts.slice(0, index + 1).join("/"));
  }
  return folders;
}

/** A folder's title in its tree popover: the path the breadcrumbs show. */
export function folderTitle(folder: string, workspaceName: string): string {
  return folder === "" ? workspaceName : folder;
}

/** The text the breadcrumbs show for `filePath`, for measuring how much room they need. */
export function breadcrumbText(filePath: string): string {
  return breadcrumbSegments(filePath)
    .map((segment) => segment.label)
    .join(" › ");
}

/** Titles the workspace root's folder tree: the root folder's name. */
export function workspaceFolderName(workspaceRoot: string | null | undefined): string {
  return breadcrumbSegments(workspaceRoot ?? "").at(-1)?.label ?? "Workspace";
}

/** A tree path for a listed entry; the file tree marks folders with a trailing slash. */
export function folderTreePath(path: string): string {
  return `${path}/`;
}

/** A chain of single-child folders a listing describes in one step, e.g. `java › com › example`. */
export interface ListedChain {
  /** The chain's deepest folder, as a tree path. */
  readonly path: string;
  /** The folders above it, each of which holds only the next. */
  readonly folders: readonly string[];
}

/**
 * What a folder listing tells a tree. `entries` are the folder's own children,
 * which the listing is authoritative for. `chains` are only hints about
 * deeper folders: true when listed, and never a deletion when they go away.
 */
export function listingTreePaths(entries: readonly WorkspaceFile[]): {
  entries: string[];
  chains: ListedChain[];
} {
  const children: string[] = [];
  const chains: ListedChain[] = [];
  for (const entry of entries) {
    if (entry.type === "file") {
      children.push(entry.path);
      continue;
    }
    children.push(folderTreePath(entry.path));
    const leaf = entry.leaf_directory_path;
    if (leaf && leaf.startsWith(`${entry.path}/`)) {
      const folders: string[] = [];
      let folder = entry.path;
      for (const name of leaf.slice(entry.path.length + 1).split("/")) {
        folders.push(folder);
        folder = `${folder}/${name}`;
      }
      chains.push({ path: folderTreePath(leaf), folders });
    }
  }
  return { entries: children, chains };
}
