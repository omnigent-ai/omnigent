// The new Pull Requests list's view model: lifecycle grouping, the sort keys and their
// tie-breaks, each row's status and CI as the list shows them, and multi-row selection.
// Rows come from the classic list's model; only how this layout presents them lives here.

import { namedCodeHost } from "@/lib/codeHosts";
import {
  type MergeReadinessStatus,
  presentMergeReadiness,
  type MergeReadinessTone,
  knownMergeReadinessStatus,
} from "@/lib/mergeReadiness";
import type { PullRequestCiRollup } from "@/lib/pullRequestListStats";
import { mergeReadinessMeaning, type PullRequestStatusMeaning } from "@/lib/pullRequestStatusIcons";
import {
  checkResult,
  type CheckResult,
  isFinishedState,
  pullRequestStateTitle,
  storedPullRequestHint,
  type PullRequestListRow,
  type PullRequestListState,
} from "@/shell/pullRequestListModel";
import type { PullRequestListEntry } from "@/shell/PullRequestListRows";
import { isNotLinkedRefusal, NOT_LINKED_DETAIL } from "./notLinkedRow";

export type PageGrouping = "status" | "repository" | "author" | "session" | "none";
export type PageSortKey =
  "updated" | "status" | "title" | "number" | "author" | "repository" | "size" | "search";
export type PageSortDirection = "ascending" | "descending";
export interface PageSort {
  key: PageSortKey;
  direction: PageSortDirection;
}
export interface PageDisplay {
  grouping: PageGrouping;
  sort: PageSort;
}

export const PAGE_GROUPINGS: readonly { id: PageGrouping; title: string }[] = [
  { id: "status", title: "Status" },
  { id: "repository", title: "Repository" },
  { id: "author", title: "Author" },
  { id: "none", title: "No grouping" },
];

/** The groupings a list offers: a session's list also groups by the agent that linked each PR. */
export function pageGroupings(
  subAgentPullRequests: boolean,
): readonly { id: PageGrouping; title: string }[] {
  // The id stays "session" so displays saved before the rename keep their grouping.
  return subAgentPullRequests
    ? [...PAGE_GROUPINGS.slice(0, 3), { id: "session", title: "Agent" }, PAGE_GROUPINGS[3]!]
    : PAGE_GROUPINGS;
}

/** A display grouped by session only where sub-agents join the list; elsewhere it groups by status. */
export function displayAllowedFor(
  display: PageDisplay,
  subAgentPullRequests: boolean,
): PageDisplay {
  if (subAgentPullRequests || display.grouping !== "session") return display;
  return { ...display, grouping: DEFAULT_PAGE_DISPLAY.grouping };
}

export const PAGE_SORTS: readonly {
  id: PageSortKey;
  title: string;
  defaultDirection: PageSortDirection;
  /** Update times and diff sizes come from the desktop shell's stats read. */
  needsStats?: boolean;
}[] = [
  { id: "updated", title: "Last updated", defaultDirection: "descending", needsStats: true },
  { id: "status", title: "Status", defaultDirection: "ascending" },
  { id: "title", title: "Title", defaultDirection: "ascending" },
  { id: "number", title: "Number", defaultDirection: "descending" },
  { id: "author", title: "Author", defaultDirection: "ascending" },
  { id: "repository", title: "Repository", defaultDirection: "ascending" },
  { id: "size", title: "Size", defaultDirection: "descending", needsStats: true },
];

/** A list in a search's own order offers it first, as it comes. */
const SEARCH_ORDER_OPTION = {
  id: "search" as const,
  title: "Search order",
  defaultDirection: "ascending" as const,
};

/** The orders a list offers: a search's own first, where its rows come in one. */
export function pageSortOptions(searchOrdered: boolean): readonly (typeof PAGE_SORTS)[number][] {
  return searchOrdered ? [SEARCH_ORDER_OPTION, ...PAGE_SORTS] : PAGE_SORTS;
}

/** What a list in a search's own order shows until the user picks another: the search's order. */
export const SEARCH_ORDER_DISPLAY: PageDisplay = {
  grouping: "none",
  sort: { key: "search", direction: "ascending" },
};

/**
 * The display a list shows for the one kept: a search-ordered list shows its search's order
 * until the user picks a display, even one that equals the default, and a list in no such
 * order never shows one. `chosen` is whether they picked one; a view kept before it was
 * recorded counts a display other than the default as picked.
 */
