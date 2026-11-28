// The Pull Requests list's rows: two-line rows for a narrow list and table rows for a
// wide one, sharing their identity, status, and action menus.

import { type MenuCloseHandOff, useMenuCloseHandOff } from "@/hooks/useMenuCloseHandOff";

import {
  ArrowDownIcon,
  ArrowUpIcon,
  CircleDashedIcon,
  CopyIcon,
  EllipsisIcon,
  ExternalLinkIcon,
  GitMergeIcon,
  GitPullRequestClosedIcon,
  GitPullRequestDraftIcon,
  GitPullRequestIcon,
  type LucideIcon,
  ServerOffIcon,
  UsersIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuSub,
  ContextMenuSubContent,
  ContextMenuSubTrigger,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  MENU_FILTER_TARGET_PROPS,
  type MenuFilter,
  MenuFilterEmpty,
  MenuFilterField,
  menuFilterMatches,
  useMenuFilter,
} from "@/components/ui/menu-filter";
import type {
  PullRequest,
  PullRequestAssociation,
  PullRequestCapabilities,
  PullRequestChecks,
  PullRequestReviewer,
} from "@/newPullRequestPage/hooks/usePullRequests";
import {
  MERGE_READINESS_TONE_DOT,
  MERGE_READINESS_TONE_TEXT,
  presentMergeReadiness,
  type MergeReadinessPresentation,
} from "@/lib/mergeReadiness";
import { gitProviderCopy } from "@/lib/gitProviders";
import {
  openGestureProps,
  type PullRequestOpenIntent,
  pullRequestOpenIntent,
} from "./pullRequestOpenIntent";
import {
  compactCount,
  type PullRequestListStats,
  type PullRequestListStatsGap,
  type PullRequestListStatsService,
} from "@/lib/pullRequestListStats";
import { absoluteTime, relativeTime } from "@/lib/relativeTime";
import type { ShortcutId } from "@/lib/shortcuts/registry";
import { cn } from "@/lib/utils";
import {
  type CommandAvailability,
  isCommandAvailable,
  type PaletteCommand,
} from "@/store/paletteCommands";
import { PullRequestAuthorAvatar } from "./PullRequestAvatar";
import type {
  PullRequestCommandId,
  PullRequestCommandSection,
  PullRequestTarget,
} from "./pullRequestCommands/model";
import { CheckRunsPopover } from "./PullRequestChecks";
import {
  PullRequestListStatsGapMark,
  PullRequestListUnavailableSort,
  pullRequestListEntryStatsCell,
} from "./PullRequestListStatsGap";
import { REVIEWER_STATE_META } from "../newPullRequestPage/shell/PullRequestReviewers";
import {
  PULL_REQUEST_TABLE_COLUMNS,
  type PullRequestTableColumn,
  type PullRequestTableLayout,
} from "./pullRequestTableLayout";
import {
  checkResult,
  isFinishedState,
  orderAvailable,
  pullRequestStateTitle,
  REVIEW_STATUS_TITLES,
  storedPullRequestHint,
  type PullRequestListDirection,
  type PullRequestListOrder,
  type PullRequestListRow,
  type PullRequestListState,
  type PullRequestReviewStatus,
} from "./pullRequestListModel";

const STATE_ICONS: Record<PullRequestListState, { Icon: LucideIcon; className: string }> = {
  open: { Icon: GitPullRequestIcon, className: "text-status-green" },
  draft: { Icon: GitPullRequestDraftIcon, className: "text-muted-foreground" },
  merged: { Icon: GitMergeIcon, className: "text-status-purple" },
  closed: { Icon: GitPullRequestClosedIcon, className: "text-status-red" },
  unknown: { Icon: CircleDashedIcon, className: "text-muted-foreground" },
};

/** A state glyph; `decorative` where adjacent text already names the state. */
export function PullRequestStateIcon({
  state,
  decorative = false,
  className,
}: {
  state: PullRequestListState;
  decorative?: boolean;
  className?: string;
}) {
  const { Icon, className: tint } = STATE_ICONS[state];
  return (
    <Icon
      {...(decorative
        ? { "aria-hidden": true }
        : { role: "img", "aria-label": pullRequestStateTitle(state) })}
      className={cn("size-3.5 shrink-0", tint, className)}
    />
  );
}

const REVIEW_TINTS: Record<PullRequestReviewStatus, string> = {
  changes_requested: "text-status-red",
  approved: "text-status-green",
  review_requested: "text-muted-foreground",
};

/** One row's live state beyond what the list model needs: the PR and what the account may do. */
export interface PullRequestListEntry {
  association: PullRequestAssociation;
  row: PullRequestListRow;
  /** The live PR, while the host can answer. */
  pr: PullRequest | undefined;
  /** Neither the host nor the runner can answer, so only stored state shows. */
  offline: boolean;
  /** Who reads the row's diff size and update time; absent when nobody can. */
  statsService?: PullRequestListStatsService;
  /** Why the row has no diff size or update time; absent while one may still arrive. */
  statsGap?: PullRequestListStatsGap;
  status: "loading" | "error" | "ready";
  error: unknown;
  /** Why linking the row's PR to the session failed, such as the server refusing a non-owner. */
  linkFailure?: string;
  mergeHead: string | undefined;
  mergeHint: string | undefined;
  /** An open merge dialog can't confirm: offline, a failed refresh, or no merge rights. */
  mergeUnavailable: boolean;
  /** What the PR's read says can be edited, where this list edits it; absent where it can't. */
  editCapabilities?: PullRequestCapabilities | null;
  /** When the row's own read of its PR answered, which its reviewers come from; absent before. */
  detailsReadAt?: number;
}

