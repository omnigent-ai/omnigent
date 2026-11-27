import { PathStore } from "./path-store";
import type {
  PathStorePathInfo,
  PathStoreVisibleAncestorRow,
  PathStoreVisibleRowContext,
  PathStoreVisibleRow as PathStoreVisibleRowData,
  PathStoreVisibleTreeProjectionData,
} from "./path-store";
import {
  arePathSetsEqual,
  getAncestorDirectoryPaths,
  getImmediateParentPath,
  toLowerCaseSearchPath,
} from "./pathHelpers";
import { type FileTreeGitStatusState, resolveFileTreeGitStatusState } from "./gitStatus";
import { normalizeSearchQuery } from "./searchHelpers";
import type {
  FileTreeBatchOperation,
  FileTreeOptions,
  FileTreeDirectoryHandle,
  FileTreeFileHandle,
  FileTreeItemHandle,
  FileTreeScrollOffset,
  FileTreeScrollRequest,
  FileTreeScrollToPathOptions,
  FileTreeVisibleRow,
  GitStatusEntry,
} from "./types";

type ProjectionIndexBuffer = Int32Array<ArrayBufferLike>;

interface FileTreeVisibleProjection {
  focusedIndex: number;
  getParentIndex: (index: number) => number;
  paths: readonly string[];
  posInSetByIndex: ProjectionIndexBuffer;
  setSizeByIndex: ProjectionIndexBuffer;
}

// Initial render only mounts a tiny viewport slice, so model startup can
// cap its first projection build and defer the full-tree metadata walk
// until the user actually navigates outside that initial window.
const INITIAL_PROJECTION_ROW_LIMIT = 512;
const CONTEXT_VISIBLE_ROW_RANGE_LIMIT = 512;

function normalizeScrollOffset(
  offset: FileTreeScrollToPathOptions["offset"],
): FileTreeScrollOffset {
  return offset === "top" || offset === "center" ? offset : "nearest";
}

// Keeps focus resolution cheap after expand/collapse by asking for only the
// candidate path and its ancestors instead of forcing a full visible-index map.
function resolveFocusedIndexByLookup(
  rowCount: number,
  getVisibleIndex: (path: string) => number | null,
  candidatePath: string | null,
): number {
  if (rowCount === 0) {
    return -1;
  }

  if (candidatePath != null) {
    const directIndex = getVisibleIndex(candidatePath);
    if (directIndex != null) {
      return directIndex;
    }

    const ancestorPaths = getAncestorDirectoryPaths(candidatePath);
    for (let index = ancestorPaths.length - 1; index >= 0; index -= 1) {
      const ancestorPath = ancestorPaths[index];
      if (ancestorPath == null) {
        continue;
      }

      const ancestorIndex = getVisibleIndex(ancestorPath);
      if (ancestorIndex != null) {
        return ancestorIndex;
      }
    }
  }

  return 0;
}

// Rebuilds the visible-row projection once so focus/navigation can use
// path-first metadata without recomputing sibling and parent info per render.
// Derives the row metadata that the renderer needs for roving tabindex and
// treeitem ARIA attrs without exposing PathStore's numeric row identities.
function createVisibleProjection(
  projection: PathStoreVisibleTreeProjectionData,
  focusedPathCandidate: string | null,
  resolveVisibleIndexByPath?: (path: string) => number | null,
): FileTreeVisibleProjection {
  if (projection.paths.length === 0) {
    return {
      focusedIndex: -1,
      getParentIndex: projection.getParentIndex,
      paths: projection.paths,
      posInSetByIndex: projection.posInSetByIndex,
      setSizeByIndex: projection.setSizeByIndex,
    };
  }

  if (focusedPathCandidate == null) {
    return {
      focusedIndex: 0,
      getParentIndex: projection.getParentIndex,
      paths: projection.paths,
      posInSetByIndex: projection.posInSetByIndex,
      setSizeByIndex: projection.setSizeByIndex,
    };
  }

  const getVisibleIndex =
    resolveVisibleIndexByPath ??
    ((path: string): number | null => projection.visibleIndexByPath.get(path) ?? null);
  return {
    focusedIndex: resolveFocusedIndexByLookup(
      projection.paths.length,
      getVisibleIndex,
      focusedPathCandidate,
    ),
    getParentIndex: projection.getParentIndex,
    paths: projection.paths,
    posInSetByIndex: projection.posInSetByIndex,
    setSizeByIndex: projection.setSizeByIndex,
  };
}

/**
 * Owns the tree's path store, behind paths rather than store IDs, plus focus,
 * selection, expansion and search. `FileTree` renders it; callers drive it
 * imperatively. Options are read once at construction.
 */
export class FileTreeModel {
  readonly coloredIcons: boolean;
  #gitStatus: FileTreeGitStatusState | null;
  readonly #listeners = new Set<() => void>();
  readonly #onSelectionChange: FileTreeOptions["onSelectionChange"];
  #emittedSelectionVersion = 0;
  #version = 0;
  #ancestorIndicesByIndex = new Map<number, readonly number[]>();
  #ancestorPathsByIndex = new Map<number, readonly string[]>();
  #focusedIndex = -1;
  #focusedPath: string | null = null;
  #hasFullProjection = false;
  #getParentIndexForVisibleRow = (_index: number): number => -1;
  #itemHandles = new Map<string, FileTreeItemHandle>();
  #knownDirectoryPaths: readonly string[] | null = null;
  #knownDirectoryPathsLowerCase: readonly string[] | null = null;
  #knownPaths: readonly string[] | null = null;
  #listedPaths: readonly string[] | null = null;
  #listedPathsLowerCase: readonly string[] | null = null;
  #projectionPaths: readonly string[] = [];
  #projectionPosInSetByIndex: ProjectionIndexBuffer = new Int32Array(0);
  #projectionSetSizeByIndex: ProjectionIndexBuffer = new Int32Array(0);
  #searchCollapsedOverrides = new Set<string>();
  #searchMatchPathSet = new Set<string>();
  #searchPreviousExpandedPaths: readonly string[] | null = null;
  #searchValue: string | null = null;
  #searchVisiblePathSet: Set<string> | null = null;
  #searchVisibleIndexByPath: Map<string, number> | null = null;
  #searchVisibleIndices: readonly number[] | null = null;
  #searchVisiblePaths: readonly string[] | null = null;
  #scrollRequest: FileTreeScrollRequest | null = null;
  #scrollRequestId = 0;
  #selectionAnchorPath: string | null = null;
  #selectedPaths = new Set<string>();
  #selectionVersion = 0;
  #store: PathStore;
  readonly #storeOptions: Pick<
    FileTreeOptions,
    "flattenEmptyDirectories" | "initialExpansion" | "sort"
  >;
  #storeVisibleCount = 0;
  #suppressStoreNotifications = false;
  #visibleCount = 0;

