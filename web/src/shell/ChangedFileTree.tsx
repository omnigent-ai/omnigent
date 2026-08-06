import { ChevronRightIcon } from "lucide-react";
import { useCallback, useState } from "react";
import { RunnerOfflineError, type WorkspaceChangedFile } from "@/hooks/useWorkspaceChangedFiles";
import { cn } from "@/lib/utils";
import { TooltipProvider } from "@/components/ui/tooltip";
import { RunnerAsleepHint } from "./RunnerAsleepHint";
import { type ChangedSort, compareChangedFiles, type SortableFile } from "./FlatFileList";
import { FileRowItem, IndentGuides, indentFor } from "./FolderTree";

// ---------------------------------------------------------------------------
// Tree data model — the changed set is fully known up front (no lazy loading),
// so the whole tree is built synchronously from the flat file list.
// ---------------------------------------------------------------------------

interface ChangedFileNode {
  type: "file";
  name: string;
  file: WorkspaceChangedFile;
}

interface ChangedDirNode {
  type: "dir";
  name: string;
  /** Full path from workspace root, e.g. "src/utils" — the toggle key. */
  path: string;
  children: ChangedTreeNode[];
}

type ChangedTreeNode = ChangedFileNode | ChangedDirNode;

/** Project a node onto the shape the shared comparator sorts by. Directories
 *  in the changed tree carry neither size nor mtime, so under those sorts they
 *  fall back to name among themselves. */
function nodeSortable(node: ChangedTreeNode): SortableFile {
  if (node.type === "file") return node.file;
  return { name: node.name, path: node.path, bytes: null, modified_at: null };
}

/** Sibling comparator: directories group ahead of files (explorer default),
 *  then entries order by the active sort via the shared comparator — so the
 *  tree and the flat Changed list agree on ordering. */
function compareChangedNodes(sort: ChangedSort) {
  const compareFiles = compareChangedFiles(sort);
  return (a: ChangedTreeNode, b: ChangedTreeNode): number => {
    if (a.type !== b.type) return a.type === "dir" ? -1 : 1;
    return compareFiles(nodeSortable(a), nodeSortable(b));
  };
}

function buildChangedTree(files: WorkspaceChangedFile[], sort: ChangedSort): ChangedTreeNode[] {
  const root: ChangedDirNode = { type: "dir", name: "", path: "", children: [] };
  for (const file of files) {
    const parts = file.path.split("/");
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const part = parts[i];
      let dir = node.children.find((c): c is ChangedDirNode => c.type === "dir" && c.name === part);
      if (!dir) {
        dir = { type: "dir", name: part, path: parts.slice(0, i + 1).join("/"), children: [] };
        node.children.push(dir);
      }
      node = dir;
    }
    node.children.push({ type: "file", name: parts[parts.length - 1], file });
  }

  const compare = compareChangedNodes(sort);
  function sortTree(node: ChangedDirNode) {
    node.children.sort(compare);
    for (const child of node.children) {
      if (child.type === "dir") sortTree(child);
    }
  }
  sortTree(root);
  return root.children;
}

function normalizeSearchQuery(query: string): string {
  return query.trim().toLowerCase();
}

function isHidden(path: string): boolean {
  return path.split("/").some((seg) => seg.startsWith("."));
}

// ---------------------------------------------------------------------------
// ChangedFileTree — the "Changed" scope rendered as a collapsible folder tree.
// Mirrors FlatFileList's loading / error / empty / hidden-files states so the
// list and tree layouts of the Changed scope behave identically apart from
// their arrangement. Folders start fully expanded (VS Code Source Control
// style); the toggle tracks an explicit *collapsed* set, so folders that
// appear as new changes land stay expanded by default.
// ---------------------------------------------------------------------------