/** A row action, shared by the row's menus and its ⌘K command. */
export interface RowAction {
  /**
   * `session:…` and `page:…` for actions a caller adds, such as the Pull Requests page's
   * session ones and its Hide Repository; `selection:…` for a checked row's selection actions.
   */
  id:
    | "merge"
    | "approve"
    | "review"
    | "review-on-provider"
    | "request-review"
    | "edit-labels"
    | "close"
    | "reopen"
    | PullRequestCommandId
    | `lifecycle:${string}`
    | `session:${string}`
    | `page:${string}`
    | `selection:${string}`;
  /**
   * Menus put a separator between sections. `page` actions act beyond the one PR, so they
   * follow its own actions, before removing it; `selection` ones act on every checked row.
   */
  section:
    PullRequestCommandSection | "merge" | "review" | "lifecycle" | "session" | "page" | "selection";
  label: string;
  /** Its label inside its section's Open › or Copy › submenu, where that verb is already said. */
  nestedLabel?: string;
  paletteLabel: string;
  /** Only ⌘K offers it; the row's menus leave it out. */
  paletteOnly?: true;
  /** A quieter line under the label, in the row menus and ⌘K alike. */
  description?: string;
  icon: LucideIcon;
  keywords?: readonly string[];
  /** Opens in a new tab from the row menus, which render the action as a link. */
  href?: string;
  /** Shared by the row menus and ⌘K, e.g. greyed while a removal is in flight. */
  availability?: CommandAvailability;
  /** Runs once its menu or ⌘K has closed, so it can take focus without them restoring it. */
  movesFocus?: boolean;
  /** With `movesFocus`, its menu first puts focus back, and focus stays there unless it moves it. */
  afterMenuFocusReturns?: boolean;
  /** The registry binding that also runs it, which the row menus show. */
  shortcut?: ShortcutId;
  run: () => void;
}

/** The pull request a row's commands are for, the same for its menus and a selection's. */
export function rowCommandTarget({ association, row }: PullRequestListEntry): PullRequestTarget {
  return {
    url: association.url,
    number: row.number,
    title: row.title || null,
    headRef: row.headRef,
    provider: association.provider,
    providerDisplay: association.provider_display,
  };
}

/** A row action's ⌘K command, shared by every action a row's menus offer. */
export function rowActionCommand(entry: PullRequestListEntry, action: RowAction): PaletteCommand {
  return {
    id: `${action.id}:${entry.association.url}`,
    label: action.paletteLabel,
    description: action.description,
    icon: action.icon,
    keywords: [...(action.keywords ?? ["pull request", "pr"]), entry.row.repository],
    availability: action.availability,
    movesFocus: action.movesFocus,
    shortcut: action.shortcut,
    run: action.run,
  };
}

const ROLLUP_LABELS = {
  passing: "Checks passed",
  failing: "Checks failing",
  pending: "Checks running",
} as const;

/**
 * The row's CI: the live checks with their counts and jobs, or the stats batch's
 * rollup as a glyph alone. Live checks need the session's host to be reachable.
 */
function RowCiBadge({ row, offline }: { row: PullRequestListRow; offline: boolean }) {
  const { ci } = row;
  if (!ci || isFinishedState(row.state)) return null;
  if (ci.source === "live") {
    return offline ? null : (
      <CheckResultBadge checks={ci.checks} incomplete={row.checksIncomplete} />
    );
  }
  const label = ROLLUP_LABELS[ci.rollup];
  return (
    <span
      role="img"
      aria-label={label}
      title={label}
      className={cn(
        "inline-flex items-center",
        ci.rollup === "failing" && "text-status-red",
        ci.rollup === "pending" && "text-status-yellow",
        ci.rollup === "passing" && "text-status-green",
      )}
    >
      <CheckGlyph tone={ci.rollup} />
    </span>
  );
}

/** Line 2's CI result: a glyph with the failing or running count; clicking lists the jobs. */
function CheckResultBadge({
  checks,
  incomplete,
}: {
  checks: PullRequestChecks;
  incomplete: boolean;
}) {
  const result = checkResult(checks, incomplete);
  if (result.tone === "none") {
    return (
      <span role="img" aria-label={result.description} title={result.description}>
        —
      </span>
    );
  }
  return (
    <CheckRunsPopover
      runs={checks.runs}
      label={result.description}
      trigger={
        <button
          type="button"
          aria-label={result.description}
          className={cn(
            "relative z-10 inline-flex cursor-pointer items-center gap-0.5 rounded-sm tabular-nums focus-visible:outline-2 focus-visible:outline-ring",
            result.tone === "failing" && "text-status-red",
            result.tone === "pending" && "text-status-yellow",
            result.tone === "passing" && "text-status-green",
            (result.tone === "cancelled" || result.tone === "skipped") && "text-muted-foreground",
          )}
        >
          <CheckGlyph tone={result.tone} />
          {result.count !== undefined && <span>{result.count}</span>}
        </button>
      }
    />
  );
}

const GLYPH_MARKS = {
  passing: "m4.4 7.1 1.7 1.7 3.5-3.6",
  failing: "m5 5 4 4m0-4-4 4",
  cancelled: "m4.6 9.4 4.8-4.8",
  skipped: "M4.5 7h5",
} as const;

/** CI status glyphs: a filled disc with a tick, cross, slash, or dash, or a ring while checks run. */
export function CheckGlyph({ tone }: { tone: keyof typeof GLYPH_MARKS | "pending" }) {
  return (
    <svg viewBox="0 0 14 14" aria-hidden className="size-3 shrink-0">
      {tone === "pending" ? (
        <circle cx="7" cy="7" r="5.25" fill="none" stroke="currentColor" strokeWidth="1.5" />
      ) : (
        <>
          <circle cx="7" cy="7" r="6" fill="currentColor" />
          <path
            d={GLYPH_MARKS[tone]}
            fill="none"
            stroke="var(--background)"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </>
      )}
    </svg>
  );
}

