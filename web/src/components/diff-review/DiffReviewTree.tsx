// The changed-files tree beside the review. Directories with a single child
// merge onto one row, so deep paths don't waste rows. Files keep their language
// icons; added and deleted files tint their names, and each row ends with its
// line counts. Choosing a file scrolls the review to it; the search box narrows
// both the tree and the review.

import { useEffect, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";
import {
  compareTreeEntries,
  FILE_TREE_ROOT_SELECTOR,
  FileTree,
  type FileTreeDirectoryHandle,
  type FileTreeItemHandle,
  type FileTreeRowDecoration,
  type FileTreeSortComparator,
  type FileTreeVisibleRow,
  useFileTree,
} from "@/components/file-tree/vendored";
import { ChevronsDownUpIcon, ChevronsUpDownIcon, SearchIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { SessionActionMenuItem } from "@/components/SessionActionMenuItem";
import { OWNS_COMMAND_ENTER_ATTRIBUTE } from "@/hooks/useApproveHotkey";
import { rowPathOf } from "@/shell/fileTree/treeRowPath";
import { useMenuFocusReturn } from "@/shell/fileCommands/useFileCommandMenu";
import { FILE_TREE_THEME, WORKSPACE_FILE_TREE_CLASS } from "@/components/file-tree/fileTreeStyle";
import { useTreeRowTooltip } from "@/components/file-tree/treeRowTooltip";
import type { DiffReviewFileAction } from "./diffReviewFileAction";
import type { DiffReviewChange, DiffReviewFile } from "./diffReviewModel";
import { treeCommandClickOpen, treeKeyOpen, treeMiddleClickOpen } from "./treeOpenGesture";
import {
  focusedReviewFolderTree,
  publishFocusedReviewFolder,
  releaseFocusedReviewFolder,
} from "./focusedReviewFolder";
import "./diffReviewTree.css";

// The reference design keeps the status letters (A, M, D) and leaves the counts to the file
// headers; today's design tints names and shows counts instead (see diffReviewTree.css).
const TREE_CLASS = `${WORKSPACE_FILE_TREE_CLASS} diff-review-tree-counts`;
const REFERENCE_TREE_CLASS = WORKSPACE_FILE_TREE_CLASS;

/** How the tree looks: today's, or the reference design's. */
export type DiffReviewTreeAppearance = "default" | "reference";

const TREE_THEME: CSSProperties = {
  ...FILE_TREE_THEME,
  // The rows sit on the panel's muted surface, as the Files tree's do.
  "--trees-bg-override": "transparent",
  "--trees-git-added-color-override": "var(--success)",
  "--trees-git-deleted-color-override": "var(--destructive)",
} as CSSProperties;

/** How the review names a file the reader owns. */
export const OWNED_BY_YOU = "Owned by you";

/** The tree's status for a file: added and deleted files stand out, modified ones don't. */
export function treeGitStatus(change: DiffReviewChange): "added" | "deleted" | null {
  return change === "modified" ? null : change;
}

/**
 * A file row's line counts, such as "+3 −1", after a dot when the reader owns the file; none
 * while the source hasn't counted them and nobody owns it.
 */
export function treeRowDecoration(file: DiffReviewFile): FileTreeRowDecoration | null {
  const parts: { text: string; color: string }[] = [];
  if (file.ownedByViewer) parts.push({ text: "●", color: "var(--primary)" });
  if (file.additions) parts.push({ text: `+${file.additions}`, color: "var(--success)" });
  if (file.deletions) parts.push({ text: `−${file.deletions}`, color: "var(--destructive)" });
  if (parts.length === 0) return null;
  const counted = [
    file.ownedByViewer ? OWNED_BY_YOU : null,
    file.additions ? `${file.additions} added` : null,
    file.deletions ? `${file.deletions} deleted` : null,
  ].filter(Boolean);
  return { text: parts.map((part) => part.text).join(" "), title: counted.join(", "), parts };
}

/** The tree's status entries: one per added or deleted file, or per file in the reference design. */
export function treeGitStatusEntries(
  files: readonly DiffReviewFile[],
  appearance: DiffReviewTreeAppearance = "default",
): { path: string; status: "added" | "deleted" | "modified" }[] {
  return files.flatMap((file) => {
    if (appearance === "reference") return [{ path: file.path, status: file.change }];
    const status = treeGitStatus(file.change);
    return status ? [{ path: file.path, status }] : [];
  });
}

/** What the rows show beyond their paths; when it changes, the rows repaint in place. */
export function treeRowContentKey(files: readonly DiffReviewFile[]): string {
  return files
    .map(
      (file) =>
        `${file.path}\u0000${file.change}\u0000${file.additions}\u0000${file.deletions}\u0000${file.ownedByViewer === true}`,
    )
    .join("\n");
}

export function DiffReviewTree({
  files,
  navigate,
  searchQuery,
  setSearchQuery,
  searchFocusRequest,
  dark,
  onOpenInNewTab,
  rowActions,
  folderActions,
  folderOwner,
  appearance = "default",
  onClose,
}: {
  /** The files to list, in tree order: the review's, already narrowed by the search. */
  files: readonly DiffReviewFile[];
  navigate: (path: string) => void;
  searchQuery: string | null;
  setSearchQuery: (value: string | null) => void;
  /** Bumped on every request to search, so an open search is focused again. */
  searchFocusRequest: number;
  dark: boolean;
  /** Opens a file in a tab of its own, from a middle click or ⌘Enter on its row. */
  onOpenInNewTab?: (path: string, options: { background: boolean }) => void;
  /** A file row's menu, from right-click or the context-menu key; none leaves rows without one. */
  rowActions?: (path: string) => readonly DiffReviewFileAction[];
  /** A folder row's actions after Collapse or Expand, such as copying its path. */
  folderActions?: (path: string) => readonly DiffReviewFileAction[];
  /** The review a focused folder belongs to, so ⌘K offers its actions there. */
  folderOwner?: string;
  appearance?: DiffReviewTreeAppearance;
  /** Closes the tree where it's a popover; the header then shows its own × for that. */
  onClose?: () => void;
}) {
  const reference = appearance === "reference";
  const searchInput = useRef<HTMLInputElement>(null);
  const searchButton = useRef<HTMLButtonElement>(null);
  const searching = searchQuery !== null;
  useEffect(() => {
    if (searching) searchInput.current?.focus({ preventScroll: true });
  }, [searching, searchFocusRequest]);
  const opensInNewTab = onOpenInNewTab !== undefined;

  return (
    <aside
      aria-label="Changed files"
      className="flex h-full min-h-0 flex-col bg-card"
      // The rows own ⌘Enter, so a pending chat prompt's accept chord leaves them be.
      {...(opensInNewTab ? { [OWNS_COMMAND_ENTER_ATTRIBUTE]: "" } : {})}
    >
      <div className="flex h-8 shrink-0 items-center gap-1 border-b border-border pr-1 pl-3 text-ui">
        {searchQuery === null ? (
          <span
            className={
              reference
                ? "flex-1 truncate text-xs font-semibold tracking-wide text-muted-foreground uppercase"
                : "flex-1 truncate text-muted-foreground"
            }
          >
            {reference ? "Changed Files" : "Changed files"}
          </span>
        ) : (
          <input
            ref={searchInput}
            value={searchQuery}
            aria-label={reference ? "Filter changed files" : "Filter changed files by name"}
            placeholder={reference ? "Search…" : "Filter by name"}
            className="min-w-0 flex-1 bg-transparent outline-none placeholder:text-muted-foreground"
            onChange={(event) => setSearchQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== "Escape") return;
              event.preventDefault();
              event.stopPropagation();
              setSearchQuery(null);
              searchButton.current?.focus({ preventScroll: true });
            }}
          />
        )}
        <Button
          ref={searchButton}
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label={searchQuery === null ? "Search changed files" : "Close changed-file search"}
          onClick={() => setSearchQuery(searchQuery === null ? "" : null)}
        >
          {searchQuery === null || reference || onClose ? <SearchIcon /> : <XIcon />}
        </Button>
        {onClose && (
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label="Close changed files"
            onClick={onClose}
          >
            <XIcon />
          </Button>
        )}
      </div>
      {/* The rows are only ever the listed files: the tree keeps no search of its own,
          so nothing it does on a click can show files the search hides. */}
      <ChangedFileRows
        key={files.map((file) => file.path).join("\n")}
        files={files}
        navigate={navigate}
        dark={dark}
        onOpenInNewTab={onOpenInNewTab}
        rowActions={rowActions}
        folderActions={folderActions}
        folderOwner={folderOwner}
        appearance={appearance}
      />
    </aside>
  );
}

