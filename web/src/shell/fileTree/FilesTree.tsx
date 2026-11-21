// The Files panel's folder tree, drawn by the vendored file tree. Folders load as they
// expand, a chain of only-child folders reads as one row (`review/rules/`),
// and the panel's sort, change marks, sizes, menus and ⌘K target carry over.
// Row interactions the tree doesn't offer are delegated from its root.

import { type CSSProperties, type FocusEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  FileTree,
  type FileTreeDirectoryHandle,
  type FileTreeItemHandle,
  type FileTreeModel,
  FILE_TREE_ROOT_SELECTOR,
  useFileTree,
} from "@/components/file-tree/vendored";
import { FILE_TREE_THEME, WORKSPACE_FILE_TREE_CLASS } from "@/components/file-tree/fileTreeStyle";
import { useTreeRowTooltip } from "@/components/file-tree/treeRowTooltip";
import { type WorkspaceFile, useWorkspaceDirectories } from "@/hooks/useWorkspaceChangedFiles";
import { type PrefetchTarget, useWorkspacePrefetch } from "@/hooks/useWorkspacePrefetch";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import { folderTreePath } from "../breadcrumbPaths";
import type { ChangedSort } from "../FlatFileList";
import { useFocusedFileSource } from "../fileCommands/focusedFile";
import { useFileCommandTarget } from "../fileCommands/target";
import { FileCommandMenuAt } from "../fileCommands/useFileCommandMenu";
import type { FolderListing } from "../folderTreeSync";
import {
  type TreeChange,
  listingsVersion,
  treeMetadata,
  visibleEntries,
  withChangedFiles,
} from "./filesTreeListings";
import { treeChangeStatuses } from "./treeChangeStatus";
import { SingleSelection, opensRow, rowClickAction, rowKeyAction } from "./treeSelection";
import { treeComparator, treeOrderSignature } from "./treeSort";
import { scrollRowIntoView } from "./revealScroll";
import { rowPathOf } from "./treeRowPath";
import { initialFolderTree, useLazyFolderTree } from "./useLazyFolderTree";

/** A folder to reveal, asked for again with a new `key`. */
export interface FilesTreeReveal {
  path: string;
  key: number;
}

/** A request to reveal the viewed file again, even where the tree already opened to it. */
export interface FilesTreeActiveReveal {
  key: number;
  /** The file asked about, relative to the browsed folder; it may have closed since. */
  file?: string | null;
}

export interface FilesTreeProps {
  conversationId: string | undefined;
  /** The browsed folder, or "" for the workspace root; tree paths are relative to it. */
  browseLocation: string;
  /** The browsed folder's own listing. */
  rootEntries: readonly WorkspaceFile[];
  /** Uncommitted changes, relative to the browsed folder; deleted files show from these. */
  changedFiles: readonly TreeChange[];
  sort: ChangedSort;
  showHidden: boolean;
  refreshToken: number;
  /** `newTab` for a ⌘-click (Ctrl-click off macOS) or a middle click, which want the file in its own tab. */
  onFileSelect: (path: string, options?: { newTab: true }) => void;
  /** Re-roots the panel onto a folder, on double-click. */
  onNavigateDir?: (path: string) => void;
  reveal: FilesTreeReveal | null;
  /** The viewed file, relative to the browsed folder; the tree opens down to it once per file. */
  activeFile?: string | null;
  /** Reveal Active File: opens down to the viewed file again and brings it into view. */
  activeReveal?: FilesTreeActiveReveal | null;
  /** The tree took up `activeReveal`, so the request can be let go. */
  onActiveRevealHandled?: (key: number) => void;
  label: string;
}

/** What each conversation's tree had open, by browsed folder, while the panel is closed. */
const expandedCache = new Map<string, Set<string>>();
/** The row at the top of each tree when it was last left, to return to. */
const scrollAnchors = new Map<string, string>();
/** The viewed file each tree last opened down to, so a folder closed beside it stays closed. */
const revealedActiveFiles = new Map<string, string>();

