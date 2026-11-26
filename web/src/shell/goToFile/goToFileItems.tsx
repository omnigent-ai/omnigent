// Go to File's rows: one per workspace file, labeled with its name and described by its
// folder. Before a query they're the session's recently opened files; after one, the
// workspace's matches in the order Go to File ranked them.

import type { MatchRange, QuickPickItem } from "@/components/ui/quick-pick";

import type { RankedFile } from "../goToFileRanking";
import { WorkspaceFileIcon } from "../WorkspaceFileIcon";

/** What accepting a Go to File row opens. */
export interface GoToFileTarget {
  path: string;
  /** Where a text search's match is, to open and preview the file there. */
  line?: number;
  column?: number;
  endColumn?: number;
}

export const RECENT_FILES_GROUP = "Recently opened";

/** A path's file name and the folder holding it ("" at the workspace root). */
export function splitFilePath(path: string): { name: string; folder: string } {
  const slash = path.lastIndexOf("/");
  return slash === -1
    ? { name: path, folder: "" }
    : { name: path.slice(slash + 1), folder: path.slice(0, slash) };
}

function fileItem(
  path: string,
  highlights?: { label?: readonly MatchRange[]; description?: readonly MatchRange[] },
  group?: string,
): QuickPickItem<GoToFileTarget> {
  const { name, folder } = splitFilePath(path);
  return {
    id: `file:${path}`,
    label: name,
    description: folder || undefined,
    icon: <WorkspaceFileIcon path={path} />,
    group,
    highlights,
    matchHighlight: "strong",
    value: { path },
  };
}

/** The session's recently opened files, for the picker before anything is typed. */
export function recentFileItems(recentPaths: readonly string[]): QuickPickItem<GoToFileTarget>[] {
  return recentPaths.map((path) => fileItem(path, undefined, RECENT_FILES_GROUP));
}

/** Ranked matches, best first, with where the typed text occurs in the name and folder. */
export function rankedFileItems(files: readonly RankedFile[]): QuickPickItem<GoToFileTarget>[] {
  return files.map((file) =>
    fileItem(file.path, {
      label: file.nameMatch ? [file.nameMatch] : undefined,
      description: file.folderMatch ? [file.folderMatch] : undefined,
    }),
  );
}