/** Sorts the tree in `paths`' order, which the review chose; anything else keeps the default. */
export function givenOrder(paths: readonly string[]): FileTreeSortComparator {
  const positions = new Map(paths.map((path, index) => [path, index]));
  return (left, right) => {
    const leftAt = positions.get(left.path);
    const rightAt = positions.get(right.path);
    if (leftAt === undefined || rightAt === undefined) return compareTreeEntries(left, right);
    return leftAt - rightAt;
  };
}

/** The tree's rows for one set of paths; a different set mounts a fresh tree. */
function ChangedFileRows({
  files,
  navigate,
  dark,
  onOpenInNewTab,
  rowActions,
  folderActions,
  folderOwner,
  appearance,
}: {
  files: readonly DiffReviewFile[];
  navigate: (path: string) => void;
  dark: boolean;
  onOpenInNewTab?: (path: string, options: { background: boolean }) => void;
  rowActions?: (path: string) => readonly DiffReviewFileAction[];
  folderActions?: (path: string) => readonly DiffReviewFileAction[];
  folderOwner?: string;
  appearance: DiffReviewTreeAppearance;
}) {
  const reference = appearance === "reference";
  const paths = files.map((file) => file.path);
  const navigateRef = useRef(navigate);
  navigateRef.current = navigate;
  const pathSet = useRef(new Set(paths));
  pathSet.current = new Set(paths);
  const filesByPath = useRef(new Map(files.map((file) => [file.path, file])));
  filesByPath.current = new Map(files.map((file) => [file.path, file]));
  const latest = useRef({ onOpenInNewTab, rowActions, folderActions });
  latest.current = { onOpenInNewTab, rowActions, folderActions };
  const [menu, setMenu] = useState<{
    actions: readonly DiffReviewFileAction[];
    /** The row to put focus back on when the menu closes. */
    path: string;
    point: { x: number; y: number };
  } | null>(null);
  const treeHolder = useRef<HTMLDivElement>(null);
  const selection = useRef<readonly string[]>([]);
  // The tree keeps the review's order; it reads input once, and a new set or order remounts it.
  const { model } = useFileTree({
    paths,
    sort: givenOrder(paths),
    gitStatus: treeGitStatusEntries(files, appearance),
    flattenEmptyDirectories: true,
    initialExpansion: "open",
    onSelectionChange: (selected) => {
      selection.current = selected;
      const [path] = selected;
      if (path && pathSet.current.has(path)) navigateRef.current(path);
    },
  });

  const renderRowDecoration = (row: FileTreeVisibleRow) => {
    const file = row.kind === "file" ? filesByPath.current.get(row.path) : undefined;
    return file ? treeRowDecoration(file) : null;
  };
  // A row's menu is the review's: a file's actions, or a folder's Collapse or Expand and its own.
  const openRowMenu = (row: FileTreeVisibleRow, anchor: DOMRect) => {
    const actions =
      row.kind === "directory"
        ? folderRowActions(model, row.path, latest.current.folderActions)
        : pathSet.current.has(row.path)
          ? (latest.current.rowActions?.(row.path) ?? [])
          : [];
    if (actions.length === 0) return;
    setMenu({ actions, path: row.path, point: { x: anchor.left, y: anchor.bottom } });
  };

  // A row's full path follows the pointer, a name truncated in the middle included.
  const rowTooltip = useTreeRowTooltip(treeHolder, model, { hidden: menu !== null });

  // Puts keyboard focus back on a row, as the tree's own menu would on close.
  const focusRow = (path: string) => {
    model.focusPath(path);
    const host = treeHolder.current?.querySelector<HTMLElement>(FILE_TREE_ROOT_SELECTOR);
    const rows = host?.querySelectorAll<HTMLElement>('[role="treeitem"][data-item-path]');
    const row = [...(rows ?? [])].find((candidate) => candidate.dataset.itemPath === path);
    row?.focus({ preventScroll: true });
  };

  // Clicking the file already picked changes no selection, so the tree reports nothing: go to
  // it again, which scrolls back and flashes its header. The capture phase sees the selection
  // before the tree handles the click.
  useEffect(() => {
    const host = treeHolder.current?.querySelector<HTMLElement>(FILE_TREE_ROOT_SELECTOR);
    if (!host) return;
    let pickedBeforeClick: string | null = null;
    const isOnlySelection = (path: string) =>
      selection.current.length === 1 && selection.current[0] === path;
    const plainRowPath = (event: MouseEvent) => {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
        return null;
      }
      const path = rowPathOf(event);
      return path && pathSet.current.has(path) ? path : null;
    };
    const beforeTree = (event: MouseEvent) => {
      const path = plainRowPath(event);
      pickedBeforeClick = path && isOnlySelection(path) ? path : null;
    };
    const afterTree = (event: MouseEvent) => {
      const path = plainRowPath(event);
      const repick = path !== null && path === pickedBeforeClick && isOnlySelection(path);
      pickedBeforeClick = null;
      if (repick) navigateRef.current(path);
    };
    host.addEventListener("click", beforeTree, true);
    host.addEventListener("click", afterTree);
    return () => {
      host.removeEventListener("click", beforeTree, true);
      host.removeEventListener("click", afterTree);
    };
  }, []);

  // ⌘-click, a middle click and ⌘Enter on a file row open it in a new tab. A ⌘-click is
  // caught before the tree sees it, so it doesn't toggle selection; ⇧-click stays a range.
  const opensInNewTab = onOpenInNewTab !== undefined;
  useEffect(() => {
    const host = treeHolder.current?.querySelector<HTMLElement>(FILE_TREE_ROOT_SELECTOR);
    if (!host || !opensInNewTab) return;
    const open = (target: { path: string; background: boolean } | null, event: Event) => {
      if (!target || !pathSet.current.has(target.path)) return;
      event.preventDefault();
      latest.current.onOpenInNewTab?.(target.path, { background: target.background });
    };
    const onMouseDown = (event: MouseEvent) => {
      // No autoscroll from the middle button over a row.
      if (event.button === 1 && rowPathOf(event)) event.preventDefault();
    };
    const onAuxClick = (event: MouseEvent) =>
      open(treeMiddleClickOpen(event, rowPathOf(event)), event);
    const onClick = (event: MouseEvent) => {
      const target = treeCommandClickOpen(event, rowPathOf(event));
      if (!target || !pathSet.current.has(target.path)) return;
      event.stopPropagation();
      open(target, event);
    };
    // Cancelling the key stops the row button's own click, which would toggle selection.
    const onKeyDown = (event: KeyboardEvent) => open(treeKeyOpen(event, rowPathOf(event)), event);
    host.addEventListener("mousedown", onMouseDown);
    host.addEventListener("auxclick", onAuxClick);
    host.addEventListener("click", onClick, true);
    host.addEventListener("keydown", onKeyDown);
    return () => {
      host.removeEventListener("mousedown", onMouseDown);
      host.removeEventListener("auxclick", onAuxClick);
      host.removeEventListener("click", onClick, true);
      host.removeEventListener("keydown", onKeyDown);
    };
  }, [opensInNewTab]);

  // A focused folder row offers ⌘K the same actions as its menu, kept current as it
  // collapses and expands; a focused file row leaves ⌘K the review's current file.
  const hasRowMenu = rowActions !== undefined;
  useEffect(() => {
    const host = treeHolder.current?.querySelector<HTMLElement>(FILE_TREE_ROOT_SELECTOR);
    if (!host || !hasRowMenu || folderOwner === undefined) return;
    const publish = () => {
      // An action run from ⌘K changes the folder while focus is in the palette.
      if (!host.contains(document.activeElement) && focusedReviewFolderTree() !== host) return;
      const focused = model.getFocusedPath();
      if (focused !== null && asFolder(model.getItem(focused))) {
        publishFocusedReviewFolder(host, {
          owner: folderOwner,
          path: folderPathOf(focused),
          actions: folderRowActions(model, focused, latest.current.folderActions),
        });
      } else {
        releaseFocusedReviewFolder(host);
      }
    };
    const unsubscribe = model.subscribe(publish);
    host.addEventListener("focusin", publish);
    return () => {
      unsubscribe();
      host.removeEventListener("focusin", publish);
      releaseFocusedReviewFolder(host);
    };
  }, [model, hasRowMenu, folderOwner]);

  // A refreshed listing can change a file's status or counts without changing its
  // path; update the mounted tree so selection, scroll, folders, and search stay put.
  const rowContent = treeRowContentKey(files);
  const shownRowContent = useRef(rowContent);
  const latestFiles = useRef(files);
  latestFiles.current = files;
  useEffect(() => {
    if (shownRowContent.current === rowContent) return;
    shownRowContent.current = rowContent;
    // Setting the statuses repaints the rows, counts included.
    model.setGitStatus(treeGitStatusEntries(latestFiles.current, appearance));
  }, [model, rowContent, appearance]);

  return (
    <>
      <div ref={treeHolder} className="flex min-h-0 flex-1 flex-col">
        <FileTree
          model={model}
          className={reference ? REFERENCE_TREE_CLASS : TREE_CLASS}
          renderRowDecoration={reference ? undefined : renderRowDecoration}
          onRowMenu={rowActions ? openRowMenu : undefined}
          style={{ ...TREE_THEME, flex: 1, minHeight: 0, colorScheme: dark ? "dark" : "light" }}
        />
      </div>
      {rowTooltip}
      {menu && (
        <TreeRowMenu
          actions={menu.actions}
          point={menu.point}
          onClose={() => setMenu(null)}
          onRestoreFocus={() => focusRow(menu.path)}
        />
      )}
    </>
  );
}