export function shownPageDisplay(
  display: PageDisplay,
  searchOrdered: boolean,
  chosen: boolean | undefined,
): PageDisplay {
  if (searchOrdered) return displayChosen(display, chosen) ? display : SEARCH_ORDER_DISPLAY;
  return display.sort.key === "search" ? { ...display, sort: DEFAULT_PAGE_DISPLAY.sort } : display;
}

/** Whether the user picked the kept display, rather than leaving the list's own. */
export function displayChosen(display: PageDisplay, chosen: boolean | undefined): boolean {
  return chosen ?? !isDefaultDisplay(display);
}

/**
 * Whether Reset has anything to put back: a picked display on a search-ordered list, whose own
 * is its search's order, else any display but the default.
 */
export function pageDisplayModified(
  display: PageDisplay,
  searchOrdered: boolean,
  chosen: boolean | undefined,
): boolean {
  return searchOrdered ? displayChosen(display, chosen) : !isDefaultDisplay(display);
}

export const DEFAULT_PAGE_DISPLAY: PageDisplay = {
  grouping: "status",
  sort: { key: "updated", direction: "descending" },
};

export function isDefaultDisplay(display: PageDisplay): boolean {
  return (
    display.grouping === DEFAULT_PAGE_DISPLAY.grouping &&
    display.sort.key === DEFAULT_PAGE_DISPLAY.sort.key &&
    display.sort.direction === DEFAULT_PAGE_DISPLAY.sort.direction
  );
}

/** A header click: the same key flips its direction, another key starts at its natural one. */
export function sortAfterHeaderClick(current: PageSort, key: PageSortKey): PageSort {
  if (current.key === key) {
    return { key, direction: current.direction === "ascending" ? "descending" : "ascending" };
  }
  return { key, direction: sortOption(key).defaultDirection };
}

function sortOption(key: PageSortKey) {
  return pageSortOptions(true).find((option) => option.id === key)!;
}

/** Whether a sort can apply now: update-time and size orders wait for stats. */
export function pageSortAvailable(key: PageSortKey, statsReady: boolean): boolean {
  return statsReady || !sortOption(key).needsStats;
}

// One value, so a list waiting for stats sorts its rows once, not again on every render.
const STATUS_SORT: PageSort = { key: "status", direction: "ascending" };

/** The sort that applies: update-time and size orders wait for stats, keeping status order. */
export function effectivePageSort(sort: PageSort, statsReady: boolean): PageSort {
  if (pageSortAvailable(sort.key, statsReady)) return sort;
  return STATUS_SORT;
}

const STATE_ORDER: readonly PullRequestListState[] = [
  "open",
  "draft",
  "merged",
  "closed",
  "unknown",
];

/** A row that needs the signed-in account now; finished work never does. */
export function needsAttention(row: PullRequestListRow): boolean {
  return row.needsYou && !isFinishedState(row.state);
}

/** Lifecycle first, and within it the rows that need you ahead of the rest. */
function statusRank(row: PullRequestListRow): number {
  const state = STATE_ORDER.indexOf(row.state) * 2;
  return needsAttention(row) ? state : state + 1;
}

/** A missing value is the smallest, so the default newest- and biggest-first orders end with it. */
function compareOptional(left: number | undefined, right: number | undefined): number {
  if (left === undefined && right === undefined) return 0;
  if (left === undefined) return -1;
  if (right === undefined) return 1;
  return left - right;
}

function size(row: PullRequestListRow): number | undefined {
  const { additions, deletions } = row.stats ?? {};
  if (additions === undefined && deletions === undefined) return undefined;
  return (additions ?? 0) + (deletions ?? 0);
}

function compareBy(key: PageSortKey, left: PullRequestListRow, right: PullRequestListRow): number {
  switch (key) {
    // Rows in a search's own order keep it; ties fall to the rest.
    case "search":
      return 0;
    case "updated":
      return compareOptional(left.stats?.updatedAt, right.stats?.updatedAt);
    case "number":
      return left.number - right.number;
    case "title":
      return (left.title ?? "").localeCompare(right.title ?? "", undefined, {
        sensitivity: "base",
      });
    case "status":
      return statusRank(left) - statusRank(right);
    case "author":
      // Rows without a known author sort after every name.
      return (left.author ?? "￿").localeCompare(right.author ?? "￿", undefined, {
        sensitivity: "base",
      });
    case "repository":
      return left.repository.localeCompare(right.repository) || left.number - right.number;
    case "size":
      return compareOptional(size(left), size(right));
  }
}

