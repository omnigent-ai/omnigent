import { ChevronRightIcon, FileIcon } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  RunnerOfflineError,
  type WorkspaceChangedFile,
  type WorkspaceFile,
  useWorkspaceDirectory,
} from "@/hooks/useWorkspaceChangedFiles";
import { cn } from "@/lib/utils";
import { TooltipProvider } from "@/components/ui/tooltip";
import { RunnerAsleepHint } from "./RunnerAsleepHint";
import { type ChangedSort, compareChangedFiles, type SortableFile } from "./FlatFileList";
import { formatBytes, gitStatusLabel, gitStatusLetter } from "./fileStatusUtils";
import { FileDownloadButton } from "./FileDownloadButton";
import { useCursorTooltip } from "./useCursorTooltip";

// VS Code–style indentation: folder chevron and file icon share the same x
// at each depth. GUIDE_OFFSET centers the indent-guide line under the chevron.
const INDENT_STEP = 16;
const BASE_PAD = 8;
const GUIDE_OFFSET = 7;
const indentFor = (depth: number) => depth * INDENT_STEP + BASE_PAD;

// One vertical guide line per ancestor level; the row must be `relative`.
function IndentGuides({ depth }: { depth: number }) {
  if (depth <= 0) return null;
  return (
    <>
      {Array.from({ length: depth }).map((_, i) => (
        <span
          // biome-ignore lint/suspicious/noArrayIndexKey: fixed positional guides
          key={i}
          aria-hidden
          className="pointer-events-none absolute top-0 bottom-0 w-px bg-border"
          style={{ left: `${indentFor(i) + GUIDE_OFFSET}px` }}
        />
      ))}
    </>
  );
}

// ---------------------------------------------------------------------------
// Directory-tree data model
// ---------------------------------------------------------------------------

interface FileNode {
  type: "file";
  name: string;
  file: WorkspaceFile;
}

interface DirNode {
  type: "dir";
  name: string;
  /** Full path from workspace root, e.g. "src/utils". Used for lazy loading. */
  path: string;
  children: TreeNode[];
  /** Directory mtime when known (from an explicit directory listing entry), so
   *  directories can participate in the "last edited" sort. Undefined for dirs
   *  synthesized from a nested file's path, which carry no mtime of their own. */
  modifiedAt?: number | null;
  /** When true the children come from an explicit directory entry and must be
   *  fetched on demand rather than being statically known from file paths. */
  lazy?: boolean;
}

type TreeNode = FileNode | DirNode;

/** Project a tree node onto the shape the shared file comparator sorts by.
 *  Directories have a name and (when known) an mtime, but never a size. */
function nodeSortable(node: TreeNode): SortableFile {
  if (node.type === "file") return node.file;
  return { name: node.name, path: node.path, bytes: null, modified_at: node.modifiedAt ?? null };
}

/**
 * Comparator for sibling tree nodes. Directories are grouped ahead of files
 * (the file-explorer default), and within each group entries are ordered by the
 * chosen criterion via the shared `compareChangedFiles` comparator — so the All
 * tree and the Changed list order entries identically. Directories carry an
 * mtime (so "last edited" reorders them) but no size, so under a size sort they
 * fall back to name among themselves.
 */
function compareTreeNodes(sort: ChangedSort) {
  const compareFiles = compareChangedFiles(sort);
  return (a: TreeNode, b: TreeNode): number => {
    if (a.type !== b.type) return a.type === "dir" ? -1 : 1;
    return compareFiles(nodeSortable(a), nodeSortable(b));
  };
}

function buildTree(files: WorkspaceFile[], sort: ChangedSort = "alpha"): TreeNode[] {
  const root: DirNode = { type: "dir", name: "", path: "", children: [] };

  for (const file of files) {
    const parts = file.path.split("/");
    let node = root;

    if (file.type === "directory") {
      // Explicit directory entry — create a lazy DirNode whose children will
      // be fetched on demand when the user expands it.
      for (let i = 0; i < parts.length - 1; i++) {
        const part = parts[i];
        let dir = node.children.find((c): c is DirNode => c.type === "dir" && c.name === part);
        if (!dir) {
          dir = { type: "dir", name: part, path: parts.slice(0, i + 1).join("/"), children: [] };
          node.children.push(dir);
        }
        node = dir;
      }
      const lastName = parts[parts.length - 1];
      // Avoid adding a duplicate if a non-lazy DirNode already exists (e.g.
      // created while processing a nested file entry).
      if (!node.children.find((c) => c.type === "dir" && c.name === lastName)) {
        node.children.push({
          type: "dir",
          name: lastName,
          path: file.path,
          children: [],
          modifiedAt: file.modified_at,
          lazy: true,
        });
      }
      continue;
    }

    // File entry — build intermediate DirNodes from path segments.
    for (let i = 0; i < parts.length - 1; i++) {
      const part = parts[i];
      let dir = node.children.find((c): c is DirNode => c.type === "dir" && c.name === part);
      if (!dir) {
        dir = { type: "dir", name: part, path: parts.slice(0, i + 1).join("/"), children: [] };
        node.children.push(dir);
      }
      node = dir;
    }
    node.children.push({ type: "file", name: parts[parts.length - 1], file });
  }

  const compare = compareTreeNodes(sort);
  function sortTree(node: DirNode) {
    node.children.sort(compare);
    for (const child of node.children) {
      if (child.type === "dir") sortTree(child);
    }
  }
  sortTree(root);

  return root.children;
}

