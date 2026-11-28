// The new Pull Requests list: a title and count, a filter field, Display options, status
// chips, and a sortable grid of rows grouped by lifecycle state. In a session's workspace, rows
// select like a mail list (click, Shift-click, ⌘-click, j/k, x); the number and title open the
// pull request.

import { removalDescription, removalPlace, type RowRemoval } from "./subAgentRemoval";
import {
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  Fragment,
  type Ref,
  useCallback,
  useEffect,
  useId,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ActivityIcon,
  ArrowDownIcon,
  ArrowUpIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  GitPullRequestIcon,
  ListChecksIcon,
  FolderGit2Icon,
  ListFilterIcon,
  type LucideIcon,
  SearchIcon,
  SlidersHorizontalIcon,
  SquareDashedIcon,
  Trash2Icon,
  XIcon,
} from "lucide-react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { pullRequestKey } from "@/lib/pullRequestReference";
import type { NativeStackAnswer } from "@/lib/pullRequestStacks/nativeStack";
import { matchesShortcut, shortcutKeys } from "@/lib/shortcuts/registry";
import { openLink } from "@/lib/openLinkInApp";
import { shortcutText } from "@/components/KeyboardShortcut";
import { SandboxResumeNotice } from "@/components/SandboxResumeNotice";
import { useWarmSharedDiffWorkers } from "@/components/diff-review/sharedDiffWorkers";
import { Checkbox } from "@/components/ui/checkbox";
import { OWNS_COMMAND_ENTER_ATTRIBUTE } from "@/hooks/useApproveHotkey";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { useNow } from "@/hooks/useNow";
import { useStableCallback } from "@/hooks/useStableCallback";
import { useDesktopFeatureFlag } from "@/hooks/useDesktopFeatureFlag";
import { PullRequestListMoreBlock, type PullRequestListMore } from "./PullRequestListMore";
import {
  type ListStackPlacement,
  withoutCollapsedStacks,
} from "@/lib/pullRequestStacks/listNesting";
import { PULL_REQUEST_STACKS_FLAG } from "@/pullRequestPage/usePullRequestStack";
import { withExtraRowActions } from "@/shell/rowActionOrder";
import {
  isCommandListed,
  type PaletteCommand,
  AVAILABLE,
  unavailableBecause,
  usePaletteCommands,
} from "@/store/paletteCommands";
import { settingCommand } from "@/store/paletteSettings";
import { pullRequestAuthorHues } from "@/shell/PullRequestAvatar";
import {
  removalBusyLabel,
  removalIncludes,
  removalPendingReason,
  removalStopper,
  STOP_REMOVING_LABEL,
} from "./removalProgress";
import { RemovalBusyFace } from "./RemovalBusyFace";
import {
  filterCounts,
  matchesFilter,
  matchesQuery,
  PULL_REQUEST_LIST_FILTERS,
  repositoryLabels,
  type PullRequestListFilter,
  type PullRequestListRow,
} from "@/shell/pullRequestListModel";
import {
  PullRequestStateIcon,
  type PullRequestListEntry,
  rowActionCommand,
  rowIdentity,
  type RowAction,
} from "@/shell/PullRequestListRows";
import {
  PullRequestListUnavailableSort,
  PullRequestListUnavailableSortReason,
} from "@/shell/PullRequestListStatsGap";
import { type OpenGesture, pullRequestOpenIntent } from "@/shell/pullRequestOpenIntent";
import {
  type GroupAction,
  type PullRequestListProps,
  usePullRequestListEntries,
} from "@/shell/usePullRequestListEntries";
import {
  effectivePageSort,
  groupPageRows,
  pageSortOptions,
  pageDisplayModified,
  shownPageDisplay,
  DEFAULT_PAGE_DISPLAY,
  displayAllowedFor,
  pageGroupings,
  pageSortAvailable,
  rangeSelection,
  retainSelection,
  showsGroupHeader,
  sortAfterHeaderClick,
  sortPageRows,
  type PageSort,
  type PageSortKey,
  repositoryChoices,
} from "./pullRequestListPageModel";
import { PullRequestListDisplayMenu } from "./PullRequestListDisplayMenu";
import {
  categoryField,
  EXCLUDE_REPOSITORY_CATEGORY,
  fieldCategoryId,
  filterMenuCategories,
  PullRequestListFilterChips,
  PullRequestListFilterMenu,
} from "./PullRequestListFilters";
import { FILTER_FIELD_BY_ID } from "./pullRequestListFields";
import type { ListFilter } from "./pullRequestListFilterModel";
import { usePullRequestListFilters } from "./usePullRequestListFilters";
import { useListMetrics } from "./useListMetrics";
import {
  PULL_REQUEST_LIST_PAGES_FLAG,
  type ListMetrics,
  itemHeight,
  listLayout,
  rowsIn,
  windowSlices,
  drawnItems,
} from "./listWindow";
import {
  PullRequestListGroupActionsButton,
  PullRequestListGroupMenu,
} from "./PullRequestListGroupMenu";
import { ListAvatar, PullRequestListPageRow, rowLink } from "./PullRequestListPageRow";
import type { ListSelectionAction } from "./listSelectionActions";
import { PullRequestListSelectionBar } from "./PullRequestListSelectionBar";
import { authorFilterKey, authorGroupActions, authorTargets } from "./authorFilter";
import { groupHeaderActions } from "./groupHeaderActions";
import { repositoryGroupActions, repositoryTargets } from "./repositoryGroupActions";
import { copySelection, useSelectionLinkActions } from "./useSelectionLinkActions";
import { useCollapsedStacks } from "./collapsedStacks";
import type { ListStackRowView } from "./PullRequestListStackParts";
import { useListStackNesting } from "./useListStackNesting";
import { type PullRequestEntryOwners, useRegisterListedEntries } from "./pullRequestEntryOwners";
import { usePullRequestListPageView } from "./usePullRequestListPageView";
import { useListGroupsTop } from "./useListGroupsTop";
import { SUB_AGENT_LIST_CAP } from "@/hooks/useSessionDescendants";
import {
  useIncludeSubAgentPullRequests,
  writeIncludeSubAgentPullRequests,
} from "@/lib/pullRequestListPreferences";
import "./pullRequestListPage.css";
import { INERT_SELECTOR } from "@/lib/inert";

// The selection's Remove key, shared by its handler, its label and its ⌘K row.
const REMOVE_SHORTCUT = "pullRequests.removeSelected";

const NO_COMMANDS: PaletteCommand[] = [];
const NO_REMOVAL_REASON = "You need edit access to remove pull requests from this session.";

// A column too narrow for its title shows a glyph; the title stays its name and tooltip.
const COLUMNS: { key?: PageSortKey; title: string; className: string; glyph?: LucideIcon }[] = [
  { key: "number", title: "#", className: "pr-list-col-number" },
  { key: "title", title: "Title", className: "pr-list-col-title" },
  { key: "status", title: "Status", className: "pr-list-col-status", glyph: ActivityIcon },
  { title: "CI", className: "pr-list-col-checks" },
  { key: "size", title: "Diff", className: "pr-list-col-diff" },
  { title: "Reviewers", className: "pr-list-col-reviewers" },
  { key: "author", title: "Author", className: "pr-list-col-author" },
  { key: "updated", title: "Updated", className: "pr-list-col-updated" },
];

const NOTHING_TO_FILTER_REASON = "There are no pull requests to filter yet.";
const NO_ROWS: readonly PullRequestListRow[] = [];
/** The rows a windowed list showed, which its reads follow, and the few it reads wherever they are. */
interface ReadWindow {
  visible: readonly string[];
  exceptions: readonly string[];
}
const NO_READ_WINDOW: ReadWindow = { visible: [], exceptions: [] };
/** Rows drawn past each edge of the viewport, so a scroll shows rows at once; they don't read. */
const LIST_OVERSCAN = 12;
const NO_KEYS: readonly string[] = [];
const NO_ENTRIES: readonly PullRequestListEntry[] = [];
const NO_GROUP_ACTIONS: readonly GroupAction[] = [];

/** Puts focus back on the list, such as when something opened from a row goes away. */
export interface PullRequestListFocus {
  /**
   * Focus the list with `url`'s row active; when that row is gone, with the row it had
   * active, else its first. False when there's no list on screen to focus.
   */
  focusRow: (url: string) => boolean;
}

export type PullRequestListPageProps = PullRequestListProps & {
  /** Lets the page put focus back on a row, such as after its sidebar goes. */
  focusHandle?: Ref<PullRequestListFocus>;
  /**
   * Nest stacked PRs under the PR they're stacked on, where stacks are shown. `foldScope`
   * names whose folds are kept on this device; nothing folds while it's null.
   */
  stacks?: {
    foldScope: string | null;
    /** GitHub's own stack for a row, as the page's read gave it; undefined where it can't say. */
    nativeStackOf?: (url: string) => NativeStackAnswer | undefined;
  };
  /** Hears every row the list has as they change, so a change it started checks each as listed. */
  onEntriesChange?: (entries: ReadonlyMap<string, PullRequestListEntry>) => void;
  /** The page's other PR menus, which take a PR this list shows from it while it's mounted. */
  entryOwners?: PullRequestEntryOwners;
  /** The session's sub-agent tree had more sub-agents than the list reads. */
  sessionSourcesTruncated?: boolean;
  /** The rows its search found that the list hasn't loaded yet, where it reads more of them. */
  more?: PullRequestListMore;
};