/** Sorted rows; ties go to the most recently updated, then the URL, so equal keys never shuffle. */
export function sortPageRows(
  rows: readonly PullRequestListRow[],
  sort: PageSort,
): PullRequestListRow[] {
  // A search's own order is the rows' order as they came.
  if (sort.key === "search")
    return sort.direction === "ascending" ? [...rows] : [...rows].reverse();
  const direction = sort.direction === "ascending" ? 1 : -1;
  return [...rows].sort((left, right) => {
    const primary = compareBy(sort.key, left, right);
    if (primary !== 0) return primary * direction;
    return compareBy("updated", right, left) || left.url.localeCompare(right.url);
  });
}

export interface PageGroup {
  id: string;
  title: string;
  /** Set when grouped by status: the lifecycle state the group holds. */
  state?: PullRequestListState;
  /** Set when grouped by author. */
  author?: string;
  rows: PullRequestListRow[];
}

const UNKNOWN_AUTHOR = "Unknown author";
const THIS_AGENT_TITLE = "This agent";

/** The group a row sorts into under a grouping, by its id and the title its header shows. */
function groupOf(
  row: PullRequestListRow,
  grouping: Exclude<PageGrouping, "none">,
): Pick<PageGroup, "id" | "title" | "state" | "author"> {
  if (grouping === "status") {
    return { id: row.state, title: pullRequestStateTitle(row.state), state: row.state };
  }
  if (grouping === "repository") return { id: row.repository, title: row.repository };
  if (grouping === "session") {
    const session = row.sessionSource?.session;
    return session ? { id: session.id, title: session.name } : { id: "", title: THIS_AGENT_TITLE };
  }
  return {
    id: row.author?.toLowerCase() ?? "",
    title: row.author ?? UNKNOWN_AUTHOR,
    author: row.author ?? undefined,
  };
}

/**
 * Rows grouped by lifecycle state, repository or author, keeping the order they came in.
 * Status groups follow the lifecycle even when merge readiness names a row's status.
 */
export function groupPageRows(
  rows: readonly PullRequestListRow[],
  grouping: PageGrouping,
): PageGroup[] {
  if (grouping === "none") return [{ id: "all", title: "All", rows: [...rows] }];
  const groups = new Map<string, PageGroup>();
  for (const row of rows) {
    const group = groupOf(row, grouping);
    let entry = groups.get(group.id);
    if (!entry) {
      entry = { ...group, rows: [] };
      groups.set(group.id, entry);
    }
    entry.rows.push(row);
  }
  const values = [...groups.values()];
  if (grouping === "status") {
    return values.sort(
      (left, right) => STATE_ORDER.indexOf(left.state!) - STATE_ORDER.indexOf(right.state!),
    );
  }
  return values.sort((left, right) => {
    // This session's own rows lead the session grouping, and the unknown author goes last.
    if (grouping === "session" && left.id === "") return -1;
    if (grouping === "session" && right.id === "") return 1;
    if (!left.author && grouping === "author") return 1;
    if (!right.author && grouping === "author") return -1;
    return left.title.localeCompare(right.title, undefined, { sensitivity: "base" });
  });
}

/**
 * Whether a group shows its header. A lone open group hides it unless it offers Remove All,
 * but a collapsed one keeps it, so filtering down to that group can't strand its rows.
 */
export function showsGroupHeader({
  grouping,
  groupCount,
  collapsed,
  offersRemoval,
  actionCount = 0,
}: {
  grouping: PageGrouping;
  groupCount: number;
  collapsed: boolean;
  /** Whether its header offers Remove All, even one it can't run yet. */
  offersRemoval: boolean;
  /** Actions its header's menus offer, such as Hide Repository, which keep it shown. */
  actionCount?: number;
}): boolean {
  return grouping !== "none" && (groupCount > 1 || collapsed || offersRemoval || actionCount > 0);
}