// ---------------------------------------------------------------------------
// Expanded-path persistence
// ---------------------------------------------------------------------------

/**
 * Module-level cache that survives component unmount/remount within a JS
 * session (e.g. when the user opens the FileViewer and navigates back).
 * Keyed by conversationId so each conversation has independent state.
 */
const expandedPathsCache = new Map<string, Set<string>>();

/** Compute the default open set: all non-lazy dirs start expanded. */
function defaultExpandedPaths(files: WorkspaceFile[]): Set<string> {
  const tree = buildTree(files);
  const paths = new Set<string>();
  function collect(nodes: TreeNode[]) {
    for (const node of nodes) {
      if (node.type === "dir" && !node.lazy) {
        paths.add(node.path);
        collect(node.children);
      }
    }
  }
  collect(tree);
  return paths;
}

// ---------------------------------------------------------------------------
// FolderTree
// ---------------------------------------------------------------------------

export function FolderTree({
  files,
  isLoading,
  isError,
  error,
  onFileSelect,
  conversationId,
  showHidden,
  onShowHidden,
  changedFiles,
  sort,
  runnerWentOffline = false,
  searchQuery = "",
  searchResults,
  isSearching = false,
  isSearchError = false,
  searchError = null,
}: {
  files: WorkspaceFile[] | undefined;
  isLoading: boolean;
  isError: boolean;
  error: Error | null;
  onFileSelect: (path: string) => void;
  conversationId: string | undefined;
  showHidden: boolean;
  /** Called when the user clicks "Show hidden files" in the search results. */
  onShowHidden?: () => void;
  changedFiles: WorkspaceChangedFile[] | undefined;
  /** Active sort order, shared with the Changed list so both views agree. */
  sort: ChangedSort;
  /**
   * Runner went offline after being connected (session status "failed",
   * e.g. host restarted) — show the reconnect hint. False for a session
   * that just hasn't started, which falls through to the empty state.
   */
  runnerWentOffline?: boolean;
  /** Active search query; when non-empty the component renders a flat results list. */
  searchQuery?: string;
  /** Matching files returned by the server-side search endpoint. */
  searchResults?: WorkspaceFile[];
  /** True while the search request is in flight. */
  isSearching?: boolean;
  /** True when the search request failed. */
  isSearchError?: boolean;
  /** Error from a failed search request. */
  searchError?: Error | null;
}) {
  // Initialise from the module-level cache so expanded state survives
  // unmount/remount (e.g. opening the FileViewer and navigating back).
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(() => {
    if (!conversationId) return new Set();
    const cached = expandedPathsCache.get(conversationId);
    if (cached) return new Set(cached);
    // If files are already available (React Query cache hit), seed defaults now
    // to avoid a flash of all-collapsed state.
    if (files) {
      const initial = defaultExpandedPaths(files);
      expandedPathsCache.set(conversationId, initial);
      return new Set(initial);
    }
    return new Set();
  });

  // When files arrive for the first time (async load) and no cache entry
  // exists yet, compute and persist the default open set.
  useEffect(() => {
    if (!conversationId) return;
    if (!files || expandedPathsCache.has(conversationId)) return;
    const initial = defaultExpandedPaths(files);
    expandedPathsCache.set(conversationId, initial);
    setExpandedPaths(new Set(initial));
  }, [conversationId, files]);

  // Map from file path → change status, for file-level badges in the tree.
  const changedFileMap = useMemo<Map<string, WorkspaceChangedFile["status"]>>(() => {
    if (!changedFiles) return new Map();
    return new Map(changedFiles.map((f) => [f.path, f.status]));
  }, [changedFiles]);

  // Map from directory path → highest-priority change status of any descendant.
  // Priority: created (3) > modified (2) > deleted (1).
  const dirtyDirMap = useMemo<Map<string, WorkspaceChangedFile["status"]>>(() => {
    if (!changedFiles) return new Map();
    const STATUS_PRIORITY = { created: 3, modified: 2, deleted: 1 } as const;
    const result = new Map<string, WorkspaceChangedFile["status"]>();
    for (const file of changedFiles) {
      const parts = file.path.split("/");
      for (let i = 1; i < parts.length; i++) {
        const dirPath = parts.slice(0, i).join("/");
        const existing = result.get(dirPath);
        if (!existing || STATUS_PRIORITY[file.status] > STATUS_PRIORITY[existing]) {
          result.set(dirPath, file.status);
        }
      }
    }
    return result;
  }, [changedFiles]);

  const togglePath = useCallback(
    (path: string) => {
      setExpandedPaths((prev) => {
        const next = new Set(prev);
        if (next.has(path)) next.delete(path);
        else next.add(path);
        if (conversationId) expandedPathsCache.set(conversationId, next);
        return next;
      });
    },
    [conversationId],
  );

  // When a search query is active, render a flat filtered list instead of the tree.
  if (searchQuery.trim().length > 0) {
    if (isSearching && !searchResults) {
      return <p className="px-2 py-1 text-muted-foreground text-xs">Searching…</p>;
    }
    if (isSearchError) {
      return (
        <p className="px-2 py-1 text-destructive text-xs">
          Search failed: {searchError instanceof Error ? searchError.message : "Unknown error"}
        </p>
      );
    }
    if (!searchResults || searchResults.length === 0) {
      return (
        <p className="px-2 py-1 text-muted-foreground text-xs">
          No files match "{searchQuery.trim()}"
        </p>
      );
    }
    const visibleResults = showHidden
      ? searchResults
      : searchResults.filter((f) => !f.path.split("/").some((seg) => seg.startsWith(".")));
    if (visibleResults.length === 0) {
      // There are matches but all are in hidden directories — distinguish from
      // a true zero-match result so the user knows to toggle hidden files.
      const hiddenCount = searchResults.length;
      return (
        <p className="px-2 py-1 text-muted-foreground text-xs">
          {hiddenCount} match{hiddenCount === 1 ? "" : "es"} in hidden directories.{" "}
          <button
            type="button"
            className="cursor-pointer underline hover:text-foreground"
            onClick={() => onShowHidden?.()}
          >
            Show hidden files
          </button>
        </p>
      );
    }
    return (
      <TooltipProvider>
        <ul className="flex flex-col gap-0.5">
          {[...visibleResults].sort(compareChangedFiles(sort)).map((file) => (
            <SearchResultRow
              key={file.path}
              file={file}
              onFileSelect={onFileSelect}
              conversationId={conversationId}
              changedFileMap={changedFileMap}
            />
          ))}
        </ul>
      </TooltipProvider>
    );
  }

  if (isLoading) {
    return <p className="px-2 py-1 text-muted-foreground text-xs">Loading…</p>;
  }
  if (isError) {
    // Runner not connected. If it went offline after being up (host
    // restarted), show the same reconnect hint as the Changed tab; if the
    // session just hasn't started, fall through to the empty state.
    if (error instanceof RunnerOfflineError) {
      if (runnerWentOffline) return <RunnerAsleepHint />;
      return <p className="px-2 py-1 text-muted-foreground text-xs">No files in workspace</p>;
    }
    return (
      <p className="px-2 py-1 text-destructive text-xs">
        Failed to load: {error instanceof Error ? error.message : String(error)}
      </p>
    );
  }
  if (!files || files.length === 0) {
    return <p className="px-2 py-1 text-muted-foreground text-xs">No files in workspace</p>;
  }

  const tree = buildTree(files, sort);
  const visibleTree = showHidden ? tree : tree.filter((n) => !n.name.startsWith("."));
  if (visibleTree.length === 0) {
    return (
      <p className="px-2 py-1 text-muted-foreground text-xs">
        All files are hidden — click the eye icon to reveal them.
      </p>
    );
  }
  return (
    <TooltipProvider>
      <ul className="flex flex-col gap-0.5">
        {visibleTree.map((node) => (
          <TreeNodeRow
            key={node.type === "file" ? node.file.path : node.path}
            node={node}
            depth={0}
            onFileSelect={onFileSelect}
            conversationId={conversationId}
            expandedPaths={expandedPaths}
            onTogglePath={togglePath}
            showHidden={showHidden}
            changedFileMap={changedFileMap}
            dirtyDirMap={dirtyDirMap}
            sort={sort}
          />
        ))}
      </ul>
    </TooltipProvider>
  );
}