const cacheKeyFor = (conversationId: string | undefined, location: string) =>
  `${conversationId ?? ""}\u0000${location}`;
/** The folder handle `item` is, if it's a folder. */
const asFolder = (item: FileTreeItemHandle | null): FileTreeDirectoryHandle | null =>
  item && "expand" in item ? item : null;
/** The folder a tree path sits in: "" for the root. */
const parentFolder = (path: string) => {
  const trimmed = path.endsWith("/") ? path.slice(0, -1) : path;
  const slash = trimmed.lastIndexOf("/");
  return slash < 0 ? "" : trimmed.slice(0, slash);
};
const trimFolder = (path: string) => (path.endsWith("/") ? path.slice(0, -1) : path);
/** The rendered tree inside `container`, if it has rendered. */
const treeRoot = (container: ParentNode | null | undefined): HTMLElement | null =>
  container?.querySelector<HTMLElement>(FILE_TREE_ROOT_SELECTOR) ?? null;
/** Whether keyboard focus is on the tree or one of its rows. */
const hasFocusWithin = (host: HTMLElement | null): boolean =>
  host !== null && host.contains(document.activeElement);

/** The first row actually on screen, skipping overscan. */
function topVisibleRow(host: HTMLElement): string | null {
  const box = host.getBoundingClientRect();
  for (const row of host.querySelectorAll<HTMLElement>('[role="treeitem"][data-item-path]')) {
    const rect = row.getBoundingClientRect();
    if (rect.height > 0 && rect.bottom > box.top + 1) return row.getAttribute("data-item-path");
  }
  return null;
}

const FILES_TREE_THEME = {
  ...FILE_TREE_THEME,
  // The rows sit on the panel's own surface, as its header does, not the file viewer's.
  "--trees-bg-override": "transparent",
} as CSSProperties;

