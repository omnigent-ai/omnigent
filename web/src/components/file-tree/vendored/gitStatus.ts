import { getAncestorDirectoryPaths } from "./pathHelpers";
import type { GitStatus, GitStatusEntry } from "./types";

export interface FileTreeGitStatusState {
  /** Canonical paths: directories end with `/`. */
  readonly statusByPath: ReadonlyMap<string, GitStatus>;
  readonly directoriesWithChanges: ReadonlySet<string>;
  readonly ignoredDirectoryPaths: ReadonlySet<string>;
}

export const GIT_STATUS_LABEL: Record<GitStatus, string | null> = {
  added: "A",
  deleted: "D",
  ignored: null,
  modified: "M",
  renamed: "R",
  untracked: "U",
};

export const GIT_STATUS_TITLE: Record<GitStatus, string> = {
  added: "Added",
  deleted: "Deleted",
  ignored: "Ignored",
  modified: "Modified",
  renamed: "Renamed",
  untracked: "Untracked",
};

export const GIT_STATUS_DESCENDANT_TITLE = "Contains changes";

export function resolveFileTreeGitStatusState(
  entries: readonly GitStatusEntry[] | undefined,
): FileTreeGitStatusState | null {
  const statusByPath = new Map<string, GitStatus>();
  const directoriesWithChanges = new Set<string>();
  const ignoredDirectoryPaths = new Set<string>();

  for (const { path, status } of entries ?? []) {
    const canonicalPath = canonicalGitStatusPath(path);
    if (canonicalPath === null) continue;
    statusByPath.set(canonicalPath, status);
    if (canonicalPath.endsWith("/") && status === "ignored")
      ignoredDirectoryPaths.add(canonicalPath);
    for (const ancestorPath of getAncestorDirectoryPaths(canonicalPath))
      directoriesWithChanges.add(ancestorPath);
  }

  return statusByPath.size === 0
    ? null
    : { statusByPath, directoriesWithChanges, ignoredDirectoryPaths };
}

/**
 * The status a row shows: its own, else `ignored` when it sits inside an
 * ignored directory. Directories inherit nothing else from their children;
 * they show a dot instead (see `directoriesWithChanges`).
 */
export function effectiveGitStatus(
  state: FileTreeGitStatusState,
  path: string,
  ancestorPaths: readonly string[],
): GitStatus | null {
  const own = state.statusByPath.get(path);
  if (own !== undefined) return own;
  if (state.ignoredDirectoryPaths.size === 0) return null;
  return ancestorPaths.some((ancestor) => state.ignoredDirectoryPaths.has(ancestor))
    ? "ignored"
    : null;
}

// Collapses repeated and trailing separators, keeping a trailing `/` as the
// directory marker the tree uses for canonical directory paths.
function canonicalGitStatusPath(path: string): string | null {
  const segments = path.split("/").filter((segment) => segment.length > 0);
  if (segments.length === 0) return null;
  return `${segments.join("/")}${path.endsWith("/") ? "/" : ""}`;
}