/** What each row needs, in either layout. */
export interface PullRequestRowProps {
  entry: PullRequestListEntry;
  /** Short repository name, set only when the list spans several repositories. */
  repository: string | undefined;
  /** Avatar hues by lowercase login. */
  personHues: ReadonlyMap<string, number>;
  actions: RowAction[];
  sandboxAsleep: boolean;
  /** The PR the panel last showed. */
  selected: boolean;
  mergeRequested: boolean;
  /** Open the PR: here, or, from ⌘-click, a middle click or ⌘Enter, behind the current tab. */
  onSelect: (intent: PullRequestOpenIntent) => void;
}

/**
 * The actions a typed query keeps, by label and keywords. Not by palette label: in a PR's own
 * menu it only repeats the PR's name, which would match every action alike.
 */
export function filterRowActions(actions: readonly RowAction[], query: string): RowAction[] {
  return actions.filter(
    (action) =>
      !action.paletteOnly &&
      menuFilterMatches([action.label, ...(action.keywords ?? [])].join(" "), query),
  );
}

/**
 * What a PR menu shows for its filter's query, and which item Enter in the field runs, marked
 * while the field has focus: an action's `target`, or else, with `marksAfter`, the first enabled
 * item of the section after the actions, such as a tab's own.
 */
/** The menu's order: a PR's copies sit right under its open actions, in the same group. */
function copiesUnderOpen(actions: readonly RowAction[]): RowAction[] {
  const rest = actions.filter((action) => action.section !== "copy");
  const at = rest.findLastIndex((action) => action.section === "open") + 1;
  if (at === 0) return [...actions];
  const copies = actions.filter((action) => action.section === "copy");
  return [...rest.slice(0, at), ...copies, ...rest.slice(at)];
}

/** The group a section's separators go around: Copy shares Open's. */
const separatorGroup = (section: RowAction["section"]) => (section === "copy" ? "open" : section);

export function rowActionMenuView(actions: readonly RowAction[], filter: MenuFilter) {
  const shown = filterRowActions(copiesUnderOpen(actions), filter.query);
  const marking = filter.fieldFocused && filter.query.trim() !== "";
  const target = marking
    ? shown.find((action) => isCommandAvailable(action.availability))?.id
    : undefined;
  return { shown, target, marksAfter: marking && target === undefined };
}

/** A PR menu's content: the filter field, then the actions it keeps, then any items after them. */
export function RowActionMenuBody({
  actions,
  parts,
  menu,
  filter,
  after,
}: {
  actions: RowAction[];
  parts: RowMenuParts;
  menu: MenuCloseHandOff;
  filter: MenuFilter;
  /** Further items after the actions, already filtered and marked per `rowActionMenuView`. */
  after?: { items: React.ReactNode; count: number };
}) {
  const { shown, target } = rowActionMenuView(actions, filter);
  const afterCount = after?.count ?? 0;
  const { Separator } = parts;
  return (
    <>
      <MenuFilterField filter={filter} />
      {/* A query lists every match inline, so filtering finds what the submenus hold. */}
      {rowMenuItems(shown, parts, menu, { target, nested: filter.query.trim() === "" })}
      {shown.length > 0 && afterCount > 0 && <Separator />}
      {afterCount > 0 && after?.items}
      {shown.length === 0 && afterCount === 0 && <MenuFilterEmpty query={filter.query} />}
    </>
  );
}

/** The pieces of one kind of menu, dropdown or context, that a row's actions render with. */
export interface RowMenuParts {
  Item: typeof DropdownMenuItem | typeof ContextMenuItem;
  Separator: typeof DropdownMenuSeparator | typeof ContextMenuSeparator;
  Sub: typeof DropdownMenuSub | typeof ContextMenuSub;
  SubTrigger: typeof DropdownMenuSubTrigger | typeof ContextMenuSubTrigger;
  SubContent: typeof DropdownMenuSubContent | typeof ContextMenuSubContent;
}

export const DROPDOWN_ROW_MENU: RowMenuParts = {
  Item: DropdownMenuItem,
  Separator: DropdownMenuSeparator,
  Sub: DropdownMenuSub,
  SubTrigger: DropdownMenuSubTrigger,
  SubContent: DropdownMenuSubContent,
};

export const CONTEXT_ROW_MENU: RowMenuParts = {
  Item: ContextMenuItem,
  Separator: ContextMenuSeparator,
  Sub: ContextMenuSub,
  SubTrigger: ContextMenuSubTrigger,
  SubContent: ContextMenuSubContent,
};

/** The sections a row menu gathers into one submenu, and how it names each. */
const SUBMENUS: Partial<Record<RowAction["section"], { label: string; icon: LucideIcon }>> = {
  open: { label: "Open", icon: ExternalLinkIcon },
  copy: { label: "Copy", icon: CopyIcon },
};

/** The actions as a menu lays them out: each alone, or a section's run of them in a submenu. */
function rowMenuEntries(actions: readonly RowAction[], nested: boolean) {
  const entries: (
    { action: RowAction } | { section: RowAction["section"]; actions: RowAction[] }
  )[] = [];
  for (const action of actions) {
    const last = entries.at(-1);
    const nests = nested && SUBMENUS[action.section] !== undefined && action.nestedLabel;
    if (nests && last && "actions" in last && last.section === action.section) {
      last.actions.push(action);
    } else if (nests) entries.push({ section: action.section, actions: [action] });
    else entries.push({ action });
  }
  // A submenu of one would only hide it: it shows as itself.
  return entries.flatMap((entry) =>
    "actions" in entry && entry.actions.length === 1 ? [{ action: entry.actions[0]! }] : [entry],
  );
}