// ---------------------------------------------------------------------------
// FileRowItem — shared file-row shell used by both tree and search modes
// ---------------------------------------------------------------------------

/**
 * Renders a single file list item with icon, label, optional status badge,
 * optional file size, and a hover download button.
 *
 * Used by both SearchResultRow (flat search results, full-path label with
 * rtl truncation) and TreeFileRow (tree leaf nodes, filename-only label
 * with depth-based indentation).  Keeping the DOM structure in one place
 * prevents the two views from drifting apart over time.
 */
function FileRowItem({
  path,
  displayLabel,
  labelIsPath = false,
  depth = 0,
  fileStatus,
  bytes,
  onFileSelect,
  conversationId,
}: {
  /** Canonical workspace-relative path, used for the download button and title. */
  path: string;
  /** Text shown in the label span — full path for search results, filename for tree. */
  displayLabel: string;
  /** When true the label uses rtl truncation and wraps content in <bdi>. */
  labelIsPath?: boolean;
  /** Tree depth (0 = root). Drives left indentation and indent guides; search
   *  results pass 0 for a flat, guide-less list. The file icon sits in the
   *  same column as a folder's chevron at this depth. */
  depth?: number;
  fileStatus: WorkspaceChangedFile["status"] | undefined;
  bytes: number | null;
  onFileSelect: (path: string) => void;
  conversationId: string | undefined;
}) {
  const isDeleted = fileStatus === "deleted";
  const fileColorClass =
    fileStatus === "created"
      ? "text-green-500 dark:text-green-400"
      : fileStatus === "modified"
        ? "text-amber-500 dark:text-amber-400"
        : isDeleted
          ? "text-destructive"
          : undefined;
  const { handlers, tooltip } = useCursorTooltip(path);

  return (
    <li>
      <div
        className="group relative flex w-full min-w-0 items-center gap-1.5 rounded-md py-1 pr-2 hover:bg-muted"
        style={{ paddingLeft: `${indentFor(depth)}px` }}
      >
        <IndentGuides depth={depth} />
        <button
          type="button"
          className="flex min-w-0 flex-1 cursor-pointer items-center gap-1.5 text-left"
          onClick={() => !isDeleted && onFileSelect(path)}
          disabled={isDeleted}
        >
          <FileIcon
            className={cn("size-3.5 shrink-0", fileColorClass ?? "text-muted-foreground")}
          />
          <span
            className={cn(
              "min-w-0 flex-1 truncate font-mono text-sm md:text-xs",
              labelIsPath ? "[direction:rtl]" : fileStatus === "created" && "font-semibold",
              isDeleted && "line-through opacity-50",
              fileColorClass,
            )}
            {...handlers}
          >
            {labelIsPath ? <bdi>{displayLabel}</bdi> : displayLabel}
          </span>
          {fileStatus && (
            <span
              className={cn(
                "shrink-0 rounded px-1 py-0.5 font-mono text-[10px]",
                isDeleted
                  ? "bg-destructive/10 text-destructive"
                  : fileStatus === "created"
                    ? "bg-green-500/10 text-green-600 dark:text-green-400"
                    : "bg-amber-500/10 text-amber-600 dark:text-amber-400",
              )}
              title={gitStatusLabel(fileStatus)}
            >
              {gitStatusLetter(fileStatus)}
            </span>
          )}
          {bytes !== null && !isDeleted && (
            <span className="shrink-0 text-muted-foreground text-[10px]">{formatBytes(bytes)}</span>
          )}
        </button>
        {!isDeleted && conversationId && (
          <FileDownloadButton conversationId={conversationId} path={path} />
        )}
      </div>
      {tooltip}
    </li>
  );
}