export function FilesTree(props: FilesTreeProps) {
  const { conversationId, browseLocation, showHidden, refreshToken } = props;
  const cacheKey = cacheKeyFor(conversationId, browseLocation);
  const dark = useResolvedThemeMode() === "dark";
  const prefetch = useWorkspacePrefetch({
    conversationId,
    location: browseLocation,
    refreshToken,
  });

  const rootListing = useMemo<FolderListing>(
    () => ({ folder: "", entries: visibleEntries(props.rootEntries, showHidden) }),
    [props.rootEntries, showHidden],
  );
  const visibleChanges = useMemo(
    () =>
      showHidden
        ? props.changedFiles
        : props.changedFiles.filter(
            (change) => !change.path.split("/").some((part) => part.startsWith(".")),
          ),
    [props.changedFiles, showHidden],
  );
  const [initial] = useState(() =>
    initialFolderTree(withChangedFiles([rootListing], visibleChanges)),
  );
  const [expandedAtStart] = useState(() => [...(expandedCache.get(cacheKey) ?? [])]);

  // Options the tree reads once go through refs, so they always see the latest props.
  const latest = useRef(props);
  latest.current = props;
  const sortState = useRef({
    sort: props.sort,
    meta: treeMetadata(withChangedFiles([rootListing], visibleChanges)),
  });
  const selectionRef = useRef<SingleSelection | null>(null);
  const [menu, setMenu] = useState<{
    path: string;
    /** The row the menu opened from, which gets focus back when it closes. */
    treePath: string;
    directory: boolean;
    point: { x: number; y: number };
  } | null>(null);

  const { model } = useFileTree({
    paths: [...initial.present],
    flattenEmptyDirectories: true,
    initialExpansion: "closed",
    initialExpandedPaths: expandedAtStart,
    gitStatus: treeChangeStatuses(props.changedFiles),
    sort: (left, right) =>
      treeComparator(sortState.current.sort, sortState.current.meta)(left, right),
    // Opening is driven by the input that chose a row; selection changes only stay single.
    onSelectionChange: () => selectionRef.current?.normalize(),
  });
  selectionRef.current ??= new SingleSelection(model);

  /** Opens a file a person chose; folders and deleted files only move focus. */
  function activate(path: string, newTab = false) {
    if (path.endsWith("/")) return;
    const deleted = latest.current.changedFiles.some(
      (change) => change.path === path && change.status === "deleted",
    );
    if (deleted) return;
    if (newTab) latest.current.onFileSelect(path, { newTab: true });
    else latest.current.onFileSelect(path);
  }

  // Folders beyond the root load as they're wanted, through the panel's listing queries.
  const [loadingFolders, setLoadingFolders] = useState<string[]>([]);
  const directories = useWorkspaceDirectories(
    conversationId,
    loadingFolders,
    browseLocation,
    refreshToken,
  );
  // Keyed on the listings' own data, so a fresh result map holding the same data changes nothing.
  const rawListings: FolderListing[] = [{ folder: "", entries: props.rootEntries }];
  for (const folder of loadingFolders) {
    const data = directories.get(folder)?.data;
    if (data) rawListings.push({ folder, entries: data });
  }
  const rawVersion = `${listingsVersion(rawListings)}\u0002${showHidden}\u0002${listingsVersion([
    { folder: "changes", entries: visibleChanges as unknown as WorkspaceFile[] },
  ])}`;
  const latestRaw = useRef(rawListings);
  latestRaw.current = rawListings;
  const listings = useMemo<FolderListing[]>(
    () =>
      withChangedFiles(
        latestRaw.current.map(({ folder, entries }) => ({
          folder,
          entries: folder === "" ? rootListing.entries : visibleEntries(entries, showHidden),
        })),
        visibleChanges,
      ),
    // The raw version names every input; the rest are read through it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [rawVersion],
  );
  const { wantedFolders, isEmpty, present, loadedFolders, currentPresent } = useLazyFolderTree(
    model,
    initial,
    listings,
    listingsVersion(listings),
    [""],
  );
  useEffect(() => {
    setLoadingFolders((current) =>
      current.length === wantedFolders.length &&
      current.every((folder, index) => folder === wantedFolders[index])
        ? current
        : wantedFolders,
    );
  }, [wantedFolders]);
  // Expansion survives leaving the panel: a folder that was open reopens when its
  // listing first adds it; after that, opening and closing it is what's recorded.
  useEffect(() => {
    const expanded = expandedCache.get(cacheKey) ?? new Set<string>();
    expandedCache.set(cacheKey, expanded);
    const seen = new Set<string>();
    const record = () => {
      for (const row of model.getVisibleRows(0, model.getVisibleCount())) {
        if (row.kind !== "directory") continue;
        if (!seen.has(row.path)) {
          seen.add(row.path);
          const folder = asFolder(model.getItem(row.path));
          if (expanded.has(row.path) && folder && !folder.isExpanded()) folder.expand();
          continue;
        }
        if (row.isExpanded) expanded.add(row.path);
        else expanded.delete(row.path);
      }
    };
    record();
    return model.subscribe(record);
  }, [model, cacheKey]);

  // Change marks follow the panel's changed files.
  useEffect(() => {
    model.setGitStatus(treeChangeStatuses(props.changedFiles));
  }, [model, props.changedFiles]);

  // A new sort, or metadata it orders by, re-sorts in place, keeping where the reader was.
  const meta = useMemo(() => treeMetadata(listings), [listings]);
  const orderSignature = treeOrderSignature(props.sort, meta);
  const appliedOrder = useRef(orderSignature);
  const containerRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    sortState.current = { sort: props.sort, meta };
    if (appliedOrder.current === orderSignature) return;
    appliedOrder.current = orderSignature;
    const host = treeRoot(containerRef.current);
    const anchor = host ? topVisibleRow(host) : null;
    const selected = model.getSelectedPaths();
    const focused = model.getFocusedPath();
    const hadFocus = hasFocusWithin(host);
    // The sync may have changed the tree in this same commit; reset to what it holds now.
    model.resetPaths([...currentPresent()], {
      initialExpandedPaths: [...(expandedCache.get(cacheKey) ?? [])],
    });
    for (const path of selected) model.getItem(path)?.select();
    if (focused && hadFocus) model.focusPath(focused);
    if (anchor) model.scrollToPath(anchor, { offset: "top", focus: false });
  }, [model, props.sort, meta, orderSignature, currentPresent, cacheKey]);

  // The focused row is ⌘K's file, from first entry and re-entry as well as arrow moves.
  const [activePath, setActivePath] = useState<string | null>(null);
  const [focusTick, setFocusTick] = useState(0);
  const activeIsFolder = activePath?.endsWith("/") ?? false;
  const activeTrimmed = activePath === null ? null : trimFolder(activePath);
  const activeDeleted =
    activeTrimmed !== null &&
    props.changedFiles.some(
      (change) => change.path === activeTrimmed && change.status === "deleted",
    );
  const activeTarget = useFileCommandTarget(activeTrimmed, {
    directory: activeIsFolder,
    deleted: activeDeleted,
  });
  const publishFocused = useFocusedFileSource(activeTarget);
  useEffect(() => {
    const host = treeRoot(containerRef.current);
    if (!host || !activeTarget || !hasFocusWithin(host)) return;
    publishFocused({ currentTarget: host } as FocusEvent<HTMLElement>);
  }, [activeTarget, focusTick, publishFocused]);
  useEffect(
    () =>
      model.subscribe(() => {
        if (!hasFocusWithin(treeRoot(containerRef.current))) return;
        setActivePath(model.getFocusedPath());
      }),
    [model],
  );

  const prefetchTarget = (path: string): PrefetchTarget => {
    const trimmed = trimFolder(path);
    if (path.endsWith("/")) return { kind: "directory", path: trimmed };
    return {
      kind: "file",
      path: trimmed,
      bytes: sortState.current.meta.get(trimmed)?.bytes ?? null,
    };
  };
  // Hover and focus load a row's target ahead; the tooltip shows its full path, a name
  // truncated in the middle included. The menu would sit under it, so it gives way.
  const rowTooltip = useTreeRowTooltip(containerRef, model, {
    hidden: menu !== null,
    onHover: (path) => {
      if (path) prefetch.intend(prefetchTarget(path));
      else prefetch.cancel();
    },
  });

  // Input the tree would handle differently from the panel, caught on its root first.
  useEffect(() => {
    const host = treeRoot(containerRef.current);
    const selection = selectionRef.current;
    if (!host || !selection) return;
    // A file opens once per click, Enter or Space on it, as that input happens. Rows are
    // native buttons, so a key that opens one holds back the button's own click.
    const onKeyDown = (event: KeyboardEvent) => {
      const path = rowPathOf(event);
      if (path === null) return;
      if (selection.apply(rowKeyAction(event)).consumed) {
        event.preventDefault();
        event.stopPropagation();
        return;
      }
      if (!opensRow(event) || path.endsWith("/")) return;
      event.preventDefault();
      selection.selectOnly(path);
      activate(path);
    };
    const onClick = (event: globalThis.MouseEvent) => {
      const path = rowPathOf(event);
      const result = selection.apply(rowClickAction(event, path));
      if (!result.consumed) {
        if (path) activate(path);
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      if (result.activate) activate(result.activate, result.newTab);
    };
    // A middle click opens a file in its own tab, as in a browser.
    const onAuxClick = (event: globalThis.MouseEvent) => {
      const path = rowPathOf(event);
      if (event.button !== 1 || !path || path.endsWith("/")) return;
      event.preventDefault();
      activate(path, true);
    };
    const onDoubleClick = (event: globalThis.MouseEvent) => {
      const path = rowPathOf(event);
      if (path?.endsWith("/")) latest.current.onNavigateDir?.(trimFolder(path));
    };
    const onFocusIn = (event: globalThis.FocusEvent) => {
      const path = rowPathOf(event) ?? model.getFocusedPath();
      setActivePath(path);
      setFocusTick((tick) => tick + 1);
      if (path) prefetch.intend(prefetchTarget(path));
    };
    host.addEventListener("keydown", onKeyDown, true);
    host.addEventListener("click", onClick, true);
    host.addEventListener("auxclick", onAuxClick);
    host.addEventListener("dblclick", onDoubleClick);
    host.addEventListener("focusin", onFocusIn);
    return () => {
      host.removeEventListener("keydown", onKeyDown, true);
      host.removeEventListener("click", onClick, true);
      host.removeEventListener("auxclick", onAuxClick);
      host.removeEventListener("dblclick", onDoubleClick);
      host.removeEventListener("focusin", onFocusIn);
    };
    // The handlers read the latest props through refs; they attach once per tree.
  }, [model, prefetch]);

  // Return to the row that was at the top when the panel was last left.
  const restored = useRef(false);
  useEffect(() => {
    if (restored.current) return;
    const anchor = scrollAnchors.get(cacheKey);
    if (!anchor) {
      restored.current = true;
      return;
    }
    // Climb from the saved row until something is known: a row that's there, or a folder
    // whose listing hasn't arrived yet. A row only counts as gone once its folder is listed.
    let path = anchor;
    for (;;) {
      if (present.has(path)) break;
      const folder = parentFolder(path);
      // Still coming: an open folder whose listing hasn't arrived. A closed one can't
      // show the row, a failed one won't list it, and a missing one is decided above it.
      const coming =
        folder !== "" &&
        present.has(folderTreePath(folder)) &&
        !loadedFolders.has(folder) &&
        !(directories.get(folder)?.isError ?? false) &&
        (expandedCache.get(cacheKey)?.has(folderTreePath(folder)) ?? false);
      const listed = !coming;
      if (!listed) return;
      if (folder === "") {
        restored.current = true;
        return;
      }
      path = folderTreePath(folder);
    }
    restored.current = true;
    model.scrollToPath(path, { offset: "top", focus: false });
  }, [model, cacheKey, present, loadedFolders, directories]);
  // Remember the top row as the reader scrolls; by the time the tree unmounts it's detached.
  useEffect(() => {
    const host = treeRoot(containerRef.current);
    if (!host) return;
    let frame = 0;
    const onScroll = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const anchor = topVisibleRow(host);
        if (anchor) scrollAnchors.set(cacheKey, anchor);
      });
    };
    // Scroll events don't bubble, but they pass the tree's root on their way down.
    host.addEventListener("scroll", onScroll, true);
    return () => {
      cancelAnimationFrame(frame);
      host.removeEventListener("scroll", onScroll, true);
    };
  }, [cacheKey]);

  // Reveal a folder from search results: open what's above it, centre it, and focus it.
  useRevealFolder(model, props.reveal, present, containerRef);
  // Open the tree down to the viewed file, once per file or when asked, without taking focus.
  useRevealActiveFile({
    model,
    selection: selectionRef.current,
    file: props.activeFile ?? null,
    request: props.activeReveal ?? null,
    onRequestHandled: props.onActiveRevealHandled,
    present,
    cacheKey,
    containerRef,
  });

  const menuTarget = useFileCommandTarget(menu?.path ?? null, {
    directory: menu?.directory ?? false,
    deleted:
      menu !== null &&
      props.changedFiles.some((change) => change.path === menu.path && change.status === "deleted"),
  });

  const failed = loadingFolders.find((folder) => directories.get(folder)?.isError);
  const pending = loadingFolders.find(
    (folder) => directories.get(folder)?.isLoading && !directories.get(folder)?.data,
  );
  return (
    // The tree stays mounted while its folder is empty, so its listeners are there once files arrive.
    <div ref={containerRef} className="flex min-h-0 flex-1 flex-col">
      {isEmpty && <p className="px-2 py-1 text-muted-foreground text-sm">This folder is empty.</p>}
      <FileTree
        model={model}
        aria-label={props.label}
        className={WORKSPACE_FILE_TREE_CLASS}
        // The panel's own menu opens, with the row's file actions.
        onRowMenu={(row, anchor) =>
          setMenu({
            path: trimFolder(row.path),
            treePath: row.path,
            directory: row.kind === "directory",
            point: { x: anchor.left, y: anchor.bottom },
          })
        }
        style={{
          ...FILES_TREE_THEME,
          flex: 1,
          minHeight: 0,
          colorScheme: dark ? "dark" : "light",
          ...(isEmpty ? { display: "none" } : {}),
        }}
      />
      {!isEmpty && (failed ?? pending) !== undefined && (
        <div
          role="status"
          className="shrink-0 truncate border-t border-border px-3 py-1.5 text-ui text-muted-foreground"
        >
          {failed !== undefined
            ? `Couldn’t load ${failed.split("/").at(-1)}.`
            : `Loading ${pending?.split("/").at(-1)}…`}
        </div>
      )}
      {rowTooltip}
      {menu && menuTarget && (
        <FileCommandMenuAt
          target={menuTarget}
          point={menu.point}
          onClose={() => setMenu(null)}
          onRestoreFocus={() => {
            model.focusPath(menu.treePath);
            treeRoot(containerRef.current)
              ?.querySelector<HTMLElement>(
                `[role="treeitem"][data-item-path="${CSS.escape(menu.treePath)}"]`,
              )
              ?.focus({ preventScroll: true });
          }}
          opens={{
            onOpenInNewTab: () => activate(menu.path, true),
            onOpenAsRoot: props.onNavigateDir && (() => props.onNavigateDir?.(menu.path)),
          }}
        />
      )}
    </div>
  );
}