export function PullRequestListPage(props: PullRequestListPageProps) {
  // A PR opened from this list finds the diff's highlighting workers already started.
  useWarmSharedDiffWorkers();
  const {
    conversationId,
    active,
    sandboxAsleep,
    selectedUrl,
    onSelect,
    onUnlink,
    sessionSourcesTruncated,
    subAgentPullRequests,
    managesSessionLinks = true,
    selectionActions,
    selectionScope,
    unlinkPending,
    removalProgress = null,
    headerActions,
    showHeading = true,
    notices,
    placeholder,
    emptyState,
    filtersByRepository = false,
    allInProgress = false,
    searchOrdered = false,
    repositoryExclusion,
    groupActions,
    entryOwners,
  } = props;
  // Only the rows near the viewport draw, and only the rows in view read, while windowing is on.
  const windowing = useDesktopFeatureFlag(PULL_REQUEST_LIST_PAGES_FLAG, true).value;
  // Later pages load only into a windowed list; without it, the list keeps its first page.
  const more = windowing ? props.more : undefined;
  // What the list showed at its last render, which the next render's reads follow.
  const [readWindow, setReadWindow] = useState<ReadWindow>(NO_READ_WINDOW);
  const {
    entries,
    rows: entryRows,
    entriesByUrl,
    stats,
    statsColumns,
    canUnlink,
    removalOf,
    actionsFor,
    mergeRequestedFor,
    unlinkFinishedCommand,
    rowCommands,
    mergeDialog,
    changeInFlightFor,
  } = usePullRequestListEntries(props, {
    approveOnRows: true,
    lifecycleOnRows: true,
    readWindow: windowing ? readWindow : undefined,
    showsReadAges: true,
  });
  // The page's other PR menus take a PR this list shows from it, while it's shown and confirmed.
  const listedByKey = useMemo(
    () =>
      new Map(
        active
          ? entries.map((entry) => [pullRequestKey(entry.association.url), entry] as const)
          : [],
      ),
    [active, entries],
  );
  const listedKeys = useMemo(() => new Set(listedByKey.keys()), [listedByKey]);
  const listedActionsFor = useCallback(
    (url: string) => {
      const entry = listedByKey.get(pullRequestKey(url));
      return entry ? actionsFor(entry) : null;
    },
    [actionsFor, listedByKey],
  );
  useRegisterListedEntries(entryOwners, "table", {
    keys: listedKeys,
    actionsFor: listedActionsFor,
  });
  // A message in the table's place leaves no rows to filter, count, select or act on.
  // A null placeholder still holds the table's place, e.g. while a saved list restores.
  const inPlaceOfTable = placeholder !== undefined ? placeholder : emptyState;
  const filterable = inPlaceOfTable === undefined;
  // Behind a placeholder nothing is known yet, so the status chips name no count, not 0.
  const countsKnown = placeholder === undefined;
  const { view, updateView } = usePullRequestListPageView(conversationId);
  const includeSubAgents = useIncludeSubAgentPullRequests();
  const { query, filter, display: keptDisplay, displayChosen: keptChosen } = view;
  // A list in a search's own order shows it until the user picks a display.
  const display = displayAllowedFor(
    shownPageDisplay(keptDisplay, searchOrdered, keptChosen),
    !!subAgentPullRequests,
  );
  const displayModified = pageDisplayModified(keptDisplay, searchOrdered, keptChosen);
  const hiddenRepositories = repositoryExclusion?.hidden ?? NO_KEYS;
  const allRows = filterable ? entryRows : NO_ROWS;
  // The view's rows' entries, which the Filter menu lists the values of.
  const viewEntries = useMemo(
    () =>
      filtersByRepository ? allRows.flatMap((row) => entriesByUrl.get(row.url) ?? []) : NO_ENTRIES,
    [allRows, entriesByUrl, filtersByRepository],
  );
  const updateFilters = useCallback(
    (change: (current: readonly ListFilter[]) => readonly ListFilter[]) =>
      updateView((current) => ({ ...current, filters: change(current.filters ?? []) })),
    [updateView],
  );
  const listFilters = usePullRequestListFilters({
    entries: viewEntries,
    filters: filtersByRepository ? view.filters : undefined,
    update: updateFilters,
  });
  const narrowed = listFilters.active;
  // A filter that applies stays in reach however few PRs are left, even none.
  const filtersApply = narrowed || hiddenRepositories.length > 0;
  // With no PRs at all, the controls that narrow the list would only point at nothing.
  const controlsShown = placeholder !== undefined || emptyState === undefined || filtersApply;
  const rows = useMemo(() => {
    if (!narrowed) return allRows;
    const kept = new Set(listFilters.keep(viewEntries).map((entry) => entry.row.url));
    return allRows.filter((row) => kept.has(row.url));
  }, [allRows, listFilters, narrowed, viewEntries]);
  const collapsed = useMemo(() => new Set(view.collapsed), [view.collapsed]);
  const [selection, setSelection] = useState<ReadonlySet<string>>(() => new Set());
  const [anchorUrl, setAnchorUrl] = useState<string>();
  const [activeUrl, setActiveUrl] = useState<string | undefined>(selectedUrl);
  // The panel reuses one list across sessions, and the page reads as one account at a time:
  // another session's or account's selection isn't this one's.
  const scopeKey = `${conversationId}\n${selectionScope ?? ""}`;
  const [selectionSession, setSelectionSession] = useState(scopeKey);
  if (selectionSession !== scopeKey) {
    setSelectionSession(scopeKey);
    setSelection(new Set());
    setAnchorUrl(undefined);
    setActiveUrl(selectedUrl);
  }
  // Beside a pull request, stepping back or forward to another one moves the list's row too.
  const [seenSelectedUrl, setSeenSelectedUrl] = useState(selectedUrl);
  if (seenSelectedUrl !== selectedUrl) {
    setSeenSelectedUrl(selectedUrl);
    if (selectedUrl) setActiveUrl(selectedUrl);
  }
  const searchRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  // Brings a row into view: scrolled to by index while windowing, since it may not be drawn yet.
  const revealRef = useRef<(url: string) => void>(() => {});
  const revealRow = useCallback((url: string) => revealRef.current(url), []);
  // A row focus asked for, which the list's own first-row fallback on focus leaves be.
  const requestedActive = useRef<string | null>(null);
  const now = useNow().getTime();

  const urls = useMemo(() => rows.map((row) => row.url), [rows]);
  // The pull request on screen beside the list stays in view: on restore, once its row
  // loads, and on each step Back or Forward, however far down a long list it is.
  const shownRowListed = selectedUrl !== undefined && urls.includes(selectedUrl);
  useEffect(() => {
    if (!shownRowListed || selectedUrl === undefined) return;
    revealRow(selectedUrl);
  }, [revealRow, selectedUrl, shownRowListed]);

  const setQuery = (next: string) => updateView((current) => ({ ...current, query: next }));
  const setFilter = useCallback(
    (next: PullRequestListFilter) => updateView((current) => ({ ...current, filter: next })),
    [updateView],
  );

  const [filterMenuOpen, setFilterMenuOpen] = useState(false);
  const [excludedMenuOpen, setExcludedMenuOpen] = useState(false);
  // A note's Show opens the hidden repositories' chip menu.
  // Only a request made after mount opens it: the owner's hides arriving later ask nothing.
  const menuRequest = repositoryExclusion?.menuRequest ?? 0;
  const seenMenuRequest = useRef(menuRequest);
  useEffect(() => {
    if (menuRequest <= seenMenuRequest.current) return;
    seenMenuRequest.current = menuRequest;
    if (hiddenRepositories.length > 0) setExcludedMenuOpen(true);
    else setFilterMenuOpen(true);
  }, [menuRequest, hiddenRepositories.length]);
  const offersFilterMenu = filtersByRepository || repositoryExclusion !== undefined;
  // A field's values opened from ⌘K, under the Filter button.
  const [fieldMenu, setFieldMenu] = useState<string | null>(null);
  const filterCategories = useMemo(
    () =>
      filterMenuCategories({
        menus: filtersByRepository ? listFilters.menus : [],
        exclusion: repositoryExclusion,
        repositoryChoices: repositoryExclusion ? repositoryChoices(allRows, []) : [],
      }),
    [allRows, filtersByRepository, listFilters.menus, repositoryExclusion],
  );
  const exclusionCategory = filterCategories.find(
    (category) => category.id === EXCLUDE_REPOSITORY_CATEGORY,
  );
  const { toggle: toggleFieldValue } = listFilters;
  const toggleFilter = useCallback(
    (categoryId: string, key: string) => {
      if (categoryId === EXCLUDE_REPOSITORY_CATEGORY) {
        repositoryExclusion?.toggle(key);
        return;
      }
      const named = categoryField(categoryId);
      if (named) toggleFieldValue(named.field, key, named.exclude);
    },
    [repositoryExclusion, toggleFieldValue],
  );
  const counts = useMemo(() => filterCounts(rows, { allInProgress }), [rows, allInProgress]);
  // A chip whose last row went away shows everything instead of nothing.
  const effectiveFilter = filter !== "all" && counts[filter] === 0 ? "all" : filter;
  // A search, chip or filter narrows the list: the rows not loaded yet load only when asked.
  const filtering = query.trim() !== "" || effectiveFilter !== "all" || narrowed;
  // Some of the search's rows aren't loaded yet: counts say how many are loaded.
  const partial = (more?.remainder ?? 0) > 0;
  const statsReady = stats.status === "ready";
  const sort = effectivePageSort(display.sort, statsReady);
  const visible = useMemo(
    () =>
      sortPageRows(
        rows.filter(
          (row) =>
            matchesFilter(row, effectiveFilter, { allInProgress }) && matchesQuery(row, query),
        ),
        sort,
      ),
    [rows, effectiveFilter, allInProgress, query, sort],
  );
  const groups = useMemo(
    () => groupPageRows(visible, display.grouping),
    [visible, display.grouping],
  );
  const stacksShown =
    useDesktopFeatureFlag(PULL_REQUEST_STACKS_FLAG, false).value && props.stacks !== undefined;
  const nesting = useListStackNesting({
    enabled: stacksShown,
    scope: scopeKey,
    groups,
    entriesByUrl,
    nativeStackOf: props.stacks?.nativeStackOf,
  });
  const folds = useCollapsedStacks(stacksShown ? (props.stacks?.foldScope ?? null) : null);
  // Each group's rows as shown: in tree order once it nests, without folded stacks.
  const shownRows = useMemo(
    () =>
      new Map(
        groups.map((group) => {
          const placements = nesting?.get(group.id);
          return [
            group.id,
            placements ? withoutCollapsedStacks(placements, folds.isCollapsed) : null,
          ] as const;
        }),
      ),
    [folds.isCollapsed, groups, nesting],
  );
  const placementOf = useMemo(() => {
    const byUrl = new Map<string, ListStackPlacement>();
    for (const placements of nesting?.values() ?? []) {
      for (const placement of placements) byUrl.set(placement.url, placement);
    }
    return byUrl;
  }, [nesting]);
  const navigable = useMemo(
    () =>
      groups.flatMap((group) =>
        collapsed.has(group.id)
          ? []
          : (shownRows.get(group.id)?.map((placement) => placement.url) ??
            group.rows.map((row) => row.url)),
      ),
    [collapsed, groups, shownRows],
  );
  // Rows that leave the screen leave the selection: a filter, a search or a collapsed group
  // hiding a row means no action reaches it.
  useEffect(() => setSelection((current) => retainSelection(current, navigable)), [navigable]);
  const { onVisibleOrderChange } = props;
  useEffect(() => onVisibleOrderChange?.(navigable), [navigable, onVisibleOrderChange]);
  // The list going is said apart from an empty order: it may be a tab switch, not a removal.
  useEffect(() => () => onVisibleOrderChange?.(null), [onVisibleOrderChange]);
  // The row the keyboard acts on, while it's shown: filtering, collapsing or removing it clears
  // it, and folding its stack makes the nearest row it's nested under shown active instead.
  const activeRow = shownAncestor(activeUrl, navigable, placementOf);
  // Where a Shift range starts: a folded-away anchor starts it at the active row.
  const anchorRow = anchorUrl && navigable.includes(anchorUrl) ? anchorUrl : activeRow;
  // The last loaded row the keyboard moved on from while the next page read: once rows land
  // after it, the next one is active, unless the keyboard went elsewhere meanwhile.
  const advanceFrom = useRef<{ url: string; extend: boolean } | null>(null);
  // A fold from anywhere, another window's too, or a re-nesting moves the keyboard for good: an
  // unfold later doesn't bring a folded-away active row or anchor back.
  useEffect(() => {
    if (activeUrl !== undefined && activeRow !== undefined && activeRow !== activeUrl) {
      setActiveUrl(activeRow);
    }
    const anchorFolded =
      anchorUrl !== undefined &&
      !navigable.includes(anchorUrl) &&
      shownAncestor(anchorUrl, navigable, placementOf) !== undefined;
    if (anchorFolded) setAnchorUrl(activeRow);
  }, [activeRow, activeUrl, anchorUrl, navigable, placementOf]);
  const idBase = useId();
  const rowDomId = useCallback(
    (url: string) => `${idBase}-row-${urls.indexOf(url)}`,
    [idBase, urls],
  );
  const repositories = useMemo(() => repositoryLabels(rows), [rows]);
  const personHues = useMemo(
    () =>
      pullRequestAuthorHues(
        rows.flatMap((row) => [
          row.author,
          ...row.reviewers.filter((reviewer) => reviewer.kind === "user").map((r) => r.login),
        ]),
      ),
    [rows],
  );
  const numberChars = useMemo(
    () => Math.max(2, ...entries.map((entry) => rowIdentity(entry).reference.length)) + 1,
    [entries],
  );
  const removeKeys = shortcutText(shortcutKeys(REMOVE_SHORTCUT) ?? []);
  // In the order shown; never a row off screen, even before the selection catches up.
  const selectedUrls = useMemo(
    () => navigable.filter((url) => selection.has(url)),
    [navigable, selection],
  );
  // The one removal rule, the hook's: a row qualifies where it unlinks something, its sub-agents'
  // links only in a tree the user owns. Every removal route, count and label derives from it.
  const canRemoveFromSession = useCallback(
    (url: string) => entriesByUrl.has(url) && (removalOf(url)?.targets.length ?? 0) > 0,
    [entriesByUrl, removalOf],
  );
  const ownSelectedUrls = useMemo(
    () => selectedUrls.filter(canRemoveFromSession),
    [canRemoveFromSession, selectedUrls],
  );
  // Where removing the selection unlinks it from, and why none of it can be, where none can.
  const selectionPlace = useMemo(
    () => removalPlace(ownSelectedUrls.flatMap((url) => removalOf(url) ?? [])),
    [ownSelectedUrls, removalOf],
  );
  const selectionKeptReason = useMemo(
    () =>
      selectedUrls
        .flatMap((url) => removalOf(url) ?? [])
        .map((removal) => removalDescription(removal).unavailableReason)
        .find((reason) => reason !== null) ?? NO_REMOVAL_REASON,
    [removalOf, selectedUrls],
  );
  const selecting = selectedUrls.length > 0;
  // Whether the selection is what the removal under way removes, which its Remove then shows.
  const selectionInRemoval = removalIncludes(removalProgress, ownSelectedUrls);
  const removalReason = !canUnlink
    ? NO_REMOVAL_REASON
    : unlinkPending
      ? removalPendingReason(removalProgress, selectionInRemoval)
      : undefined;

  // ⌘-click, a middle click or ⌘Enter open it in a new tab behind this one, as links do.
  // The rows' handlers keep one identity, so a click re-renders only the rows it changes.
  const open = useStableCallback((url: string, gesture?: OpenGesture) =>
    onSelect(url, gesture ? pullRequestOpenIntent(gesture) : "here"),
  );

  const remove = useCallback(
    (targets: readonly string[]) => {
      const removable = targets.filter(canRemoveFromSession);
      if (removalReason || removable.length === 0) return;
      setSelection((current) => new Set([...current].filter((url) => !removable.includes(url))));
      onUnlink?.(removable);
    },
    [canRemoveFromSession, onUnlink, removalReason],
  );

  // Selection gathers rows to remove from the session or to act on together; a list that
  // offers neither is never selecting.
  const selectable = managesSessionLinks || selectionActions !== undefined;
  const toggle = useStableCallback((url: string, extend: boolean) => {
    setActiveUrl(url);
    if (!selectable) return;
    if (extend) {
      const range = rangeSelection(navigable, anchorRow, url);
      setSelection((current) => new Set([...current, ...range]));
      return;
    }
    setAnchorUrl(url);
    setSelection((current) => {
      const next = new Set(current);
      if (next.has(url)) next.delete(url);
      else next.add(url);
      return next;
    });
  });

  // A click on the row makes it active; only its number and title open the pull request.
  // ⌘-click opens a link in a new tab, as everywhere else; elsewhere it toggles the row.
  const activate = useStableCallback((url: string, event: ReactMouseEvent) => {
    const link = rowLink(event.target);
    if (link) event.preventDefault();
    if (selectable && event.shiftKey && !event.metaKey && !event.ctrlKey) {
      event.preventDefault();
      toggle(url, true);
      return;
    }
    if (selectable && !link && (event.metaKey || event.ctrlKey)) {
      toggle(url, false);
      return;
    }
    setActiveUrl(url);
    setAnchorUrl(url);
    // A drag across a link selects its text rather than following it.
    if (link && !hasTextSelection()) open(url, event);
  });

  const moveActive = useCallback(
    (offset: number, extend: boolean) => {
      if (navigable.length === 0) return;
      const index = activeRow ? navigable.indexOf(activeRow) : -1;
      // Past the last loaded row, the next page reads, and the next row is active once it lands.
      if (offset > 0 && index === navigable.length - 1 && more && partial && !filtering) {
        advanceFrom.current = { url: activeRow!, extend };
        more.request();
        return;
      }
      const nextIndex =
        index === -1
          ? offset > 0
            ? 0
            : navigable.length - 1
          : Math.min(navigable.length - 1, Math.max(0, index + offset));
      const next = navigable[nextIndex]!;
      setActiveUrl(next);
      if (extend && selectable) {
        const anchor = anchorRow ?? next;
        if (anchorUrl !== anchor) setAnchorUrl(anchor);
        setSelection(new Set(rangeSelection(navigable, anchor, next)));
      } else {
        setAnchorUrl(next);
      }
      revealRow(next);
    },
    [activeRow, anchorRow, anchorUrl, navigable, revealRow, selectable, more, partial, filtering],
  );
  // The page read past the last loaded row landed: the keyboard moves on to the next row.
  useEffect(() => {
    const pending = advanceFrom.current;
    if (!pending) return;
    const index = navigable.indexOf(pending.url);
    if (index === -1 || activeRow !== pending.url) {
      advanceFrom.current = null;
      return;
    }
    if (index === navigable.length - 1) return;
    advanceFrom.current = null;
    moveActive(1, pending.extend);
  }, [navigable, activeRow, moveActive]);
  // Another row of a stack becomes active, as j/k would make it.
  const goToRow = useCallback(
    (url: string) => {
      setActiveUrl(url);
      setAnchorUrl(url);
      revealRow(url);
    },
    [revealRow],
  );

  // Folding a stack keeps the keyboard on its top row, so nothing active is folded away.
  const setStackFolded = useStableCallback((url: string, folded: boolean) => {
    folds.setCollapsed(url, folded);
    if (folded && activeUrl && isNestedUnder(activeUrl, url, placementOf)) goToRow(url);
  });
  const toggleStack = useStableCallback((url: string) =>
    setStackFolded(url, !folds.isCollapsed(url)),
  );
  // Right opens a folded stack, then steps into it; Left folds it, then steps out to its parent.
  const stepInStack = useCallback(
    (url: string, inward: boolean) => {
      const placement = placementOf.get(url);
      if (!placement) return false;
      const parent = placement.descendants > 0;
      const folded = parent && folds.isCollapsed(url);
      if (inward) {
        if (!parent) return false;
        if (folded) setStackFolded(url, false);
        else goToRow(navigable[navigable.indexOf(url) + 1]!);
        return true;
      }
      if (parent && !folded) {
        setStackFolded(url, true);
        return true;
      }
      if (placement.parentUrl === null) return false;
      goToRow(placement.parentUrl);
      return true;
    },
    [folds, goToRow, navigable, placementOf, setStackFolded],
  );

  const focusRow = useStableCallback((url: string) => {
    const list = listRef.current;
    if (!list?.isConnected || list.closest(`[hidden], ${INERT_SELECTOR}`)) return false;
    // The row itself, else the row the list had active, else its first.
    const target = navigable.includes(url) ? url : (activeRow ?? navigable[0]);
    if (target) {
      requestedActive.current = target;
      setActiveUrl(target);
      setAnchorUrl(target);
      revealRow(target);
    }
    list.focus({ preventScroll: true });
    requestedActive.current = null;
    return document.activeElement === list;
  });
  const { focusHandle } = props;
  useImperativeHandle(focusHandle, () => ({ focusRow }), [focusRow]);

  // Folding or opening a stack, from its row's menus, ⌘K and the list's Left and Right keys.
  const stackActionFor = useCallback(
    (entry: PullRequestListEntry): RowAction | null => {
      const url = entry.association.url;
      if ((placementOf.get(url)?.descendants ?? 0) === 0) return null;
      const folded = folds.isCollapsed(url);
      const { reference } = rowIdentity(entry);
      return {
        id: "page:stack-fold",
        section: "page",
        label: folded ? "Expand Stack" : "Collapse Stack",
        paletteLabel: `${folded ? "Expand" : "Collapse"} the stack under ${reference}`,
        icon: folded ? ChevronRightIcon : ChevronDownIcon,
        keywords: ["stack", "stacked", "fold", "collapse", "expand", "tree"],
        // Focus comes back to the list on the stack's top row, which stays shown.
        movesFocus: true,
        run: () => {
          setStackFolded(url, !folded);
          focusRow(url);
        },
      };
    },
    [focusRow, folds, placementOf, setStackFolded],
  );
  // Each entry's actions, built once while what they depend on stands, so its row keeps its props.
  const rowActionsFor = useMemo(() => {
    const built = new WeakMap<PullRequestListEntry, RowAction[]>();
    return (entry: PullRequestListEntry) => {
      const cached = built.get(entry);
      if (cached) return cached;
      const stackAction = stackActionFor(entry);
      const actions = actionsFor(entry);
      const rowActions = stackAction ? withExtraRowActions(actions, [stackAction]) : actions;
      built.set(entry, rowActions);
      return rowActions;
    };
  }, [actionsFor, stackActionFor]);
  const stackCommands = useMemo(
    () =>
      entries.flatMap((entry) => {
        const action = stackActionFor(entry);
        return action ? [rowActionCommand(entry, action)] : [];
      }),
    [entries, stackActionFor],
  );

  const selectAll = useCallback(() => {
    if (selectable) setSelection(new Set(navigable));
  }, [navigable, selectable]);
  const clearSelection = useCallback(() => setSelection(new Set()), []);
  const focusFilter = useCallback(() => {
    searchRef.current?.focus();
    searchRef.current?.select();
  }, []);

  const onKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    // Keys in the list's menus and popovers bubble here through their portals; those are theirs.
    if (!(event.target instanceof Node) || !event.currentTarget.contains(event.target)) return;
    const inSearch = event.target === searchRef.current;
    if (event.key === "Escape") {
      if (selecting) clearSelection();
      else if (inSearch && query) setQuery("");
      else if (inSearch) searchRef.current?.blur();
      else if (effectiveFilter !== "all") setFilter("all");
      else if (narrowed) listFilters.clear();
      else return;
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    if (inSearch) {
      if (event.key === "ArrowDown" || event.key === "Enter") {
        event.preventDefault();
        searchRef.current?.blur();
        listRef.current?.focus();
        if (event.key === "Enter" && navigable.length === 1) open(navigable[0]!, event);
        else moveActive(1, false);
      }
      return;
    }
    if (isEditable(event.target)) return;
    if (filterable && event.key === "/" && !event.metaKey && !event.ctrlKey && !event.altKey) {
      event.preventDefault();
      focusFilter();
      return;
    }
    if (
      offersFilterMenu &&
      (event.key === "f" || event.key === "F") &&
      !event.metaKey &&
      !event.ctrlKey &&
      !event.altKey
    ) {
      event.preventDefault();
      setFilterMenuOpen(true);
      return;
    }
    // List keys act on the active row, so they apply only while the list itself has focus;
    // a focused button or menu keeps its own keys.
    if (event.target !== listRef.current && !rowLink(event.target)) return;
    const command = event.metaKey || event.ctrlKey;
    if (event.key === "ArrowDown" || (event.key === "j" && !command)) {
      event.preventDefault();
      moveActive(1, event.shiftKey);
    } else if (event.key === "ArrowUp" || (event.key === "k" && !command)) {
      event.preventDefault();
      moveActive(-1, event.shiftKey);
    } else if ((event.key === "Enter" || (event.key === "o" && !command)) && activeRow) {
      event.preventDefault();
      open(activeRow, event);
    } else if (
      (event.key === "ArrowRight" || event.key === "ArrowLeft") &&
      !command &&
      !event.shiftKey &&
      activeRow &&
      stepInStack(activeRow, event.key === "ArrowRight")
    ) {
      event.preventDefault();
    } else if (!selectable) {
      return;
    } else if ((event.key === "x" || event.key === " ") && !command && activeRow) {
      event.preventDefault();
      toggle(activeRow, event.shiftKey);
    } else if (event.key === "a" && command) {
      event.preventDefault();
      selectAll();
    } else if (managesSessionLinks && selecting && matchesShortcut(event, REMOVE_SHORTCUT)) {
      event.preventDefault();
      remove(ownSelectedUrls);
    }
  };

  const setSort = useCallback(
    (key: PageSortKey) =>
      updateView((current) => ({
        ...current,
        display: {
          ...shownPageDisplay(current.display, searchOrdered, current.displayChosen),
          sort: sortAfterHeaderClick(sort, key),
        },
        displayChosen: true,
      })),
    [sort, updateView, searchOrdered],
  );
  const setDisplay = useCallback(
    (next: typeof display) =>
      updateView((current) => ({ ...current, display: next, displayChosen: true })),
    [updateView],
  );
  // Back to the list's own display: its search's order where it has one, else the default.
  const resetDisplay = useCallback(
    () =>
      updateView((current) => ({
        ...current,
        display: DEFAULT_PAGE_DISPLAY,
        displayChosen: false,
      })),
    [updateView],
  );
  const reverseOrder = useCallback(
    () =>
      updateView((current) => ({
        ...current,
        display: {
          ...shownPageDisplay(current.display, searchOrdered, current.displayChosen),
          sort: { ...sort, direction: sort.direction === "ascending" ? "descending" : "ascending" },
        },
        displayChosen: true,
      })),
    [sort, updateView, searchOrdered],
  );
  const toggleGroup = useCallback(
    (id: string) =>
      updateView((current) => ({
        ...current,
        collapsed: current.collapsed.includes(id)
          ? current.collapsed.filter((value) => value !== id)
          : [...current.collapsed, id],
      })),
    [updateView],
  );
  // Each group's rows as shown now, for a header action that runs after its menu was built. Keyed
  // by grouping too: an author named "open" isn't the Open status group.
  const shownByGroup = useRef(new Map<string, readonly PullRequestListEntry[]>());
  // What each group offers, shared by its header and its ⌘K commands.
  const groupFacts = useMemo(
    () =>
      groups.map((group) => {
        const isCollapsed = collapsed.has(group.id);
        const liveKey = `${display.grouping}\n${group.id}`;
        const cleansUp = canUnlink && (group.state === "merged" || group.state === "closed");
        const removable = cleansUp
          ? group.rows.map((row) => row.url).filter(canRemoveFromSession)
          : [];
        // A finished group whose rows the user can't remove yet keeps Remove All, saying why.
        const removalBlocked =
          cleansUp && removable.length === 0
            ? (group.rows
                .flatMap((row) => removalOf(row.url) ?? [])
                .map((removal) => removalDescription(removal).unavailableReason)
                .find((reason) => reason !== null) ?? null)
            : null;
        // The removal under way names the group's rows, so its Remove All says how far it got.
        const groupUrls = group.rows.map((row) => row.url);
        const inRemoval = unlinkPending && removalIncludes(removalProgress, groupUrls);
        const removalPending = unlinkPending
          ? removalPendingReason(removalProgress, inRemoval)
          : null;
        const removalBusy = inRemoval && removalProgress ? removalBusyLabel(removalProgress) : null;
        const removalStop = inRemoval ? removalStopper(removalProgress) : null;
        const shownUrls = [
          ...new Set(
            shownRows.get(group.id)?.map((placement) => placement.url) ??
              group.rows.map((row) => row.url),
          ),
        ];
        const shownEntries = shownUrls.flatMap((url) => entriesByUrl.get(url) ?? []);
        // Every row's repository, shown or not, so a collapsed header keeps its links.
        const groupRepositories =
          display.grouping === "repository"
            ? repositoryTargets(group.rows.flatMap((row) => entriesByUrl.get(row.url) ?? []))
            : [];
        // Every row's author by host, so a login on two hosts names each.
        const groupAuthors =
          display.grouping === "author"
            ? authorTargets(group.rows.flatMap((row) => entriesByUrl.get(row.url) ?? []))
            : [];
        const pageActions =
          groupActions?.({
            grouping: display.grouping,
            id: group.id,
            title: group.title,
            state: group.state,
            repositories: groupRepositories,
            entries: shownEntries,
            shownEntries: () => shownByGroup.current.get(liveKey) ?? null,
          }) ?? NO_GROUP_ACTIONS;
        const actions =
          display.grouping === "none"
            ? pageActions
            : groupHeaderActions({
                title: group.title,
                collapsed: isCollapsed,
                shownUrls,
                selectable,
                selection,
                pageActions: [
                  ...repositoryGroupActions({
                    title: group.title,
                    targets: groupRepositories,
                    open: (url) => openLink(conversationId, url),
                    copy: copySelection,
                  }),
                  ...authorGroupActions({
                    targets: groupAuthors,
                    filter: filtersByRepository
                      ? (author) => listFilters.only("author", authorFilterKey(author))
                      : undefined,
                  }),
                  ...pageActions,
                ],
                select: (checked) => {
                  const shownNow = shownByGroup.current.get(liveKey) ?? [];
                  setSelection((current) => {
                    const next = new Set(current);
                    for (const entry of shownNow) {
                      if (checked) next.add(entry.association.url);
                      else next.delete(entry.association.url);
                    }
                    return next;
                  });
                },
              });
        return {
          group,
          liveKey,
          isCollapsed,
          removable,
          removalBlocked,
          removalPending,
          removalBusy,
          removalStop,
          entries: shownEntries,
          actions,
          header: showsGroupHeader({
            grouping: display.grouping,
            groupCount: groups.length,
            collapsed: isCollapsed,
            offersRemoval: removable.length > 0 || removalBlocked !== null,
            actionCount: pageActions.filter((action) => !action.onRows).length,
          }),
          ...groupRemoval(group.title, removable, removalOf),
        };
      }),
    [
      canRemoveFromSession,
      removalOf,
      canUnlink,
      collapsed,
      conversationId,
      display.grouping,
      entriesByUrl,
      filtersByRepository,
      groups,
      groupActions,
      selectable,
      selection,
      listFilters,
      shownRows,
      unlinkPending,
      removalProgress,
    ],
  );
  shownByGroup.current = new Map(
    groupFacts.flatMap((facts) => (facts.isCollapsed ? [] : [[facts.liveKey, facts.entries]])),
  );
  // The list as fixed-height items: each group's header, then its rows as shown.
  const layoutGroups = useMemo(
    () =>
      groupFacts.map(({ group, isCollapsed, header }) => ({
        id: group.id,
        header,
        urls: isCollapsed
          ? []
          : (shownRows.get(group.id)?.map((placement) => placement.url) ??
            group.rows.map((row) => row.url)),
      })),
    [groupFacts, shownRows],
  );
  const listMetrics = useListMetrics(listRef);
  const layout = useMemo(
    () => listLayout(layoutGroups, listMetrics.metrics),
    [layoutGroups, listMetrics.metrics],
  );
  const groupsRef = useRef<HTMLDivElement>(null);
  const groupsTop = useListGroupsTop(groupsRef, {
    nothingMatches: visible.length === 0,
    layout,
    // What a layout loop's record names: inputs by name, then counts and lifecycle sections.
    describe: () => ({
      inputs: {
        layout,
        layoutGroups,
        groupFacts,
        groups,
        shownRows,
        visible,
        collapsed,
        selection,
        entriesByUrl,
        filtersByRepository,
        listFilters,
        groupActions,
        removalOf,
        canRemoveFromSession,
        canUnlink,
        grouping: display.grouping,
        selectable,
        conversationId,
      },
      shape: () => ({
        where: managesSessionLinks ? "session" : "page",
        grouping: display.grouping,
        rows: visible.length,
        groups: groups.length,
        items: layout.items.length,
        selected: selection.size,
        collapsed: collapsed.size,
        windowing,
        sections:
          display.grouping === "status" || display.grouping === "none"
            ? groups.map((group) => group.id)
            : [],
      }),
    }),
  });
  // The keyboard's active row stays mounted wherever a scroll takes the viewport, so the grid's
  // active descendant still names it; it reads only as the read window's exception.
  const keptIndex = activeRow !== undefined ? layout.rowIndex.get(activeRow) : undefined;
  const rangeExtractor = useCallback(
    (range: Parameters<typeof drawnItems>[0]) => drawnItems(range, keptIndex),
    [keptIndex],
  );
  // Keys name the row height, so a width change re-estimates every item at its new height.
  const rowHeight = layout.metrics.rowHeight;
  const getItemKey = useCallback((index: number) => `${rowHeight}:${index}`, [rowHeight]);
  const virtualizer = useVirtualizer({
    count: windowing ? layout.items.length : 0,
    getScrollElement: () => listRef.current,
    getItemKey,
    estimateSize: (index) => itemHeight(layout.items[index]!, layout.metrics),
    overscan: LIST_OVERSCAN,
    scrollMargin: groupsTop,
    rangeExtractor,
  });
  const drawn = virtualizer.getVirtualItems();
  const windowed = windowing
    ? windowSlices(layout, layoutGroups, drawn.length > 0 ? drawn.map((item) => item.index) : null)
    : null;
  const inView = virtualizer.range
    ? rowsIn(layout, { start: virtualizer.range.startIndex, end: virtualizer.range.endIndex })
    : [];
  const drawnUrls = new Set(
    windowed ? [...windowed.slices.values()].flatMap((each) => each.urls) : [],
  );
  revealRef.current = (url: string) => {
    const index = layout.rowIndex.get(url);
    if (windowing) {
      // A width the list hasn't been laid out at yet reveals once it has, at the new offsets.
      if (listMetrics.measured.current !== layout.metrics) pendingReveal.current = url;
      else if (index !== undefined) virtualizer.scrollToIndex(index, { align: "auto" });
      return;
    }
    listRef.current
      ?.querySelector<HTMLElement>(`[data-pull-request-row="${CSS.escape(url)}"]`)
      ?.scrollIntoView({ block: "nearest" });
  };
  const pendingReveal = useRef<string | null>(null);
  // Rows a page brings in above the viewport, grouped or sorted in, and a width change that
  // resizes the rows, leave the first row in view where it was; a change of order, grouping or
  // filter moves the list as it always did.
  const scrollAnchor = useRef<{
    url: string;
    offset: number;
    order: string;
    count: number;
    metrics: ListMetrics;
  } | null>(null);
  const orderKey = `${sort.key}:${sort.direction}|${display.grouping}|${effectiveFilter}|${query}`;
  useLayoutEffect(() => {
    if (!windowing) return;
    const element = listRef.current;
    const kept = scrollAnchor.current;
    const index = kept ? layout.rowIndex.get(kept.url) : undefined;
    if (
      element &&
      kept &&
      index !== undefined &&
      kept.order === orderKey &&
      (visible.length > kept.count || kept.metrics !== layout.metrics)
    ) {
      const moved = layout.offsets[index]! - kept.offset;
      if (moved !== 0) element.scrollTop += moved;
    }
    const first = inView[0];
    const firstIndex = first !== undefined ? layout.rowIndex.get(first) : undefined;
    scrollAnchor.current =
      firstIndex !== undefined
        ? {
            url: first!,
            offset: layout.offsets[firstIndex]!,
            order: orderKey,
            count: visible.length,
            metrics: layout.metrics,
          }
        : null;
  });
  useLayoutEffect(() => {
    const url = pendingReveal.current;
    if (url === null || listMetrics.measured.current !== layout.metrics) return;
    pendingReveal.current = null;
    revealRef.current(url);
  });
  // The rows in view, and the active and open rows wherever they are, read on the next render.
  const inViewKey = inView.join("\n");
  const exceptionsKey = [activeRow, selectedUrl].filter(Boolean).join("\n");
  useEffect(() => {
    if (!windowing) return;
    setReadWindow((current) =>
      current.visible.join("\n") === inViewKey && current.exceptions.join("\n") === exceptionsKey
        ? current
        : {
            visible: inViewKey ? inViewKey.split("\n") : [],
            exceptions: exceptionsKey ? exceptionsKey.split("\n") : [],
          },
    );
  }, [exceptionsKey, inViewKey, windowing]);
  // Escape clears the selection, then the status chip; while either applies, it's the list's.
  const ownsEscape = active && (selecting || effectiveFilter !== "all" || narrowed);

  const allVisibleSelected = navigable.length > 0 && navigable.every((url) => selection.has(url));
  // What the selected rows offer, worked out afresh as the selection or its rows change.
  const { onEntriesChange } = props;
  useEffect(() => {
    onEntriesChange?.(entriesByUrl);
  }, [entriesByUrl, onEntriesChange]);
  // Every list that selects rows can open and copy them, after what its caller offers.
  // Each opens as its row's Open in Workspace Sidebar does, in the order the list shows them.
  const openInWorkspace = useCallback(
    (checked: readonly string[]) => {
      for (const url of checked) onSelect(url, "foreground");
    },
    [onSelect],
  );
  const selectionLinks = useSelectionLinkActions(
    conversationId,
    scopeKey,
    props.opensInWorkspace ? openInWorkspace : undefined,
  );
  const linkActionsFor = selectionLinks.actionsFor;
  // The caller's changes, then the list's own opens and copies, which menus set apart.
  const bulkActionGroups = useMemo((): (readonly ListSelectionAction[])[] => {
    if (!selecting) return [];
    const selectedEntries = selectedUrls.flatMap((url) => entriesByUrl.get(url) ?? []);
    return [selectionActions?.(selectedEntries) ?? [], linkActionsFor(selectedEntries)];
  }, [entriesByUrl, linkActionsFor, selecting, selectionActions, selectedUrls]);
  const bulkActions = useMemo(() => bulkActionGroups.flat(), [bulkActionGroups]);
  // The bar's own buttons, and what its More menu always lists, a group at a time.
  const barActions = useMemo(
    () => bulkActions.filter((action) => !action.inMoreMenu),
    [bulkActions],
  );
  const moreGroups = useMemo(
    () => bulkActionGroups.map((group) => group.filter((action) => action.inMoreMenu)),
    [bulkActionGroups],
  );
  // A checked row's menus offer what the selection bar does, from the same definitions, its opens
  // and copies in Open › and Copy › submenus as a single row's are.
  const selectionMenuActions = useMemo(
    (): RowAction[] =>
      selectedUrls.length < 2
        ? []
        : bulkActions.map((action) => ({
            id: `selection:${action.id}`,
            section: action.menuSection ?? "selection",
            ...(action.nestedLabel ? { nestedLabel: action.nestedLabel } : {}),
            label: action.paletteLabel,
            paletteLabel: action.paletteLabel,
            icon: action.icon,
            keywords: action.keywords,
            movesFocus: action.movesFocus,
            availability: action.unavailableReason
              ? unavailableBecause(action.unavailableReason)
              : AVAILABLE,
            run: action.run,
          })),
    [bulkActions, selectedUrls.length],
  );

  const paletteCommands = useMemo<PaletteCommand[]>(() => {
    const commands: PaletteCommand[] = [
      {
        id: "filter",
        label: "Filter pull requests",
        icon: SearchIcon,
        keywords: ["search", "find", "pull requests", "pr", "/"],
        movesFocus: true,
        availability: filterable ? undefined : unavailableBecause(NOTHING_TO_FILTER_REASON),
        run: focusFilter,
      },
      settingCommand({
        id: "pull-requests.filter",
        owner: conversationId,
        label: "Show pull requests",
        icon: ListFilterIcon,
        keywords: ["filter", "status", "open", "merged", "closed", "needs you"],
        current: effectiveFilter,
        choices: PULL_REQUEST_LIST_FILTERS.filter(
          (option) => option.id === "all" || counts[option.id] > 0,
        ).map((option) => ({ value: option.id, label: `${option.title} (${counts[option.id]})` })),
        choose: (value) => setFilter(value as PullRequestListFilter),
      }),
      settingCommand({
        id: "pull-requests.grouping",
        owner: conversationId,
        label: "Group pull requests by",
        icon: SlidersHorizontalIcon,
        keywords: ["display", "group"],
        current: display.grouping,
        choices: pageGroupings(!!subAgentPullRequests).map((option) => ({
          value: option.id,
          label: option.title,
        })),
        choose: (value) => {
          const option = pageGroupings(!!subAgentPullRequests).find(
            (candidate) => candidate.id === value,
          );
          if (option) setDisplay({ ...display, grouping: option.id });
        },
      }),
      settingCommand({
        id: "pull-requests.order",
        owner: conversationId,
        label: "Order pull requests by",
        icon: SlidersHorizontalIcon,
        keywords: ["display", "sort", "order"],
        current: sort.key,
        choices: pageSortOptions(searchOrdered)
          .filter((option) => pageSortAvailable(option.id, statsReady))
          .map((option) => ({
            value: option.id,
            label: option.title,
          })),
        choose: (value) => {
          const option = pageSortOptions(searchOrdered).find((candidate) => candidate.id === value);
          if (option) {
            setDisplay({
              ...display,
              sort: { key: option.id, direction: option.defaultDirection },
            });
          }
        },
      }),
    ];
    if (offersFilterMenu) {
      commands.push({
        id: "pull-requests.add-filter",
        label: "Add pull request filter",
        icon: ListFilterIcon,
        keywords: ["filter", "repository", "exclude", "hide", "f"],
        movesFocus: true,
        run: () => setFilterMenuOpen(true),
      });
    }
    if (filtersByRepository) {
      const repositoryValues =
        listFilters.menus.find((menu) => menu.field === "repository" && !menu.exclude)?.choices ??
        [];
      const onlyRepository = listFilters.onlyValue("repository");
      if (repositoryValues.length > 1 || onlyRepository) {
        commands.push(
          settingCommand({
            id: "pull-requests.repository",
            owner: conversationId,
            label: "Show pull requests in repository",
            icon: FolderGit2Icon,
            keywords: ["filter", "repository", "repo", "only"],
            current: onlyRepository ?? "",
            choices: [
              { value: "", label: "All repositories" },
              ...repositoryValues.map((choice) => ({
                value: choice.key,
                label: `${choice.label} (${choice.count})`,
              })),
            ],
            choose: (value) => listFilters.only("repository", value || null),
          }),
        );
      }
      // Each field's values from ⌘K, opened under the Filter button, as its submenu shows them.
      for (const menu of listFilters.menus) {
        if (menu.unsupported) continue;
        const { label } = FILTER_FIELD_BY_ID[menu.field];
        commands.push({
          id: `pull-requests.filter-by.${fieldCategoryId(menu.field, menu.exclude)}`,
          label: menu.exclude ? `Exclude ${label}…` : `Filter by ${label}…`,
          icon: ListFilterIcon,
          keywords: [
            "filter",
            "pull requests",
            label.toLowerCase(),
            ...(menu.exclude ? ["exclude", "without", "not"] : []),
          ],
          movesFocus: true,
          run: () => setFieldMenu(fieldCategoryId(menu.field, menu.exclude)),
        });
      }
      if (narrowed) {
        commands.push({
          id: "pull-requests.clear-filters",
          label: "Clear Pull Request Filters",
          icon: ListFilterIcon,
          keywords: ["filter", "clear", "reset", "remove", "chips"],
          run: listFilters.clear,
        });
      }
    }
    commands.push({
      id: "pull-requests.reverse-order",
      label:
        sort.direction === "ascending"
          ? "Order pull requests descending"
          : "Order pull requests ascending",
      icon: sort.direction === "ascending" ? ArrowDownIcon : ArrowUpIcon,
      keywords: ["display", "sort", "order", "reverse", "direction"],
      run: reverseOrder,
    });
    for (const {
      group,
      isCollapsed,
      removable,
      removalBlocked,
      removalPending,
      actions,
      header,
      removeTitle,
      removeDetail,
    } of groupFacts) {
      if (!header) continue;
      commands.push({
        id: `pull-requests.group.${group.id}`,
        label: `${isCollapsed ? "Expand" : "Collapse"} the ${group.title} group`,
        icon: ChevronRightIcon,
        keywords: ["group", "collapse", "expand", "fold"],
        run: () => toggleGroup(group.id),
      });
      for (const action of actions) {
        if (!action.paletteLabel) continue;
        commands.push({
          id: `pull-requests.group.${group.id}.${action.id}`,
          label: action.paletteLabel,
          icon: action.icon,
          keywords: [...(action.keywords ?? []), "group", group.title.toLowerCase()],
          availability: action.unavailableReason
            ? unavailableBecause(action.unavailableReason)
            : undefined,
          run: action.run,
        });
      }
      if (removable.length > 0 || removalBlocked !== null) {
        commands.push({
          id: `pull-requests.group.${group.id}.remove`,
          label: removeTitle,
          ...(removeDetail ? { description: removeDetail } : {}),
          icon: Trash2Icon,
          keywords: ["remove all", "unlink", "clean up"],
          availability: removalBlocked
            ? unavailableBecause(removalBlocked)
            : removalPending
              ? unavailableBecause(removalPending)
              : undefined,
          run: () => remove(removable),
        });
      }
    }
    if (subAgentPullRequests) {
      commands.push({
        id: "pull-requests.include-sub-agents",
        label: includeSubAgents
          ? "Hide sub-agent pull requests"
          : "Include sub-agent pull requests",
        icon: SlidersHorizontalIcon,
        keywords: ["display", "sub-agent", "sub agent", "agent", "session"],
        run: () => writeIncludeSubAgentPullRequests(!includeSubAgents),
      });
    }
    if (displayModified) {
      commands.push({
        id: "pull-requests.reset-display",
        label: "Reset pull request display options",
        icon: SlidersHorizontalIcon,
        keywords: ["display", "default", "group", "order"],
        run: resetDisplay,
      });
    }
    if (selectable && navigable.length > 0 && !allVisibleSelected) {
      commands.push({
        id: "pull-requests.select-all",
        label: partial
          ? `Select all loaded pull requests (${navigable.length})`
          : `Select all pull requests (${navigable.length})`,
        icon: ListChecksIcon,
        keywords: ["selection", "check"],
        run: selectAll,
      });
    }
    if (selecting) {
      for (const action of bulkActions) {
        commands.push({
          id: `pull-requests.selection.${action.id}`,
          label: action.paletteLabel,
          icon: action.icon,
          keywords: [...(action.keywords ?? []), "selection", "selected"],
          movesFocus: action.movesFocus,
          availability: action.unavailableReason
            ? unavailableBecause(action.unavailableReason)
            : undefined,
          run: action.run,
        });
      }
    }
    if (selecting && managesSessionLinks) {
      const own = ownSelectedUrls.length;
      commands.push({
        id: "pull-requests.remove-selected",
        label: `Remove ${own} selected ${plural(own, "pull request")} from ${selectionPlace.text}`,
        ...(selectionPlace.detail ? { description: selectionPlace.detail } : {}),
        icon: Trash2Icon,
        keywords: ["unlink", "remove", "selection"],
        shortcut: REMOVE_SHORTCUT,
        availability: removalReason
          ? unavailableBecause(removalReason)
          : own === 0
            ? unavailableBecause(selectionKeptReason)
            : undefined,
        run: () => remove(ownSelectedUrls),
      });
    }
    if (selecting) {
      commands.push({
        id: "pull-requests.clear-selection",
        label: "Clear pull request selection",
        icon: SquareDashedIcon,
        keywords: ["selection", "deselect"],
        run: clearSelection,
      });
    }
    if (unlinkFinishedCommand) commands.push(unlinkFinishedCommand);
    commands.push(...rowCommands, ...stackCommands);
    return commands.filter((command) => isCommandListed(command.availability));
  }, [
    allVisibleSelected,
    bulkActions,
    clearSelection,
    conversationId,
    filtersByRepository,
    offersFilterMenu,
    listFilters,
    narrowed,
    counts,
    display,
    displayModified,
    resetDisplay,
    searchOrdered,
    effectiveFilter,
    filterable,
    focusFilter,
    groupFacts,
    managesSessionLinks,
    navigable.length,
    includeSubAgents,
    ownSelectedUrls,
    partial,
    remove,
    removalReason,
    reverseOrder,
    rowCommands,
    selectAll,
    selectable,
    selecting,
    subAgentPullRequests,
    setDisplay,
    setFilter,
    sort.direction,
    sort.key,
    stackCommands,
    statsReady,
    toggleGroup,
    unlinkFinishedCommand,
    selectionPlace.text,
    selectionPlace.detail,
    selectionKeptReason,
  ]);
  // Behind a placeholder only Filter stays, saying why it can't run.
  const placeholderCommands = useMemo(
    () => paletteCommands.filter((command) => command.id === "filter"),
    [paletteCommands],
  );
  // A hidden list's commands would act on rows nobody sees; each mounted list owns its own.
  const paletteInstance = useId();
  usePaletteCommands(
    `pull-requests:${conversationId}:${paletteInstance}`,
    "Pull requests",
    !active ? NO_COMMANDS : filterable ? paletteCommands : placeholderCommands,
  );

  return (
    <TooltipProvider delayDuration={0}>
      <main
        className={`pr-list${selecting ? " selecting" : ""}`}
        aria-label="Pull requests"
        data-escape-owner={ownsEscape ? "" : undefined}
        onKeyDown={onKeyDown}
      >
        {(controlsShown || showHeading || headerActions) && (
          <header className="pr-list-header">
            {showHeading && (
              <div className="pr-list-heading">
                <h1>Pull Requests</h1>
                <span className="pr-list-heading-count">{rows.length}</span>
              </div>
            )}
            {controlsShown && (
              <div className="pr-list-toolbar">
                <label className="pr-list-search">
                  <SearchIcon aria-hidden />
                  <input
                    ref={searchRef}
                    type="search"
                    value={query}
                    disabled={!filterable}
                    placeholder="Filter by title, number, author, or branch"
                    aria-label="Filter pull requests"
                    spellCheck={false}
                    {...{ [OWNS_COMMAND_ENTER_ATTRIBUTE]: "" }}
                    onChange={(event) => setQuery(event.target.value)}
                  />
                  {query ? (
                    <button
                      type="button"
                      aria-label="Clear Filter"
                      onClick={() => {
                        setQuery("");
                        searchRef.current?.focus();
                      }}
                    >
                      <XIcon aria-hidden />
                    </button>
                  ) : (
                    filterable && <kbd aria-hidden>/</kbd>
                  )}
                </label>
                {offersFilterMenu && (
                  <PullRequestListFilterMenu
                    categories={filterCategories}
                    onToggle={toggleFilter}
                    active={filtersApply}
                    open={filterMenuOpen}
                    onOpenChange={setFilterMenuOpen}
                    field={fieldMenu}
                    onFieldClose={() => setFieldMenu(null)}
                  />
                )}
                <PullRequestListDisplayMenu
                  display={display}
                  modified={displayModified}
                  sortOptions={pageSortOptions(searchOrdered)}
                  sort={sort}
                  statsReady={statsReady}
                  onChange={setDisplay}
                  onReset={resetDisplay}
                  onReverse={reverseOrder}
                  subAgents={
                    subAgentPullRequests
                      ? { included: includeSubAgents, onChange: writeIncludeSubAgentPullRequests }
                      : undefined
                  }
                  subAgentPullRequests={!!subAgentPullRequests}
                />
              </div>
            )}
            {headerActions && <div className="pr-list-header-actions">{headerActions}</div>}
          </header>
        )}

        {/* The state tabs, then the active filters on the same row, wrapping when they don't fit. */}
        {controlsShown && (
          <div className="pr-list-state-row">
            <nav className="pr-list-filters" aria-label="Status">
              {PULL_REQUEST_LIST_FILTERS.filter(
                (option) => option.id === "all" || counts[option.id] > 0,
              ).map((option) => (
                <button
                  key={option.id}
                  type="button"
                  className={`pr-list-filter${effectiveFilter === option.id ? " selected" : ""}`}
                  aria-pressed={effectiveFilter === option.id}
                  aria-label={
                    countsKnown
                      ? `${chipTitle(option.id, option.title)} (${counts[option.id]})`
                      : chipTitle(option.id, option.title)
                  }
                  onClick={() => setFilter(option.id)}
                >
                  {option.id === "attention" ? (
                    <span className="pr-list-attention-glyph" aria-hidden />
                  ) : (
                    option.id !== "all" && (
                      <PullRequestStateIcon
                        decorative
                        state={option.id}
                        className="pr-list-state"
                      />
                    )
                  )}
                  <span>{chipTitle(option.id, option.title)}</span>
                  {countsKnown && <span className="pr-list-filter-count">{counts[option.id]}</span>}
                </button>
              ))}
            </nav>
            {offersFilterMenu && (
              <PullRequestListFilterChips
                chips={listFilters.chips}
                onToggleChip={(model, key) => listFilters.toggleChip(model.chip, key)}
                onOperatorChange={(model, operator) => {
                  if (operator === "anyOf" || operator === "allOf" || operator === "noneOf") {
                    listFilters.setOperator(model.chip, operator);
                  }
                }}
                onRemove={(model) => listFilters.remove(model.chip)}
                exclusionCategory={exclusionCategory}
                onToggle={toggleFilter}
                exclusion={repositoryExclusion}
                excludedMenuOpen={excludedMenuOpen}
                onExcludedMenuOpenChange={setExcludedMenuOpen}
              />
            )}
          </div>
        )}

        <div className="pr-list-notices">
          {notices}
          {sessionSourcesTruncated && (
            <p className="pr-list-notice-text" role="note">
              Showing pull requests from the first {SUB_AGENT_LIST_CAP.toLocaleString()} sub-agents.
            </p>
          )}
          {sandboxAsleep && (
            <SandboxResumeNotice
              sessionId={conversationId}
              layout="banner"
              purpose="load the PR details"
            />
          )}
        </div>

        {inPlaceOfTable !== undefined ? (
          <div className="pr-list-placeholder">{inPlaceOfTable}</div>
        ) : (
          <div
            ref={listRef}
            className={`pr-list-list${statsColumns ? "" : " no-stats"}${selectable ? "" : " no-row-selection"}`}
            role={stacksShown ? "treegrid" : "grid"}
            aria-label={managesSessionLinks ? "Linked pull requests" : "Pull requests"}
            // ⌘Enter on the focused list opens its active row behind, never a prompt's accept.
            {...{ [OWNS_COMMAND_ENTER_ATTRIBUTE]: "" }}
            aria-multiselectable={selectable}
            // A tree counts every row it draws: the column header, group headers and PR rows.
            aria-rowcount={
              (stacksShown
                ? 1 + groupFacts.filter((facts) => facts.header).length + navigable.length
                : visible.length) +
              // The rows not loaded yet count too, so the list says how long it is.
              (partial && !filtering ? more!.remainder : 0)
            }
            // Only a drawn row can be named; a windowed list scrolls the active row in as it moves.
            aria-activedescendant={
              activeRow && (!windowed || drawnUrls.has(activeRow)) ? rowDomId(activeRow) : undefined
            }
            tabIndex={0}
            style={{ "--pr-list-number-chars": numberChars } as CSSProperties}
            onFocus={(event) => {
              const requested = requestedActive.current;
              requestedActive.current = null;
              if (event.target === listRef.current && !activeRow && !requested && navigable[0]) {
                setActiveUrl(navigable[0]);
              }
            }}
          >
            <ColumnHeader
              sort={sort}
              setSort={setSort}
              statsReady={statsReady}
              sortReasonId={`${idBase}-sort-unavailable`}
              selectable={selectable}
              allSelected={allVisibleSelected}
              someSelected={selecting && !allVisibleSelected}
              toggleAll={() => (allVisibleSelected ? clearSelection() : selectAll())}
            />
            {visible.length === 0 && (
              <div className="pr-list-empty">
                <SearchIcon aria-hidden />
                <strong>No pull requests match</strong>
                <span>
                  {query ? (
                    <>
                      Nothing matches “{query}”
                      {effectiveFilter !== "all" || narrowed ? " in this view" : ""}.
                    </>
                  ) : (
                    "Nothing matches this view."
                  )}
                </span>
                <button
                  type="button"
                  className="pr-list-quiet-button"
                  onClick={() => {
                    setQuery("");
                    setFilter("all");
                    listFilters.clear();
                  }}
                >
                  Clear Filters
                </button>
              </div>
            )}
            <div ref={groupsRef}>
              {windowed && windowed.before > 0 && <ListSpacer height={windowed.before} />}
              {groupFacts.map(({ group, isCollapsed, removable, actions, header, ...removal }) => {
                // A windowed list draws only the groups near the viewport, and only their rows there.
                const slice = windowed?.slices.get(group.id);
                if (windowed && !slice) return null;
                const groupShown = shownRows.get(group.id) ?? group.rows;
                // The space for the rows left out right above each drawn one.
                const spaceAbove = slice
                  ? new Map(slice.urls.map((url, place) => [url, slice.spaceAbove[place]!]))
                  : null;
                const drawnRows = spaceAbove
                  ? groupShown.filter((shown) => spaceAbove.has(shown.url))
                  : groupShown;
                return (
                  <Fragment key={group.id}>
                    {slice && slice.above > 0 && <ListSpacer height={slice.above} />}
                    <section
                      className="pr-list-group"
                      aria-label={display.grouping === "none" ? undefined : group.title}
                    >
                      {header && (
                        <PullRequestListGroupMenu actions={actions}>
                          <div
                            className="pr-list-group-header"
                            role="row"
                            aria-level={stacksShown ? 1 : undefined}
                            aria-expanded={stacksShown ? !isCollapsed : undefined}
                          >
                            <button
                              type="button"
                              className="pr-list-group-toggle"
                              aria-expanded={!isCollapsed}
                              aria-label={`${group.title} group (${group.rows.length}${partial ? " loaded" : ""})`}
                              onClick={() => toggleGroup(group.id)}
                            >
                              <ChevronRightIcon className="pr-list-chevron" aria-hidden />
                              {group.state ? (
                                <PullRequestStateIcon
                                  decorative
                                  state={group.state}
                                  className="pr-list-state"
                                />
                              ) : display.grouping === "author" && group.author ? (
                                <ListAvatar
                                  login={group.author}
                                  host={entriesByUrl.get(group.rows[0]!.url)?.association.host}
                                  hue={personHues.get(group.author.toLowerCase())}
                                  size="small"
                                />
                              ) : (
                                <GitPullRequestIcon className="pr-list-group-glyph" aria-hidden />
                              )}
                              <span className="pr-list-group-title">{group.title}</span>
                              <span className="pr-list-group-count">
                                {partial ? `${group.rows.length} loaded` : group.rows.length}
                              </span>
                            </button>
                            {(removable.length > 0 || removal.removalBlocked !== null) && (
                              <GroupRemoveAllButton
                                reason={removal.removalBlocked ?? removal.removalPending}
                                busy={removal.removalBusy}
                                onStop={removal.removalStop}
                                tooltip={removal.removeTitle}
                                onRemove={() => remove(removable)}
                              />
                            )}
                            <PullRequestListGroupActionsButton
                              actions={actions}
                              groupTitle={group.title}
                            />
                          </div>
                        </PullRequestListGroupMenu>
                      )}
                      {!isCollapsed &&
                        drawnRows.map((shown) => {
                          const entry = entriesByUrl.get(shown.url)!;
                          const { row } = entry;
                          const placement = "depth" in shown ? shown : undefined;
                          const stack = placement && stackRowView(placement, folds.isCollapsed);
                          const space = spaceAbove?.get(row.url) ?? 0;
                          return (
                            <Fragment key={row.url}>
                              {space > 0 && <ListSpacer height={space} />}
                              <PullRequestListPageRow
                                entry={entry}
                                actions={
                                  selection.has(row.url) && selectionMenuActions.length > 0
                                    ? withSelectionActions(
                                        selectionMenuActions,
                                        rowActionsFor(entry),
                                      )
                                    : rowActionsFor(entry)
                                }
                                now={now}
                                repository={repositories?.get(row.repository)}
                                personHues={personHues}
                                sandboxAsleep={sandboxAsleep}
                                mergeRequested={mergeRequestedFor(row.url)}
                                changeInFlight={changeInFlightFor(row.url)}
                                managesSessionLinks={managesSessionLinks}
                                selectable={selectable}
                                isSelected={selection.has(row.url)}
                                isActive={row.url === activeRow}
                                domId={rowDomId(row.url)}
                                tree={
                                  stacksShown
                                    ? {
                                        level: (header ? 2 : 1) + (placement?.depth ?? 0),
                                        expanded: stack?.placement.descendants
                                          ? !stack.collapsed
                                          : undefined,
                                      }
                                    : undefined
                                }
                                stack={stack}
                                isShown={row.url === selectedUrl}
                                onActivate={activate}
                                onOpen={open}
                                onToggle={toggle}
                                onToggleStack={toggleStack}
                              />
                            </Fragment>
                          );
                        })}
                      {slice && slice.after > 0 && <ListSpacer height={slice.after} />}
                    </section>
                  </Fragment>
                );
              })}
              {windowed && windowed.after > 0 && <ListSpacer height={windowed.after} />}
            </div>
            {more && (
              <PullRequestListMoreBlock
                more={more}
                filtering={filtering}
                loaded={allRows.length}
                grouped={display.grouping !== "none"}
                scrollRoot={listRef}
              />
            )}
          </div>
        )}

        {selecting && (
          <PullRequestListSelectionBar
            selectedCount={selectedUrls.length}
            selectAllCount={
              !allVisibleSelected && navigable.length > selectedUrls.length
                ? navigable.length
                : null
            }
            loadedOnly={partial}
            onSelectAll={selectAll}
            actions={barActions}
            moreGroups={moreGroups}
            remove={
              managesSessionLinks
                ? {
                    reason: ownSelectedUrls.length === 0 ? selectionKeptReason : removalReason,
                    ...(selectionInRemoval && unlinkPending && removalProgress
                      ? {
                          busy: removalBusyLabel(removalProgress),
                          stop: removalStopper(removalProgress) ?? undefined,
                        }
                      : {}),
                    place: selectionPlace,
                    keys: removeKeys,
                    share:
                      ownSelectedUrls.length < selectedUrls.length
                        ? { acting: ownSelectedUrls.length, of: selectedUrls.length }
                        : undefined,
                    run: () => remove(ownSelectedUrls),
                  }
                : null
            }
            onClear={clearSelection}
          />
        )}
      </main>
      <PullRequestListUnavailableSortReason id={`${idBase}-sort-unavailable`} />
      {mergeDialog}
      {selectionLinks.dialog}
    </TooltipProvider>
  );
}