// ---------------------------------------------------------------------------
// SearchResultRow — flat row used in search-results mode
// ---------------------------------------------------------------------------

function SearchResultRow({
  file,
  onFileSelect,
  conversationId,
  changedFileMap,
}: {
  file: WorkspaceFile;
  onFileSelect: (path: string) => void;
  conversationId: string | undefined;
  changedFileMap: Map<string, WorkspaceChangedFile["status"]>;
}) {
  return (
    <FileRowItem
      path={file.path}
      displayLabel={file.path}
      labelIsPath={true}
      fileStatus={changedFileMap.get(file.path)}
      bytes={file.bytes}
      onFileSelect={onFileSelect}
      conversationId={conversationId}
    />
  );
}

// ---------------------------------------------------------------------------
// TreeFileRow — file leaf node with hover download button
// ---------------------------------------------------------------------------

function TreeFileRow({
  node,
  depth,
  onFileSelect,
  conversationId,
  fileStatus,
}: {
  node: FileNode;
  depth: number;
  onFileSelect: (path: string) => void;
  conversationId: string | undefined;
  fileStatus: WorkspaceChangedFile["status"] | undefined;
}) {
  return (
    <FileRowItem
      path={node.file.path}
      displayLabel={node.name}
      depth={depth}
      fileStatus={fileStatus}
      bytes={node.file.bytes}
      onFileSelect={onFileSelect}
      conversationId={conversationId}
    />
  );
}