/**
 * The row that shows `path`: its own, or the merged row of a chain of only-child
 * folders that covers it, such as `a/b/c/` for `a/` or `a/b/`.
 */
function displayedRowPath(model: FileTreeModel, path: string): string {
  for (const row of model.getVisibleRows(0, model.getVisibleCount())) {
    if (row.path === path) return path;
    if (row.flattenedSegments?.some((segment) => segment.path === path)) return row.path;
  }
  return path;
}

/**
 * Expands a revealed folder's ancestors, then once its row exists centres it and
 * moves focus there, unless the reader has moved focus elsewhere meanwhile; then
 * the row is only selected, which shows without focus.
 */
function useRevealFolder(
  model: FileTreeModel,
  reveal: FilesTreeReveal | null,
  present: ReadonlySet<string>,
  containerRef: React.RefObject<HTMLDivElement | null>,
) {
  const pending = useRef<{ reveal: FilesTreeReveal; focusAtStart: Element | null } | null>(null);
  // A reveal's focus attempts outlive later syncs; only unmounting stops them.
  const frame = useRef(0);
  useEffect(() => () => cancelAnimationFrame(frame.current), []);
  useEffect(() => {
    if (!reveal) return;
    pending.current = { reveal, focusAtStart: document.activeElement };
  }, [reveal]);
  useEffect(() => {
    const request = pending.current;
    if (!request) return;
    const target = folderTreePath(trimFolder(request.reveal.path));
    const parts = trimFolder(request.reveal.path).split("/");
    for (let count = 1; count < parts.length; count++) {
      const ancestor = asFolder(model.getItem(folderTreePath(parts.slice(0, count).join("/"))));
      if (ancestor && !ancestor.isExpanded()) ancestor.expand();
    }
    if (!present.has(target)) return;
    pending.current = null;
    const shown = displayedRowPath(model, target);
    model.scrollToPath(shown, { offset: "center", focus: false });
    const focusStayed = () => {
      const active = document.activeElement;
      return (
        !active ||
        active === document.body ||
        active === request.focusAtStart ||
        (containerRef.current?.contains(active) ?? false)
      );
    };
    if (!focusStayed()) {
      model.getItem(shown)?.select();
      return;
    }
    let attempts = 0;
    const focusRow = () => {
      if (!focusStayed()) {
        model.getItem(shown)?.select();
        return;
      }
      model.focusPath(shown);
      const row = treeRoot(containerRef.current)?.querySelector<HTMLElement>(
        `[role="treeitem"][data-item-path="${CSS.escape(shown)}"]`,
      );
      if (row) {
        row.focus({ preventScroll: true });
        return;
      }
      if (++attempts < 30) frame.current = requestAnimationFrame(focusRow);
    };
    frame.current = requestAnimationFrame(focusRow);
    // A new request runs it at once; a listing that arrives later retries one still waiting.
  }, [reveal, model, present, containerRef]);
}