/** Every row between the anchor and the target, inclusive, in list order. */
export function rangeSelection(
  order: readonly string[],
  anchor: string | undefined,
  target: string,
): string[] {
  const end = order.indexOf(target);
  if (end === -1) return [];
  const start = anchor === undefined ? -1 : order.indexOf(anchor);
  if (start === -1) return [target];
  const [from, to] = start <= end ? [start, end] : [end, start];
  return order.slice(from, to + 1);
}

/** The selection without rows that left the list; the same set when nothing left. */
export function retainSelection(
  selection: ReadonlySet<string>,
  urls: readonly string[],
): ReadonlySet<string> {
  const present = new Set(urls);
  const retained = new Set([...selection].filter((url) => present.has(url)));
  return retained.size === selection.size ? selection : retained;
}

export type StatusTone = "positive" | "danger" | "warning" | "merged" | "muted";

export interface PageStatus {
  /** `loading` shows no status while the title is a placeholder. */
  kind: "loading" | "waiting" | "issue" | "offline" | "readiness" | "lifecycle" | "merging";
  label: string;
  /** What it says, which picks its icon. */
  meaning: PullRequestStatusMeaning;
  tone: StatusTone;
  /** Emphasized: the signed-in account can act on it now. */
  attention: boolean;
  /** Why, for the tooltip. */
  detail?: string;
}

const READINESS_TONES: Record<MergeReadinessTone, StatusTone> = {
  green: "positive",
  red: "danger",
  yellow: "warning",
  purple: "merged",
  gray: "muted",
};

// Title Case labels for each merge readiness state, as the reference list shows them.
const READINESS_LABELS: Record<MergeReadinessStatus, string> = {
  ready: "Ready",
  approved: "Approved",
  open: "Open",
  conflicts: "Conflicts",
  checks_failing: "Checks Failing",
  ci_failing: "CI Failing",
  policy_failing: "Policy",
  returned: "Returned to You",
  changes_requested: "Changes Requested",
  needs_reviewer: "Needs Reviewer",
  pending_approval: "Pending Approval",
  pending: "Pending",
  ci_running: "CI Running",
  review_required: "Review Required",
  draft: "Draft",
  merged: "Merged",
  closed: "Closed",
  unknown: "Unknown",
};

const LIFECYCLE_TONES: Record<PullRequestListState, StatusTone> = {
  open: "positive",
  draft: "muted",
  merged: "merged",
  closed: "danger",
  unknown: "muted",
};

/** Whether a row's read failed because GitHub is limiting this Mac's reads. */
function isRateLimited(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    (error as { code?: unknown }).code === "rate_limited"
  );
}

/**
 * The Status column. Merge readiness decides where it's read; elsewhere GitHub's own
 * signals: a requested change or an approval, a pending reviewer, or the lifecycle.
 */
export function pageStatus(
  entry: PullRequestListEntry,
  { sandboxAsleep, mergeRequested }: { sandboxAsleep: boolean; mergeRequested: boolean },
): PageStatus {
  const { row } = entry;
  const attention = needsAttention(row);
  const finished = isFinishedState(row.state);
  if (entry.offline && !finished) {
    return {
      kind: "offline",
      label: sandboxAsleep ? "Sandbox Asleep" : "Host Offline",
      meaning: sandboxAsleep ? "asleep" : "offline",
      tone: "muted",
      attention: false,
      detail: storedPullRequestHint(entry.association, sandboxAsleep),
    };
  }
  if (!entry.offline && entry.status === "loading" && !row.stateRead) {
    return {
      kind: "loading",
      label: "Loading",
      meaning: "unknown",
      tone: "muted",
      attention: false,
    };
  }
  // GitHub limiting reads is the page's to explain once: a row keeps what it last read.
  const rateLimited = !entry.offline && entry.status === "error" && isRateLimited(entry.error);
  if (rateLimited && !row.stateRead) {
    return {
      kind: "waiting",
      label: "Waiting for GitHub",
      meaning: "waiting",
      tone: "muted",
      attention: false,
      detail: "GitHub is limiting requests from this Mac. The list tries again on its own.",
    };
  }
  // The session's machine reads only PRs the session links; the row says so, not that it failed.
  if (!entry.offline && entry.status === "error" && isNotLinkedRefusal(entry.error)) {
    return {
      kind: "issue",
      label: "Not Linked",
      meaning: "not-linked",
      tone: "muted",
      attention: false,
      detail: entry.linkFailure ?? NOT_LINKED_DETAIL,
    };
  }
  if (!entry.offline && entry.status === "error" && !rateLimited) {
    return {
      kind: "issue",
      label: "Couldn’t Refresh",
      meaning: "refresh-failed",
      tone: "warning",
      attention: false,
      detail: entry.error ? String(entry.error) : undefined,
    };
  }
  if (mergeRequested) {
    return {
      kind: "merging",
      label: "Merge Requested",
      meaning: "merge-requested",
      tone: "merged",
      attention: false,
    };
  }
  const view = row.mergeReadinessView;
  const readiness = presentMergeReadiness(view);
  if (readiness && view && view.kind !== "off") {
    const status = knownMergeReadinessStatus(view);
    return {
      kind: "readiness",
      label: status ? READINESS_LABELS[status] : readiness.label,
      meaning: mergeReadinessMeaning(view),
      tone: READINESS_TONES[readiness.tone],
      attention,
      detail: readiness.detail,
    };
  }
  if (!finished && row.state !== "draft") {
    if (row.review === "changes_requested") {
      return {
        kind: "lifecycle",
        label: "Changes Requested",
        meaning: "changes-requested",
        tone: "danger",
        attention,
      };
    }
    if (row.review === "approved") {
      return {
        kind: "lifecycle",
        label: "Approved",
        meaning: "approved",
        tone: "positive",
        attention,
      };
    }
    if (row.review === "review_requested") {
      return {
        kind: "lifecycle",
        label: "Review Requested",
        meaning: "review-requested",
        tone: "warning",
        attention,
      };
    }
  }
  return {
    kind: "lifecycle",
    label: pullRequestStateTitle(row.state),
    meaning: row.state,
    tone: LIFECYCLE_TONES[row.state],
    attention,
  };
}