  public constructor(options: FileTreeOptions) {
    const {
      coloredIcons = true,
      gitStatus,
      initialSearchQuery,
      initialSelectedPaths,
      onSelectionChange,
      ...storeOptions
    } = options;
    this.coloredIcons = coloredIcons;
    this.#gitStatus = resolveFileTreeGitStatusState(gitStatus);
    this.#onSelectionChange = onSelectionChange;
    this.#storeOptions = {
      flattenEmptyDirectories: storeOptions.flattenEmptyDirectories,
      initialExpansion: storeOptions.initialExpansion,
      sort: storeOptions.sort,
    };
    this.#store = new PathStore(storeOptions);
    const resolvedInitialSelectedPaths =
      initialSelectedPaths
        ?.map((path) => this.#resolveSelectionPath(path))
        .filter((resolved): resolved is string => resolved != null) ?? [];
    const initialFocusedPath = resolvedInitialSelectedPaths.at(-1) ?? null;
    if (resolvedInitialSelectedPaths.length > 0) {
      this.#selectedPaths = new Set(resolvedInitialSelectedPaths);
      this.#selectionAnchorPath = initialFocusedPath;
      this.#selectionVersion = 1;
    }
    this.#rebuildVisibleProjection(initialFocusedPath, false);
    if (initialSearchQuery != null) {
      this.#setSearchState(initialSearchQuery, false);
    }
    this.#emittedSelectionVersion = this.#selectionVersion;
    this.#subscribeToStore();
  }

  public focusFirstItem(): void {
    if (this.#getCurrentVisiblePaths().length > 0) {
      this.#setFocusedIndex(0);
    }
  }

  public focusLastItem(): void {
    if (this.#visibleCount <= 0) {
      return;
    }

    this.#ensureFullProjection();
    this.#setFocusedIndex(this.#visibleCount - 1);
  }

  public focusNextItem(): void {
    this.#moveFocus(1);
  }