/** A column's sort control; a glyph column names itself by its tooltip and accessible name. */
function SortButton({
  title,
  glyph: Glyph,
  direction,
  onSort,
}: {
  title: string;
  glyph: LucideIcon | undefined;
  direction: PageSort["direction"] | undefined;
  onSort: () => void;
}) {
  const arrow =
    direction === "ascending" ? (
      <ArrowUpIcon aria-hidden />
    ) : direction === "descending" ? (
      <ArrowDownIcon aria-hidden />
    ) : null;
  if (!Glyph) {
    return (
      <button type="button" className={direction ? "sorted" : ""} onClick={onSort}>
        {title}
        {arrow}
      </button>
    );
  }
  const name = title.toLowerCase();
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          className={`glyph${direction ? " sorted" : ""}`}
          aria-label={title}
          onClick={onSort}
        >
          <Glyph aria-hidden className="column-glyph" />
          {arrow}
        </button>
      </TooltipTrigger>
      <TooltipContent>
        {direction ? `Sorted by ${name}, ${direction}` : `Sort by ${name}`}
      </TooltipContent>
    </Tooltip>
  );
}

function ColumnHeader({
  sort,
  setSort,
  statsReady,
  sortReasonId,
  selectable,
  allSelected,
  someSelected,
  toggleAll,
}: {
  sort: PageSort;
  setSort: (key: PageSortKey) => void;
  statsReady: boolean;
  sortReasonId: string;
  selectable: boolean;
  allSelected: boolean;
  someSelected: boolean;
  toggleAll: () => void;
}) {
  return (
    <div className="pr-list-columns" role="row">
      {selectable && (
        <span className="pr-list-col-select" role="columnheader">
          <Checkbox
            className="pr-list-checkbox"
            checked={someSelected ? "indeterminate" : allSelected}
            aria-label={allSelected ? "Deselect All" : "Select All"}
            onClick={(event) => {
              event.preventDefault();
              toggleAll();
            }}
          />
        </span>
      )}
      {COLUMNS.map((column) => {
        const sorted = column.key !== undefined && sort.key === column.key;
        return (
          <span
            key={column.title}
            className={column.className}
            role="columnheader"
            aria-label={column.glyph ? column.title : undefined}
            aria-sort={sorted ? sort.direction : undefined}
          >
            {column.key && !pageSortAvailable(column.key, statsReady) ? (
              <PullRequestListUnavailableSort reasonId={sortReasonId}>
                {column.title}
              </PullRequestListUnavailableSort>
            ) : column.key ? (
              <SortButton
                title={column.title}
                glyph={column.glyph}
                direction={sorted ? sort.direction : undefined}
                onSort={() => setSort(column.key!)}
              />
            ) : (
              column.title
            )}
          </span>
        );
      })}
    </div>
  );
}

