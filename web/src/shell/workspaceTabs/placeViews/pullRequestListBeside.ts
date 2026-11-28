// Whether a session's pull request list shows beside the pull request it opened. With one
// linked PR there's nothing else to open, so opening it leaves the list folded; with more,
// the list shows. Either way the toggle wins once used, for the rest of the session.

import { create } from "zustand";

import type { PullRequestsTarget, ShowPullRequestsOptions } from "@/shell/FileViewerContext";

interface ListBeside {
  /** Shown or folded right now. */
  shown: boolean;
  /** The person chose it with the toggle; an open no longer decides it. */
  chosen: boolean;
}

const useListBeside = create<Record<string, ListBeside>>(() => ({}));

/** The one linked PR, or null with none or several. */
export function onlyLinkedPullRequest(linkedUrls: readonly string[]): string | null {
  return linkedUrls.length === 1 ? linkedUrls[0]! : null;
}

/** Which pull requests a session shows: its own only, or with ones its sub-agents linked. */
export interface SessionPullRequestsScope {
  /** Some of the PRs came from sub-agents, so the session's list is what shows them. */
  includesSubAgent?: boolean;
}

/**
 * What showing a session's pull requests opens: its one PR on Overview, beside the list,
 * where it has just one of its own; otherwise all of them, sub-agents' included, in the list.
 */
export function sessionPullRequestsTarget(
  linkedUrls: readonly string[],
  scope: SessionPullRequestsScope = {},
): PullRequestsTarget {
  const only = scope.includesSubAgent ? null : onlyLinkedPullRequest(linkedUrls);
  return only ? { kind: "pull", url: only, list: true, overview: true } : { kind: "overview" };
}

/**
 * Whether the list shows beside a PR opened now: the person's choice once made, else folded
 * for a session's only PR and shown with more. An unknown count keeps the list shown.
 */
export function listShownOnOpen(
  linkedCount: number | undefined,
  current: ListBeside | undefined,
): boolean {
  if (current?.chosen) return current.shown;
  return linkedCount !== 1;
}

/**
 * A pull request is opening beside `sessionId`'s list: settle whether the list shows. Only an
 * open decides it, so a PR linked later never unfolds a list on its own.
 */
export function pullRequestOpening(sessionId: string, linkedCount: number | undefined): void {
  const current = useListBeside.getState()[sessionId];
  const shown = listShownOnOpen(linkedCount, current);
  if (current?.shown === shown) return;
  useListBeside.setState({ [sessionId]: { shown, chosen: current?.chosen ?? false } });
}

/** What {@link showSessionPullRequests} shows: the session's target, or the overview without one. */
export function sessionPullRequestsShowTarget(
  sessionId: string | null,
  linkedUrls: readonly string[],
  scope: SessionPullRequestsScope = {},
): PullRequestsTarget {
  return sessionId ? sessionPullRequestsTarget(linkedUrls, scope) : { kind: "overview" };
}

/**
 * Show a session's pull requests from outside its tabs, such as the composer's PR chip: its one
 * PR on Overview with the list folded, or all of them. Choosing a PR in its list decides alike.
 */
export function showSessionPullRequests(
  show: (target: PullRequestsTarget, options: ShowPullRequestsOptions) => void,
  sessionId: string | null,
  linkedUrls: readonly string[],
  options: ShowPullRequestsOptions,
  scope: SessionPullRequestsScope = {},
): void {
  const target = sessionPullRequestsShowTarget(sessionId, linkedUrls, scope);
  if (sessionId && target.kind === "pull") pullRequestOpening(sessionId, linkedUrls.length);
  show(target, options);
}

/** The person showed or folded the list with its toggle; later opens keep that. */
export function pullRequestListChosen(sessionId: string, shown: boolean): void {
  useListBeside.setState({ [sessionId]: { shown, chosen: true } });
}

/** Whether `sessionId`'s list shows beside its pull request right now, outside a render. */
export function pullRequestListShownNow(sessionId: string): boolean {
  return useListBeside.getState()[sessionId]?.shown ?? true;
}

/** Whether `sessionId`'s list shows beside its pull request; shown until an open says otherwise. */
export function usePullRequestListShown(sessionId: string): boolean {
  return useListBeside((state) => state[sessionId]?.shown ?? true);
}

export function resetPullRequestListBesideForTests(): void {
  useListBeside.setState({}, true);
}
