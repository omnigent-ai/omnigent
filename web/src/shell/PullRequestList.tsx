// The Pull Requests list a session with several PRs opens on: a filter box, status
// chips with counts, and collapsible groups of rows, a sortable table when the list is
// wide and two-line rows when it's narrow. Row actions live in each row's menu (also
// its context menu) and in ⌘K, all from one action definition.

import {
  removalIncludes,
  removalPendingReason,
  type RemovalProgress,
} from "@/pullRequestList/removalProgress";
import { useCallback, useMemo, useRef, useState, useId } from "react";
import {
  ChevronRightIcon,
  GitPullRequestIcon,
  ListFilterIcon,
  SearchIcon,
  SlidersHorizontalIcon,
  XIcon,
} from "lucide-react";
import { useWarmSharedDiffWorkers } from "@/components/diff-review/sharedDiffWorkers";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
} from "@/components/ui/input-group";
import { CompactKbd } from "@/components/ui/kbd";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useElementWidth } from "@/hooks/useElementWidth";
import { useNow } from "@/hooks/useNow";
import {
  DEFAULT_PULL_REQUEST_LIST_DISPLAY,
  readPullRequestListDisplay,
  writePullRequestListDisplay,
  type PullRequestListDisplay,
} from "@/lib/pullRequestListPreferences";
import { cn } from "@/lib/utils";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";
import { settingCommand } from "@/store/paletteSettings";
import { pullRequestAuthorHues } from "./PullRequestAvatar";
import { type PullRequestListProps, usePullRequestListEntries } from "./usePullRequestListEntries";
import type { PullRequestOpenIntent } from "./pullRequestOpenIntent";
import { pullRequestTableLayout, type PullRequestTableLayout } from "./pullRequestTableLayout";
import { PullRequestListUnavailableSortReason } from "./PullRequestListStatsGap";
import {
  effectiveOrder,
  filterCounts,
  displayAfter,
  isFinishedState,
  nextOrder,
  orderAvailable,
  PULL_REQUEST_LIST_FILTERS,
  PULL_REQUEST_LIST_GROUPINGS,
  PULL_REQUEST_LIST_ORDERS,
  pullRequestListViewSettings,
  repositoryLabels,
  visibleGroups,
  type PullRequestListDirection,
  type PullRequestListFilter,
  type PullRequestListGroup,
  type PullRequestListGrouping,
  type PullRequestListOrder,
  type PullRequestListRow,
  type PullRequestListViewChange,
} from "./pullRequestListModel";
import {
  PullRequestListItem,
  PullRequestStateIcon,
  PullRequestTableRow,
  rowIdentity,
  TableColumnHeader,
} from "./PullRequestListRows";

import { SandboxResumeNotice } from "@/components/SandboxResumeNotice";

const NO_COMMANDS: PaletteCommand[] = [];

