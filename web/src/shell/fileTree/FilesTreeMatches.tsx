// A name filter's matches as a tree: each match under its folders, all open, so the
// reader sees where every match lives. Choosing a file opens it, as in the full tree.

import { useEffect, useRef } from "react";
import { FileTree, useFileTree } from "@/components/file-tree/vendored";
import { FILE_TREE_THEME, WORKSPACE_FILE_TREE_CLASS } from "@/components/file-tree/fileTreeStyle";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import { folderTreePath } from "../breadcrumbPaths";
import { SingleSelection } from "./treeSelection";
import { useTreeRowInput } from "./useTreeRowInput";

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
  /** `newTab` for a ⌘-click (Ctrl-click off macOS) or a middle click, which want the file in its own tab. */
  onFileSelect: (path: string, options?: { newTab: true }) => void;
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
  // The viewed file is the selection; a file opens only from the input that chose it.
  const shownMatch = activeFile && paths.includes(activeFile) ? activeFile : null;
  const selectionRef = useRef<SingleSelection | null>(null);
  const { model } = useFileTree({
    paths,
    flattenEmptyDirectories: true,
    initialExpansion: "open",
    initialSelectedPaths: shownMatch ? [shownMatch] : undefined,
    onSelectionChange: () => selectionRef.current?.normalize(),
  });
  selectionRef.current ??= new SingleSelection(model);
  const selection = selectionRef.current;
  const containerRef = useRef<HTMLDivElement>(null);
  useTreeRowInput(containerRef, selection, (path, newTab) => {
    if (path.endsWith("/")) return;
    if (newTab) onFileSelect(path, { newTab: true });
    else onFileSelect(path);
  });
  // A file opened elsewhere, as from the breadcrumbs, moves the selection to it.
  useEffect(() => {
    if (shownMatch) selection.selectWithoutFocus(shownMatch);
    else selection.clear();
  }, [selection, shownMatch]);
  return (
    <div ref={containerRef} className="flex min-h-0 flex-1 flex-col">
      <FileTree
        model={model}
        aria-label={label}
        className={WORKSPACE_FILE_TREE_CLASS}
        style={{ ...FILE_TREE_THEME, flex: 1, minHeight: 0, colorScheme: dark ? "dark" : "light" }}
      />
    </div>
  );
}