/**
 * A row action menu's items, shared by the row's ellipsis menu and its context menu. With
 * `nested`, each section's Open and Copy actions gather into an Open › or Copy › submenu.
 */
export function rowMenuItems(
  actions: RowAction[],
  parts: RowMenuParts,
  menu: MenuCloseHandOff,
  {
    target,
    nested = false,
  }: {
    /** The action Enter in the menu's filter field runs, marked as if highlighted. */
    target?: string;
    nested?: boolean;
  } = {},
) {
  const { Separator, Sub, SubTrigger, SubContent } = parts;
  let previous: RowAction["section"] | null = null;
  return rowMenuEntries(actions, nested).flatMap((entry) => {
    const group = separatorGroup("action" in entry ? entry.action.section : entry.section);
    const separate = previous !== null && previous !== group;
    previous = group;
    let item: React.ReactNode;
    if ("action" in entry) {
      item = rowActionItem(entry.action, entry.action.label, parts, menu, target);
    } else {
      const submenu = SUBMENUS[entry.section]!;
      const SubmenuIcon = submenu.icon;
      item = (
        <Sub key={`submenu:${entry.section}`}>
          <SubTrigger>
            <SubmenuIcon aria-hidden /> {submenu.label}
          </SubTrigger>
          <SubContent>
            {entry.actions.map((action) =>
              rowActionItem(action, action.nestedLabel ?? action.label, parts, menu, target),
            )}
          </SubContent>
        </Sub>
      );
    }
    const key = "action" in entry ? entry.action.id : entry.section;
    return separate ? [<Separator key={`separator:${key}`} />, item] : [item];
  });
}

/** One action's menu item, a link where it opens a page. */
function rowActionItem(
  action: RowAction,
  label: string,
  { Item }: RowMenuParts,
  menu: MenuCloseHandOff,
  target: string | undefined,
) {
  const ActionIcon = action.icon;
  const content = (
    <>
      <ActionIcon aria-hidden /> {label}
    </>
  );
  const marked = action.id === target ? MENU_FILTER_TARGET_PROPS : {};
  return action.href ? (
    <Item key={action.id} asChild {...marked}>
      <a href={action.href} target="_blank" rel="noreferrer">
        {content}
      </a>
    </Item>
  ) : (
    <Item
      key={action.id}
      {...marked}
      disabled={!isCommandAvailable(action.availability)}
      shortcut={action.shortcut}
      description={unavailableReasonOf(action.availability) ?? action.description}
      onSelect={() =>
        action.movesFocus
          ? menu.runAfterClose(action.run, { afterFocusReturns: action.afterMenuFocusReturns })
          : action.run()
      }
    >
      {content}
    </Item>
  );
}

/** Why a greyed-out action can't run, which its menu item says as ⌘K and the bar do. */
function unavailableReasonOf(availability: RowAction["availability"]): string | undefined {
  return availability?.available === false ? availability.reason : undefined;
}

/** The row's accessible name and the identity parts both layouts show. */
export function rowIdentity({ association, row }: PullRequestListEntry) {
  const copy = gitProviderCopy(association.provider, undefined, association.provider_display);
  const reference = `${copy.prNumberPrefix}${row.number}`;
  const host = association.host === copy.defaultHost ? "" : `${association.host}/`;
  const identity = `${host}${row.repository} ${reference}${row.inferred ? " (from branch)" : ""}`;
  return {
    reference,
    actionsLabel: `Actions for ${host}${row.repository} ${reference}`,
    label: row.title ? `${identity} — ${row.title}` : identity,
  };
}

export function PullRequestListItem({
  entry,
  repository,
  personHues,
  actions,
  sandboxAsleep,
  selected,
  mergeRequested,
  onSelect,
  statsColumns,
  now,
}: PullRequestRowProps & {
  /** Rows show their update time, as the table's Updated column would. */
  statsColumns: boolean;
  now: number;
}) {
  const { row } = entry;
  const { reference, label, actionsLabel } = rowIdentity(entry);
  const finished = isFinishedState(row.state);
  const menu = useMenuCloseHandOff();
  // The ellipsis and context menus each filter on their own.
  const ellipsisFilter = useMenuFilter();
  const contextFilter = useMenuFilter();
  return (
    <li>
      <ContextMenu>
        <ContextMenuTrigger asChild>
          <div
            data-pull-request-row={row.url}
            className={cn(
              "group/row relative grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-2 gap-y-0.5 rounded-md px-2 py-1.5 hover:bg-muted/50 has-[>button:first-child:focus-visible]:bg-muted/50",
              finished && "text-muted-foreground",
              selected && SELECTED_ROW,
            )}
          >
            {/* The whole row opens the PR; its links and controls sit above this button. */}
            <button
              type="button"
              {...openGestureProps(onSelect)}
              title={label}
              aria-label={label}
              aria-current={selected || undefined}
              className="absolute inset-0 cursor-pointer rounded-md focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
            />
            <PullRequestStateIcon
              state={row.state}
              className="pointer-events-none row-span-2 self-start mt-0.5"
            />
            <div className="pointer-events-none flex min-w-0 items-baseline gap-1.5">
              <span
                className={cn(
                  "truncate text-ui",
                  finished ? "font-normal" : "font-medium text-foreground",
                )}
              >
                {row.title ?? `${row.repository} ${reference}`}
              </span>
              {repository && (
                <span
                  className="shrink-0 truncate text-xs text-muted-foreground"
                  title={row.repository}
                >
                  {repository}
                </span>
              )}
            </div>
            <div className="flex items-center gap-1">
              {statsColumns && (
                <span className="relative z-10 shrink-0">
                  <UpdatedCell entry={entry} now={now} />
                </span>
              )}
              {row.author && (
                <span className="pointer-events-none" title={row.author}>
                  <PullRequestAuthorAvatar
                    author={row.author}
                    hue={personHues.get(row.author.toLowerCase())}
                  />
                  <span className="sr-only">Author {row.author}</span>
                </span>
              )}
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    aria-label={actionsLabel}
                    className="relative z-10 size-5 shrink-0 text-muted-foreground opacity-0 group-hover/row:opacity-100 focus-visible:opacity-100 data-[state=open]:opacity-100 pointer-coarse:opacity-100"
                  >
                    <EllipsisIcon aria-hidden className="size-3.5" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent
                  align="end"
                  {...ellipsisFilter.contentProps}
                  onCloseAutoFocus={menu.onCloseAutoFocus}
                >
                  <RowActionMenuBody
                    actions={actions}
                    parts={DROPDOWN_ROW_MENU}
                    menu={menu}
                    filter={ellipsisFilter}
                  />
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
            {/* A narrow rail puts a merge status on its own line under the number. */}
            <div className="pointer-events-none flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground @max-[280px]/rail:flex-wrap @max-[280px]/rail:gap-y-0.5">
              <span className="shrink-0 tabular-nums">{reference}</span>
              <RowStatus
                entry={entry}
                sandboxAsleep={sandboxAsleep}
                mergeRequested={mergeRequested}
              />
            </div>
            <div className="flex justify-end text-xs text-muted-foreground">
              <RowCiBadge row={row} offline={entry.offline} />
            </div>
          </div>
        </ContextMenuTrigger>
        <ContextMenuContent
          {...contextFilter.contentProps}
          onCloseAutoFocus={menu.onCloseAutoFocus}
        >
          <RowActionMenuBody
            actions={actions}
            parts={CONTEXT_ROW_MENU}
            menu={menu}
            filter={contextFilter}
          />
        </ContextMenuContent>
      </ContextMenu>
    </li>
  );
}

