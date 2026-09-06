import { ChevronRightIcon, FileIcon } from "lucide-react";
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { RefObject } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  type DirectoryResult,
  RunnerOfflineError,
  type WorkspaceChangedFile,
  type WorkspaceFile,
  useWorkspaceDirectories,
} from "@/hooks/useWorkspaceChangedFiles";
import { cn } from "@/lib/utils";
import { TooltipProvider } from "@/components/ui/tooltip";
import { RunnerAsleepHint } from "./RunnerAsleepHint";
import { type ChangedSort, compareChangedFiles, type SortableFile } from "./FlatFileList";
import {
  ROW_ACTION_SIZE_CLASS,
  ROW_META_SLOT_CLASS,
  ROW_STATUS_SLOT_CLASS,
  formatBytes,
  gitStatusLabel,
  gitStatusLetter,
} from "./fileStatusUtils";
import { CopyPathButton } from "./CopyPathButton";
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
      {Array.from({ length: depth }, (_, level) => indentFor(level) + GUIDE_OFFSET).map((left) => (
        <span
          key={left}
          aria-hidden
          className="pointer-events-none absolute top-0 bottom-0 w-px bg-border"
          style={{ left: `${left}px` }}
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
// Flattening the visible tree for virtualization
// ---------------------------------------------------------------------------

/**
 * One rendered line in the virtualized tree. A `node` row is a real dir/file;
 * a `placeholder` row is the inline "Loading…" / "all hidden" line a lazy dir
 * shows in place of its children. Every row carries its `depth` for indent and
 * a stable `key` for React reconciliation across window slides.
 */
type FlatRow =
  | {
      kind: "node";
      key: string;
      depth: number;
      node: TreeNode;
      open: boolean;
      lazyLoading: boolean;
    }
  | { kind: "placeholder"; key: string; depth: number; text: string };

/** Convert one level of fetched lazy-dir entries into sorted TreeNodes. */
function lazyChildrenToNodes(entries: WorkspaceFile[], sort: ChangedSort): TreeNode[] {
  return entries
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
    .sort(compareTreeNodes(sort));
}

/**
 * Depth-first flatten of the currently-visible tree into a linear row list.
 *
 * Descends only through expanded directories; a lazy dir's children come from
 * `dirData` (fetched centrally, see {@link useWorkspaceDirectories}) rather than
 * from the node itself. Emits the same inline "Loading…" / "all hidden"
 * placeholder rows the recursive renderer used, so behaviour is unchanged — the
 * output is just a flat array the virtualizer can window.
 */
function flattenTree(
  roots: TreeNode[],
  opts: {
    expandedPaths: Set<string>;
    showHidden: boolean;
    sort: ChangedSort;
    dirData: Map<string, DirectoryResult>;
  },
): FlatRow[] {
  const { expandedPaths, showHidden, sort, dirData } = opts;
  const rows: FlatRow[] = [];
  const visible = (nodes: TreeNode[]) =>
    showHidden ? nodes : nodes.filter((n) => !n.name.startsWith("."));

  function walk(nodes: TreeNode[], depth: number) {
    for (const node of nodes) {
      if (node.type === "file") {
        rows.push({
          kind: "node",
          key: node.file.path,
          depth,
          node,
          open: false,
          lazyLoading: false,
        });
        continue;
      }
      const open = expandedPaths.has(node.path);
      const isLazyDir = node.lazy === true;
      const lazy = isLazyDir && open ? dirData.get(node.path) : undefined;
      const lazyLoading = !!lazy?.isLoading && lazy?.data === undefined;
      const lazyError = !!lazy?.isError && lazy?.data === undefined;
      rows.push({ kind: "node", key: node.path, depth, node, open, lazyLoading });
      if (!open) continue;

      const rawChildren = isLazyDir
        ? lazy?.data
          ? lazyChildrenToNodes(lazy.data, sort)
          : []
        : node.children;
      const children = visible(rawChildren);
      // Placeholder keys are prefixed by kind (not the raw path) so they can't
      // collide with a real file/dir path.
      if (lazyLoading) {
        rows.push({
          kind: "placeholder",
          key: `loading:${node.path}`,
          depth: depth + 1,
          text: "Loading…",
        });
      } else if (lazyError) {
        rows.push({
          kind: "placeholder",
          key: `error:${node.path}`,
          depth: depth + 1,
          text: "Failed to load this folder.",
        });
      } else if (children.length === 0 && rawChildren.length > 0) {
        rows.push({
          kind: "placeholder",
          key: `hidden:${node.path}`,
          depth: depth + 1,
          text: "All files are hidden — click the eye icon to reveal them.",
        });
      }
      walk(children, depth + 1);
    }
  }
  walk(visible(roots), 0);
  return rows;
}

/**
 * Every expanded lazy-directory path reachable through currently-available
 * data — the set to fetch. Descends a lazy dir only once its own listing is in
 * `dirData`, so deeper expanded lazy dirs are discovered incrementally as their
 * parents' fetches land (each arrival re-renders and widens the set).
 */
function expandedLazyPaths(
  roots: TreeNode[],
  expandedPaths: Set<string>,
  showHidden: boolean,
  sort: ChangedSort,
  dirData: Map<string, DirectoryResult>,
): string[] {
  const out: string[] = [];
  const visible = (nodes: TreeNode[]) =>
    showHidden ? nodes : nodes.filter((n) => !n.name.startsWith("."));
  function walk(nodes: TreeNode[]) {
    for (const node of nodes) {
      if (node.type !== "dir" || !expandedPaths.has(node.path)) continue;
      if (node.lazy === true) {
        out.push(node.path);
        const data = dirData.get(node.path)?.data;
        if (data) walk(visible(lazyChildrenToNodes(data, sort)));
      } else {
        walk(visible(node.children));
      }
    }
  }
  walk(visible(roots));
  return out;
}

// ---------------------------------------------------------------------------
// Expanded-path persistence
// ---------------------------------------------------------------------------

/**
 * Module-level cache that survives component unmount/remount within a JS
 * session (e.g. when the user opens the FileViewer and navigates back).
 *
 * Keyed by conversation AND browse location: node paths are relative to the
 * browsed root, so a set captured at one root describes different directories
 * at another. Carrying it across a re-root would collapse the new tree (its
 * paths match nothing) and could expand an unrelated same-named folder.
 * Keying by both also means navigating back restores what was open there.
 */
const expandedPathsCache = new Map<string, Set<string>>();

/** Cache key for one conversation's tree at one browsed root. */
function expandedCacheKey(conversationId: string, browseLocation: string): string {
  return `${conversationId}\u0000${browseLocation}`;
}

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
  browseLocation = "",
  onNavigateDir,
  scrollParentRef,
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
  /**
   * Absolute path currently browsed, or "" for the workspace root. Lazy
   * directory expansion resolves node paths against it.
   */
  browseLocation?: string;
  /**
   * Re-root the panel onto one of the tree's directories, given its path
   * relative to the current location. Double-clicking a folder opens it,
   * matching Finder; a single click still just expands in place.
   */
  onNavigateDir?: (relativePath: string) => void;
  /**
   * The scroll container the tree lives in (FilesPanel's `<section>`). When
   * provided, the virtualizer windows rows against THIS element so it shares
   * one scroller with the panel's scroll-position persistence — rather than a
   * second nested scroll container the restore logic wouldn't track. When
   * omitted (tests, stories, standalone card mode), the tree falls back to its
   * own internal scroll container.
   */
  scrollParentRef?: RefObject<HTMLElement | null>;
}) {
  // Initialise from the module-level cache so expanded state survives
  // unmount/remount (e.g. opening the FileViewer and navigating back).
  const cacheKey = conversationId ? expandedCacheKey(conversationId, browseLocation) : null;
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(() => {
    if (!cacheKey) return new Set();
    const cached = expandedPathsCache.get(cacheKey);
    if (cached) return new Set(cached);
    // If files are already available (React Query cache hit), seed defaults now
    // to avoid a flash of all-collapsed state.
    if (files) {
      const initial = defaultExpandedPaths(files);
      expandedPathsCache.set(cacheKey, initial);
      return new Set(initial);
    }
    return new Set();
  });

  // When files arrive for the first time (async load) and no cache entry
  // exists yet, compute and persist the default open set. Also re-sync from
  // the cache when the panel switches conversations OR re-roots onto another
  // directory without remounting — otherwise the tree keeps an expanded set
  // describing a different root. A layout effect so the switch resolves
  // before paint (no collapsed flash).
  const expandedForRef = useRef(cacheKey);
  useLayoutEffect(() => {
    if (!cacheKey) return;
    const switched = expandedForRef.current !== cacheKey;
    expandedForRef.current = cacheKey;
    const cached = expandedPathsCache.get(cacheKey);
    if (cached) {
      if (switched) setExpandedPaths(new Set(cached));
      return;
    }
    if (!files) return;
    const initial = defaultExpandedPaths(files);
    expandedPathsCache.set(cacheKey, initial);
    setExpandedPaths(new Set(initial));
  }, [cacheKey, files]);

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
        if (cacheKey) expandedPathsCache.set(cacheKey, next);
        return next;
      });
    },
    [cacheKey],
  );

  // Build the nested top-level tree once per files/sort/showHidden change.
  // Hoisted above the early returns (Rules of Hooks) and memoized so an
  // unrelated re-render (a background refetch toggling isFetching, a store tick)
  // doesn't rebuild it. `undefined` when there are no files yet.
  const visibleTree = useMemo<TreeNode[] | undefined>(() => {
    if (!files || files.length === 0) return undefined;
    const tree = buildTree(files, sort);
    return showHidden ? tree : tree.filter((n) => !n.name.startsWith("."));
  }, [files, sort, showHidden]);

  // Fetch every expanded lazy directory's listing centrally (not per row), so
  // rows the virtualizer scrolls out of view can unmount without dropping their
  // fetch. The set of paths to fetch is grown in state to a fixpoint: each pass
  // descends only into expanded lazy dirs whose parent listing is already in
  // `dirData`, so as one level's fetch lands the effect widens the set by the
  // next level, until it stops changing. Holding it in state (rather than
  // deriving it inline from a lagging ref) guarantees a real re-render drives
  // each widening step — so a restored/re-rooted multi-level expansion resolves
  // all the way down, and it converges the same whether listings arrive async
  // or are already cached.
  const [lazyPaths, setLazyPaths] = useState<string[]>([]);
  const dirData = useWorkspaceDirectories(conversationId, lazyPaths, browseLocation);
  useEffect(() => {
    const next = visibleTree
      ? expandedLazyPaths(visibleTree, expandedPaths, showHidden, sort, dirData)
      : [];
    setLazyPaths((prev) =>
      prev.length === next.length && prev.every((p, i) => p === next[i]) ? prev : next,
    );
  }, [visibleTree, expandedPaths, showHidden, sort, dirData]);

  // Flatten the visible tree into linear rows for the virtualizer.
  const flatRows = useMemo<FlatRow[]>(
    () =>
      visibleTree ? flattenTree(visibleTree, { expandedPaths, showHidden, sort, dirData }) : [],
    [visibleTree, expandedPaths, showHidden, sort, dirData],
  );

  // Virtualized scroller: window the flat rows against the panel's own scroll
  // container when one is passed in (so windowing and scroll-position
  // persistence share one scroller), else against a fallback internal one.
  const ownScrollRef = useRef<HTMLDivElement>(null);
  const scrollElementRef = scrollParentRef ?? ownScrollRef;
  const rowVirtualizer = useVirtualizer({
    count: flatRows.length,
    getScrollElement: () => scrollElementRef.current,
    estimateSize: () => 28,
    overscan: 12,
    getItemKey: (index) => flatRows[index]?.key ?? index,
  });

  // When a search query is active, render a flat filtered list instead of the tree.
  if (searchQuery.trim().length > 0) {
    if (isSearching && !searchResults) {
      return <p className="px-2 py-1 text-muted-foreground text-sm">Searching…</p>;
    }
    if (isSearchError) {
      return (
        <p className="px-2 py-1 text-destructive text-sm">
          Search failed: {searchError instanceof Error ? searchError.message : "Unknown error"}
        </p>
      );
    }
    if (!searchResults || searchResults.length === 0) {
      return (
        <p className="px-2 py-1 text-muted-foreground text-sm">
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
        <p className="px-2 py-1 text-muted-foreground text-sm">
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
    return <p className="px-2 py-1 text-muted-foreground text-sm">Loading…</p>;
  }
  if (isError) {
    // Runner not connected. If it went offline after being up (host
    // restarted), show the same reconnect hint as the Changed tab; if the
    // session just hasn't started, fall through to the empty state.
    if (error instanceof RunnerOfflineError) {
      if (runnerWentOffline) return <RunnerAsleepHint />;
      return <p className="px-2 py-1 text-muted-foreground text-sm">No files in workspace</p>;
    }
    return (
      <p className="px-2 py-1 text-destructive text-sm">
        Failed to load: {error instanceof Error ? error.message : String(error)}
      </p>
    );
  }
  if (!files || files.length === 0 || visibleTree === undefined) {
    return <p className="px-2 py-1 text-muted-foreground text-sm">No files in workspace</p>;
  }

  if (visibleTree.length === 0) {
    return (
      <p className="px-2 py-1 text-muted-foreground text-sm">
        All files are hidden — click the eye icon to reveal them.
      </p>
    );
  }
  const virtualItems = rowVirtualizer.getVirtualItems();
  // Spacer sized to the full row count so the scroll container scrolls the whole
  // tree; only the windowed slice is mounted, positioned absolutely by
  // translateY.
  const spacer = (
    <div className="relative w-full" style={{ height: `${rowVirtualizer.getTotalSize()}px` }}>
      {virtualItems.map((vi) => {
        const row = flatRows[vi.index];
        if (!row) return null;
        return (
          <div
            key={vi.key}
            data-index={vi.index}
            ref={rowVirtualizer.measureElement}
            className="absolute top-0 left-0 w-full"
            style={{ transform: `translateY(${vi.start}px)` }}
          >
            {row.kind === "placeholder" ? (
              <p
                className="relative py-1 pr-2 text-muted-foreground text-sm"
                style={{ paddingLeft: `${indentFor(row.depth)}px` }}
              >
                <IndentGuides depth={row.depth} />
                {row.text}
              </p>
            ) : (
              <TreeNodeRow
                node={row.node}
                depth={row.depth}
                open={row.open}
                onFileSelect={onFileSelect}
                conversationId={conversationId}
                onTogglePath={togglePath}
                changedFileMap={changedFileMap}
                dirtyDirMap={dirtyDirMap}
                onNavigateDir={onNavigateDir}
              />
            )}
          </div>
        );
      })}
    </div>
  );
  return (
    <TooltipProvider>
      {/* When the panel owns the scroller (scrollParentRef), render the spacer
          straight into it; otherwise provide a fallback scroll container. */}
      {scrollParentRef ? (
        spacer
      ) : (
        <div ref={ownScrollRef} className="h-full overflow-y-auto">
          {spacer}
        </div>
      )}
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
              "min-w-0 flex-1 truncate font-mono text-ui md:text-sm",
              labelIsPath ? "[direction:rtl]" : fileStatus === "created" && "font-semibold",
              isDeleted && "line-through opacity-50",
              fileColorClass,
            )}
            {...handlers}
          >
            {labelIsPath ? <bdi>{displayLabel}</bdi> : displayLabel}
          </span>
          {fileStatus && (
            // Centred in the shared status column so the A/M/D badge lands in
            // the same x as a directory row's dirty dot.
            <span
              className={cn("flex shrink-0 items-center justify-center", ROW_STATUS_SLOT_CLASS)}
            >
              <span
                className={cn(
                  "rounded px-1 py-0.5 font-mono text-[10px]",
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
            </span>
          )}
        </button>
        {/* One trailing column, always rendered so every row (directories
            included) shares it: metadata at rest, the copy/download pair on
            hover. */}
        <span
          className={cn("relative flex shrink-0 items-center justify-end", ROW_META_SLOT_CLASS)}
        >
          {bytes !== null && !isDeleted && (
            <span className="text-muted-foreground text-[10px] group-hover:invisible">
              {formatBytes(bytes)}
            </span>
          )}
          <span className="absolute inset-0 flex items-center justify-end gap-0.5">
            {!isDeleted && conversationId ? (
              <FileDownloadButton conversationId={conversationId} path={path} />
            ) : (
              <span className={cn("shrink-0", ROW_ACTION_SIZE_CLASS)} aria-hidden />
            )}
            <CopyPathButton path={path} revealOnHover />
          </span>
        </span>
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

/**
 * One presentational tree row — a file leaf or a directory header. Pure: it
 * owns no hooks, does no fetching, and never renders its children. Expansion
 * (`open`), lazy-child fetching, and descendant rows are all handled by the
 * flattening pass in FolderTree; this component just paints a single line.
 * Memoized so a window slide re-renders only the rows that actually change.
 */
const TreeNodeRow = memo(function TreeNodeRow({
  node,
  depth,
  open,
  onFileSelect,
  conversationId,
  onTogglePath,
  changedFileMap,
  dirtyDirMap,
  onNavigateDir,
}: {
  node: TreeNode;
  depth: number;
  /** Whether this directory is expanded (ignored for file nodes). */
  open: boolean;
  onFileSelect: (path: string) => void;
  conversationId: string | undefined;
  onTogglePath: (path: string) => void;
  changedFileMap: Map<string, WorkspaceChangedFile["status"]>;
  dirtyDirMap: Map<string, WorkspaceChangedFile["status"]>;
  /** Re-root onto a directory (double-click), path relative to the root. */
  onNavigateDir?: (relativePath: string) => void;
}) {
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
    // The row is a div, not a button: the copy button below is a sibling of the
    // toggle, and a button nested inside a button is invalid HTML. The toggle
    // still spans everything up to the copy button, so the clickable area is
    // effectively unchanged.
    <div
      className="group relative flex w-full min-w-0 items-center gap-1.5 rounded-md py-1 pr-2 hover:bg-muted"
      style={{ paddingLeft: `${indentFor(depth)}px` }}
    >
      <IndentGuides depth={depth} />
      <button
        type="button"
        className="flex min-w-0 flex-1 cursor-pointer items-center gap-1.5 text-left"
        onClick={() => onTogglePath(node.path)}
        // Finder's contract: single click expands in place, double click opens
        // the folder as the new working folder. The browser fires the two
        // single clicks first, so the row toggles twice (a no-op) before
        // re-rooting replaces the tree outright.
        onDoubleClick={onNavigateDir ? () => onNavigateDir(node.path) : undefined}
        aria-expanded={open}
      >
        <ChevronRightIcon
          className={cn(
            "size-3.5 shrink-0 text-muted-foreground transition-transform",
            open && "rotate-90",
          )}
        />
        <span
          className={cn(
            "min-w-0 flex-1 truncate font-mono text-ui md:text-sm",
            dirStatus === "created" && "font-semibold",
            dirDotClass,
          )}
        >
          {node.name}/
        </span>
        {dirStatus && (
          <span
            className={cn("flex shrink-0 items-center justify-center", ROW_STATUS_SLOT_CLASS)}
            aria-hidden
          >
            <span className={cn("text-[8px] leading-none", dirDotClass)}>●</span>
          </span>
        )}
      </button>
      {/* The same trailing column as a file row. A folder has no size and
          nothing to download, so the column shows only the copy button — with
          the download's footprint reserved beside it so that button lands in
          the same x as every file row's. */}
      <span className={cn("relative flex shrink-0 items-center justify-end", ROW_META_SLOT_CLASS)}>
        <span className="absolute inset-0 flex items-center justify-end gap-0.5">
          <span className={cn("shrink-0", ROW_ACTION_SIZE_CLASS)} aria-hidden />
          <CopyPathButton path={node.path} label="Copy folder path" revealOnHover />
        </span>
      </span>
    </div>
  );
});