// ---------------------------------------------------------------------------
// TreeNodeRow
// ---------------------------------------------------------------------------

function TreeNodeRow({
  node,
  depth,
  onFileSelect,
  conversationId,
  expandedPaths,
  onTogglePath,
  showHidden,
  changedFileMap,
  dirtyDirMap,
  sort,
}: {
  node: TreeNode;
  depth: number;
  onFileSelect: (path: string) => void;
  conversationId: string | undefined;
  expandedPaths: Set<string>;
  onTogglePath: (path: string) => void;
  showHidden: boolean;
  changedFileMap: Map<string, WorkspaceChangedFile["status"]>;
  dirtyDirMap: Map<string, WorkspaceChangedFile["status"]>;
  sort: ChangedSort;
}) {
  const open = node.type === "dir" && expandedPaths.has(node.path);
  const isLazyDir = node.type === "dir" && node.lazy === true;

  // Fetch children on demand when a lazy directory is expanded.
  const { data: lazyData, isLoading: lazyLoading } = useWorkspaceDirectory(
    conversationId,
    isLazyDir && open ? node.path : null,
  );

  if (node.type === "file") {
    return (
      <TreeFileRow
        node={node}
        depth={depth}
        onFileSelect={onFileSelect}
        conversationId={conversationId}
        fileStatus={changedFileMap.get(node.file.path)}
      />
    );
  }

  // Build the child node list: for lazy dirs use fetched data (converted
  // directly — no need to run buildTree since the API returns one level at a
  // time); otherwise use the statically known children.
  const rawChildNodes: TreeNode[] =
    isLazyDir && lazyData
      ? lazyData
          .map((file): TreeNode => {
            if (file.type === "directory") {
              return {
                type: "dir",
                name: file.name,
                path: file.path,
                children: [],
                modifiedAt: file.modified_at,
                lazy: true,
              };
            }
            return { type: "file", name: file.name, file };
          })
          .sort(compareTreeNodes(sort))
      : node.children;
  const childNodes = showHidden
    ? rawChildNodes
    : rawChildNodes.filter((n) => !n.name.startsWith("."));

  const dirStatus = dirtyDirMap.get(node.path);
  const dirDotClass =
    dirStatus === "created"
      ? "text-green-500 dark:text-green-400"
      : dirStatus === "modified"
        ? "text-amber-500 dark:text-amber-400"
        : dirStatus === "deleted"
          ? "text-destructive"
          : undefined;

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
        <span
          className={cn(
            "min-w-0 flex-1 truncate font-mono text-sm md:text-xs",
            dirStatus === "created" && "font-semibold",
            dirDotClass,
          )}
        >
          {node.name}/
        </span>
        {dirStatus && (
          <span className={cn("shrink-0 text-[8px] leading-none", dirDotClass)} aria-hidden>
            ●
          </span>
        )}
      </button>
      {open && (
        <ul className="flex flex-col gap-0.5">
          {lazyLoading && (
            <li
              className="relative py-1 pr-2 text-muted-foreground text-xs"
              style={{ paddingLeft: `${indentFor(depth + 1)}px` }}
            >
              <IndentGuides depth={depth + 1} />
              Loading…
            </li>
          )}
          {!lazyLoading && childNodes.length === 0 && rawChildNodes.length > 0 && (
            <li
              className="relative py-1 pr-2 text-muted-foreground text-xs"
              style={{ paddingLeft: `${indentFor(depth + 1)}px` }}
            >
              <IndentGuides depth={depth + 1} />
              All files are hidden — click the eye icon to reveal them.
            </li>
          )}
          {childNodes.map((child) => (
            <TreeNodeRow
              key={child.type === "file" ? child.file.path : child.path}
              node={child}
              depth={depth + 1}
              onFileSelect={onFileSelect}
              conversationId={conversationId}
              expandedPaths={expandedPaths}
              onTogglePath={onTogglePath}
              showHidden={showHidden}
              changedFileMap={changedFileMap}
              dirtyDirMap={dirtyDirMap}
              sort={sort}
            />
          ))}
        </ul>
      )}
    </li>
  );
}