export function ChangedFileTree({
  files,
  isLoading,
  isError,
  error,
  onFileSelect,
  showHidden,
  onShowHidden,
  searchQuery,
  sort,
  conversationId,
  runnerWentOffline = false,
}: {
  files: WorkspaceChangedFile[] | undefined;
  isLoading: boolean;
  isError: boolean;
  error: Error | null;
  onFileSelect: (path: string) => void;
  showHidden: boolean;
  onShowHidden: () => void;
  searchQuery: string;
  sort: ChangedSort;
  conversationId: string | undefined;
  /** Runner went offline after connecting (session "failed") — show the
   *  reconnect hint instead of the generic empty state. */
  runnerWentOffline?: boolean;
}) {
  // Folders the user has explicitly collapsed. Empty = everything expanded,
  // which is the required "fully expanded initially" default and keeps newly
  // arriving changed folders open without a recompute effect.
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const togglePath = useCallback((path: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  if (isLoading) {
    return <p className="px-2 py-1 text-muted-foreground text-xs">Loading…</p>;
  }
  if (isError) {
    // Runner not connected. Distinguish "went offline after being up" (show the
    // reconnect hint) from "hasn't started yet" (normal empty state), matching
    // FlatFileList so the two layouts read identically.
    if (error instanceof RunnerOfflineError) {
      if (runnerWentOffline) return <RunnerAsleepHint />;
      return <p className="px-2 py-1 text-muted-foreground text-xs">No workspace changes yet</p>;
    }
    return (
      <p className="px-2 py-1 text-destructive text-xs">
        Failed to load: {error instanceof Error ? error.message : String(error)}
      </p>
    );
  }
  if (!files || files.length === 0) {
    return <p className="px-2 py-1 text-muted-foreground text-xs">No workspace changes yet</p>;
  }

  const visibleFiles = showHidden ? files : files.filter((f) => !isHidden(f.path));
  const hiddenCount = files.length - visibleFiles.length;
  if (visibleFiles.length === 0) {
    return (
      <p className="px-2 py-1 text-muted-foreground text-xs">
        All changes are in hidden files.{" "}
        <button
          type="button"
          className="cursor-pointer underline hover:text-foreground"
          onClick={onShowHidden}
        >
          Click to show
        </button>
      </p>
    );
  }

  const normalizedSearch = normalizeSearchQuery(searchQuery);
  const searchActive = normalizedSearch.length > 0;
  const matchedFiles = searchActive
    ? visibleFiles.filter(
        (f) =>
          f.name.toLowerCase().includes(normalizedSearch) ||
          f.path.toLowerCase().includes(normalizedSearch),
      )
    : visibleFiles;
  if (matchedFiles.length === 0) {
    return (
      <p className="px-2 py-1 text-muted-foreground text-xs">
        No changed files match "{searchQuery.trim()}"
      </p>
    );
  }

  const tree = buildChangedTree(matchedFiles, sort);

  return (
    <>
      {hiddenCount > 0 && (
        <p className="px-2 py-1 text-muted-foreground text-xs">
          {hiddenCount} file{hiddenCount === 1 ? "" : "s"} hidden.{" "}
          <button
            type="button"
            className="cursor-pointer underline hover:text-foreground"
            onClick={onShowHidden}
          >
            Click to show
          </button>
        </p>
      )}
      <TooltipProvider>
        <ul className="flex flex-col gap-0.5">
          {tree.map((node) => (
            <ChangedTreeNodeRow
              key={node.type === "file" ? node.file.path : node.path}
              node={node}
              depth={0}
              onFileSelect={onFileSelect}
              conversationId={conversationId}
              collapsed={collapsed}
              onTogglePath={togglePath}
              // A search forces every branch open so matches deep in a
              // collapsed folder still surface.
              forceExpanded={searchActive}
            />
          ))}
        </ul>
      </TooltipProvider>
    </>
  );
}

// ---------------------------------------------------------------------------
// ChangedTreeNodeRow
// ---------------------------------------------------------------------------

function ChangedTreeNodeRow({
  node,
  depth,
  onFileSelect,
  conversationId,
  collapsed,
  onTogglePath,
  forceExpanded,
}: {
  node: ChangedTreeNode;
  depth: number;
  onFileSelect: (path: string) => void;
  conversationId: string | undefined;
  collapsed: Set<string>;
  onTogglePath: (path: string) => void;
  forceExpanded: boolean;
}) {
  if (node.type === "file") {
    return (
      <FileRowItem
        path={node.file.path}
        displayLabel={node.name}
        depth={depth}
        fileStatus={node.file.status}
        bytes={node.file.bytes}
        onFileSelect={onFileSelect}
        conversationId={conversationId}
      />
    );
  }

  const open = forceExpanded || !collapsed.has(node.path);
  return (
    <li>
      <button
        type="button"
        className="group relative flex w-full min-w-0 cursor-pointer items-center gap-1.5 rounded-md py-1 pr-2 text-left hover:bg-muted"
        style={{ paddingLeft: `${indentFor(depth)}px` }}
        onClick={() => onTogglePath(node.path)}
        aria-expanded={open}
      >
        <IndentGuides depth={depth} />
        <ChevronRightIcon
          className={cn(
            "size-3.5 shrink-0 text-muted-foreground transition-transform",
            open && "rotate-90",
          )}
        />
        <span className="min-w-0 flex-1 truncate font-mono text-sm md:text-xs">{node.name}/</span>
      </button>
      {open && (
        <ul className="flex flex-col gap-0.5">
          {node.children.map((child) => (
            <ChangedTreeNodeRow
              key={child.type === "file" ? child.file.path : child.path}
              node={child}
              depth={depth + 1}
              onFileSelect={onFileSelect}
              conversationId={conversationId}
              collapsed={collapsed}
              onTogglePath={onTogglePath}
              forceExpanded={forceExpanded}
            />
          ))}
        </ul>
      )}
    </li>
  );
}
