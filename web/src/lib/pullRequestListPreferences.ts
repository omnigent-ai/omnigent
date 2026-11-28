// Persisted, per-device choice of how the Pull Requests list groups and orders
// its rows (the list's Display menu), so it survives reloads and session switches.

import { useSyncExternalStore } from "react";

const STORAGE_KEY = "omnigent:pull-request-list-display";

export type PullRequestListGrouping = "status" | "repository" | "none";
export type PullRequestListOrder =
  "updated" | "status" | "number" | "title" | "repository" | "size";
export type PullRequestListDirection = "ascending" | "descending";

const GROUPINGS: readonly string[] = [
  "status",
  "repository",
  "none",
] satisfies PullRequestListGrouping[];
const ORDERS: readonly string[] = [
  "updated",
  "status",
  "number",
  "title",
  "repository",
  "size",
] satisfies PullRequestListOrder[];

/** Each order's first direction: newest, biggest, and highest number first; names A to Z. */
export const NATURAL_DIRECTIONS: Record<PullRequestListOrder, PullRequestListDirection> = {
  updated: "descending",
  status: "ascending",
  number: "descending",
  title: "ascending",
  repository: "ascending",
  size: "descending",
};

export interface PullRequestListDisplay {
  grouping: PullRequestListGrouping;
  order: PullRequestListOrder;
  direction: PullRequestListDirection;
}

export const DEFAULT_PULL_REQUEST_LIST_DISPLAY: PullRequestListDisplay = {
  grouping: "status",
  order: "updated",
  direction: "descending",
};

/** The stored display, validating each field so an unknown value falls back alone. */
export function readPullRequestListDisplay(): PullRequestListDisplay {
  if (typeof window === "undefined") return DEFAULT_PULL_REQUEST_LIST_DISPLAY;
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "null");
    if (typeof parsed !== "object" || parsed === null) return DEFAULT_PULL_REQUEST_LIST_DISPLAY;
    const stored = parsed as Record<string, unknown>;
    const order = ORDERS.includes(stored.order as string)
      ? (stored.order as PullRequestListOrder)
      : DEFAULT_PULL_REQUEST_LIST_DISPLAY.order;
    return {
      grouping: GROUPINGS.includes(stored.grouping as string)
        ? (stored.grouping as PullRequestListGrouping)
        : DEFAULT_PULL_REQUEST_LIST_DISPLAY.grouping,
      order,
      // Displays saved before directions existed sort the way they always did.
      direction:
        stored.direction === "ascending" || stored.direction === "descending"
          ? stored.direction
          : NATURAL_DIRECTIONS[order],
    };
  } catch {
    return DEFAULT_PULL_REQUEST_LIST_DISPLAY;
  }
}

/** Persist the display. Swallows quota and access errors. */
export function writePullRequestListDisplay(display: PullRequestListDisplay): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(display));
  } catch {
    // A local view preference; losing it is harmless.
  }
}

const INCLUDE_SUB_AGENTS_KEY = "omnigent:pull-request-list-include-sub-agents";

/** Whether a session's list shows its sub-agents' pull requests; on until the user turns it off. */
export function readIncludeSubAgentPullRequests(): boolean {
  if (typeof window === "undefined") return true;
  try {
    return window.localStorage.getItem(INCLUDE_SUB_AGENTS_KEY) !== "false";
  } catch {
    return true;
  }
}

const includeSubAgentListeners = new Set<() => void>();

/** Persist the choice and tell every list on screen. Swallows quota and access errors. */
export function writeIncludeSubAgentPullRequests(include: boolean): void {
  try {
    window.localStorage.setItem(INCLUDE_SUB_AGENTS_KEY, String(include));
  } catch {
    // A local view preference; losing it is harmless.
  }
  for (const listener of includeSubAgentListeners) listener();
}

/** The device's sub-agent choice, kept current across every list on screen. */
export function useIncludeSubAgentPullRequests(): boolean {
  return useSyncExternalStore(
    (onChange) => {
      includeSubAgentListeners.add(onChange);
      return () => includeSubAgentListeners.delete(onChange);
    },
    readIncludeSubAgentPullRequests,
    () => true,
  );
}