/** Line 2's status text: why the row has no live data, or its review state. */
function RowStatus({
  entry,
  sandboxAsleep,
  mergeRequested,
}: {
  entry: PullRequestListEntry;
  sandboxAsleep: boolean;
  mergeRequested: boolean;
}) {
  const { row } = entry;
  const parts: React.ReactElement[] = [];
  /** Parts a narrow rail leaves to screen readers, by key. */
  const narrowOnlyForScreenReaders = new Set<string>();
  let showsReadiness = false;
  if (entry.offline) {
    parts.push(
      <Tooltip key="offline">
        <TooltipTrigger asChild>
          <span
            tabIndex={0}
            role="status"
            className="pointer-events-auto relative z-10 inline-flex items-center gap-1"
          >
            <ServerOffIcon aria-hidden className="size-3" />
            {sandboxAsleep ? "Sandbox asleep" : "Host offline"}
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs">
          {storedPullRequestHint(entry.association, sandboxAsleep)}
        </TooltipContent>
      </Tooltip>,
    );
  } else if (entry.status === "error") {
    parts.push(
      <span key="error" title={entry.error ? String(entry.error) : undefined}>
        Status unavailable
      </span>,
    );
  } else if (entry.status === "loading" && !row.stateRead) {
    parts.push(<span key="loading">Loading…</span>);
  } else {
    const readiness = presentMergeReadiness(row.mergeReadinessView);
    if (mergeRequested) parts.push(<span key="merging">Merge requested</span>);
    // Merge readiness leads, so a narrow row keeps it in view; it also covers review and draft.
    if (readiness) parts.push(<MergeReadinessLabel key="readiness" presentation={readiness} />);
    showsReadiness = readiness !== null;
    if (row.needsYou) {
      // Beside a merge status it ellipsizes, and a narrow rail shows it only to screen
      // readers: the status says what's needed, and the Needs You filter still counts it.
      if (readiness) narrowOnlyForScreenReaders.add("needs-you");
      parts.push(
        <span
          key="needs-you"
          className={cn(
            "inline-flex min-w-0 items-center gap-1 text-foreground",
            readiness && NARROW_SCREEN_READER_ONLY,
          )}
        >
          <span aria-hidden className="size-1.5 shrink-0 rounded-full bg-status-yellow" />
          <span className="truncate">Needs you</span>
        </span>,
      );
    }
    if (!readiness) {
      if (row.review && !isFinishedState(row.state)) {
        parts.push(
          <span key="review" className={REVIEW_TINTS[row.review]}>
            {REVIEW_STATUS_TITLES[row.review]}
          </span>,
        );
      }
      if (row.state === "draft") parts.push(<span key="draft">Draft</span>);
    }
  }
  // Finished rows say so even offline, from the state the host last stored.
  if (isFinishedState(row.state)) {
    parts.unshift(<span key="state">{pullRequestStateTitle(row.state)}</span>);
  }
  if (row.inferred) parts.push(<span key="inferred">From branch</span>);
  return (
    <span
      className={cn(
        "flex min-w-0 items-center gap-1.5 truncate",
        showsReadiness && "@max-[280px]/rail:basis-full",
      )}
    >
      {parts.flatMap((part, index) =>
        index === 0
          ? [part]
          : [
              <span
                key={`separator:${part.key}`}
                aria-hidden
                className={
                  narrowOnlyForScreenReaders.has(String(part.key))
                    ? "@max-[280px]/rail:hidden"
                    : undefined
                }
              >
                ·
              </span>,
              part,
            ],
      )}
    </span>
  );
}