/** A folder row's path as the review names folders: no trailing slash. */
export function folderPathOf(treePath: string): string {
  return treePath.replace(/\/+$/, "");
}

function asFolder(item: FileTreeItemHandle | null): FileTreeDirectoryHandle | null {
  return item?.isDirectory() ? (item as FileTreeDirectoryHandle) : null;
}

/** Collapses an open folder row, or expands a closed one, as its chevron does. */
export function folderToggleAction(
  model: Pick<ReturnType<typeof useFileTree>["model"], "getItem">,
  treePath: string,
): DiffReviewFileAction {
  const expanded = asFolder(model.getItem(treePath))?.isExpanded() === true;
  return {
    id: expanded ? "collapse-folder" : "expand-folder",
    label: expanded ? "Collapse folder" : "Expand folder",
    icon: expanded ? ChevronsDownUpIcon : ChevronsUpDownIcon,
    run: () => asFolder(model.getItem(treePath))?.toggle(),
  };
}

/** A folder row's actions, in its menu and in ⌘K: Collapse or Expand, then the review's own. */
export function folderRowActions(
  model: Pick<ReturnType<typeof useFileTree>["model"], "getItem">,
  treePath: string,
  folderActions: ((path: string) => readonly DiffReviewFileAction[]) | undefined,
): DiffReviewFileAction[] {
  return [folderToggleAction(model, treePath), ...(folderActions?.(folderPathOf(treePath)) ?? [])];
}

