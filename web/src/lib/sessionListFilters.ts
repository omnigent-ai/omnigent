// Narrows the sidebar's session list by each session's linked pull request, status, harness
// and project. Kept apart from the owner slice (All, Mine, Shared, Archived), which they
// further narrow.

import type { Conversation } from "@/hooks/useConversations";
import { NATIVE_CODING_AGENTS, nativeCodingAgentForSession } from "@/lib/nativeCodingAgents";
import { PROJECT_LABEL_KEY } from "@/lib/sessionListCache";

/** Which sessions to keep by their pull request. */
export type SessionPullRequestFilter = "all" | "open" | "draft" | "closed" | "none";

/** The menu's order. */
export const SESSION_PULL_REQUEST_FILTERS: readonly SessionPullRequestFilter[] = [
  "all",
  "open",
  "draft",
  "closed",
  "none",
];

export const DEFAULT_SESSION_PULL_REQUEST_FILTER: SessionPullRequestFilter = "all";

/** Each choice as the menu names it, in the owner filter's "… sessions" wording. */
export const SESSION_PULL_REQUEST_FILTER_LABELS: Record<SessionPullRequestFilter, string> = {
  all: "All sessions",
  open: "With an open pull request",
  draft: "With a draft pull request",
  closed: "With a merged or closed pull request",
  none: "Without a pull request",
};

type PullRequestFields = Pick<Conversation, "pull_request" | "pull_requests_synced">;

/**
 * Whether `row` stays under `filter`. Open includes drafts. A pull request the session list
 * names counts by its state, even before the server has imported all of the session's pull
 * requests: a known one is known. A state not fetched yet matches only All, and so does a
 * session with none named until the import says it has none.
 */
export function matchesSessionPullRequestFilter(
  row: PullRequestFields,
  filter: SessionPullRequestFilter,
): boolean {
  if (filter === "all") return true;
  const pullRequest = row.pull_request;
  if (!pullRequest) return filter === "none" && row.pull_requests_synced === true;
  const state = pullRequest.state?.toUpperCase();
  switch (filter) {
    case "open":
      return state === "OPEN";
    case "draft":
      return state === "OPEN" && pullRequest.is_draft === true;
    case "closed":
      return state === "MERGED" || state === "CLOSED";
    case "none":
      return false;
  }
}

/**
 * The rows that stay under `filter`, and the open session's row whatever its pull request, so
 * the session in view stays listed. Under All it's `rows` itself; any other filter returns a new
 * array, even when every row stays.
 */
export function applySessionPullRequestFilter<Row extends PullRequestFields & { id: string }>(
  rows: Row[],
  filter: SessionPullRequestFilter,
  openSessionId: string | null = null,
): Row[] {
  if (filter === "all") return rows;
  return rows.filter(
    (row) => row.id === openSessionId || matchesSessionPullRequestFilter(row, filter),
  );
}

/** Which sessions to keep by the status the sidebar's Status grouping gives them. */
export type SessionStatusFilter = "all" | "attention" | "unread" | "working" | "draft" | "done";

/** The menu's order, which is the Status grouping's. */
export const SESSION_STATUS_FILTERS: readonly SessionStatusFilter[] = [
  "all",
  "attention",
  "unread",
  "working",
  "draft",
  "done",
];

/** Each status as the Status grouping names its group, so a filter and a group always agree. */
export const SESSION_STATUS_FILTER_LABELS: Record<SessionStatusFilter, string> = {
  all: "Any status",
  attention: "Needs attention",
  unread: "Unread",
  working: "Working",
  draft: "Draft",
  done: "Done",
};

/** Which sessions to keep by project: any, those filed in none, or those in one, by id. */
export type SessionProjectFilter =
  { kind: "all" } | { kind: "none" } | { kind: "project"; id: string };

/** The sidebar's filters together; each narrows the owner slice further. */
export interface SessionListFilter {
  pullRequest: SessionPullRequestFilter;
  status: SessionStatusFilter;
  /**
   * A harness key, as {@link sessionHarnessKey} gives it: "" keeps sessions without a harness,
   * and null keeps every harness.
   */
  harness: string | null;
  project: SessionProjectFilter;
}

export const DEFAULT_SESSION_LIST_FILTER: SessionListFilter = {
  pullRequest: DEFAULT_SESSION_PULL_REQUEST_FILTER,
  status: "all",
  harness: null,
  project: { kind: "all" },
};

/** What the filters read about a session that its row doesn't say alone. */
export interface SessionListFilterContext {
  /** The session's Status group name, as the Status grouping gives it. */
  statusOf: (row: Conversation) => string;
  /** The session's harness key. */
  harnessOf: (row: Conversation) => string;
  /** The id of the project the session is filed in, or null when it's in none. */
  projectOf: (row: Conversation) => string | null;
}