/** `url` if it's shown, else the nearest row it's nested under that is; undefined if none. */
function shownAncestor(
  url: string | undefined,
  shown: readonly string[],
  placements: ReadonlyMap<string, ListStackPlacement>,
): string | undefined {
  for (let at = url; at !== undefined; at = placements.get(at)?.parentUrl ?? undefined) {
    if (shown.includes(at)) return at;
  }
  return undefined;
}

function isNestedUnder(
  url: string,
  ancestor: string,
  placements: ReadonlyMap<string, ListStackPlacement>,
): boolean {
  for (let at = placements.get(url)?.parentUrl; at; at = placements.get(at)?.parentUrl) {
    if (at === ancestor) return true;
  }
  return false;
}

// One view per placement and fold, so an unchanged stacked row keeps its props.
const stackRowViews = new WeakMap<
  ListStackPlacement,
  { folded: ListStackRowView; open: ListStackRowView }
>();

function stackRowView(
  placement: ListStackPlacement,
  isCollapsed: (url: string) => boolean,
): ListStackRowView | undefined {
  if (
    placement.depth === 0 &&
    placement.descendants === 0 &&
    !placement.hint &&
    !placement.tolerated
  ) {
    return undefined;
  }
  let views = stackRowViews.get(placement);
  if (!views) {
    views = {
      folded: { placement, collapsed: true },
      open: { placement, collapsed: false },
    };
    stackRowViews.set(placement, views);
  }
  return isCollapsed(placement.url) ? views.folded : views.open;
}