/** A row's menu, opened where the pointer, the row or its actions button was. */
function TreeRowMenu({
  actions,
  point,
  onClose,
  onRestoreFocus,
}: {
  actions: readonly DiffReviewFileAction[];
  point: { x: number; y: number };
  onClose: () => void;
  /** Returns focus to the row the menu opened from. */
  onRestoreFocus: () => void;
}) {
  const focusReturn = useMenuFocusReturn(onRestoreFocus);
  if (actions.length === 0) return null;
  return createPortal(
    <DropdownMenu open modal={false} onOpenChange={(open) => !open && onClose()}>
      <DropdownMenuTrigger asChild>
        <span className="fixed" style={{ left: point.x, top: point.y }} />
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        data-testid="diff-review-tree-row-menu"
        // Portalled outside the tree, so it owns ⌘Enter itself: a pending prompt keeps it.
        {...{ [OWNS_COMMAND_ENTER_ATTRIBUTE]: "" }}
        {...focusReturn}
      >
        {actions.map(({ id, label, icon: Icon, disabledReason, link, run }) => (
          <SessionActionMenuItem
            key={id}
            disabledReason={disabledReason}
            link={link}
            onSelect={run}
          >
            <Icon />
            {label}
          </SessionActionMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>,
    document.body,
  );
}