/** A row's merge readiness in its tint, with the reason on hover or focus. */
function MergeReadinessLabel({ presentation }: { presentation: MergeReadinessPresentation }) {
  // It keeps its full width; the row's other parts truncate first. A narrow rail shows the
  // short label, and screen readers still hear the full one.
  const { label: full, short } = presentation;
  const label = (
    <span
      className={cn(
        // Wrapping whole words is the last resort in the narrowest rail.
        "shrink-0 whitespace-nowrap @max-[280px]/rail:min-w-0 @max-[280px]/rail:shrink @max-[280px]/rail:whitespace-normal",
        MERGE_READINESS_TONE_TEXT[presentation.tone],
      )}
    >
      {short === full ? (
        full
      ) : (
        <>
          <span className="@max-[280px]/rail:hidden">{full}</span>
          <span className="hidden @max-[280px]/rail:inline">
            <span aria-hidden>{short}</span>
            <span className="sr-only">{full}</span>
          </span>
        </>
      )}
    </span>
  );
  if (!presentation.detail) return label;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          tabIndex={0}
          className="pointer-events-auto relative z-10 inline-flex shrink-0 rounded-sm @max-[280px]/rail:min-w-0 @max-[280px]/rail:shrink focus-visible:outline-2 focus-visible:outline-ring"
        >
          {label}
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">{presentation.detail}</TooltipContent>
    </Tooltip>
  );
}

/** Hidden from sight in a narrow rail, still read by screen readers. */
const NARROW_SCREEN_READER_ONLY = "@max-[280px]/rail:sr-only";

/** The active row: a tinted background and a leading accent bar. */
const SELECTED_ROW =
  "bg-muted/70 before:pointer-events-none before:absolute before:inset-y-1.5 before:left-0 before:w-0.5 before:rounded-full before:bg-primary";

export function TableColumnHeader({
  id,
  statusText,
  sort,
  onSort,
  statsAvailable,
  sortReasonId,
}: {
  id: PullRequestTableColumn;
  statusText: boolean;
  sort: { order: PullRequestListOrder; direction: PullRequestListDirection };
  onSort: (order: PullRequestListOrder) => void;
  /** Size and update-time orders can apply. */
  statsAvailable: boolean;
  /** The list's reason for sorts that can't apply yet. */
  sortReasonId: string;
}) {
  const column = PULL_REQUEST_TABLE_COLUMNS.find((candidate) => candidate.id === id)!;
  const sorted = column.order !== undefined && sort.order === column.order;
  // A dot-wide status column has no room for its name; its sort button keeps one.
  const title = id === "status" && !statusText ? "" : column.title;
  const ArrowIcon = sort.direction === "ascending" ? ArrowUpIcon : ArrowDownIcon;
  return (
    <div
      role="columnheader"
      aria-sort={sorted ? sort.direction : undefined}
      className={cn("flex min-w-0 items-center", RIGHT_ALIGNED.has(id) && "justify-end")}
    >
      {id === "actions" ? (
        <span className="sr-only">Actions</span>
      ) : column.order && !orderAvailable(column.order, statsAvailable) ? (
        <PullRequestListUnavailableSort
          label={`Sort by ${column.title}`}
          reasonId={sortReasonId}
          className="-mx-1 inline-flex min-w-0 items-center rounded-sm px-1 py-0.5 font-medium focus-visible:outline-2 focus-visible:outline-ring"
        >
          <span className="truncate">{title}</span>
        </PullRequestListUnavailableSort>
      ) : column.order ? (
        <button
          type="button"
          aria-label={`Sort by ${column.title === "#" ? "number" : column.title}`}
          title={`Sort by ${column.title === "#" ? "number" : column.title}`}
          onClick={() => onSort(column.order!)}
          className={cn(
            "-mx-1 inline-flex min-w-0 cursor-pointer items-center gap-0.5 rounded-sm px-1 py-0.5 font-medium hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring",
            sorted && "text-foreground",
          )}
        >
          <span className="truncate">{title}</span>
          {sorted && <ArrowIcon aria-hidden className="size-3 shrink-0" />}
        </button>
      ) : (
        <span className="truncate font-medium">{title}</span>
      )}
    </div>
  );
}

const RIGHT_ALIGNED: ReadonlySet<PullRequestTableColumn> = new Set(["diff", "updated", "actions"]);