function hasTextSelection(): boolean {
  const selection = window.getSelection();
  return Boolean(selection && !selection.isCollapsed && selection.toString().trim());
}

function isEditable(target: EventTarget | null): boolean {
  return (
    target instanceof HTMLElement &&
    (target instanceof HTMLInputElement ||
      target instanceof HTMLTextAreaElement ||
      target instanceof HTMLSelectElement ||
      target.isContentEditable)
  );
}

/** The chip's title, in the list's Title Case. */
function chipTitle(id: PullRequestListFilter, title: string): string {
  return id === "attention" ? "Needs You" : title;
}

/**
 * A checked row's menu while several are checked: the selection's actions, then only the row's
 * own list controls, such as its stack's fold. What acts on the one PR would act on it alone.
 */
export function withSelectionActions(
  selection: readonly RowAction[],
  row: readonly RowAction[],
): RowAction[] {
  return [...selection, ...row.filter((action) => action.section === "page")];
}

/**
 * A finished group's Remove All. Where it can't run, it stays focusable and does nothing, and
 * its tooltip, shown on focus as on hover, says why. While it removes and can be stopped,
 * it's the Stop button.
 */
function GroupRemoveAllButton({
  reason,
  busy,
  onStop,
  tooltip,
  onRemove,
}: {
  reason: string | null;
  /** Its label while the group's rows are being removed, such as "Removing 3 of 8". */
  busy: string | null;
  /** Stops the removal under way, where it can be stopped now. */
  onStop: (() => void) | null;
  tooltip: string;
  onRemove: () => void;
}) {
  const reasonId = useId();
  const stoppable = busy !== null && onStop !== null;
  const unavailable = reason !== null && !stoppable;
  return (
    <>
      {/* Before the button, not in it: it describes it without joining its name, and leaves
          the header's actions side by side. */}
      {reason && (
        <span id={reasonId} className="sr-only">
          {reason}
        </span>
      )}
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            className="pr-list-group-action"
            aria-label={stoppable ? STOP_REMOVING_LABEL : undefined}
            aria-disabled={unavailable ? true : undefined}
            aria-describedby={reason ? reasonId : undefined}
            aria-busy={busy ? true : undefined}
            data-busy={busy ? true : undefined}
            data-stoppable={stoppable ? true : undefined}
            onClick={stoppable ? onStop : unavailable ? undefined : onRemove}
          >
            {busy ? <RemovalBusyFace label={busy} stoppable={stoppable} /> : "Remove All"}
          </button>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs whitespace-pre-line">
          {stoppable ? STOP_REMOVING_LABEL : (reason ?? tooltip)}
        </TooltipContent>
      </Tooltip>
    </>
  );
}

/**
 * A group's Remove All: its label, which its tooltip shows as it is, and the detail naming
 * every session it unlinks from, which ⌘K shows under its label.
 */
function groupRemoval(
  groupTitle: string,
  removable: readonly string[],
  removalOf: (url: string) => RowRemoval | undefined,
): { removeTitle: string; removeDetail: string | null } {
  if (removable.length === 0) {
    return {
      removeTitle: `Remove the ${groupTitle.toLowerCase()} pull requests`,
      removeDetail: null,
    };
  }
  const place = removalPlace(removable.flatMap((url) => removalOf(url) ?? []));
  return {
    removeTitle: `Remove ${removable.length} ${groupTitle.toLowerCase()} ${plural(removable.length, "pull request")} from ${place.text}`,
    removeDetail: place.detail,
  };
}

function plural(count: number, noun: string): string {
  return count === 1 ? noun : `${noun}s`;
}

/** The room rows that aren't drawn would take, so the scrollbar spans the whole list. */
function ListSpacer({ height }: { height: number }) {
  return <div aria-hidden className="pr-list-spacer" style={{ height }} />;
}
