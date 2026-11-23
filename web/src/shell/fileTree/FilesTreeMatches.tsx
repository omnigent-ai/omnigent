// A name filter's matches as a tree: each match under its folders, all open, so the
// reader sees where every match lives. Choosing a file opens it.

import { useEffect, useRef } from "react";
import { FileTree, useFileTree } from "@/components/file-tree/vendored";
import { FILE_TREE_THEME, WORKSPACE_FILE_TREE_CLASS } from "@/components/file-tree/fileTreeStyle";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import { folderTreePath } from "../breadcrumbPaths";

/** The tree paths for `matches`: their folders, then each match, folders marked with "/". */
export function matchTreePaths(matches: readonly WorkspaceFile[]): string[] {
  const paths = new Set<string>();
  for (const match of matches) {
    const parts = match.path.split("/").filter(Boolean);
    for (let depth = 1; depth < parts.length; depth += 1) {
      paths.add(folderTreePath(parts.slice(0, depth).join("/")));
    }
    paths.add(match.type === "directory" ? folderTreePath(parts.join("/")) : parts.join("/"));
  }
  return [...paths];
}

export interface FilesTreeMatchesProps {
  /** The matches, relative to the browsed folder. */
  matches: readonly WorkspaceFile[];
  /** The viewed file, relative to the browsed folder, selected when it matches. */
  activeFile: string | null;
  onFileSelect: (path: string) => void;
  label: string;
  dark: boolean;
}

/** Remounts for each new set of matches, so the tree always starts from them. */
export function FilesTreeMatches(props: FilesTreeMatchesProps) {
  const paths = matchTreePaths(props.matches);
  return <MatchesTree key={paths.join("\0")} {...props} paths={paths} />;
}

function MatchesTree({
  paths,
  activeFile,
  onFileSelect,
  label,
  dark,
}: FilesTreeMatchesProps & { paths: string[] }) {
  const onFileSelectRef = useRef(onFileSelect);
  onFileSelectRef.current = onFileSelect;
  // The viewed file is the selection: picking it again opens nothing, any other file opens.
  const shownMatch = activeFile && paths.includes(activeFile) ? activeFile : null;
  const shownMatchRef = useRef(shownMatch);
  shownMatchRef.current = shownMatch;
  const { model } = useFileTree({
    paths,
    flattenEmptyDirectories: true,
    initialExpansion: "open",
    initialSelectedPaths: shownMatch ? [shownMatch] : undefined,
    onSelectionChange: (selected) => {
      const path = selected.at(-1);
      if (!path || path.endsWith("/") || path === shownMatchRef.current) return;
      onFileSelectRef.current(path);
    },
  });
  // A file opened elsewhere, as from the breadcrumbs, moves the selection to it.
  useEffect(() => {
    for (const path of model.getSelectedPaths()) {
      if (path !== shownMatch) model.deselectPath(path);
    }
    if (shownMatch) model.selectPath(shownMatch);
  }, [model, shownMatch]);
  return (
    <FileTree
      model={model}
      aria-label={label}
      className={WORKSPACE_FILE_TREE_CLASS}
      style={{ ...FILE_TREE_THEME, flex: 1, minHeight: 0, colorScheme: dark ? "dark" : "light" }}
    />
  );
}