/**
 * Expands the folders above `file` one listing at a time, then selects its row and
 * brings it into view without moving focus. Each tree does this once per viewed file:
 * the same file again, after a refresh or a remount, leaves the reader's folders as
 * they are, unless a Reveal Active File `request` asks for it again. A folder collapsed
 * while a deeper listing loads ends the reveal, so it stays shut, across a remount too.
 */
function useRevealActiveFile({
  model,
  selection,
  file,
  request,
  onRequestHandled,
  present,
  cacheKey,
  containerRef,
}: {
  model: FileTreeModel;
  selection: SingleSelection;
  file: string | null;
  request: FilesTreeActiveReveal | null;
  onRequestHandled?: (key: number) => void;
  present: ReadonlySet<string>;
  cacheKey: string;
  containerRef: React.RefObject<HTMLDivElement | null>;
}) {
  // `seenOpen`: the folders above the file this reveal has seen open, by it or already.
  // `asked`: a Reveal Active File request, which carries on though its file has closed.
  const pending = useRef<{ file: string; seenOpen: Set<string>; asked: boolean } | null>(null);
  const handledRequest = useRef<number | null>(null);
  const latestHandled = useRef(onRequestHandled);
  latestHandled.current = onRequestHandled;
  useEffect(() => {
    if (file === null || revealedActiveFiles.get(cacheKey) === file) {
      pending.current = null;
      return;
    }
    pending.current = { file, seenOpen: new Set(), asked: false };
  }, [file, cacheKey]);
  // An asked-for reveal starts afresh, past the once-per-file rule and any folder closed since.
  useEffect(() => {
    if (request === null || request.key === handledRequest.current) return;
    handledRequest.current = request.key;
    latestHandled.current?.(request.key);
    const target = request.file ?? file;
    if (target) pending.current = { file: target, seenOpen: new Set(), asked: true };
  }, [file, request]);
  // Closing a folder the reveal saw open ends it at once, so a remount before the
  // next listing still respects it. Only that reveal's few folders are checked.
  useEffect(
    () =>
      model.subscribe(() => {
        const current = pending.current;
        if (!current) return;
        for (const path of current.seenOpen) {
          if (asFolder(model.getItem(path))?.isExpanded()) continue;
          pending.current = null;
          revealedActiveFiles.set(cacheKey, current.file);
          return;
        }
      }),
    [model, cacheKey],
  );
  useEffect(() => {
    const current = pending.current;
    if (!current || (!current.asked && current.file !== file)) return;
    const finish = () => {
      pending.current = null;
      revealedActiveFiles.set(cacheKey, current.file);
    };
    const parts = current.file.split("/");
    for (let depth = 1; depth < parts.length; depth++) {
      const path = folderTreePath(parts.slice(0, depth).join("/"));
      const folder = asFolder(model.getItem(path));
      // Its parent's listing hasn't arrived yet; the next sync carries on from here.
      if (!folder) return;
      if (folder.isExpanded()) {
        current.seenOpen.add(path);
        continue;
      }
      // The reader closed a folder this reveal saw open: respect that, now and later.
      if (current.seenOpen.has(path)) return finish();
      folder.expand();
      if (folder.isExpanded()) current.seenOpen.add(path);
    }
    // Scroll only once the file's own row exists, after every listing above it arrived.
    if (!present.has(current.file)) return;
    finish();
    const shown = displayedRowPath(model, current.file);
    selection.selectWithoutFocus(shown);
    scrollRowIntoView(model, treeRoot(containerRef.current), shown);
  }, [model, selection, file, request, present, cacheKey, containerRef]);
}
