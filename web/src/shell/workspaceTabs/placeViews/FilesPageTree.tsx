// Shows the browsed folder, or name-filter matches, with root, reveal and Go to File
// controls and the tree's ⋯ menu.

import { FolderDotIcon, ListFilterIcon, LocateFixedIcon, SearchIcon } from "lucide-react";
import {
  type KeyboardEvent,
  type RefObject,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";

import { Button } from "@/components/ui/button";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { ChangedSort } from "@/lib/changedSort";
import { isOwnerLevel } from "@/lib/permissionsApi";
import { shortcutAriaKeys, shortcutKeys } from "@/lib/shortcuts/registry";
import { cn } from "@/lib/utils";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import { useSession } from "@/hooks/useSession";
import {
  IgnoredFilesSearchError,
  RunnerOfflineError,
  joinBrowseLocation,
  finderQueryPrefix,
  relativizeToWorkspace,
  listingCutOff,
  listingIgnoredFiles,
  useWorkspaceAllFiles,
  useWorkspaceChangedFiles,
  useWorkspaceEnvironment,
  useWorkspaceFileSearch,
  type WorkspaceFile,
} from "@/hooks/useWorkspaceChangedFiles";
import { requestFileFinder, useFileFinderAvailable } from "@/store/fileFinder";
import { usePaletteCommands } from "@/store/paletteCommands";
import { breadcrumbSegments, workspaceFolderName } from "@/shell/breadcrumbPaths";
import { useRememberedBrowseLocation } from "@/shell/filesPane/browseLocations";
import { type TreeFilterValues, treeFilterSummary } from "@/shell/filesPane/treeFilterSummary";
import { useTreeFilters } from "@/shell/filesPane/useTreeFilters";
import { activeFileTreePath } from "@/shell/fileTree/activeFileTreePath";
import {
  type TreePatterns,
  changesWithinFolder,
  treeFilePathFilter,
  treeRootEntries,
  visibleEntries,
} from "@/shell/fileTree/filesTreeListings";
import { FilesTree } from "@/shell/fileTree/FilesTree";
import { FilesTreeMatches } from "@/shell/fileTree/FilesTreeMatches";
import { REVEAL_ACTIVE_FILE_LABEL } from "@/shell/fileTree/revealActiveFile";
import { GO_TO_FILE_LABEL, GO_TO_FILE_UNAVAILABLE } from "@/shell/goToFile/goToFileCommands";
import {
  BACK_TO_WORKING_FOLDER_LABEL,
  planFolderAsTreeRoot,
  planWorkingFolderReturn,
  showTreeRoot,
} from "@/shell/fileTree/treeRoot";
import {
  CLEAR_TREE_FILTER_LABEL,
  useTreeNameFilterRequests,
} from "@/shell/fileTree/treeNameFilter";
import { useTreeActiveReveal } from "@/shell/fileTree/useTreeActiveReveal";

import { FilesTreeFilterBar } from "./FilesTreeFilterBar";
import { FilesTreeOptionsMenu } from "./filesTreeOptions";
import { useTreeFolderActions } from "./useTreeFolderActions";

export interface FilesPageTreeProps {
  conversationId: string;
  /** The tree is on screen, so it takes up the session's reveal requests. */
  active: boolean;
  /** The tree's pane owns the keyboard, so ⌘K offers its commands; at most one pane does. */
  commandsActive: boolean;
  /** The file the page shows, which the tree opens down to. */
  activePath: string | undefined;
  sort: ChangedSort;
  onSortChange: (sort: ChangedSort) => void;
  showHidden: boolean;
  onShowHiddenChange: (showHidden: boolean) => void;
  /** List what Git ignores, marked as ignored; otherwise leave it out. */
  showIgnored: boolean;
  onShowIgnoredChange: (showIgnored: boolean) => void;
  /**
   * A file picked in the tree, workspace-relative or absolute outside it. `newTab` for
   * a ⌘-click (Ctrl-click off macOS) or a middle click, which want it in its own tab.
   */
  onFileSelect: (path: string, options?: { newTab: true }) => void;
}

/** The globs a filter narrows the tree by: only while it has no name, which searches instead. */
function treePatternsOf(filter: TreeFilterValues): TreePatterns | undefined {
  const summary = treeFilterSummary(filter);
  return summary.active && !summary.search
    ? { include: filter.include, exclude: filter.exclude }
    : undefined;
}

function TreeMessage({ children, alert = false }: { children: string; alert?: boolean }) {
  return (
    <p
      role={alert ? "alert" : "status"}
      className={
        alert ? "px-3 py-2 text-ui text-destructive" : "px-3 py-2 text-ui text-muted-foreground"
      }
    >
      {children}
    </p>
  );
}

/** Why a listing shows nothing: everything left is hidden, or Git ignores the rest. */
function hiddenListingMessage(entries: readonly WorkspaceFile[], ignoredHidden: boolean): string {
  if (entries.length === 0) return "No files here, apart from any that Git ignores.";
  // The listing stopped short, so files past it may well show.
  if (listingCutOff(entries)) {
    return `The first ${entries.length.toLocaleString("en-US")} entries here are hidden, and the list stops there.`;
  }
  return ignoredHidden
    ? "Every file here is hidden or ignored by Git."
    : "Every file here is hidden.";
}

function HeaderButton({
  label,
  tooltip,
  shortcut,
  ariaKeyShortcuts,
  disabled = false,
  onClick,
  onKeyDown,
  children,
}: {
  label: string;
  tooltip: string;
  /** Keys shown in the tooltip. */
  shortcut?: readonly string[];
  ariaKeyShortcuts?: string;
  disabled?: boolean;
  onClick: () => void;
  onKeyDown?: (event: KeyboardEvent<HTMLButtonElement>) => void;
  children: React.ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {/* aria-disabled, not disabled, so the tooltip can still say why. */}
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label={label}
          aria-disabled={disabled || undefined}
          aria-keyshortcuts={ariaKeyShortcuts}
          className="shrink-0 text-muted-foreground hover:text-foreground aria-disabled:opacity-50 aria-disabled:hover:text-muted-foreground"
          onClick={() => {
            if (!disabled) onClick();
          }}
          onKeyDown={onKeyDown}
        >
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom" shortcut={shortcut?.length ? shortcut : undefined}>
        {tooltip}
      </TooltipContent>
    </Tooltip>
  );
}

export function FilesPageTree({
  conversationId,
  active,
  commandsActive,
  activePath,
  sort,
  onSortChange,
  showHidden,
  onShowHiddenChange,
  showIgnored,
  onShowIgnoredChange,
  onFileSelect,
}: FilesPageTreeProps) {
  const onShowHidden = () => onShowHiddenChange(true);
  const onShowIgnored = () => onShowIgnoredChange(true);
  const environment = useWorkspaceEnvironment(conversationId);
  const workspaceRoot = environment.data?.root ?? null;
  const browsed = useRememberedBrowseLocation(conversationId);
  // The wire form: "" for the workspace root, relative inside it, absolute outside.
  const location = relativizeToWorkspace(browsed, workspaceRoot);
  const { session } = useSession(conversationId);
  const canBrowseOutside = isOwnerLevel(session?.permissionLevel ?? null);
  const files = useWorkspaceAllFiles(conversationId, { hideIgnored: !showIgnored }, location);
  // Until this listing answers, assume its host can leave ignored files out, so none show early.
  const searchHidesIgnored = !showIgnored && listingIgnoredFiles(files) !== "included";
  const changes = useWorkspaceChangedFiles(conversationId);
  const reveal = useTreeActiveReveal({
    conversationId,
    active,
    activePath: activePath ?? null,
    location,
    workspaceRoot,
    workspaceKnown: environment.data !== undefined || environment.isError,
    showHidden,
    showIgnored,
    onShowIgnored,
  });
  const away = planWorkingFolderReturn({ browsed, workspaceRoot }).availability.available;
  const folderName =
    browsed === null
      ? workspaceFolderName(workspaceRoot)
      : (breadcrumbSegments(browsed).at(-1)?.label ?? browsed);
  const folder = browsed ?? workspaceRoot;

  // The filter, remembered per session so a popover closing after a pick keeps it.
  const filters = useTreeFilters(conversationId);
  const summary = treeFilterSummary(filters.applied);
  // A name searches; globs without one narrow the tree itself.
  const searching = summary.active && summary.search;
  const patterns = treePatternsOf(filters.applied);
  const [filterOpen, setFilterOpen] = useState(() =>
    Boolean(filters.edited.nameContains || filters.edited.include || filters.edited.exclude),
  );
  const filterInput = useRef<HTMLInputElement>(null);
  const [focusFilterToken, setFocusFilterToken] = useState(0);
  const openFilter = () => {
    setFilterOpen(true);
    setFocusFilterToken((token) => token + 1);
  };
  useEffect(() => {
    if (focusFilterToken > 0) filterInput.current?.focus();
  }, [focusFilterToken]);
  // Closing the filter takes away the field that had focus, so focus goes to the ⋯ it came from.
  const root = useRef<HTMLDivElement>(null);
  const [focusToggleToken, setFocusToggleToken] = useState(0);
  useEffect(() => {
    if (focusToggleToken === 0) return;
    root.current?.querySelector<HTMLElement>("[data-tree-options-trigger]")?.focus();
  }, [focusToggleToken]);
  const closeFilter = () => {
    filters.clear();
    setFilterOpen(false);
    setFocusToggleToken((token) => token + 1);
  };
  useTreeNameFilterRequests(active ? conversationId : undefined, openFilter);
  // A reveal shows the file in the whole tree, so it ends a name filter, even one still
  // waiting to apply; the matches never take a reveal. Once per request, so a name typed
  // while a reveal waits, as behind a listing that failed, stays.
  const revealKey = reveal.activeReveal?.key ?? null;
  const filterEndedFor = useRef<number | null>(null);
  const filterName = filters.applied.nameContains || filters.edited.nameContains;
  const { clearNameContains } = filters;
  useEffect(() => {
    if (revealKey === null || filterEndedFor.current === revealKey) return;
    filterEndedFor.current = revealKey;
    if (filterName) clearNameContains();
  }, [revealKey, filterName, clearNameContains]);
  // Likewise a reveal of a file the globs leave out ends them, so the file can show, even
  // globs still waiting to apply, which would otherwise hide it once the reveal is done.
  const revealFile = reveal.activeReveal?.file ?? null;
  const revealFiltered =
    revealFile !== null &&
    [patterns, treePatternsOf(filters.edited)].some(
      (globs) => globs !== undefined && !treeFilePathFilter(true, globs)(revealFile),
    );
  const { clear: clearFilters } = filters;
  useEffect(() => {
    if (revealFiltered) clearFilters();
  }, [revealFiltered, clearFilters]);
  const clearCommands = useMemo(
    () =>
      commandsActive && summary.active
        ? [
            {
              id: "files.clear-tree-filter",
              label: CLEAR_TREE_FILTER_LABEL,
              icon: ListFilterIcon,
              keywords: ["files", "tree", "filter", "clear", "name"],
              run: () => filters.clear(),
            },
          ]
        : [],
    // The command only needs to exist while a filter is on; `clear` is stable per session.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [commandsActive, summary.active, conversationId],
  );
  // Each mounted tree registers its own, so an inactive tab can't drop the active tab's row.
  const commandSource = useId();
  usePaletteCommands(`files-page-tree-filter:${commandSource}`, "Files", clearCommands);
  const search = useWorkspaceFileSearch(
    conversationId,
    filters.applied.nameContains,
    filters.applied.include,
    filters.applied.exclude,
    // The matches leave out what Git ignores where the listing does, on a host that can.
    { enabled: searching, hideIgnored: searchHidesIgnored },
    location,
  );
  // Refresh reloads what the tree shows: the root, its changes, the filter's matches and,
  // through the token, every folder opened under the root.
  const [refreshToken, setRefreshToken] = useState(0);
  const folderActions = useTreeFolderActions({
    conversationId,
    folder,
    workspaceRoot,
    hostId: session?.hostId ?? null,
    canBrowseOutside,
    reach: environment.data?.reachable ?? null,
    onNavigate: (treeRoot) => showTreeRoot(conversationId, treeRoot),
    refreshing: files.isFetching || changes.isFetching || (searching && search.isFetching),
    onRefresh: () => {
      setRefreshToken((token) => token + 1);
      void files.refetch();
      void changes.refetch();
      if (searching) void search.refetch();
    },
  });
  // Typing keeps the previous matches in view, but a new root's search starts from none:
  // matches from the old folder would open the wrong paths. Switching ignored files off
  // starts from none too, so the old search's ignored matches don't linger.
  const settledSearch = useRef<string | null>(null);
  const searchScope = `${location}\0${searchHidesIgnored}`;
  useEffect(() => {
    if (search.data && !search.isPlaceholderData) settledSearch.current = searchScope;
  });
  const matchesFromOtherSearch = search.isPlaceholderData && settledSearch.current !== searchScope;
  // Git-only paths count when deciding whether the globs leave the tree empty.
  const listing = files.data?.available ? files.data.data : null;
  const folderChanges = useMemo(
    () =>
      changesWithinFolder(
        changes.data?.available ? changes.data.data : [],
        location,
        workspaceRoot,
      ),
    [changes.data, location, workspaceRoot],
  );
  const allHidden = listing !== null && visibleEntries(listing, showHidden).length === 0;
  const filteredOutText =
    summary.active &&
    listing !== null &&
    patterns !== undefined &&
    treeRootEntries(listing, folderChanges, showHidden, patterns).length === 0
      ? summary.emptyText
      : null;
  const filteredOut = filteredOutText !== null;
  // A listing with nothing to show mounts no tree, so a reveal it holds is missed here.
  const emptyListing = !searching && !revealFiltered && (allHidden || filteredOut);
  const pendingReveal = reveal.activeReveal;
  useEffect(() => {
    if (!pendingReveal || !emptyListing) return;
    reveal.onActiveRevealHandled(pendingReveal.key);
    const file = pendingReveal.file ?? activeFileTreePath(activePath, location, workspaceRoot);
    if (file) reveal.onActiveRevealMissed(file);
    // Runs once per reveal; the rest is read from the render that holds it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingReveal, emptyListing]);
  const dark = useResolvedThemeMode() === "dark";
  const treeArea = useRef<HTMLDivElement>(null);
  useFocusTreeAfterRootChange(treeArea, location);

  const fileFinderAvailable = useFileFinderAvailable(conversationId);
  const goToFileKeys = shortcutKeys("palette.files");
  // Go to File starts within the browsed folder, plus any letter typed on its button.
  const goToFile = (typed = "") =>
    requestFileFinder(conversationId, {
      query: `${finderQueryPrefix(location)}${typed}` || undefined,
    });

  const header = (
    <div
      className="flex h-8 shrink-0 items-center gap-0.5 border-b border-border pr-1 pl-3"
      data-testid="files-page-tree-header"
    >
      {/* A right-click on the folder's name offers the same folder actions as the ⋯. */}
      <ContextMenu>
        <ContextMenuTrigger asChild>
          <span
            className={cn(
              "min-w-0 flex-1 truncate text-ui",
              away ? "text-foreground" : "text-muted-foreground",
            )}
            title={folder ?? undefined}
            data-testid="browse-location-menu-trigger"
          >
            {folderName}
          </span>
        </ContextMenuTrigger>
        <ContextMenuContent>
          {folderActions.map((action) => (
            <ContextMenuItem key={action.key} disabled={action.disabled} onSelect={action.onSelect}>
              {action.icon}
              {action.label}
            </ContextMenuItem>
          ))}
        </ContextMenuContent>
      </ContextMenu>
      {away ? (
        <HeaderButton
          label={BACK_TO_WORKING_FOLDER_LABEL}
          tooltip={BACK_TO_WORKING_FOLDER_LABEL}
          onClick={() => showTreeRoot(conversationId, null)}
        >
          <FolderDotIcon />
        </HeaderButton>
      ) : null}
      <HeaderButton
        label={REVEAL_ACTIVE_FILE_LABEL}
        tooltip={
          reveal.plan.availability.available
            ? REVEAL_ACTIVE_FILE_LABEL
            : (reveal.plan.availability.reason ?? REVEAL_ACTIVE_FILE_LABEL)
        }
        disabled={!reveal.plan.availability.available}
        onClick={reveal.revealActiveFile}
      >
        <LocateFixedIcon />
      </HeaderButton>
      <HeaderButton
        label={GO_TO_FILE_LABEL}
        tooltip={fileFinderAvailable ? GO_TO_FILE_LABEL : GO_TO_FILE_UNAVAILABLE}
        disabled={!fileFinderAvailable}
        shortcut={fileFinderAvailable ? goToFileKeys : undefined}
        ariaKeyShortcuts={shortcutAriaKeys("palette.files")}
        onClick={() => goToFile()}
        // A letter typed on it starts the finder's query with that letter.
        onKeyDown={(event) => {
          if (!fileFinderAvailable || event.key.length !== 1 || event.key === " ") return;
          if (event.metaKey || event.ctrlKey || event.altKey) return;
          event.preventDefault();
          goToFile(event.key);
        }}
      >
        <SearchIcon />
      </HeaderButton>
      <FilesTreeOptionsMenu
        sort={sort}
        onSortChange={onSortChange}
        showHidden={showHidden}
        onShowHiddenChange={onShowHiddenChange}
        showIgnored={showIgnored}
        onShowIgnoredChange={onShowIgnoredChange}
        filtering={summary.active}
        onFilter={() => (filterOpen ? filterInput.current?.focus() : openFilter())}
        folderActions={folderActions}
      />
    </div>
  );

  // Says what the filter narrows, over the matches or the narrowed tree, and clears it.
  const strip = summary.active ? (
    <div
      role="status"
      className="mx-2 mt-2 flex shrink-0 items-center gap-1.5 rounded-md bg-muted py-1 pr-1 pl-2 text-xs text-muted-foreground"
      data-testid="tree-filter-strip"
    >
      <ListFilterIcon className="size-3.5 shrink-0" aria-hidden />
      <span className="min-w-0 flex-1 truncate" title={summary.text}>
        {summary.text}
      </span>
      <Button type="button" variant="ghost" size="xs" onClick={() => filters.clear()}>
        Clear
      </Button>
    </div>
  ) : null;

  const filteredBody = () => {
    if (!searching) return null;
    const result = matchesFromOtherSearch ? undefined : search.data;
    // The host can't search, or the search waits, never starting, on an unreachable workspace.
    const unsearchable =
      result?.unavailable || (!result && search.isPending && search.fetchStatus === "idle");
    let content: React.ReactNode;
    if (search.isError && !result && search.error instanceof IgnoredFilesSearchError) {
      // Retried already; the reader can try again, or search with ignored files too.
      content = (
        <div className="flex flex-col items-start gap-2 pb-2">
          <TreeMessage>{search.error.message}</TreeMessage>
          <div className="mx-3 flex flex-wrap gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={search.isFetching}
              onClick={() => void search.refetch()}
            >
              Try again
            </Button>
            <Button type="button" variant="outline" size="sm" onClick={onShowIgnored}>
              Show git-ignored files
            </Button>
          </div>
        </div>
      );
    } else if (search.isError && !result) {
      content = (
        <TreeMessage alert>{`Couldn’t search the files: ${search.error.message}`}</TreeMessage>
      );
    } else if (unsearchable) {
      content = (
        <TreeMessage>
          The files can’t be searched right now. Send a message to reconnect the session’s machine.
        </TreeMessage>
      );
    } else if (!result) {
      content = <TreeMessage>Searching…</TreeMessage>;
    } else {
      const matches = result.files.filter(
        (match) => showHidden || !match.path.split("/").some((part) => part.startsWith(".")),
      );
      content =
        matches.length === 0 ? (
          <TreeMessage>
            {result.truncated
              ? "No matches in the files searched so far. The search stopped early, so narrow it with an include glob to search the rest."
              : result.hasMore
                ? "The first matches are all hidden files, so others may come after them. Type more of the name to narrow it."
                : summary.emptyText}
          </TreeMessage>
        ) : (
          <>
            <FilesTreeMatches
              matches={matches}
              activeFile={activeFileTreePath(activePath, location, workspaceRoot)}
              onFileSelect={(path, options) =>
                options
                  ? onFileSelect(joinBrowseLocation(location, path), options)
                  : onFileSelect(joinBrowseLocation(location, path))
              }
              label="Matching workspace files"
              dark={dark}
            />
            {!search.isPlaceholderData && (result.truncated || result.hasMore) ? (
              <p className="shrink-0 border-t border-border px-3 py-1.5 text-xs text-muted-foreground">
                Showing the first matches. Type more of the name to narrow them.
              </p>
            ) : null}
          </>
        );
    }
    return (
      <>
        {strip}
        {content}
      </>
    );
  };

  const treeBody = () => {
    if (files.isPending) return <TreeMessage>Loading files…</TreeMessage>;
    if (files.isError) {
      return files.error instanceof RunnerOfflineError ? (
        <TreeMessage>The session’s machine is offline. Send a message to reconnect it.</TreeMessage>
      ) : (
        <TreeMessage alert>{`Couldn’t list the files: ${files.error.message}`}</TreeMessage>
      );
    }
    if (!files.data.available) {
      return <TreeMessage>This session has no workspace files.</TreeMessage>;
    }
    const entries = files.data.data;
    // Only a host that left out what Git ignores can be hiding more than this listing shows.
    const ignoredHidden = files.data.ignoredHidden === true;
    if (entries.length === 0 && !ignoredHidden) {
      return (
        <TreeMessage>{away ? "This folder is empty." : "No files in the workspace."}</TreeMessage>
      );
    }
    if (visibleEntries(entries, showHidden).length === 0) {
      return (
        <div className="flex flex-col items-start gap-2 pb-2">
          <TreeMessage>{hiddenListingMessage(entries, ignoredHidden)}</TreeMessage>
          <div className="mx-3 flex flex-wrap gap-2">
            {entries.length > 0 ? (
              <Button type="button" variant="outline" size="sm" onClick={onShowHidden}>
                Show hidden files
              </Button>
            ) : null}
            {ignoredHidden ? (
              <Button type="button" variant="outline" size="sm" onClick={onShowIgnored}>
                Show git-ignored files
              </Button>
            ) : null}
          </div>
        </div>
      );
    }
    if (filteredOutText !== null) return <TreeMessage>{filteredOutText}</TreeMessage>;
    return (
      <FilesTree
        key={`${conversationId}\0${location}`}
        conversationId={conversationId}
        browseLocation={location}
        rootEntries={entries}
        changedFiles={folderChanges}
        sort={sort}
        showHidden={showHidden}
        showIgnored={showIgnored}
        patterns={patterns}
        refreshToken={refreshToken}
        onFileSelect={(path, options) => onFileSelect(joinBrowseLocation(location, path), options)}
        // Double-clicking a folder, or its menu's Open folder as root, roots the tree there.
        onNavigateDir={(path) => {
          const plan = planFolderAsTreeRoot({
            folder: joinBrowseLocation(location, path),
            browsed,
            workspaceRoot,
            canBrowseOutside,
          });
          if (plan.availability.available) showTreeRoot(conversationId, plan.root);
        }}
        reveal={null}
        activeFile={activeFileTreePath(activePath, location, workspaceRoot)}
        // Held back while globs that hide the file clear, so the tree can't miss it first.
        activeReveal={revealFiltered ? null : reveal.activeReveal}
        onActiveRevealMissed={reveal.onActiveRevealMissed}
        onActiveRevealHandled={reveal.onActiveRevealHandled}
        label="Workspace files"
      />
    );
  };

  const body = filteredBody() ?? (
    <>
      {strip}
      {treeBody()}
    </>
  );

  return (
    <div ref={root} className="flex min-h-0 flex-1 flex-col">
      {header}
      {filterOpen ? (
        <FilesTreeFilterBar filters={filters} inputRef={filterInput} onClose={closeFilter} />
      ) : null}
      <div
        ref={treeArea}
        // A tree popover starts here, and the area hands focus to the tree's current row.
        data-sidebar-initial-focus
        tabIndex={-1}
        className="flex min-h-0 flex-1 flex-col outline-none"
        onFocus={(event) => {
          if (event.target !== event.currentTarget) return;
          const area = event.currentTarget;
          focusTreeRow(area, () => document.activeElement === area);
        }}
      >
        {body}
      </div>
    </div>
  );
}

/** Focuses the tree's current row in `area` once its rows have rendered. */
function focusTreeRow(area: HTMLElement | null, stillWanted: () => boolean): () => void {
  let frame = 0;
  let attempts = 0;
  const focusRow = () => {
    if (!stillWanted()) return;
    const row = area?.querySelector<HTMLElement>('[role="treeitem"][tabindex="0"]');
    if (row) row.focus({ preventScroll: true });
    else if (++attempts < 60) frame = requestAnimationFrame(focusRow);
  };
  frame = requestAnimationFrame(focusRow);
  return () => cancelAnimationFrame(frame);
}

/**
 * After the tree moves to another root, focus that was in it, or left with the
 * control that moved it (Back in the header), lands on the new tree's current row.
 */
function useFocusTreeAfterRootChange(area: RefObject<HTMLDivElement | null>, location: string) {
  const shown = useRef(location);
  useEffect(() => {
    if (shown.current === location) return;
    shown.current = location;
    const page = area.current?.parentElement;
    return focusTreeRow(area.current, () => {
      const focused = document.activeElement;
      if (!focused || focused === document.body) return true;
      // Typing in the filter keeps its focus.
      return page?.contains(focused) === true && !(focused instanceof HTMLInputElement);
    });
  }, [area, location]);
}
