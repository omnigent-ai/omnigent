// Shows the browsed folder, or name-filter matches, with root and reveal controls.

import { FolderDotIcon, ListFilterIcon, LocateFixedIcon } from "lucide-react";
import { type RefObject, useEffect, useId, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { ChangedSort } from "@/lib/changedSort";
import { isOwnerLevel } from "@/lib/permissionsApi";
import { cn } from "@/lib/utils";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import { useSession } from "@/hooks/useSession";
import {
  IgnoredFilesSearchError,
  RunnerOfflineError,
  joinBrowseLocation,
  relativizeToWorkspace,
  listingCutOff,
  listingIgnoredFiles,
  useWorkspaceAllFiles,
  useWorkspaceChangedFiles,
  useWorkspaceEnvironment,
  useWorkspaceFileSearch,
  type WorkspaceFile,
} from "@/hooks/useWorkspaceChangedFiles";
import { usePaletteCommands } from "@/store/paletteCommands";
import { breadcrumbSegments, workspaceFolderName } from "@/shell/breadcrumbPaths";
import { useRememberedBrowseLocation } from "@/shell/filesPane/browseLocations";
import { treeFilterSummary } from "@/shell/filesPane/treeFilterSummary";
import { useTreeFilters } from "@/shell/filesPane/useTreeFilters";
import { activeFileTreePath } from "@/shell/fileTree/activeFileTreePath";
import { changesWithinFolder, visibleEntries } from "@/shell/fileTree/filesTreeListings";
import { FilesTree } from "@/shell/fileTree/FilesTree";
import { FilesTreeMatches } from "@/shell/fileTree/FilesTreeMatches";
import { REVEAL_ACTIVE_FILE_LABEL } from "@/shell/fileTree/revealActiveFile";
import {
  BACK_TO_WORKING_FOLDER_LABEL,
  planFolderAsTreeRoot,
  planWorkingFolderReturn,
  showTreeRoot,
} from "@/shell/fileTree/treeRoot";
import {
  CLEAR_TREE_FILTER_LABEL,
  FILTER_TREE_BY_NAME_LABEL,
  useTreeNameFilterRequests,
} from "@/shell/fileTree/treeNameFilter";
import { useTreeActiveReveal } from "@/shell/fileTree/useTreeActiveReveal";

import { FilesTreeFilterBar } from "./FilesTreeFilterBar";

export interface FilesPageTreeProps {
  conversationId: string;
  /** The tree is on screen, so it takes up the session's reveal requests. */
  active: boolean;
  /** The tree's pane owns the keyboard, so ⌘K offers its commands; at most one pane does. */
  commandsActive: boolean;
  /** The file the page shows, which the tree opens down to. */
  activePath: string | undefined;
  sort: ChangedSort;
  showHidden: boolean;
  /** Shows hidden files, for a workspace whose every file is hidden. */
  onShowHidden?: () => void;
  /** List what Git ignores, marked as ignored; otherwise leave it out. */
  showIgnored: boolean;
  /** Shows ignored files, for a workspace where Git ignores every file. */
  onShowIgnored?: () => void;
  /**
   * A file picked in the tree, workspace-relative or absolute outside it. `newTab` for
   * a ⌘-click (Ctrl-click off macOS) or a middle click, which want it in its own tab.
   */
  onFileSelect: (path: string, options?: { newTab: true }) => void;
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
  disabled = false,
  pressed,
  marked = false,
  onClick,
  children,
}: {
  label: string;
  tooltip: string;
  disabled?: boolean;
  /** A toggle's state, for a button that opens and closes something. */
  pressed?: boolean;
  /** Shows a dot, as while a filter narrows the tree. */
  marked?: boolean;
  onClick: () => void;
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
          aria-pressed={pressed}
          data-tree-filter-toggle={pressed === undefined ? undefined : ""}
          className={cn(
            "relative shrink-0 text-muted-foreground hover:text-foreground aria-disabled:opacity-50 aria-disabled:hover:text-muted-foreground",
            (pressed || marked) && "text-foreground",
          )}
          onClick={() => {
            if (!disabled) onClick();
          }}
        >
          {children}
          {marked ? (
            <span
              aria-hidden
              data-testid="tree-filter-dot"
              className="absolute top-0.5 right-0.5 size-1.5 rounded-full bg-primary"
            />
          ) : null}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom">{tooltip}</TooltipContent>
    </Tooltip>
  );
}