/** One table row: each visible column's cell; the title opens the PR, as the whole row does. */
export function PullRequestTableRow({
  entry,
  repository,
  personHues,
  actions,
  sandboxAsleep,
  selected,
  mergeRequested,
  onSelect,
  layout,
  now,
}: PullRequestRowProps & {
  layout: Extract<PullRequestTableLayout, { kind: "table" }>;
  now: number;
}) {
  const { row } = entry;
  const { reference, label, actionsLabel } = rowIdentity(entry);
  const finished = isFinishedState(row.state);
  const menu = useMenuCloseHandOff();
  // The ellipsis and context menus each filter on their own.
  const ellipsisFilter = useMenuFilter();
  const contextFilter = useMenuFilter();
  const cell = (id: PullRequestTableColumn): React.ReactNode => {
    switch (id) {
      case "number":
        return (
          <span className="truncate text-xs tabular-nums text-muted-foreground">{reference}</span>
        );
      case "title":
        return (
          <span className="flex min-w-0 items-center gap-1.5">
            {row.needsYou && (
              <span
                role="img"
                aria-label="Needs you"
                title="Needs you"
                className="size-1.5 shrink-0 rounded-full bg-status-yellow"
              />
            )}
            {/* The keyboard and screen-reader way in; a click anywhere on the row opens it too. */}
            <button
              type="button"
              data-row-open
              {...openGestureProps(onSelect)}
              title={label}
              aria-label={label}
              aria-current={selected || undefined}
              className={cn(
                "min-w-0 flex-1 cursor-pointer truncate text-left text-ui focus-visible:outline-none",
                finished ? "font-normal" : "font-medium text-foreground",
              )}
            >
              {row.title ?? `${row.repository} ${reference}`}
            </button>
            {/* Secondary labels give way first, so the title always keeps most of the cell. */}
            {repository && (
              <span
                className="min-w-0 max-w-[40%] truncate text-xs text-muted-foreground"
                title={row.repository}
              >
                {repository}
              </span>
            )}
            {row.inferred && (
              <span
                className="min-w-0 max-w-[30%] truncate text-xs text-muted-foreground"
                title="Found from the session’s branch"
              >
                From branch
              </span>
            )}
          </span>
        );
      case "status":
        return (
          <StatusCell
            entry={entry}
            showText={layout.statusText}
            sandboxAsleep={sandboxAsleep}
            mergeRequested={mergeRequested}
          />
        );
      case "checks":
        return (
          <span className="text-xs text-muted-foreground">
            <RowCiBadge row={row} offline={entry.offline} />
          </span>
        );
      case "diff":
        return <DiffCell entry={entry} />;
      case "reviewers":
        return <ReviewerStack reviewers={row.reviewers} personHues={personHues} />;
      case "updated":
        return <UpdatedCell entry={entry} now={now} />;
      case "actions":
        return (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label={actionsLabel}
                className="relative z-10 size-5 shrink-0 text-muted-foreground opacity-0 group-hover/row:opacity-100 focus-visible:opacity-100 data-[state=open]:opacity-100 pointer-coarse:opacity-100"
              >
                <EllipsisIcon aria-hidden className="size-3.5" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent
              align="end"
              {...ellipsisFilter.contentProps}
              onCloseAutoFocus={menu.onCloseAutoFocus}
            >
              <RowActionMenuBody
                actions={actions}
                parts={DROPDOWN_ROW_MENU}
                menu={menu}
                filter={ellipsisFilter}
              />
            </DropdownMenuContent>
          </DropdownMenu>
        );
    }
  };
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <div
          role="row"
          data-pull-request-row={row.url}
          className={cn(
            "group/row relative grid h-9 cursor-pointer items-center gap-x-1.5 rounded-md px-2 hover:bg-muted/50 has-[[data-row-open]:focus-visible]:bg-muted/50 has-[[data-row-open]:focus-visible]:outline-2 has-[[data-row-open]:focus-visible]:-outline-offset-2 has-[[data-row-open]:focus-visible]:outline-ring",
            finished && "text-muted-foreground",
            selected && SELECTED_ROW,
          )}
          style={{ gridTemplateColumns: layout.gridTemplate }}
          // Clicks on the row's own controls (CI, the menu) do their own thing.
          onClick={(event) => {
            if (!(event.target instanceof Element)) return;
            if (event.target.closest("button, a, [role=menuitem]")) return;
            onSelect(pullRequestOpenIntent(event));
          }}
          // A middle click on the row's own cells opens it behind, without the browser's autoscroll.
          onMouseDown={(event) => {
            if (event.button !== 1 || !(event.target instanceof Element)) return;
            if (event.target.closest("button, a, [role=menuitem]")) return;
            event.preventDefault();
          }}
          onAuxClick={(event) => {
            if (event.button !== 1 || !(event.target instanceof Element)) return;
            if (event.target.closest("button, a, [role=menuitem]")) return;
            event.preventDefault();
            onSelect(pullRequestOpenIntent(event));
          }}
        >
          {layout.columns.map((id) => (
            <div
              key={id}
              role="cell"
              data-column={id}
              className={cn("flex min-w-0 items-center", RIGHT_ALIGNED.has(id) && "justify-end")}
            >
              {cell(id)}
            </div>
          ))}
        </div>
      </ContextMenuTrigger>
      <ContextMenuContent {...contextFilter.contentProps} onCloseAutoFocus={menu.onCloseAutoFocus}>
        <RowActionMenuBody
          actions={actions}
          parts={CONTEXT_ROW_MENU}
          menu={menu}
          filter={contextFilter}
        />
      </ContextMenuContent>
    </ContextMenu>
  );
}

const STATE_DOTS: Record<PullRequestListState, string> = {
  open: "bg-status-green",
  draft: "bg-muted-foreground",
  merged: "bg-status-purple",
  closed: "bg-status-red",
  unknown: "bg-muted-foreground/50",
};

/** The Status column: the lifecycle state as a dot and its name, or why it's unknown. */
function StatusCell({
  entry,
  showText,
  sandboxAsleep,
  mergeRequested,
}: {
  entry: PullRequestListEntry;
  showText: boolean;
  sandboxAsleep: boolean;
  mergeRequested: boolean;
}) {
  const { row } = entry;
  // Short words fit the column; the full one is the accessible name and the tooltip.
  const text = (short: string, full: string) =>
    short === full || !showText ? (
      <span className={showText ? "truncate" : "sr-only"}>{full}</span>
    ) : (
      <>
        <span aria-hidden className="truncate">
          {short}
        </span>
        <span className="sr-only">{full}</span>
      </>
    );
  if (entry.offline && !isFinishedState(row.state)) {
    const [short, full] = sandboxAsleep
      ? ["Asleep", "Sandbox asleep"]
      : ["Offline", "Host offline"];
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            tabIndex={0}
            role="status"
            className="relative z-10 inline-flex min-w-0 items-center gap-1 rounded-sm text-xs text-muted-foreground focus-visible:outline-2 focus-visible:outline-ring"
          >
            <ServerOffIcon aria-hidden className="size-3 shrink-0" />
            {text(short, full)}
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs">
          {storedPullRequestHint(entry.association, sandboxAsleep)}
        </TooltipContent>
      </Tooltip>
    );
  }
  // A stats read may already know the state while the PR's own read loads.
  if (!entry.offline && entry.status === "loading" && !row.stateRead) {
    return (
      <span className="truncate text-xs text-muted-foreground" title="Loading…">
        {showText ? "Loading…" : "…"}
      </span>
    );
  }
  const readiness =
    !entry.offline && !mergeRequested ? presentMergeReadiness(row.mergeReadinessView) : null;
  if (readiness) {
    return (
      <span
        className="inline-flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground"
        title={readiness.detail ? `${readiness.label}. ${readiness.detail}` : readiness.label}
      >
        <span
          aria-hidden
          className={cn("size-2 shrink-0 rounded-full", MERGE_READINESS_TONE_DOT[readiness.tone])}
        />
        {text(readiness.short, readiness.label)}
      </span>
    );
  }
  const unavailable = !entry.offline && entry.status === "error" && row.state === "unknown";
  const [short, full] = mergeRequested
    ? ["Merging", "Merge requested"]
    : unavailable
      ? ["Unknown", "Status unavailable"]
      : [pullRequestStateTitle(row.state), pullRequestStateTitle(row.state)];
  return (
    <span
      className="inline-flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground"
      title={unavailable && entry.error ? String(entry.error) : full}
    >
      <span aria-hidden className={cn("size-2 shrink-0 rounded-full", STATE_DOTS[row.state])} />
      {text(short, full)}
    </span>
  );
}

