// localStorage-backed default workspace directory per project. Lets a new
// session started under a project pre-fill the working directory to the one
// last used for that project, instead of the host-wide most-recent path.

import { useCallback, useMemo, useState } from "react";

const STORAGE_KEY = "omnigent:project-workspaces";

// Map of project name -> the last-used absolute workspace path for it.
type ProjectMap = Record<string, string>;

function readAll(): ProjectMap {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed !== "object") return {};
    // Keep only string values; drop anything malformed so a corrupted
    // entry can't crash the picker.
    const out: ProjectMap = {};
    for (const [project, path] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof path === "string") out[project] = path;
    }
    return out;
  } catch {
    return {};
  }
}

function writeAll(map: ProjectMap): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
  } catch {
    // Quota exceeded or storage disabled — non-fatal; the default just
    // stops persisting until the next successful write.
  }
}

export interface ProjectWorkspaces {
  /** The stored default workspace for the current project, or ``null``. */
  projectWorkspace: string | null;
  /**
   * Record ``path`` as the default workspace for the current project.
   * No-op when ``project`` is empty or the path is blank.
   */
  setProjectWorkspace: (path: string) => void;
}

/**
 * Track the default workspace directory for one project.
 *
 * @param project Project name whose default to read/write. ``""`` (unfiled)
 *   yields a ``null`` default and a no-op setter.
 * @returns The project's stored workspace plus a recorder.
 */
export function useProjectWorkspaces(project: string): ProjectWorkspaces {
  // Bumped by the setter to recompute after a write; the map is read
  // synchronously below, not hydrated via an effect.
  const [revision, setRevision] = useState(0);

  const projectWorkspace = useMemo(
    () => (project === "" ? null : (readAll()[project] ?? null)),
    [project, revision],
  );

  const setProjectWorkspace = useCallback(
    (path: string) => {
      if (project === "") return;
      const trimmed = path.trim();
      if (!trimmed) return;
      const all = readAll();
      if (all[project] === trimmed) return;
      all[project] = trimmed;
      writeAll(all);
      setRevision((r) => r + 1);
    },
    [project],
  );

  return { projectWorkspace, setProjectWorkspace };
}