export function FilesPageTree({
  conversationId,
  active,
  commandsActive,
  activePath,
  sort,
  showHidden,
  onShowHidden,
  showIgnored,
  onShowIgnored,
  onFileSelect,
}: FilesPageTreeProps) {
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

  // The name filter, remembered per session so a popover closing after a pick keeps it.
  const filters = useTreeFilters(conversationId);
  const summary = treeFilterSummary(filters.applied);
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
  // Closing the filter takes away the field that had focus, so focus goes to its button.
  const root = useRef<HTMLDivElement>(null);
  const [focusToggleToken, setFocusToggleToken] = useState(0);
  useEffect(() => {
    if (focusToggleToken === 0) return;
    root.current?.querySelector<HTMLElement>("[data-tree-filter-toggle]")?.focus();
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
    { enabled: summary.active, hideIgnored: searchHidesIgnored },
    location,
  );
  // Typing keeps the previous matches in view, but a new root's search starts from none:
  // matches from the old folder would open the wrong paths. Switching ignored files off
  // starts from none too, so the old search's ignored matches don't linger.
  const settledSearch = useRef<string | null>(null);
  const searchScope = `${location}\0${searchHidesIgnored}`;
  useEffect(() => {
    if (search.data && !search.isPlaceholderData) settledSearch.current = searchScope;
  });
  const matchesFromOtherSearch = search.isPlaceholderData && settledSearch.current !== searchScope;
  // A listing with nothing to show mounts no tree, so a reveal it holds is missed here.
  const emptyListing =
    !summary.active &&
    files.data?.available === true &&
    visibleEntries(files.data.data, showHidden).length === 0;
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

  const header = (
    <div
      className="flex h-8 shrink-0 items-center gap-0.5 border-b border-border pr-1 pl-3"
      data-testid="files-page-tree-header"
    >
      <span
        className={cn(
          "min-w-0 flex-1 truncate text-ui",
          away ? "text-foreground" : "text-muted-foreground",
        )}
        title={browsed ?? workspaceRoot ?? undefined}
      >
        {folderName}
      </span>
      <HeaderButton
        label={FILTER_TREE_BY_NAME_LABEL}
        tooltip={summary.active ? `${FILTER_TREE_BY_NAME_LABEL}: on` : FILTER_TREE_BY_NAME_LABEL}
        pressed={filterOpen}
        marked={summary.active}
        onClick={() => (filterOpen ? closeFilter() : openFilter())}
      >
        <ListFilterIcon />
      </HeaderButton>
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
    </div>
  );

  const filteredBody = () => {
    if (!summary.active) return null;
    const strip = (
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
    );
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
            {onShowIgnored ? (
              <Button type="button" variant="outline" size="sm" onClick={onShowIgnored}>
                Show git-ignored files
              </Button>
            ) : null}
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

  const body = (() => {
    const filtered = filteredBody();
    if (filtered) return filtered;
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
            {entries.length > 0 && onShowHidden ? (
              <Button type="button" variant="outline" size="sm" onClick={onShowHidden}>
                Show hidden files
              </Button>
            ) : null}
            {ignoredHidden && onShowIgnored ? (
              <Button type="button" variant="outline" size="sm" onClick={onShowIgnored}>
                Show git-ignored files
              </Button>
            ) : null}
          </div>
        </div>
      );
    }
    return (
      <FilesTree
        key={`${conversationId}\0${location}`}
        conversationId={conversationId}
        browseLocation={location}
        rootEntries={entries}
        changedFiles={changesWithinFolder(
          changes.data?.available ? changes.data.data : [],
          location,
          workspaceRoot,
        )}
        sort={sort}
        showHidden={showHidden}
        showIgnored={showIgnored}
        refreshToken={0}
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
        activeReveal={reveal.activeReveal}
        onActiveRevealMissed={reveal.onActiveRevealMissed}
        onActiveRevealHandled={reveal.onActiveRevealHandled}
        label="Workspace files"
      />
    );
  })();

  return (
    <div ref={root} className="flex min-h-0 flex-1 flex-col">
      {header}
      {filterOpen ? (
        <FilesTreeFilterBar
          filters={filters}
          inertGlobs={!summary.active && summary.inertGlobs}
          inputRef={filterInput}
          onClose={closeFilter}
        />
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