function pickChoice<T extends string>(allowed: readonly T[], value: unknown, fallback: T): T {
  return allowed.find((choice) => choice === value) ?? fallback;
}

function parseProjectFilter(value: unknown): SessionProjectFilter {
  if (typeof value !== "object" || value === null) return { kind: "all" };
  const { kind, id } = value as Record<string, unknown>;
  if (kind === "none") return { kind: "none" };
  if (kind === "project" && typeof id === "string" && id !== "") return { kind: "project", id };
  return { kind: "all" };
}

/** A stored filter, each part falling back to All on its own. */
export function parseSessionListFilter(value: unknown): SessionListFilter {
  const stored =
    typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
  return {
    pullRequest: pickChoice(
      SESSION_PULL_REQUEST_FILTERS,
      stored.pullRequest,
      DEFAULT_SESSION_PULL_REQUEST_FILTER,
    ),
    status: pickChoice(SESSION_STATUS_FILTERS, stored.status, "all"),
    // "" is a choice of its own, sessions without a harness; only a missing one keeps them all.
    harness: typeof stored.harness === "string" ? stored.harness : null,
    project: parseProjectFilter(stored.project),
  };
}

/** How many of the filters narrow the list; the menu says it as "2 on". */
export function sessionListFilterCount(filter: SessionListFilter): number {
  return [
    filter.pullRequest !== "all",
    filter.status !== "all",
    filter.harness !== null,
    filter.project.kind !== "all",
  ].filter(Boolean).length;
}

/** Whether `row` stays under every one of the filters. */
export function matchesSessionListFilter(
  row: Conversation,
  filter: SessionListFilter,
  context: SessionListFilterContext,
): boolean {
  if (!matchesSessionPullRequestFilter(row, filter.pullRequest)) return false;
  if (
    filter.status !== "all" &&
    context.statusOf(row) !== SESSION_STATUS_FILTER_LABELS[filter.status]
  )
    return false;
  if (filter.harness !== null && context.harnessOf(row) !== filter.harness) return false;
  if (filter.project.kind === "none") return context.projectOf(row) === null;
  if (filter.project.kind === "project") return context.projectOf(row) === filter.project.id;
  return true;
}

/**
 * The rows that stay under the filters, and the open session's row whatever it is, so the
 * session in view stays listed. With no filter on it's `rows` itself.
 */
export function applySessionListFilter(
  rows: Conversation[],
  filter: SessionListFilter,
  context: SessionListFilterContext,
  openSessionId: string | null = null,
): Conversation[] {
  if (sessionListFilterCount(filter) === 0) return rows;
  return rows.filter(
    (row) => row.id === openSessionId || matchesSessionListFilter(row, filter, context),
  );
}

/** What the harness helpers read of a session. */
interface HarnessFields {
  harness?: string | null;
  labels?: Record<string, string>;
}

/**
 * The session's harness key: its native coding agent where it has one, as the Harness grouping
 * names it, else its harness id ("" when it has none).
 */
export function sessionHarnessKey(row: HarnessFields): string {
  const nativeAgent = nativeCodingAgentForSession(row);
  return nativeAgent ? `native:${nativeAgent.key}` : (row.harness ?? "");
}

/** A harness key's name: the native agent's, else the catalog's, else the id itself. */
export function sessionHarnessName(key: string, catalogLabels: Record<string, string>): string {
  if (key.startsWith("native:")) {
    const kind = key.slice("native:".length);
    const nativeAgent = NATIVE_CODING_AGENTS.find((agent) => agent.key === kind);
    if (nativeAgent) return nativeAgent.displayName;
  }
  return catalogLabels[key] ?? (key || "Unknown harness");
}

/**
 * The harnesses to offer, by name: those among `rows`, and the chosen one even when no loaded
 * session uses it, so a filter can always be cleared.
 */
export function sessionHarnessChoices(
  rows: readonly HarnessFields[],
  chosen: string | null,
  catalogLabels: Record<string, string>,
): { key: string; name: string }[] {
  const keys = new Set(rows.map(sessionHarnessKey));
  if (chosen !== null) keys.add(chosen);
  return [...keys]
    .map((key) => ({ key, name: sessionHarnessName(key, catalogLabels) }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * The id of the project `row` is filed in, read both ways the sidebar's folders read it: its
 * project id, or the legacy project label naming one of `projects`. Null when in none.
 */
export function sessionProjectId(
  row: { project_id?: string | null; labels?: Record<string, string> },
  projects: readonly { id: string; name: string }[],
): string | null {
  if (row.project_id) return row.project_id;
  const label = row.labels?.[PROJECT_LABEL_KEY];
  if (!label) return null;
  return projects.find((project) => project.name === label)?.id ?? null;
}
