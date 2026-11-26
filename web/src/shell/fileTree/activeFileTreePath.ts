// Where the viewed file sits in a Files tree browsing some folder, so the tree
// can reveal it without ever re-rooting to reach it.

import { relativizeToWorkspace } from "@/hooks/useWorkspaceChangedFiles";

/**
 * `file`'s path relative to the browsed `location`, or null when the tree can't
 * show it. `file` is workspace-relative or absolute; `location` is the panel's
 * wire form: `""` for the workspace root, relative inside it, absolute outside.
 */
export function activeFileTreePath(
  file: string | null | undefined,
  location: string,
  workspaceRoot: string | null,
): string | null {
  if (!file) return null;
  // Compare in the location's terms: absolute paths for a folder outside the
  // workspace (which may hold it), workspace-relative ones inside it.
  const scoped = location.startsWith("/")
    ? absoluteFile(file, workspaceRoot)
    : relativeFile(file, workspaceRoot);
  if (scoped === null) return null;
  if (location === "") return scoped;
  const prefix = location.endsWith("/") ? location : `${location}/`;
  if (!scoped.startsWith(prefix)) return null;
  const within = scoped.slice(prefix.length);
  return within === "" ? null : within;
}

/** `file` as an absolute path, or null for a relative one with no known workspace root. */
function absoluteFile(file: string, workspaceRoot: string | null): string | null {
  if (file.startsWith("/")) return file;
  if (!workspaceRoot) return null;
  return `${workspaceRoot.replace(/\/$/, "")}/${file}`;
}

/** `file` relative to the workspace, or null when it lies outside it. */
function relativeFile(file: string, workspaceRoot: string | null): string | null {
  const relative = file.startsWith("/") ? relativizeToWorkspace(file, workspaceRoot) : file;
  return relative.startsWith("/") || relative === "" ? null : relative;
}
