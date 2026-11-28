// The Pull Requests list model: one row per linked PR, plus the filtering,
// grouping, ordering, and status summaries the list renders. Pure, so the
// rules are tested without the panel.

import type {
  PullRequest,
  PullRequestAssociation,
  PullRequestChecks,
  PullRequestReviewer,
} from "@/newPullRequestPage/hooks/usePullRequests";
import {
  MERGE_READINESS_STATUSES,
  mergeReadinessNeedsAttention,
  type MergeReadinessStatus,
  mergeReadinessStatusLabel,
  type MergeReadinessView,
  knownMergeReadinessStatus,
} from "@/lib/mergeReadiness";
import { absoluteTime } from "@/lib/relativeTime";
import {
  NATURAL_DIRECTIONS,
  type PullRequestListDirection,
  type PullRequestListDisplay,
  type PullRequestListGrouping,
  type PullRequestListOrder,
} from "@/lib/pullRequestListPreferences";
import { parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import type { PullRequestListStats, PullRequestListStatsTarget } from "@/lib/pullRequestListStats";
import {
  resolvePullRequestListStatus,
  type ResolvedPullRequestCi,
} from "@/lib/pullRequestListStatus";

import type { PullRequestSessionSource } from "@/pullRequestList/subAgentPullRequests";

export type { PullRequestListDirection, PullRequestListGrouping, PullRequestListOrder };

export type PullRequestListState = "open" | "draft" | "merged" | "closed" | "unknown";
export type PullRequestListFilter = "all" | "attention" | Exclude<PullRequestListState, "unknown">;
export type PullRequestReviewStatus = "changes_requested" | "approved" | "review_requested";

/** One list row: the stored association overlaid with the live PR when it has loaded. */
export interface PullRequestListRow {
  url: string;
  number: number;
  title: string | undefined;
  repository: string;
  state: PullRequestListState;
  author: string | null;
  headRef: string | null;
  labels: string[];
  /** Undefined until the live PR loads. */
  checks: PullRequestChecks | undefined;
  /**
   * The CI the row shows: the live checks, or the stats batch's rollup, whichever
   * read the PR's newest head; null when no read did.
   */
  ci: ResolvedPullRequestCi;
  /** The state came from a read of the PR, not only from what the server stored. */
  stateRead: boolean;
  /** Some checks couldn't be read, so counts are a lower bound. */
  checksIncomplete: boolean;
  review: PullRequestReviewStatus | undefined;
  /** Assigned reviewers; empty until the live PR loads. */
  reviewers: PullRequestReviewer[];
  /** Diff size and last update, where the desktop shell could read them. */
  stats: PullRequestListStats | undefined;
  /** Waiting on the signed-in account; see {@link needsViewer}. */
  needsYou: boolean;
  /** Found from the session's branch rather than linked or created. */
  inferred: boolean;
  /** An integration's merge readiness the row shows; see {@link effectiveMergeReadiness}. */
  mergeReadinessView?: MergeReadinessView;
  /** The status the row sorts and groups under while merge readiness applies to it. */
  mergeReadiness?: MergeReadinessStatus;
  /** Which sub-agent linked the PR, and the others that did; absent on a session-less list. */
  sessionSource?: PullRequestSessionSource;
}

export const PULL_REQUEST_LIST_FILTERS: { id: PullRequestListFilter; title: string }[] = [
  { id: "all", title: "All" },
  { id: "attention", title: "Needs You" },
  { id: "open", title: "Open" },
  { id: "draft", title: "Draft" },
  { id: "merged", title: "Merged" },
  { id: "closed", title: "Closed" },
];

export const PULL_REQUEST_LIST_GROUPINGS: { id: PullRequestListGrouping; title: string }[] = [
  { id: "status", title: "Status" },
  { id: "repository", title: "Repository" },
  { id: "none", title: "None" },
];

export const PULL_REQUEST_LIST_ORDERS: {
  id: PullRequestListOrder;
  title: string;
  /** Needs diff stats and update times, which only the desktop shell reads. */
  needsStats?: boolean;
}[] = [
  { id: "updated", title: "Last updated", needsStats: true },
  { id: "status", title: "Status" },
  { id: "number", title: "Number" },
  { id: "title", title: "Title" },
  { id: "repository", title: "Repository" },
  { id: "size", title: "Diff size", needsStats: true },
];

/** Status order, the list's sort before update times existed. */
const FALLBACK_ORDER = { order: "status", direction: "ascending" } as const;

/** Whether an order can apply now: update-time and size orders wait for stats. */
export function orderAvailable(order: PullRequestListOrder, statsAvailable: boolean): boolean {
  return (
    statsAvailable || !PULL_REQUEST_LIST_ORDERS.find((option) => option.id === order)?.needsStats
  );
}

/** The order the list applies: one that needs stats falls back to status while they're unavailable. */
export function effectiveOrder(
  display: Pick<PullRequestListDisplay, "order" | "direction">,
  statsAvailable: boolean,
): { order: PullRequestListOrder; direction: PullRequestListDirection } {
  return orderAvailable(display.order, statsAvailable)
    ? { order: display.order, direction: display.direction }
    : FALLBACK_ORDER;
}

/** Choosing a column's order again flips it; a new order starts in its natural direction. */
export function nextOrder(
  current: Pick<PullRequestListDisplay, "order" | "direction">,
  order: PullRequestListOrder,
): { order: PullRequestListOrder; direction: PullRequestListDirection } {
  if (current.order !== order) return { order, direction: NATURAL_DIRECTIONS[order] };
  return { order, direction: current.direction === "ascending" ? "descending" : "ascending" };
}

/** A change to the list's view: a status filter, grouping, order, or the default display. */
export type PullRequestListViewChange =
  | { kind: "filter"; filter: PullRequestListFilter }
  | { kind: "grouping"; grouping: PullRequestListGrouping }
  | { kind: "order"; order: PullRequestListOrder }
  | { kind: "reset" };

export interface PullRequestListViewCommand {
  id: string;
  /** ⌘K's label, naming what it does. */
  label: string;
  keywords: readonly string[];
  change: PullRequestListViewChange;
}

/** What the view commands need to know about the list as it shows now. */
export interface PullRequestListViewFacts {
  filter: PullRequestListFilter;
  counts: Record<PullRequestListFilter, number>;
  display: PullRequestListDisplay;
  /** The order applied, which may be a fallback for the chosen one. */
  order: PullRequestListOrder;
  statsAvailable: boolean;
  /** The display differs from the default. */
  modified: boolean;
}

/** One of the list's view settings in ⌘K: its values, each the change it makes. */
export interface PullRequestListViewSetting {
  id: "filter" | "grouping" | "order";
  label: string;
  keywords: readonly string[];
  current: string;
  choices: readonly { value: string; label: string; change: PullRequestListViewChange }[];
}

/** ⌘K's names for the status pills and the Display menu's sections. */
export const PULL_REQUEST_VIEW_SETTING_LABELS = {
  filter: "Pull request filter",
  grouping: "Group pull requests by",
  order: "Order pull requests by",
} as const;

/**
 * The status pills and the Display menu's grouping and order as ⌘K settings, each listing
 * the values the list shows a control for, and the reset as a command while it applies.
 */
export function pullRequestListViewSettings(facts: PullRequestListViewFacts): {
  settings: PullRequestListViewSetting[];
  reset: PullRequestListViewCommand | null;
} {
  const settings: PullRequestListViewSetting[] = [
    {
      id: "filter",
      label: PULL_REQUEST_VIEW_SETTING_LABELS.filter,
      keywords: ["pull requests", "filter", "status", "show"],
      current: facts.filter,
      choices: PULL_REQUEST_LIST_FILTERS.filter(
        (option) =>
          option.id === "all" || option.id === facts.filter || facts.counts[option.id] > 0,
      ).map((option) => ({
        value: option.id,
        label: option.title,
        change: { kind: "filter", filter: option.id },
      })),
    },
    {
      id: "grouping",
      label: PULL_REQUEST_VIEW_SETTING_LABELS.grouping,
      keywords: ["pull requests", "group", "display"],
      current: facts.display.grouping,
      choices: PULL_REQUEST_LIST_GROUPINGS.map((option) => ({
        value: option.id,
        label: option.title,
        change: { kind: "grouping", grouping: option.id },
      })),
    },
    {
      id: "order",
      label: PULL_REQUEST_VIEW_SETTING_LABELS.order,
      keywords: ["pull requests", "sort", "order", "display"],
      current: facts.order,
      choices: PULL_REQUEST_LIST_ORDERS.filter((option) =>
        orderAvailable(option.id, facts.statsAvailable),
      ).map((option) => ({
        value: option.id,
        label: option.title,
        change: { kind: "order", order: option.id },
      })),
    },
  ];
  const reset: PullRequestListViewCommand | null = facts.modified
    ? {
        id: "reset-display",
        label: "Reset the pull request display",
        keywords: ["pull requests", "display", "default", "reset", "group", "order"],
        change: { kind: "reset" },
      }
    : null;
  return { settings, reset };
}

/** The display after `change`; a filter change leaves the display as it is. */
export function displayAfter(
  display: PullRequestListDisplay,
  change: PullRequestListViewChange,
  defaults: PullRequestListDisplay,
): PullRequestListDisplay {
  switch (change.kind) {
    case "grouping":
      return { ...display, grouping: change.grouping };
    case "order":
      return { ...display, order: change.order, direction: NATURAL_DIRECTIONS[change.order] };
    case "reset":
      return defaults;
    case "filter":
      return display;
  }
}

const STATE_ORDER: PullRequestListState[] = ["open", "draft", "merged", "closed", "unknown"];
const STATE_TITLES: Record<PullRequestListState, string> = {
  open: "Open",
  draft: "Draft",
  merged: "Merged",
  closed: "Closed",
  unknown: "Status unknown",
};

export function pullRequestStateTitle(state: PullRequestListState): string {
  return STATE_TITLES[state];
}

/** Lifecycle state from a provider's `OPEN | MERGED | CLOSED` and its draft flag. */
export function pullRequestListState(
  summary: Pick<PullRequestAssociation, "state" | "is_draft">,
): PullRequestListState {
  switch (summary.state?.toUpperCase()) {
    case "OPEN":
      return summary.is_draft ? "draft" : "open";
    case "MERGED":
      return "merged";
    case "CLOSED":
      return "closed";
    default:
      return "unknown";
  }
}

export function isFinishedState(state: PullRequestListState): boolean {
  return state === "merged" || state === "closed";
}

/** A list row as its host last answered it, for choosing who reads its stats. */
export interface PullRequestListStatsRow {
  association: Pick<PullRequestAssociation, "url" | "repository" | "number" | "state" | "is_draft">;
  offline: boolean;
  pr:
    (Pick<PullRequestAssociation, "state" | "is_draft"> & { head_sha?: string | null }) | undefined;
}

/** The GitHub rows the session's host reads stats for itself, as its own account. */
export function hostListStatsTargets(
  rows: readonly PullRequestListStatsRow[],
  sessionId: string,
): PullRequestListStatsTarget[] {
  return rows.flatMap(({ association, offline, pr }) => {
    const link = parseCodeHostPullRequestUrl(association.url);
    if (offline || link?.provider !== "github") return [];
    return [
      {
        url: association.url,
        source: { kind: "host" as const, sessionId, host: link.host },
        repository: association.repository,
        number: association.number,
        headSha: pr?.head_sha ?? undefined,
        open: !isFinishedState(pullRequestListState(pr ?? association)),
      },
    ];
  });
}

/** The strongest review outcome: requested changes outrank approvals, which outrank waiting. */
export function pullRequestReviewStatus(
  reviewers: readonly PullRequestReviewer[] | undefined,
): PullRequestReviewStatus | undefined {
  if (!reviewers?.length) return undefined;
  if (reviewers.some((reviewer) => reviewer.state === "CHANGES_REQUESTED")) {
    return "changes_requested";
  }
  if (reviewers.some((reviewer) => reviewer.state === "APPROVED")) return "approved";
  if (reviewers.some((reviewer) => reviewer.state === "PENDING")) return "review_requested";
  return undefined;
}

export const REVIEW_STATUS_TITLES: Record<PullRequestReviewStatus, string> = {
  changes_requested: "Changes requested",
  approved: "Approved",
  review_requested: "Review requested",
};

/** Whether `pr` waits on a review from `login` itself, not only from a team it's on. */
export function reviewRequestedFrom(
  pr: Pick<PullRequest, "reviewers">,
  login: string | null | undefined,
): boolean {
  if (!login) return false;
  const wanted = login.toLowerCase();
  return (pr.reviewers ?? []).some(
    (reviewer) =>
      reviewer.kind === "user" &&
      reviewer.state === "PENDING" &&
      reviewer.login.toLowerCase() === wanted,
  );
}

/**
 * An open, non-draft PR needs the signed-in account when it asks that account for a
 * review, or when the account wrote it and a reviewer requested changes or a check failed.
 * Where an integration's merge readiness is known, it decides the author's part instead: a
 * blocking failure, a review to answer, a reviewer to find, or a PR ready to merge.
 */
export function needsViewer(
  pr: Pick<PullRequest, "state" | "is_draft" | "author" | "reviewers" | "checks">,
  viewer: string | null | undefined,
  mergeReadiness?: MergeReadinessStatus,
): boolean {
  if (!viewer || pr.state.toUpperCase() !== "OPEN" || pr.is_draft) return false;
  const login = viewer.toLowerCase();
  if (reviewRequestedFrom(pr, viewer)) return true;
  if (pr.author?.toLowerCase() !== login) return false;
  if (mergeReadiness !== undefined) return mergeReadinessNeedsAttention(mergeReadiness);
  return pullRequestReviewStatus(pr.reviewers) === "changes_requested" || pr.checks.failing > 0;
}

/**
 * The merge readiness a row shows, sorts and groups by. Off or unsupported leaves the row to
 * GitHub status, and a confirmed merge or close beats a readiness read before it.
 */
export function effectiveMergeReadiness(
  view: MergeReadinessView | undefined,
  state: PullRequestListState,
): MergeReadinessView | undefined {
  if (!view || view.kind === "off" || isFinishedState(state)) return undefined;
  return view;
}

/** Where a shown readiness sorts: a failed read as Unknown, one still checking as Pending. */
function mergeReadinessOrderStatus(view: MergeReadinessView): MergeReadinessStatus {
  if (view.kind === "unknown") return "unknown";
  if (view.kind === "checking") return "pending";
  return knownMergeReadinessStatus(view) ?? "unknown";
}

/**
 * Builds a row; `pr` is the live PR for this association, or undefined while it loads,
 * read at `liveDispatchedAt`. State and CI come from the newest of the live PR, the
 * stats batch and the stored association. Only a known merge readiness decides
 * "Needs you"; a failed read leaves it to GitHub.
 */
export function pullRequestListRow(
  association: PullRequestAssociation,
  pr: PullRequest | undefined,
  viewer: string | null | undefined,
  stats?: PullRequestListStats,
  readinessView?: MergeReadinessView,
  liveDispatchedAt = 0,
): PullRequestListRow {
  const status = resolvePullRequestListStatus({
    live: pr ? { pr, dispatchedAt: liveDispatchedAt } : undefined,
    batch: stats,
    stored: association,
  });
  const state = pullRequestListState({ state: status.state, is_draft: status.isDraft });
  const mergeReadinessView = effectiveMergeReadiness(readinessView, state);
  const title = (pr?.title ?? association.title)?.trim() || undefined;
  return {
    url: association.url,
    number: association.number,
    title,
    repository: association.repository,
    state,
    author: pr?.author ?? null,
    headRef: pr?.head_ref ?? null,
    labels: pr?.labels?.map((label) => label.name) ?? [],
    checks: pr?.checks,
    ci: status.ci,
    stateRead: status.lifecycleSource === "live" || status.lifecycleSource === "batch",
    checksIncomplete: !!pr && (!!pr.checks.partial || pr.checks_supported === false),
    review: pullRequestReviewStatus(pr?.reviewers),
    reviewers: pr?.reviewers ?? [],
    stats,
    needsYou: !!pr && needsViewer(pr, viewer, knownMergeReadinessStatus(mergeReadinessView)),
    inferred: association.relationship === "inferred",
    ...(mergeReadinessView === undefined
      ? {}
      : { mergeReadinessView, mergeReadiness: mergeReadinessOrderStatus(mergeReadinessView) }),
  };
}

export function matchesFilter(
  row: PullRequestListRow,
  filter: PullRequestListFilter,
  { allInProgress = false }: { allInProgress?: boolean } = {},
): boolean {
  if (filter === "all") return !allInProgress || !isFinishedState(row.state);
  if (filter === "attention") return row.needsYou;
  return row.state === filter;
}

export function filterCounts(
  rows: readonly PullRequestListRow[],
  options: { allInProgress?: boolean } = {},
): Record<PullRequestListFilter, number> {
  const counts: Record<PullRequestListFilter, number> = {
    all: 0,
    attention: 0,
    open: 0,
    draft: 0,
    merged: 0,
    closed: 0,
  };
  for (const row of rows) {
    for (const { id } of PULL_REQUEST_LIST_FILTERS) {
      if (matchesFilter(row, id, options)) counts[id] += 1;
    }
  }
  return counts;
}

/**
 * Every whitespace-separated term must match the number, title, repository, branch,
 * author, or a label, so `#42`, `app`, and `ada retry` all narrow the list.
 */
export function matchesQuery(row: PullRequestListRow, query: string): boolean {
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (terms.length === 0) return true;
  const searchable = [
    `#${row.number}`,
    row.title,
    row.repository,
    row.headRef,
    row.author,
    ...row.labels,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return terms.every((term) => searchable.includes(term));
}

export function sortRows(
  rows: readonly PullRequestListRow[],
  order: PullRequestListOrder,
  direction: PullRequestListDirection = NATURAL_DIRECTIONS[order],
): PullRequestListRow[] {
  const sign = direction === "ascending" ? 1 : -1;
  const readiness = readinessInEffect(rows);
  return [...rows].sort((left, right) => {
    const leftKey = sortKey(order, left, readiness);
    const rightKey = sortKey(order, right, readiness);
    // Rows without a value (no title yet, no stats) sit last in either direction.
    if (leftKey === undefined || rightKey === undefined) {
      if (leftKey !== rightKey) return leftKey === undefined ? 1 : -1;
    } else {
      const compared =
        typeof leftKey === "string" && typeof rightKey === "string"
          ? leftKey.localeCompare(rightKey, undefined, { sensitivity: "base" })
          : (leftKey as number) - (rightKey as number);
      if (compared) return sign * compared;
    }
    // Ties keep the newest number first so equal keys never shuffle between polls.
    return right.number - left.number || left.url.localeCompare(right.url);
  });
}

function sortKey(
  order: PullRequestListOrder,
  row: PullRequestListRow,
  readiness: boolean,
): string | number | undefined {
  switch (order) {
    case "status":
      return statusRank(row, readiness);
    case "number":
      return row.number;
    case "title":
      return row.title;
    case "repository":
      return row.repository;
    case "updated":
      return row.stats?.updatedAt;
    case "size": {
      const { additions, deletions } = row.stats ?? {};
      return additions === undefined || deletions === undefined ? undefined : additions + deletions;
    }
  }
}

/** Merge readiness orders the list once any row has it; otherwise the lifecycle does. */
function readinessInEffect(rows: readonly PullRequestListRow[]): boolean {
  return rows.some((row) => row.mergeReadiness !== undefined);
}

/** The readiness status a row sorts and groups under; rows without one go by their lifecycle. */
function readinessOf(row: PullRequestListRow): MergeReadinessStatus {
  return row.mergeReadiness ?? row.state;
}

/** The lifecycle a readiness status belongs to, for its group's icon and folding. */
function readinessState(status: MergeReadinessStatus): PullRequestListState {
  return status === "draft" || status === "merged" || status === "closed" ? status : "open";
}

/** Rows that need you first within their status, then the lifecycle or readiness order. */
function statusRank(row: PullRequestListRow, readiness: boolean): number {
  const rank = readiness
    ? MERGE_READINESS_STATUSES.indexOf(readinessOf(row)) * 2
    : STATE_ORDER.indexOf(row.state) * 2;
  return row.needsYou ? rank : rank + 1;
}

export interface PullRequestListGroup {
  id: string;
  title: string;
  /** Set when grouping by status. */
  state?: PullRequestListState;
  rows: PullRequestListRow[];
}

export function groupRows(
  rows: readonly PullRequestListRow[],
  grouping: PullRequestListGrouping,
): PullRequestListGroup[] {
  if (grouping === "none") return [{ id: "all", title: "All", rows: [...rows] }];
  if (grouping === "status" && readinessInEffect(rows)) return groupByReadiness(rows);
  const groups = new Map<string, PullRequestListGroup>();
  for (const row of rows) {
    const id = grouping === "status" ? row.state : row.repository;
    let group = groups.get(id);
    if (!group) {
      group =
        grouping === "status"
          ? { id, title: pullRequestStateTitle(row.state), state: row.state, rows: [] }
          : { id, title: row.repository, rows: [] };
      groups.set(id, group);
    }
    group.rows.push(row);
  }
  const values = [...groups.values()];
  return grouping === "status"
    ? values.sort(
        (left, right) => STATE_ORDER.indexOf(left.state!) - STATE_ORDER.indexOf(right.state!),
      )
    : values.sort((left, right) => left.title.localeCompare(right.title));
}

function groupByReadiness(rows: readonly PullRequestListRow[]): PullRequestListGroup[] {
  const groups = new Map<MergeReadinessStatus, PullRequestListGroup>();
  for (const row of rows) {
    const status = readinessOf(row);
    let group = groups.get(status);
    if (!group) {
      group = { id: status, title: pullRequestStateTitle(row.state), state: row.state, rows: [] };
      groups.set(status, group);
    }
    // Any row with merge readiness names the group by it, e.g. "Merge status unknown".
    if (row.mergeReadiness !== undefined) {
      group.title = mergeReadinessStatusLabel(status);
      group.state = readinessState(status);
    }
    group.rows.push(row);
  }
  return [...groups.entries()]
    .sort(
      ([left], [right]) =>
        MERGE_READINESS_STATUSES.indexOf(left) - MERGE_READINESS_STATUSES.indexOf(right),
    )
    .map(([, group]) => group);
}

export function visibleGroups(
  rows: readonly PullRequestListRow[],
  view: {
    query: string;
    filter: PullRequestListFilter;
    grouping: PullRequestListGrouping;
    order: PullRequestListOrder;
    direction?: PullRequestListDirection;
  },
): PullRequestListGroup[] {
  const matching = rows.filter(
    (row) => matchesFilter(row, view.filter) && matchesQuery(row, view.query),
  );
  return groupRows(sortRows(matching, view.order, view.direction), view.grouping);
}

/** Short repository names, shown only when the rows span several repositories. */
export function repositoryLabels(
  rows: readonly PullRequestListRow[],
): Map<string, string> | undefined {
  const repositories = new Set(rows.map((row) => row.repository));
  if (repositories.size <= 1) return undefined;
  const byName = new Map<string, string[]>();
  for (const repository of repositories) {
    const name = repository.split("/").at(-1) ?? repository;
    byName.set(name, [...(byName.get(name) ?? []), repository]);
  }
  const labels = new Map<string, string>();
  for (const [name, owners] of byName) {
    // Two owners with the same repository name keep their owner prefix.
    for (const repository of owners)
      labels.set(repository, owners.length === 1 ? name : repository);
  }
  return labels;
}

export interface CheckResult {
  tone: "passing" | "failing" | "pending" | "cancelled" | "skipped" | "none";
  /** The number beside the glyph: failing, running, or cancelled checks. */
  count?: number;
  /** Full sentence for the tooltip and screen readers. */
  description: string;
}

const checksWord = (count: number) => (count === 1 ? "check" : "checks");

/**
 * The compact CI result, worst first: failures, running checks, cancellations, then a
 * pass. Only checks that passed or were skipped count as a pass.
 */
export function checkResult(checks: PullRequestChecks, incomplete = false): CheckResult {
  const { total, passing, failing, pending } = checks;
  const cancelled = checks.cancelled ?? 0;
  const skipped = checks.skipped ?? 0;
  if (total === 0) {
    return {
      tone: "none",
      description: incomplete ? "Checks couldn’t be loaded" : "No checks reported",
    };
  }
  // Incomplete counts are a lower bound, so they never claim everything passed.
  const atLeast = incomplete ? "At least " : "";
  const passed = `${passing} of ${total} passed`;
  if (failing > 0) {
    return {
      tone: "failing",
      count: failing,
      description: `${atLeast}${failing} failing ${checksWord(failing)} · ${passed}`,
    };
  }
  if (pending > 0) {
    return {
      tone: "pending",
      count: pending,
      description: `${atLeast}${pending} running · ${passed}`,
    };
  }
  if (cancelled > 0) {
    return {
      tone: "cancelled",
      count: cancelled,
      description: `${atLeast}${cancelled} cancelled ${checksWord(cancelled)} · ${passed}`,
    };
  }
  // Counts that don't add up to the total leave some checks unaccounted for.
  if (incomplete || passing + skipped < total) {
    return { tone: "pending", description: `Some checks unavailable · ${passed}` };
  }
  if (passing === 0) {
    return { tone: "skipped", description: `All ${total} ${checksWord(total)} skipped` };
  }
  return {
    tone: "passing",
    description:
      skipped > 0
        ? `All checks passed · ${passing} passed, ${skipped} skipped`
        : `All checks passed · ${passed}`,
  };
}

/** The signed-in account the provider CLI acts as, when the host reports it. */
export function viewerLogin(auth: {
  selected_account: string | null;
  accounts: { login: string; active: boolean }[] | null;
}): string | null {
  return auth.selected_account ?? auth.accounts?.find((account) => account.active)?.login ?? null;
}

/** Why a PR can't be linked yet while the session's sandbox is asleep or resuming. */
export function sandboxAsleepLinkHint(resuming: boolean): string {
  return resuming
    ? "The sandbox is resuming. You can link a PR once it’s back."
    : "To link a PR, resume the session’s sandbox.";
}

/** When the host last refreshed a stored PR, or "" if it never has. */
export function storedPullRequestLastSync(
  pr: Pick<PullRequestAssociation, "state_checked_at">,
): string {
  return pr.state_checked_at ? `Last sync time: ${absoluteTime(pr.state_checked_at * 1000)}.` : "";
}

/** Why a stored PR shows no live details, with when the host last refreshed it. */
export function storedPullRequestHint(
  pr: Pick<PullRequestAssociation, "state_checked_at">,
  sandboxAsleep: boolean,
): string {
  const lastSync = storedPullRequestLastSync(pr);
  return (
    (lastSync ? `${lastSync} ` : "") +
    (sandboxAsleep
      ? "To load the PR details, resume the session’s sandbox."
      : "To load the PR details, please reconnect the session’s host.")
  );
}