export function PullRequestList(listProps: PullRequestListProps) {
  const { conversationId, active, sandboxAsleep, selectedUrl, onSelect, onUnlink, unlinkPending } =
    listProps;
  // A PR opened from this list finds the diff's highlighting workers already started.
  useWarmSharedDiffWorkers();
  const {
    entries,
    rows,
    entriesByUrl,
    stats,
    statsColumns,
    canUnlink,
    actionsFor,
    mergeRequestedFor,
    unlinkFinishedCommand,
    rowCommands,
    mergeDialog,
  } = usePullRequestListEntries(listProps);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<PullRequestListFilter>("all");
  const [display, setDisplayState] = useState(readPullRequestListDisplay);
  const setDisplay = useCallback((next: PullRequestListDisplay) => {
    setDisplayState(next);
    writePullRequestListDisplay(next);
  }, []);
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set());
  const filterRef = useRef<HTMLInputElement>(null);
  const counts = useMemo(() => filterCounts(rows), [rows]);
  // A filter whose last row went away shows everything instead of nothing.
  const effectiveFilter = filter !== "all" && counts[filter] === 0 ? "all" : filter;
  // Update-time and size orders wait for their stats; until then rows keep status order.
  const sort = effectiveOrder(display, stats.status === "ready");
  const groups = useMemo(
    () =>
      visibleGroups(rows, {
        query,
        filter: effectiveFilter,
        grouping: display.grouping,
        order: sort.order,
        direction: sort.direction,
      }),
    [rows, query, effectiveFilter, display.grouping, sort.order, sort.direction],
  );
  const repositories = useMemo(() => repositoryLabels(rows), [rows]);
  // Authors and reviewers share one palette, so a person keeps their color in every column.
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
  const scrollRef = useRef<HTMLDivElement>(null);
  const numberLength = useMemo(
    () => Math.max(0, ...entries.map((entry) => rowIdentity(entry).reference.length)),
    [entries],
  );
  const layout = pullRequestTableLayout(useElementWidth(scrollRef), {
    statsColumns,
    numberLength,
  });
  const now = useNow().getTime();
  const sortBy = useCallback(
    // Toggle from what the headers show, which may be the status fallback for a stored order.
    (order: PullRequestListOrder) => setDisplay({ ...display, ...nextOrder(sort, order) }),
    [display, setDisplay, sort],
  );
  const displayModified =
    display.grouping !== DEFAULT_PULL_REQUEST_LIST_DISPLAY.grouping ||
    display.order !== DEFAULT_PULL_REQUEST_LIST_DISPLAY.order ||
    display.direction !== DEFAULT_PULL_REQUEST_LIST_DISPLAY.direction;
  // The status pills, the Display menu and ⌘K all change the view through this.
  const applyViewChange = useCallback(
    (change: PullRequestListViewChange) => {
      if (change.kind === "filter") setFilter(change.filter);
      else setDisplay(displayAfter(display, change, DEFAULT_PULL_REQUEST_LIST_DISPLAY));
    },
    [display, setDisplay],
  );
  const viewCommands = useMemo(
    () =>
      pullRequestListViewSettings({
        filter: effectiveFilter,
        counts,
        display,
        order: sort.order,
        statsAvailable: stats.status === "ready",
        modified: displayModified,
      }),
    [effectiveFilter, counts, display, sort.order, stats.status, displayModified],
  );
  const focusFilter = useCallback(() => {
    filterRef.current?.focus();
    filterRef.current?.select();
  }, []);
  const paletteCommands = useMemo<PaletteCommand[]>(() => {
    const commands: PaletteCommand[] = [
      {
        id: "filter",
        label: "Filter pull requests",
        icon: SearchIcon,
        keywords: ["search", "find", "pull requests", "pr"],
        movesFocus: true,
        run: focusFilter,
      },
    ];
    for (const setting of viewCommands.settings) {
      commands.push(
        settingCommand({
          id: `pull-requests.${setting.id}`,
          owner: conversationId,
          label: setting.label,
          icon: setting.id === "filter" ? ListFilterIcon : SlidersHorizontalIcon,
          keywords: setting.keywords,
          current: setting.current,
          choices: setting.choices.map(({ value, label }) => ({ value, label })),
          choose: (value) => {
            const choice = setting.choices.find((candidate) => candidate.value === value);
            if (choice) applyViewChange(choice.change);
          },
        }),
      );
    }
    const reset = viewCommands.reset;
    if (reset) {
      commands.push({
        id: `view.${reset.id}`,
        label: reset.label,
        icon: SlidersHorizontalIcon,
        keywords: [...reset.keywords],
        run: () => applyViewChange(reset.change),
      });
    }
    if (unlinkFinishedCommand) commands.push(unlinkFinishedCommand);
    commands.push(...rowCommands);
    return commands;
  }, [
    applyViewChange,
    conversationId,
    focusFilter,
    rowCommands,
    unlinkFinishedCommand,
    viewCommands,
  ]);
  // A collapsed rail keeps the list mounted; its commands would act on a hidden list.
  // Each mounted list owns its rows, so a hidden one can't clear the shown one's.
  const paletteInstance = useId();
  const sortReasonId = `${paletteInstance}-sort-unavailable`;
  usePaletteCommands(
    `pull-requests:${conversationId}:${paletteInstance}`,
    "Pull requests",
    active ? paletteCommands : NO_COMMANDS,
  );

  const toggleGroup = (id: string) =>
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const anyRows = groups.some((group) => group.rows.length > 0);

  return (
    <TooltipProvider delayDuration={0}>
      <div
        className="flex h-full min-h-0 flex-col"
        onKeyDown={(event) => {
          // `/` jumps to the filter from anywhere in the list, as on GitHub.
          if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
          if (event.target instanceof HTMLInputElement) return;
          event.preventDefault();
          filterRef.current?.focus();
        }}
      >
        <div className="flex shrink-0 flex-col gap-2 border-b border-border px-2 py-2">
          <div className="flex items-center gap-1.5">
            <InputGroup className="h-7 min-w-0 flex-1">
              <InputGroupAddon>
                <SearchIcon aria-hidden className="size-3.5" />
              </InputGroupAddon>
              <InputGroupInput
                ref={filterRef}
                type="search"
                value={query}
                spellCheck={false}
                aria-label="Filter pull requests"
                placeholder="Filter by title, number, author, or branch"
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key !== "Escape") return;
                  // First Escape clears the filter, the next leaves the field.
                  if (query) setQuery("");
                  else event.currentTarget.blur();
                  event.preventDefault();
                  event.stopPropagation();
                }}
                className="text-ui [&::-webkit-search-cancel-button]:hidden"
              />
              <InputGroupAddon align="inline-end">
                {query ? (
                  <InputGroupButton
                    size="icon-xs"
                    aria-label="Clear filter"
                    onClick={() => {
                      setQuery("");
                      filterRef.current?.focus();
                    }}
                  >
                    <XIcon aria-hidden />
                  </InputGroupButton>
                ) : (
                  <CompactKbd>/</CompactKbd>
                )}
              </InputGroupAddon>
            </InputGroup>
            <DisplayMenu
              display={display}
              order={sort.order}
              statsAvailable={stats.status === "ready"}
              modified={displayModified}
              onChange={applyViewChange}
            />
          </div>
          <div role="group" aria-label="Status" className="flex flex-wrap items-center gap-1">
            {PULL_REQUEST_LIST_FILTERS.filter(
              (option) => option.id === "all" || counts[option.id] > 0,
            ).map((option) => {
              const selected = effectiveFilter === option.id;
              return (
                <Button
                  key={option.id}
                  type="button"
                  size="xs"
                  variant={selected ? "secondary" : "ghost"}
                  aria-pressed={selected}
                  aria-label={`${option.title} (${counts[option.id]})`}
                  onClick={() => applyViewChange({ kind: "filter", filter: option.id })}
                  className={cn("gap-1 px-2", !selected && "text-muted-foreground")}
                >
                  {option.id === "attention" ? (
                    <span aria-hidden className="size-1.5 rounded-full bg-status-yellow" />
                  ) : (
                    option.id !== "all" && (
                      <PullRequestStateIcon state={option.id} className="size-3" />
                    )
                  )}
                  {option.title}
                  <span className="tabular-nums text-muted-foreground">{counts[option.id]}</span>
                </Button>
              );
            })}
          </div>
        </div>

        {sandboxAsleep && (
          <SandboxResumeNotice
            sessionId={conversationId}
            layout="banner"
            purpose="load the PR details"
          />
        )}
        <div
          ref={scrollRef}
          data-pull-request-layout={layout.kind}
          className={cn(
            "min-h-0 flex-1 overflow-y-auto",
            layout.kind === "table" ? "pb-1" : "py-1",
          )}
        >
          {!anyRows ? (
            <div className="flex flex-col items-center gap-2 p-6 text-center">
              <SearchIcon aria-hidden className="size-6 text-muted-foreground/50" />
              <p className="text-ui font-medium">No matching pull requests</p>
              <p className="text-ui text-muted-foreground">
                {query ? <>Nothing matches “{query}”.</> : "Nothing matches this view."}
              </p>
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  setQuery("");
                  setFilter("all");
                }}
              >
                Clear filters
              </Button>
            </div>
          ) : (
            <PullRequestGroups
              groups={groups}
              grouping={display.grouping}
              layout={layout}
              sort={sort}
              onSort={sortBy}
              statsAvailable={stats.status === "ready"}
              sortReasonId={sortReasonId}
              collapsed={collapsed}
              onToggleGroup={toggleGroup}
              unlinkAll={canUnlink ? onUnlink : undefined}
              unlinkPending={unlinkPending}
              removalProgress={listProps.removalProgress ?? null}
              renderRow={(row) => {
                const entry = entriesByUrl.get(row.url)!;
                const props = {
                  entry,
                  repository: repositories?.get(row.repository),
                  personHues,
                  actions: actionsFor(entry),
                  sandboxAsleep,
                  selected: row.url === selectedUrl,
                  mergeRequested: mergeRequestedFor(row.url),
                  onSelect: (intent: PullRequestOpenIntent) => onSelect(row.url, intent),
                };
                return layout.kind === "table" ? (
                  <PullRequestTableRow key={row.url} {...props} layout={layout} now={now} />
                ) : (
                  <PullRequestListItem
                    key={row.url}
                    {...props}
                    statsColumns={statsColumns}
                    now={now}
                  />
                );
              }}
            />
          )}
        </div>
      </div>
      <PullRequestListUnavailableSortReason id={sortReasonId} />
      {mergeDialog}
    </TooltipProvider>
  );
}