/**
 * Whether the row's state glyph already says all its status does, so the Status cell stays
 * empty: an open, draft, merged, closed or unknown PR with nothing more to add.
 */
export function statusSaidByState(status: PageStatus, state: PullRequestListState): boolean {
  return status.meaning === state;
}

/** The status's accessible name and tooltip: its label, then why, where it says. */
export function statusDescription(status: PageStatus): string {
  return status.detail ? `${status.label}. ${status.detail}` : status.label;
}

export type CiTone = CheckResult["tone"];

export interface PageCi {
  tone: CiTone;
  /** Accessible name and tooltip. */
  title: string;
  /** Failing checks, shown beside the glyph. */
  count?: number;
  /** Share of checks finished, 0–1, for a running ring; absent where the read gave no counts. */
  progress?: number;
}

const ROLLUP_TITLES: Record<PullRequestCiRollup, string> = {
  passing: "Checks passed",
  failing: "Checks failing",
  pending: "Checks running",
};

/**
 * The CI cell. Counts come only from a live read of the PR's checks; a stats rollup shows
 * its glyph alone, and its running state has no progress to show. Finished PRs show none.
 */
export function pageCi(row: PullRequestListRow, offline: boolean): PageCi | null {
  const { ci } = row;
  if (!ci || isFinishedState(row.state)) return null;
  if (ci.source === "batch") return { tone: ci.rollup, title: ROLLUP_TITLES[ci.rollup] };
  if (offline) return null;
  const { checks } = ci;
  const result = checkResult(checks, row.checksIncomplete);
  return {
    tone: result.tone,
    title: result.description,
    count: result.tone === "failing" ? result.count : undefined,
    progress:
      result.tone === "pending" && checks.pending > 0 && checks.total > 0
        ? (checks.total - checks.pending) / checks.total
        : undefined,
  };
}