/** The Diff column: lines added and removed, compact past a thousand, or why there are none. */
function DiffCell({ entry }: { entry: PullRequestListEntry }) {
  const cell = pullRequestListEntryStatsCell(entry, "diff");
  if (cell.kind === "blank") return null;
  if (cell.kind === "gap")
    return <PullRequestListStatsGapMark label={cell.label} reason={cell.reason} />;
  return <DiffValue stats={cell.stats} />;
}

function DiffValue({ stats }: { stats: PullRequestListStats }) {
  if (stats.additions === undefined || stats.deletions === undefined) return null;
  const files =
    stats.changedFiles === undefined
      ? ""
      : ` in ${stats.changedFiles.toLocaleString()} ${stats.changedFiles === 1 ? "file" : "files"}`;
  const description = `${stats.additions.toLocaleString()} ${
    stats.additions === 1 ? "line" : "lines"
  } added, ${stats.deletions.toLocaleString()} removed${files}`;
  return (
    <span
      role="img"
      aria-label={description}
      title={description}
      className="inline-flex gap-1 text-xs tabular-nums"
    >
      <span className="text-status-green">+{compactCount(stats.additions)}</span>
      <span className="text-status-red">−{compactCount(stats.deletions)}</span>
    </span>
  );
}

const REVIEWER_RINGS: Record<PullRequestReviewer["state"], string> = {
  APPROVED: "ring-status-green",
  CHANGES_REQUESTED: "ring-status-red",
  PENDING: "ring-status-yellow",
  COMMENTED: "ring-border",
  DISMISSED: "ring-border",
};
// Requested changes first: they're what a glance at the column should catch.
const REVIEWER_ORDER: PullRequestReviewer["state"][] = [
  "CHANGES_REQUESTED",
  "APPROVED",
  "PENDING",
  "COMMENTED",
  "DISMISSED",
];
const SHOWN_REVIEWERS = 3;

/** The Reviewers column: up to three avatars ringed by review state, then a count. */
function ReviewerStack({
  reviewers,
  personHues,
}: {
  reviewers: readonly PullRequestReviewer[];
  personHues: ReadonlyMap<string, number>;
}) {
  if (reviewers.length === 0) return null;
  const ordered = [...reviewers].sort(
    (left, right) => REVIEWER_ORDER.indexOf(left.state) - REVIEWER_ORDER.indexOf(right.state),
  );
  const shown = ordered.slice(0, SHOWN_REVIEWERS);
  const hidden = ordered.length - shown.length;
  const description = ordered
    .map((reviewer) => `${reviewer.login}: ${REVIEWER_STATE_META[reviewer.state].label}`)
    .join(", ");
  return (
    <span
      role="img"
      aria-label={`Reviewers: ${description}`}
      title={ordered
        .map((reviewer) => `${reviewer.login} · ${REVIEWER_STATE_META[reviewer.state].label}`)
        .join("\n")}
      className="flex items-center"
    >
      <span className="flex -space-x-1">
        {shown.map((reviewer) => (
          <span
            key={`${reviewer.kind}:${reviewer.login}`}
            className={cn(
              "flex rounded-full ring-[1.5px] ring-offset-1 ring-offset-background",
              REVIEWER_RINGS[reviewer.state],
            )}
          >
            {reviewer.kind === "team" ? (
              <span className="inline-flex size-4 items-center justify-center rounded-full bg-muted text-muted-foreground">
                <UsersIcon aria-hidden className="size-2.5" />
              </span>
            ) : (
              <PullRequestAuthorAvatar
                author={reviewer.login}
                hue={personHues.get(reviewer.login.toLowerCase())}
              />
            )}
          </span>
        ))}
      </span>
      {hidden > 0 && (
        <span aria-hidden className="ml-1.5 text-xs tabular-nums text-muted-foreground">
          +{hidden}
        </span>
      )}
    </span>
  );
}

/** The Updated column: how long ago GitHub last saw a change, with the exact time on hover. */
function UpdatedCell({ entry, now }: { entry: PullRequestListEntry; now: number }) {
  const cell = pullRequestListEntryStatsCell(entry, "updated");
  if (cell.kind === "blank") return null;
  if (cell.kind === "gap")
    return <PullRequestListStatsGapMark label={cell.label} reason={cell.reason} />;
  const updatedAt = cell.stats.updatedAt;
  if (updatedAt === undefined) return null;
  const exact = absoluteTime(updatedAt);
  return (
    <time
      dateTime={new Date(updatedAt).toISOString()}
      title={`Updated ${exact}`}
      aria-label={`Updated ${exact}`}
      className="text-xs tabular-nums text-muted-foreground"
    >
      {relativeTime(updatedAt, now)}
    </time>
  );
}