function DisplayMenu({
  display,
  order,
  statsAvailable,
  modified,
  onChange,
}: {
  display: PullRequestListDisplay;
  /** The order the list applies, which may be a fallback for the chosen one. */
  order: PullRequestListOrder;
  /** Update-time and size orders need the desktop shell's stats. */
  statsAvailable: boolean;
  modified: boolean;
  onChange: (change: PullRequestListViewChange) => void;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          size="xs"
          variant="ghost"
          aria-label="Display options"
          className={cn(
            "h-7 shrink-0 gap-1 px-2",
            modified ? "text-foreground" : "text-muted-foreground",
          )}
        >
          <SlidersHorizontalIcon aria-hidden className="size-3.5" />
          Display
          {modified && <span aria-hidden className="size-1.5 rounded-full bg-primary" />}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-44">
        <DropdownMenuLabel>Group by</DropdownMenuLabel>
        <DropdownMenuRadioGroup
          aria-label="Group by"
          value={display.grouping}
          onValueChange={(grouping) => {
            const option = PULL_REQUEST_LIST_GROUPINGS.find(
              (candidate) => candidate.id === grouping,
            );
            if (option) onChange({ kind: "grouping", grouping: option.id });
          }}
        >
          {PULL_REQUEST_LIST_GROUPINGS.map((option) => (
            <DropdownMenuRadioItem key={option.id} value={option.id}>
              {option.title}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
        <DropdownMenuSeparator />
        <DropdownMenuLabel>Order by</DropdownMenuLabel>
        <DropdownMenuRadioGroup
          aria-label="Order by"
          value={order}
          onValueChange={(value) => {
            const option = PULL_REQUEST_LIST_ORDERS.find((candidate) => candidate.id === value);
            if (option) onChange({ kind: "order", order: option.id });
          }}
        >
          {PULL_REQUEST_LIST_ORDERS.filter((option) =>
            orderAvailable(option.id, statsAvailable),
          ).map((option) => (
            <DropdownMenuRadioItem key={option.id} value={option.id}>
              {option.title}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
        {modified && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => onChange({ kind: "reset" })}>
              Reset to default
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** The groups and rows, as a sortable table or as two-line rows on a narrow list. */
function PullRequestGroups({
  groups,
  grouping,
  layout,
  sort,
  onSort,
  statsAvailable,
  sortReasonId,
  collapsed,
  onToggleGroup,
  unlinkAll,
  unlinkPending,
  removalProgress,
  renderRow,
}: {
  groups: PullRequestListGroup[];
  grouping: PullRequestListGrouping;
  layout: PullRequestTableLayout;
  sort: { order: PullRequestListOrder; direction: PullRequestListDirection };
  onSort: (order: PullRequestListOrder) => void;
  /** Size and update-time orders can apply. */
  statsAvailable: boolean;
  sortReasonId: string;
  collapsed: ReadonlySet<string>;
  onToggleGroup: (id: string) => void;
  /** Absent when the account can't unlink. */
  unlinkAll: ((urls: string[]) => void) | undefined;
  unlinkPending: boolean;
  removalProgress: RemovalProgress | null;
  renderRow: (row: PullRequestListRow) => React.ReactNode;
}) {
  const table = layout.kind === "table" ? layout : undefined;
  const body = groups.map((group) => {
    const open = !collapsed.has(group.id);
    const finishedUrls =
      group.state && isFinishedState(group.state) ? group.rows.map((row) => row.url) : [];
    const canUnlinkGroup = !!unlinkAll && finishedUrls.length > 0;
    const showHeader = grouping !== "none" && (groups.length > 1 || !open || canUnlinkGroup);
    const rows = group.rows.map(renderRow);
    if (!showHeader) {
      return table ? (
        <div key={group.id} role="rowgroup">
          {rows}
        </div>
      ) : (
        rows
      );
    }
    const header = (
      <div className="flex h-7 items-center gap-1 px-2">
        <CollapsibleTrigger
          aria-label={`${group.title} group (${group.rows.length})`}
          className="flex min-w-0 flex-1 cursor-pointer items-center gap-1.5 rounded-sm text-xs font-medium text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring"
        >
          <ChevronRightIcon
            aria-hidden
            className={cn("size-3.5 shrink-0 transition-transform", open && "rotate-90")}
          />
          {group.state ? (
            <PullRequestStateIcon decorative state={group.state} className="size-3" />
          ) : (
            <GitPullRequestIcon aria-hidden className="size-3 shrink-0" />
          )}
          <span className="truncate">{group.title}</span>
          <span className="tabular-nums">{group.rows.length}</span>
        </CollapsibleTrigger>
        {canUnlinkGroup && (
          <Button
            size="xs"
            variant="ghost"
            disabled={unlinkPending}
            title={
              unlinkPending
                ? removalPendingReason(
                    removalProgress,
                    removalIncludes(removalProgress, finishedUrls),
                  )
                : `Unlink ${finishedUrls.length} ${group.title.toLowerCase()} ${
                    finishedUrls.length === 1 ? "pull request" : "pull requests"
                  } from this session`
            }
            className="h-6 px-1.5 text-muted-foreground"
            onClick={() => unlinkAll?.(finishedUrls)}
          >
            Unlink all
          </Button>
        )}
      </div>
    );
    return (
      <Collapsible key={group.id} open={open} onOpenChange={() => onToggleGroup(group.id)} asChild>
        {table ? (
          <div role="rowgroup" aria-label={group.title}>
            <div role="row">
              <div role="cell" aria-colspan={table.columns.length}>
                {header}
              </div>
            </div>
            <CollapsibleContent asChild>
              <div role="none">{rows}</div>
            </CollapsibleContent>
          </div>
        ) : (
          <li>
            {header}
            <CollapsibleContent asChild>
              <ul aria-label={group.title}>{rows}</ul>
            </CollapsibleContent>
          </li>
        )}
      </Collapsible>
    );
  });
  if (!table) {
    return (
      <ul aria-label="Pull requests" className="px-1">
        {body}
      </ul>
    );
  }
  return (
    <div
      role="table"
      aria-label="Pull requests"
      aria-colcount={table.columns.length}
      className="px-1"
    >
      <div role="rowgroup" className="sticky top-0 z-20 -mx-1 bg-background px-1">
        <div
          role="row"
          className="grid h-7 items-center gap-x-1.5 border-b border-border px-2 text-xs text-muted-foreground"
          style={{ gridTemplateColumns: table.gridTemplate }}
        >
          {table.columns.map((id) => (
            <TableColumnHeader
              key={id}
              id={id}
              statusText={table.statusText}
              sort={sort}
              onSort={onSort}
              statsAvailable={statsAvailable}
              sortReasonId={sortReasonId}
            />
          ))}
        </div>
      </div>
      <div className="pt-1">{body}</div>
    </div>
  );
}