/** Short ages, `now`, `12m`, `5h`, `3d`, `2w`, then a calendar date, as the reference list shows. */
export function compactAge(time: number, now: number = Date.now()): string {
  const seconds = Math.max(0, (now - time) / 1000);
  if (seconds < 60) return "now";
  if (seconds < 3_600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3_600)}h`;
  if (seconds < 7 * 86_400) return `${Math.floor(seconds / 86_400)}d`;
  if (seconds < 35 * 86_400) return `${Math.floor(seconds / (7 * 86_400))}w`;
  const date = new Date(time);
  const sameYear = date.getFullYear() === new Date(now).getFullYear();
  return dateFormat(
    sameYear ? "monthDay" : "monthYear",
    sameYear ? { month: "short", day: "numeric" } : { month: "short", year: "numeric" },
  ).format(date);
}

const dateFormats = new Map<string, Intl.DateTimeFormat>();

/** A formatter built once: building one costs far more than formatting, and a review has hundreds. */
function dateFormat(key: string, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  let format = dateFormats.get(key);
  if (!format) {
    format = new Intl.DateTimeFormat(undefined, options);
    dateFormats.set(key, format);
  }
  return format;
}

/** The exact time, for the Updated tooltip. */
export function fullDate(time: number): string {
  return dateFormat("full", { dateStyle: "medium", timeStyle: "short" }).format(new Date(time));
}

/**
 * The title link's tooltip: the title, its repository and branch, for a named host's row the host
 * it's on, and the agents that link it, which narrower rows don't show.
 */
export function rowLinkTooltip(
  row: Pick<PullRequestListRow, "url" | "title" | "headRef" | "repository" | "sessionSource">,
  reference: string,
): string {
  const lines = [row.title ?? reference];
  if (row.repository) lines.push(row.repository);
  if (row.headRef) lines.push(row.headRef);
  const host = urlHost(row.url);
  if (host && namedCodeHost(host)) lines.push(`On ${host}`);
  const source = row.sessionSource;
  if (source?.session) lines.push(`Linked by ${source.session.name}`);
  if (source && source.alsoIn.length > 0) {
    lines.push(`Also in ${source.alsoIn.map((session) => session.name).join(", ")}`);
  }
  return lines.join("\n");
}

function urlHost(url: string): string {
  try {
    return new URL(url).host.toLowerCase();
  } catch {
    return "";
  }
}

/** Up to two initials, from words or a login's separators. */
export function initials(value: string): string {
  const words = value.replace(/[_-]+/g, " ").trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length === 1) return words[0]!.slice(0, 2).toUpperCase();
  return (words[0]!.slice(0, 1) + words[1]!.slice(0, 1)).toUpperCase();
}

const GITHUB_LOGIN = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$/;

/**
 * GitHub's public avatar for a github.com login, read without credentials. Other hosts'
 * logins name other people, and bot or team names have no picture, so they get none.
 */
export function githubAvatarUrl(
  login: string,
  host: string | null | undefined,
): string | undefined {
  if (host !== "github.com" || !GITHUB_LOGIN.test(login)) return undefined;
  return `https://avatars.githubusercontent.com/${encodeURIComponent(login)}?s=64`;
}

/**
 * Repositories a list's owner hides for good, which its Filter menu shows as Exclude
 * Repository and changes through the owner.
 */
export interface RepositoryExclusion {
  /** `owner/name` as {@link repositoryFilterKey} gives it, most recently hidden first. */
  hidden: readonly string[];
  /** The repositories it could hide, with their rows; the list's own rows where absent. */
  candidates?: readonly RepositoryChoice[];
  toggle: (repository: string) => void;
  clear: () => void;
  /** Changes to open the hidden repositories' chip menu, e.g. from a note's Show. */
  menuRequest?: number;
}

/** A repository as the repository filters key it: `owner/name`, ignoring case. */
export const repositoryFilterKey = (repository: string) => repository.toLowerCase();

/** A repository a filter can pick: its name as rows give it, and how many rows it has. */
export interface RepositoryChoice {
  key: string;
  repository: string;
  count: number;
}

/**
 * The repositories the include filter offers: every one the rows name, and any it already
 * picks that the rows no longer do, so a pick never goes out of reach. Most rows first.
 */
export function repositoryChoices(
  rows: readonly Pick<PullRequestListRow, "repository">[],
  picked: readonly string[] = [],
): RepositoryChoice[] {
  const choices = new Map<string, RepositoryChoice>();
  for (const row of rows) {
    const key = repositoryFilterKey(row.repository);
    const choice = choices.get(key) ?? { key, repository: row.repository, count: 0 };
    choice.count += 1;
    choices.set(key, choice);
  }
  for (const key of picked) {
    if (!choices.has(key)) choices.set(key, { key, repository: key, count: 0 });
  }
  return [...choices.values()].sort(
    (left, right) => right.count - left.count || left.key.localeCompare(right.key),
  );
}

/** A chip's values: one repository by name, or how many. */
export function repositoriesSummary(names: readonly string[]): string {
  return names.length === 1 ? names[0]! : `${names.length} repositories`;
}