  public focusParentItem(): void {
    if (this.#focusedPath == null) {
      return;
    }

    const parentPath = getImmediateParentPath(this.#focusedPath);
    if (parentPath == null) {
      return;
    }

    const nextFocusedIndex = this.#resolveFocusedIndex(parentPath);
    if (nextFocusedIndex >= 0) {
      this.#setFocusedIndex(nextFocusedIndex);
    }
  }

  public focusPath(path: string): void {
    const resolvedPath = this.#store.getPathInfo(path)?.path ?? null;
    if (resolvedPath == null) {
      return;
    }

    this.#ensureFullProjection();
    const nextFocusedIndex = this.#resolveFocusedIndex(resolvedPath);
    if (nextFocusedIndex >= 0) {
      this.#setFocusedIndex(nextFocusedIndex);
    }
  }

  // Records a one-shot scroll request for the mounted view. By default the
  // target also becomes the model-focused row; callers can pass `focus: false`
  // to reveal a row without changing model focus or DOM focus.
  public scrollToPath(path: string, options?: FileTreeScrollToPathOptions): void {
    const resolvedPath = this.#store.getPathInfo(path)?.path ?? null;
    if (resolvedPath == null) {
      return;
    }

    this.#ensureFullProjection();
    const targetIndex = this.#getExactCurrentVisibleIndexByPath(resolvedPath);
    if (targetIndex < 0) {
      return;
    }

    const targetPath = this.#resolveVisiblePathAtIndex(targetIndex);
    if (targetPath == null) {
      return;
    }

    if (options?.focus !== false) {
      this.#setFocusedIndex(targetIndex, false);
    }
    this.#scrollRequest = {
      id: (this.#scrollRequestId += 1),
      offset: normalizeScrollOffset(options?.offset),
      path: targetPath,
      visibleIndex: targetIndex,
    };
    this.#emit();
  }

  /** The row index that shows `path` now, or -1 when it's gone or inside a closed folder. */
  public getVisibleIndexOfPath(path: string): number {
    const resolvedPath = this.#store.getPathInfo(path)?.path ?? null;
    if (resolvedPath == null) {
      return -1;
    }

    this.#ensureFullProjection();
    return this.#getExactCurrentVisibleIndexByPath(resolvedPath);
  }

  // DOM row events already know the target row is mounted, so they can focus it
  // by path without materializing every visible row in large open trees.
  public focusMountedPathFromInput(path: string): void {
    const resolvedPath = this.#store.getPathInfo(path)?.path ?? null;
    if (resolvedPath == null) {
      return;
    }

    const nextFocusedIndex = this.#resolveFocusedIndex(resolvedPath);
    if (nextFocusedIndex >= 0) {
      this.#setFocusedIndex(nextFocusedIndex);
    }
  }

  public focusPreviousItem(): void {
    this.#moveFocus(-1);
  }

  public getFocusedIndex(): number {
    return this.#focusedIndex;
  }

  public getFocusedItem(): FileTreeItemHandle | null {
    return this.#focusedPath == null ? null : this.#getOrCreateItemHandle(this.#focusedPath);
  }

  public getFocusedPath(): string | null {
    return this.#focusedPath;
  }

  public getScrollRequest(): FileTreeScrollRequest | null {
    return this.#scrollRequest;
  }

  public clearScrollRequest(id: number): void {
    if (this.#scrollRequest?.id === id) {
      this.#scrollRequest = null;
    }
  }

  public getSelectedPaths(): readonly string[] {
    return [...this.#selectedPaths];
  }

  /** Increments on every state change; a snapshot for `useSyncExternalStore`. */
  public getVersion(): number {
    return this.#version;
  }

  public getVisibleCount(): number {
    return this.#visibleCount;
  }

  public getVisibleRows(start: number, end: number): readonly FileTreeVisibleRow[] {
    if (end < start || this.#visibleCount === 0) {
      return [];
    }

    const boundedStart = Math.max(0, start);
    const boundedEnd = Math.min(this.#visibleCount - 1, end);
    if (boundedEnd < boundedStart) {
      return [];
    }

    const boundedLength = boundedEnd - boundedStart + 1;
    if (
      this.#searchVisibleIndices == null &&
      !this.#hasFullProjection &&
      boundedEnd >= this.#projectionPaths.length &&
      boundedLength <= CONTEXT_VISIBLE_ROW_RANGE_LIMIT
    ) {
      const rows: FileTreeVisibleRow[] = [];
      for (let index = boundedStart; index <= boundedEnd; index += 1) {
        const context = this.#store.getVisibleRowContext(index);
        if (context == null) {
          break;
        }

        rows.push(this.#createVisibleRowFromContext(context));
      }
      return rows;
    }

    if (!this.#hasFullProjection && boundedEnd >= this.#projectionPaths.length) {
      this.#ensureFullProjection();
    }

    if (this.#searchVisibleIndices != null) {
      const projectionIndices = Array.from(
        { length: boundedEnd - boundedStart + 1 },
        (_, visibleOffset) =>
          this.#getProjectionIndexFromVisibleIndex(boundedStart + visibleOffset),
      );
      const visibleRowByProjectionIndex = new Map<
        number,
        ReturnType<PathStore["getVisibleSlice"]>[number]
      >();
      let runStartIndex = projectionIndices[0] ?? -1;
      let runEndIndex = runStartIndex;
      for (let index = 1; index <= projectionIndices.length; index += 1) {
        const projectionIndex = projectionIndices[index];
        if (projectionIndex != null && projectionIndex === runEndIndex + 1) {
          runEndIndex = projectionIndex;
          continue;
        }

        if (runStartIndex >= 0) {
          const visibleSlice = this.#store.getVisibleSlice(runStartIndex, runEndIndex);
          visibleSlice.forEach((row, offset) => {
            visibleRowByProjectionIndex.set(runStartIndex + offset, row);
          });
        }

        if (projectionIndex == null) {
          runStartIndex = -1;
          runEndIndex = -1;
          continue;
        }

        runStartIndex = projectionIndex;
        runEndIndex = projectionIndex;
      }

      return Array.from({ length: boundedEnd - boundedStart + 1 }, (_, visibleOffset) => {
        const visibleIndex = boundedStart + visibleOffset;
        const projectionIndex = this.#getProjectionIndexFromVisibleIndex(visibleIndex);
        const row = visibleRowByProjectionIndex.get(projectionIndex);
        const projectionPath = this.#projectionPaths[projectionIndex];
        if (row == null || projectionPath == null) {
          throw new Error(
            `Missing projection row for filtered visible index ${String(visibleIndex)}`,
          );
        }

        return this.#createVisibleRow(row, visibleIndex, projectionIndex, {
          ancestorPaths: this.#getAncestorPaths(projectionIndex),
          path: projectionPath,
        });
      });
    }

    return this.#store.getVisibleSlice(boundedStart, boundedEnd).map((row, offset) => {
      const index = boundedStart + offset;
      const projectionPath = this.#projectionPaths[index];
      if (projectionPath == null) {
        throw new Error(`Missing projection path for visible index ${String(index)}`);
      }

      return this.#createVisibleRow(row, index, index, {
        ancestorPaths: this.#getAncestorPaths(index),
        path: projectionPath,
      });
    });
  }

  /**
   * Returns the item handle for the given path.
   *
   * Accepts both canonical directory paths (`src/`) and bare directory lookup
   * paths (`src`) so callers do not need to know the canonical slash rules.
   */
  public getItem(path: string): FileTreeItemHandle | null {
    const itemInfo = this.#store.getPathInfo(path);
    return itemInfo == null ? null : this.#getOrCreateItemHandle(itemInfo.path, itemInfo);
  }

  // Only use this for paths sourced from currently mounted directory rows. The
  // mounted-row invariant lets click handling skip public handle creation while
  // still revalidating stale DOM events against the live store.
  public resolveMountedDirectoryPathFromInput(path: string): string | null {
    const pathInfo = this.#store.getPathInfo(path);
    return pathInfo?.kind === "directory" ? pathInfo.path : null;
  }

  // Only use this for paths sourced from currently mounted directory rows. The
  // live-path check prevents stale DOM events from throwing if the row was
  // removed or became a file before the click handler ran.
  public toggleMountedDirectoryFromInput(path: string): void {
    const directoryPath = this.resolveMountedDirectoryPathFromInput(path);
    if (directoryPath == null) {
      return;
    }

    // During a search, record when the user collapses a matched directory so
    // the search keeps it collapsed. A second toggle of the same directory
    // clears the override and returns the directory to the search default.
    if (this.#searchValue != null && this.#searchValue.length > 0) {
      if (this.#searchCollapsedOverrides.has(directoryPath)) {
        this.#searchCollapsedOverrides.delete(directoryPath);
      } else if (this.#store.isExpanded(directoryPath)) {
        this.#searchCollapsedOverrides.add(directoryPath);
      }
    }

    this.#toggleDirectory(directoryPath);
  }

  public selectAllVisiblePaths(): void {
    this.#ensureFullProjection();
    const nextSelectedPaths = [...this.#getCurrentVisiblePaths()];
    this.#applySelection(nextSelectedPaths, this.#focusedPath ?? this.#selectionAnchorPath);
  }

  // Only use this for paths sourced from currently mounted rows. Visible rows
  // already provide canonical public paths, so regular row clicks can update
  // selection without re-normalizing the same path through the store.
  public selectOnlyMountedPathFromInput(path: string): void {
    this.#applySelection([path], path);
  }

  public selectPath(path: string): void {
    const resolvedPath = this.#resolveSelectionPath(path);
    if (resolvedPath == null || this.#selectedPaths.has(resolvedPath)) {
      return;
    }

    this.#applySelection([...this.#selectedPaths, resolvedPath]);
  }

  public deselectPath(path: string): void {
    const resolvedPath = this.#resolveSelectionPath(path);
    if (resolvedPath == null || !this.#selectedPaths.has(resolvedPath)) {
      return;
    }

    this.#applySelection(
      [...this.#selectedPaths].filter((selectedPath) => selectedPath !== resolvedPath),
    );
  }

  public toggleFocusedSelection(): void {
    if (this.#focusedPath == null) {
      return;
    }

    this.togglePathSelectionFromInput(this.#focusedPath);
  }

  public togglePathSelectionFromInput(path: string): void {
    const resolvedPath = this.#resolveSelectionPath(path);
    if (resolvedPath == null) {
      return;
    }

    if (this.#selectedPaths.has(resolvedPath)) {
      this.#applySelection(
        [...this.#selectedPaths].filter((selectedPath) => selectedPath !== resolvedPath),
        resolvedPath,
      );
      return;
    }

    this.#applySelection([...this.#selectedPaths, resolvedPath], resolvedPath);
  }

  public selectPathRange(path: string, unionSelection: boolean): void {
    const resolvedPath = this.#resolveSelectionPath(path);
    if (resolvedPath == null) {
      return;
    }

    this.#ensureFullProjection();
    const anchorPath = this.#selectionAnchorPath;
    const anchorIndex = anchorPath == null ? -1 : this.#getVisibleIndexByPath(anchorPath);
    const targetIndex = this.#getVisibleIndexByPath(resolvedPath);
    if (anchorIndex === -1 || targetIndex === -1) {
      const nextSelectedPaths = unionSelection
        ? [...this.#selectedPaths, resolvedPath]
        : [resolvedPath];
      this.#applySelection(nextSelectedPaths, resolvedPath);
      return;
    }

    const [startIndex, endIndex] =
      anchorIndex <= targetIndex ? [anchorIndex, targetIndex] : [targetIndex, anchorIndex];
    const rangePaths = this.#getCurrentVisiblePaths().slice(startIndex, endIndex + 1);
    const nextSelectedPaths = unionSelection ? [...this.#selectedPaths, ...rangePaths] : rangePaths;
    this.#applySelection(nextSelectedPaths, anchorPath);
  }

  public extendSelectionFromFocused(offset: -1 | 1): void {
    if (this.#focusedPath == null) {
      return;
    }

    const focusedIndex = this.#focusedIndex;
    if (focusedIndex === -1) {
      return;
    }

    const nextIndex = Math.min(this.#visibleCount - 1, Math.max(0, focusedIndex + offset));
    if (nextIndex === focusedIndex) {
      return;
    }

    if (!this.#hasFullProjection && nextIndex >= this.#projectionPaths.length) {
      this.#ensureFullProjection();
    }

    const visiblePaths = this.#getCurrentVisiblePaths();
    const currentPath = visiblePaths[focusedIndex] ?? null;
    const nextPath = visiblePaths[nextIndex] ?? null;
    if (currentPath == null || nextPath == null) {
      return;
    }

    const nextSelectedPaths = new Set(this.#selectedPaths);
    if (nextSelectedPaths.has(currentPath) && nextSelectedPaths.has(nextPath)) {
      nextSelectedPaths.delete(currentPath);
    } else {
      nextSelectedPaths.add(nextPath);
    }

    this.#applySelection([...nextSelectedPaths], this.#selectionAnchorPath ?? currentPath, false);
    this.#setFocusedIndex(nextIndex);
  }

  /** Calls `listener` after every state change. */
  public subscribe(listener: () => void): () => void {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  }

  /** The resolved git statuses the rows show, or null when the tree has none. */
  public get gitStatus(): FileTreeGitStatusState | null {
    return this.#gitStatus;
  }

  /** Replaces the git statuses, so rows and folder marks repaint. */
  public setGitStatus(entries: readonly GitStatusEntry[] | undefined): void {
    this.#gitStatus = resolveFileTreeGitStatusState(entries);
    this.#emit();
  }

  // The tree is read-only for users; callers grow it, for example when a lazily
  // listed directory finishes loading its children.
  public addPaths(paths: readonly string[]): void {
    if (paths.length === 0) {
      return;
    }

    this.#store.addPaths(paths);
  }

  /**
   * Applies adds and removes in order. Adds grow the tree in place; any removal
   * rebuilds it from the remaining paths, keeping expansion, selection and focus
   * on rows that are still there. A folder left empty by a removal stays.
   */
  public batch(operations: readonly FileTreeBatchOperation[]): void {
    if (operations.length === 0) {
      return;
    }

    if (operations.every((operation) => operation.type === "add")) {
      this.addPaths(operations.map((operation) => operation.path));
      return;
    }

    const paths = new Set(this.#getListedPaths());
    const removedFolders: string[] = [];
    const emptiedFolders = new Set<string>();
    for (const operation of operations) {
      if (operation.type === "add") {
        paths.add(operation.path);
        continue;
      }

      const removed = this.#store.getPathInfo(operation.path)?.path ?? operation.path;
      const folder = removed.endsWith("/") && operation.recursive === true ? removed : null;
      for (const path of [...paths]) {
        if (path === removed || (folder !== null && path.startsWith(folder))) {
          paths.delete(path);
        }
      }
      if (folder !== null) {
        removedFolders.push(folder);
      }
      const parent = getImmediateParentPath(removed);
      if (parent != null) {
        emptiedFolders.add(parent);
      }
    }

    for (const folder of emptiedFolders) {
      const gone = removedFolders.some((removed) => folder.startsWith(removed));
      const occupied = [...paths].some((path) => path.startsWith(folder));
      if (!gone && !occupied) {
        paths.add(folder);
      }
    }

    this.#rebuildStore([...paths], new Set(this.#getExpandedDirectoryPaths()));
  }

  /**
   * Rebuilds the tree from `paths`, for example to apply a new sort. Folders in
   * `initialExpandedPaths` open and the rest close; without it, open folders
   * stay open. Selection and focus stay on rows that are still there.
   */
  public resetPaths(
    paths: readonly string[],
    options: { initialExpandedPaths?: readonly string[] } = {},
  ): void {
    const expanded =
      options.initialExpandedPaths === undefined
        ? new Set(this.#getExpandedDirectoryPaths())
        : new Set(options.initialExpandedPaths);
    this.#rebuildStore(paths, expanded);
  }

  public setSearch(value: string | null): void {
    this.#setSearchState(value, true);
  }

  public isSearchOpen(): boolean {
    return this.#searchValue !== null;
  }

  #resolveFocusedIndex(path: string): number {
    const directIndex = this.#getVisibleIndexByPath(path);
    if (directIndex !== -1) {
      return directIndex;
    }

    const ancestorPaths = getAncestorDirectoryPaths(path);
    for (let index = ancestorPaths.length - 1; index >= 0; index -= 1) {
      const ancestorPath = ancestorPaths[index];
      if (ancestorPath == null) {
        continue;
      }

      const ancestorIndex = this.#getVisibleIndexByPath(ancestorPath);
      if (ancestorIndex !== -1) {
        return ancestorIndex;
      }
    }

    return this.#getCurrentVisiblePaths().length > 0 ? 0 : -1;
  }

  #getOrCreateItemHandle(path: string, itemInfo?: PathStorePathInfo): FileTreeItemHandle | null {
    const cachedHandle = this.#itemHandles.get(path);
    if (cachedHandle != null) {
      return cachedHandle;
    }

    const resolvedItemInfo = itemInfo ?? this.#store.getPathInfo(path);
    if (resolvedItemInfo == null) {
      return null;
    }

    const handle =
      resolvedItemInfo.kind === "directory"
        ? this.#createDirectoryHandle(resolvedItemInfo.path)
        : this.#createFileHandle(resolvedItemInfo.path);
    this.#itemHandles.set(resolvedItemInfo.path, handle);
    return handle;
  }

  #createVisibleRow(
    row: PathStoreVisibleRowData,
    visibleIndex: number,
    projectionIndex: number,
    projection: {
      ancestorPaths: readonly string[];
      path: string;
      posInSet?: number;
      setSize?: number;
    },
  ): FileTreeVisibleRow {
    return {
      ancestorPaths: projection.ancestorPaths,
      depth: row.depth,
      flattenedSegments: row.flattenedSegments?.map((segment) => ({
        isTerminal: segment.isTerminal,
        name: segment.name,
        path: segment.path,
      })),
      hasChildren: row.hasChildren,
      index: visibleIndex,
      isExpanded: row.isExpanded,
      isFlattened: row.isFlattened,
      isFocused: projection.path === this.#focusedPath,
      isSelected: this.#selectedPaths.has(projection.path),
      kind: row.kind,
      name: row.name,
      path: projection.path,
      posInSet: projection.posInSet ?? this.#projectionPosInSetByIndex[projectionIndex] ?? 0,
      setSize: projection.setSize ?? this.#projectionSetSizeByIndex[projectionIndex] ?? 0,
    };
  }

  #createVisibleRowFromContext(
    context: PathStoreVisibleRowContext | PathStoreVisibleAncestorRow,
  ): FileTreeVisibleRow {
    return this.#createVisibleRow(context.row, context.index, context.index, {
      ancestorPaths: context.ancestorPaths,
      path: context.row.path,
      posInSet: context.posInSet,
      setSize: context.setSize,
    });
  }

  #getAncestorIndices(index: number): readonly number[] {
    const cached = this.#ancestorIndicesByIndex.get(index);
    if (cached != null) {
      return cached;
    }

    const parentIndex = this.#getParentIndexForVisibleRow(index);
    const ancestorIndices =
      parentIndex < 0 ? [] : [...this.#getAncestorIndices(parentIndex), parentIndex];
    this.#ancestorIndicesByIndex.set(index, ancestorIndices);
    return ancestorIndices;
  }

  #getAncestorPaths(index: number): readonly string[] {
    const cached = this.#ancestorPathsByIndex.get(index);
    if (cached != null) {
      return cached;
    }

    const ancestorPaths = this.#getAncestorIndices(index)
      .map((ancestorIndex) => this.#projectionPaths[ancestorIndex] ?? "")
      .filter((path) => path !== "");
    this.#ancestorPathsByIndex.set(index, ancestorPaths);
    return ancestorPaths;
  }

  #collapseDirectory(path: string): void {
    this.#store.collapse(path);
  }

  #applySelection(
    nextSelectedPaths: readonly string[],
    nextAnchorPath: string | null = this.#selectionAnchorPath,
    emit = true,
  ): void {
    const uniqueSelectedPaths = [...new Set(nextSelectedPaths)];
    const selectionChanged = !arePathSetsEqual(this.#selectedPaths, uniqueSelectedPaths);
    const anchorChanged = this.#selectionAnchorPath !== nextAnchorPath;
    if (!selectionChanged && !anchorChanged) {
      return;
    }

    this.#selectedPaths = new Set(uniqueSelectedPaths);
    this.#selectionAnchorPath = nextAnchorPath;
    if (selectionChanged) {
      this.#selectionVersion += 1;
    }
    if (emit) {
      this.#emit();
    }
  }

  #createDirectoryHandle(path: string): FileTreeDirectoryHandle {
    return {
      collapse: () => {
        this.#collapseDirectory(path);
      },
      deselect: () => {
        this.deselectPath(path);
      },
      expand: () => {
        this.#expandDirectory(path);
      },
      focus: () => {
        this.focusPath(path);
      },
      getPath: () => path,
      isDirectory: () => true,
      isExpanded: () => this.#store.isExpanded(path),
      isFocused: () => this.#focusedPath === path,
      isSelected: () => this.#selectedPaths.has(path),
      select: () => {
        this.selectPath(path);
      },
      toggle: () => {
        this.#toggleDirectory(path);
      },
    };
  }

  #createFileHandle(path: string): FileTreeFileHandle {
    return {
      deselect: () => {
        this.deselectPath(path);
      },
      focus: () => {
        this.focusPath(path);
      },
      getPath: () => path,
      isDirectory: () => false,
      isFocused: () => this.#focusedPath === path,
      isSelected: () => this.#selectedPaths.has(path),
      select: () => {
        this.selectPath(path);
      },
    };
  }

  #getListedPaths(): readonly string[] {
    if (this.#listedPaths != null) {
      return this.#listedPaths;
    }

    this.#listedPaths = this.#store.list();
    return this.#listedPaths;
  }

  #getAllKnownPaths(): readonly string[] {
    if (this.#knownPaths != null) {
      return this.#knownPaths;
    }

    const knownPaths = new Set<string>();
    for (const path of this.#getListedPaths()) {
      knownPaths.add(path);
      for (const ancestorPath of getAncestorDirectoryPaths(path)) {
        knownPaths.add(ancestorPath);
      }
    }

    this.#knownPaths = [...knownPaths].sort();
    return this.#knownPaths;
  }

  // Cache lowercased path keys once so incremental search does not re-normalize
  // every file and directory path on each keystroke.
  #getListedPathsLowerCase(): readonly string[] {
    if (this.#listedPathsLowerCase != null) {
      return this.#listedPathsLowerCase;
    }

    this.#listedPathsLowerCase = this.#getListedPaths().map(toLowerCaseSearchPath);
    return this.#listedPathsLowerCase;
  }

  #getAllKnownDirectoryPaths(): readonly string[] {
    if (this.#knownDirectoryPaths != null) {
      return this.#knownDirectoryPaths;
    }

    this.#knownDirectoryPaths = this.#getAllKnownPaths().filter((path) => path.endsWith("/"));
    return this.#knownDirectoryPaths;
  }

  #getAllKnownDirectoryPathsLowerCase(): readonly string[] {
    if (this.#knownDirectoryPathsLowerCase != null) {
      return this.#knownDirectoryPathsLowerCase;
    }

    this.#knownDirectoryPathsLowerCase =
      this.#getAllKnownDirectoryPaths().map(toLowerCaseSearchPath);
    return this.#knownDirectoryPathsLowerCase;
  }

  #invalidateKnownPathCaches(): void {
    this.#knownDirectoryPaths = null;
    this.#knownDirectoryPathsLowerCase = null;
    this.#knownPaths = null;
    this.#listedPaths = null;
    this.#listedPathsLowerCase = null;
  }

  #getExpandedDirectoryPaths(): readonly string[] {
    return this.#getAllKnownDirectoryPaths().filter((path) => this.#store.isExpanded(path));
  }

  #restoreSearchExpandedPaths(keepSelectedOpen: boolean): void {
    const expandedPaths = new Set(this.#searchPreviousExpandedPaths ?? []);
    if (keepSelectedOpen) {
      for (const selectedPath of this.#selectedPaths) {
        for (const ancestorPath of getAncestorDirectoryPaths(selectedPath)) {
          expandedPaths.add(ancestorPath);
        }
      }
    }
    this.#setExpandedPaths(expandedPaths);
  }

  #setExpandedPaths(expandedPaths: ReadonlySet<string>): void {
    this.#suppressStoreNotifications = true;
    try {
      for (const directoryPath of this.#getAllKnownDirectoryPaths()) {
        const shouldExpand = expandedPaths.has(directoryPath);
        const isExpanded = this.#store.isExpanded(directoryPath);
        if (shouldExpand && !isExpanded) {
          this.#store.expand(directoryPath);
        } else if (!shouldExpand && isExpanded) {
          this.#store.collapse(directoryPath);
        }
      }
    } finally {
      this.#suppressStoreNotifications = false;
    }
  }

  #syncSearchVisibilityState(): void {
    if (this.#searchValue == null || this.#searchValue.length === 0) {
      this.#searchVisibleIndices = null;
      this.#searchVisiblePaths = null;
      this.#searchVisibleIndexByPath = null;
      this.#visibleCount = this.#storeVisibleCount;
      return;
    }

    const currentVisiblePaths = this.#projectionPaths;
    if (this.#searchMatchPathSet.size === 0) {
      this.#searchVisibleIndices = null;
      this.#searchVisiblePaths = null;
      this.#searchVisibleIndexByPath = null;
      this.#visibleCount = this.#storeVisibleCount;
      return;
    }

    const visibleIndices: number[] = [];
    const visiblePaths: string[] = [];
    const visibleIndexByPath = new Map<string, number>();
    const searchVisiblePathSet = this.#searchVisiblePathSet;
    for (const [index, path] of currentVisiblePaths.entries()) {
      if (searchVisiblePathSet?.has(path) !== true) {
        continue;
      }

      visibleIndexByPath.set(path, visiblePaths.length);
      visibleIndices.push(index);
      visiblePaths.push(path);
    }

    this.#searchVisibleIndices = visibleIndices;
    this.#searchVisiblePaths = visiblePaths;
    this.#searchVisibleIndexByPath = visibleIndexByPath;
    this.#visibleCount = visiblePaths.length;
  }

  #getCurrentVisiblePaths(): readonly string[] {
    return this.#searchVisiblePaths ?? this.#projectionPaths;
  }

  #getExactCurrentVisibleIndexByPath(path: string): number {
    if (this.#searchVisiblePaths != null) {
      return this.#searchVisibleIndexByPath?.get(path) ?? -1;
    }

    return this.#store.getVisibleIndex(path) ?? -1;
  }

  #getProjectionIndexFromVisibleIndex(index: number): number {
    return this.#searchVisibleIndices?.[index] ?? index;
  }

  #getVisibleIndexByPath(path: string): number {
    const searchIndex = this.#searchVisibleIndexByPath?.get(path);
    if (searchIndex != null) {
      return searchIndex;
    }

    return this.#store.getVisibleIndex(path) ?? -1;
  }

  #setSearchState(value: string | null, emitChange: boolean): void {
    const normalizedValue = value == null ? null : normalizeSearchQuery(value);
    const previousSearch = this.#searchValue;
    if (previousSearch === normalizedValue) {
      return;
    }

    if (previousSearch == null && normalizedValue != null) {
      this.#searchPreviousExpandedPaths = this.#getExpandedDirectoryPaths();
    }

    this.#searchValue = normalizedValue;

    if (normalizedValue == null) {
      this.#searchCollapsedOverrides.clear();
      this.#restoreSearchExpandedPaths(true);
      this.#searchPreviousExpandedPaths = null;
      this.#searchMatchPathSet.clear();
      this.#searchVisiblePathSet = null;
      this.#rebuildVisibleProjection(this.#focusedPath, true);
    } else if (normalizedValue.length === 0) {
      this.#searchCollapsedOverrides.clear();
      this.#restoreSearchExpandedPaths(false);
      this.#searchMatchPathSet.clear();
      this.#searchVisiblePathSet = null;
      this.#rebuildVisibleProjection(this.#focusedPath, true);
    } else {
      const focusCandidate = this.#refreshActiveSearchState();
      this.#rebuildVisibleProjection(focusCandidate, true);
    }

    if (emitChange) {
      this.#emit();
    }
  }

  #refreshActiveSearchState(): string | null {
    if (this.#searchValue == null || this.#searchValue.length === 0) {
      this.#searchMatchPathSet.clear();
      return this.#focusedPath;
    }

    this.#pruneStaleSearchCollapsedOverrides();
    const searchValue = this.#searchValue;
    const listedPaths = this.#getListedPaths();
    const listedPathsLowerCase = this.#getListedPathsLowerCase();
    const matchingPaths: string[] = [];
    const matchingPathSet = new Set<string>();
    let focusCandidate: string | null = null;

    for (let index = 0; index < listedPaths.length; index += 1) {
      const lowerPath = listedPathsLowerCase[index];
      if (!lowerPath.includes(searchValue)) {
        continue;
      }

      const path = listedPaths[index];
      matchingPaths.push(path);
      matchingPathSet.add(path);
      focusCandidate ??= path;
    }

    const knownDirectoryPaths = this.#getAllKnownDirectoryPaths();
    const knownDirectoryPathsLowerCase = this.#getAllKnownDirectoryPathsLowerCase();
    for (let index = 0; index < knownDirectoryPaths.length; index += 1) {
      const lowerPath = knownDirectoryPathsLowerCase[index];
      if (!lowerPath.includes(searchValue)) {
        continue;
      }

      const path = knownDirectoryPaths[index];
      if (matchingPathSet.has(path)) {
        continue;
      }

      matchingPaths.push(path);
      matchingPathSet.add(path);
      focusCandidate ??= path;
    }

    this.#searchMatchPathSet = matchingPathSet;
    // Non-matching rows are hidden. With no matches at all the tree shows every
    // row collapsed rather than an empty list.
    const searchVisiblePathSet = matchingPaths.length > 0 ? new Set<string>() : null;
    this.#searchVisiblePathSet = searchVisiblePathSet;
    const expandedPaths = new Set<string>();

    for (const matchingPath of matchingPaths) {
      if (searchVisiblePathSet != null) {
        searchVisiblePathSet.add(matchingPath);
      }
      if (matchingPath.endsWith("/")) {
        expandedPaths.add(matchingPath);
      }
      for (const ancestorPath of getAncestorDirectoryPaths(matchingPath)) {
        expandedPaths.add(ancestorPath);
        if (searchVisiblePathSet != null) {
          searchVisiblePathSet.add(ancestorPath);
        }
      }
    }

    // For each directory the user collapsed during the search, remove it from
    // the expanded set. Keep the directory and its ancestors visible, so the
    // user can expand it again.
    for (const collapsedPath of this.#searchCollapsedOverrides) {
      expandedPaths.delete(collapsedPath);
      if (searchVisiblePathSet != null) {
        searchVisiblePathSet.add(collapsedPath);
        for (const ancestorPath of getAncestorDirectoryPaths(collapsedPath)) {
          searchVisiblePathSet.add(ancestorPath);
        }
      }
    }

    this.#setExpandedPaths(expandedPaths);
    return focusCandidate ?? this.#focusedPath;
  }

  #emit(): void {
    this.#version += 1;
    if (this.#selectionVersion !== this.#emittedSelectionVersion) {
      this.#emittedSelectionVersion = this.#selectionVersion;
      this.#onSelectionChange?.(this.getSelectedPaths());
    }
    for (const listener of this.#listeners) {
      listener();
    }
  }

  #expandDirectory(path: string): void {
    for (const ancestorPath of getAncestorDirectoryPaths(path)) {
      if (this.#store.isExpanded(ancestorPath)) {
        continue;
      }

      this.#store.expand(ancestorPath);
    }

    if (!this.#store.isExpanded(path)) {
      this.#store.expand(path);
    }
  }

  #moveFocus(offset: -1 | 1): void {
    const itemCount = this.#visibleCount;
    if (itemCount === 0) {
      return;
    }

    const currentIndex = this.#focusedIndex === -1 ? 0 : this.#focusedIndex;
    const nextIndex = Math.min(itemCount - 1, Math.max(0, currentIndex + offset));
    if (nextIndex !== currentIndex || this.#focusedIndex === -1) {
      if (
        !this.#hasFullProjection &&
        this.#searchVisibleIndices == null &&
        nextIndex >= this.#projectionPaths.length
      ) {
        this.#ensureFullProjection();
      }
      this.#setFocusedIndex(nextIndex);
    }
  }

  #rebuildVisibleProjection(focusedPathCandidate: string | null, full = true): void {
    const rawVisibleCount = this.#store.getVisibleCount();
    this.#storeVisibleCount = rawVisibleCount;
    const projectionData = this.#store.getVisibleTreeProjectionData(
      full ? undefined : Math.min(rawVisibleCount, INITIAL_PROJECTION_ROW_LIMIT),
    );
    const projection = createVisibleProjection(
      projectionData,
      focusedPathCandidate,
      full ? (path) => this.#store.getVisibleIndex(path) : undefined,
    );
    this.#ancestorIndicesByIndex.clear();
    this.#ancestorPathsByIndex.clear();
    this.#hasFullProjection = projection.paths.length >= rawVisibleCount;
    this.#getParentIndexForVisibleRow = projection.getParentIndex;
    this.#projectionPaths = projection.paths;
    this.#projectionPosInSetByIndex = projection.posInSetByIndex;
    this.#projectionSetSizeByIndex = projection.setSizeByIndex;
    this.#syncSearchVisibilityState();
    this.#focusedIndex =
      focusedPathCandidate == null
        ? this.#getCurrentVisiblePaths().length > 0
          ? 0
          : -1
        : this.#resolveFocusedIndex(focusedPathCandidate);
    this.#focusedPath =
      this.#focusedIndex < 0 ? null : this.#resolveVisiblePathAtIndex(this.#focusedIndex);
  }

  #resolveVisiblePathAtIndex(index: number): string | null {
    const projectedPath = this.#getCurrentVisiblePaths()[index];
    if (projectedPath != null) {
      return projectedPath;
    }

    if (this.#searchVisibleIndices != null) {
      return null;
    }

    return this.#store.getVisibleRowContext(index)?.row.path ?? null;
  }

  #resolveSelectionPath(path: string): string | null {
    return this.#store.getPathInfo(path)?.path ?? null;
  }

  #setFocusedIndex(index: number, emit = true): void {
    const nextPath = this.#resolveVisiblePathAtIndex(index);
    if (nextPath == null) {
      return;
    }

    if (this.#focusedIndex === index && this.#focusedPath === nextPath) {
      return;
    }

    this.#focusedIndex = index;
    this.#focusedPath = nextPath;
    if (emit) {
      this.#emit();
    }
  }

  #ensureFullProjection(): void {
    if (this.#hasFullProjection) {
      return;
    }

    this.#rebuildVisibleProjection(this.#focusedPath, true);
  }

  // Each collapsed-directory override is a directory path. Drop any override
  // when its directory no longer exists or is no longer a directory. A stale
  // entry must not affect a later search.
  #pruneStaleSearchCollapsedOverrides(): void {
    if (this.#searchCollapsedOverrides.size === 0) {
      return;
    }

    for (const collapsedPath of [...this.#searchCollapsedOverrides]) {
      if (this.resolveMountedDirectoryPathFromInput(collapsedPath) !== collapsedPath) {
        this.#searchCollapsedOverrides.delete(collapsedPath);
      }
    }
  }

  #rebuildStore(paths: readonly string[], expandedPaths: ReadonlySet<string>): void {
    const focusedPath = this.#focusedPath;
    const previousSelection = [...this.#selectedPaths];
    this.#store = new PathStore({ ...this.#storeOptions, paths });
    this.#itemHandles.clear();
    this.#invalidateKnownPathCaches();
    this.#scrollRequest = null;
    this.#setExpandedPaths(
      new Set([...expandedPaths].map((path) => this.#store.getPathInfo(path)?.path ?? path)),
    );

    const keptSelection = previousSelection.filter((path) => this.#store.getPathInfo(path) != null);
    if (keptSelection.length !== previousSelection.length) {
      this.#selectedPaths = new Set(keptSelection);
      this.#selectionVersion += 1;
    }
    if (this.#selectionAnchorPath != null && !this.#selectedPaths.has(this.#selectionAnchorPath)) {
      this.#selectionAnchorPath = keptSelection.at(-1) ?? null;
    }

    this.#subscribeToStore();
    const focusCandidate =
      this.#searchValue != null && this.#searchValue.length > 0
        ? this.#refreshActiveSearchState()
        : focusedPath;
    this.#rebuildVisibleProjection(focusCandidate, true);
    this.#emit();
  }

  #subscribeToStore(): void {
    this.#store.on("*", (event) => {
      if (this.#suppressStoreNotifications) {
        return;
      }
      if (event.canonicalChanged) {
        this.#itemHandles.clear();
        this.#invalidateKnownPathCaches();
      }
      // Keep the current focus if it is still a visible search row after the
      // mutation. If it is not, use the refreshed best match. This keeps the
      // focus on a row the user revealed and prevents an unwanted focus move.
      let searchFocusCandidate: string | null;
      if (this.#searchValue != null && this.#searchValue.length > 0) {
        const refreshedSearchFocusCandidate = this.#refreshActiveSearchState();
        searchFocusCandidate =
          this.#focusedPath != null && this.#searchVisiblePathSet?.has(this.#focusedPath) === true
            ? this.#focusedPath
            : refreshedSearchFocusCandidate;
      } else if (this.#searchValue === "") {
        searchFocusCandidate = this.#focusedPath;
      } else {
        searchFocusCandidate = this.#focusedPath;
      }
      const shouldBuildFullProjection =
        this.#searchValue != null ||
        (event.operation !== "expand" && event.operation !== "collapse");
      this.#rebuildVisibleProjection(searchFocusCandidate, shouldBuildFullProjection);
      this.#emit();
    });
  }

  #toggleDirectory(path: string): void {
    if (this.#store.isExpanded(path)) {
      this.#collapseDirectory(path);
      return;
    }

    this.#expandDirectory(path);
  }
}
