// Closing or reopening a pull request from its comment box, optionally with a comment: the
// comment posts first and the state changes only after GitHub took it. One chain runs per
// workspace user and pull request; what it came to stays shown to that user until dismissed.

import { useSyncExternalStore } from "react";

import type { PullRequestStateChangeOutcome } from "@/newPullRequestPage/hooks/usePullRequests";
import {
  pullRequestIdentity,
  registerUnconfirmedStateChanges,
  type StateChangeOperation,
  unconfirmedStateChangesChanged,
} from "@/lib/pullRequestStateHolds";
import type { PullRequestState } from "@/lib/pullRequestWriteAnswers";
import type { HostChangeRoute } from "./pullRequestPageRoute";

/** What a chain came to. */
export type StateChainOutcome =
  | { kind: "changed"; state: PullRequestState }
  /** The comment didn't post, so nothing else was tried; the box shows the comment's error. */
  | { kind: "comment-failed" }
  /**
   * Not run: the box went away, what the click captured changed, or another change to the
   * PR's state was still being made when it was clicked (`busy`, nothing sent at all).
   */
  | { kind: "skipped"; reason: "disposed" | "changed" | "busy"; commentPosted: boolean }
  /** GitHub or main refused the state change: nothing changed, it may be tried again. */
  | { kind: "refused"; message: string; commentPosted: boolean }
  /** GitHub may have changed the state; the PR is read again and nothing is retried. */
  | { kind: "unknown"; commentPosted: boolean }
  /** The workspace user changed while the state change was being made. */
  | { kind: "abandoned"; commentPosted: boolean };

export interface StateChainSteps {
  /** Post the box's comment first. */
  withComment: boolean;
  /** Resolves true once GitHub took the comment. */
  post: () => Promise<boolean>;
  /** Whether the box that started the chain is gone; checked after every wait. */
  disposed: () => boolean;
  /** Whether everything the click captured (PR, accounts, route, the offer) still holds. */
  stillCurrent: () => boolean;
  change: () => Promise<PullRequestStateChangeOutcome>;
}

/** Run a chain: the comment, if any, then the state change, each only if all still holds. */
export async function changeStateWithComment(steps: StateChainSteps): Promise<StateChainOutcome> {
  let commentPosted = false;
  if (steps.withComment) {
    const posted = await steps.post();
    if (!posted) return { kind: "comment-failed" };
    commentPosted = true;
  }
  if (steps.disposed()) return { kind: "skipped", reason: "disposed", commentPosted };
  if (!steps.stillCurrent()) return { kind: "skipped", reason: "changed", commentPosted };
  const outcome = await steps.change();
  switch (outcome.kind) {
    case "changed":
      return outcome;
    case "refused":
      return { kind: "refused", message: outcome.message, commentPosted };
    case "unknown":
      return { kind: "unknown", commentPosted };
    case "abandoned":
      return { kind: "abandoned", commentPosted };
  }
}

/** What a chain left for its user to see, until they dismiss it or a new chain starts. */
export interface StateChainNotice {
  operation: StateChangeOperation;
  /** The send that left it: whatever started that send knows it by this. */
  attempt: string;
  outcome: Exclude<StateChainOutcome, { kind: "changed" } | { kind: "comment-failed" }>;
  /** Who the first click acted as, which a retry must still be. */
  boundTo: {
    authGeneration: number;
    actor: { login: string; generation: number };
    /** The session's host the change was sent to. */
    host: HostChangeRoute;
  };
}

export interface StateChainState {
  /** Which change runs, for every surface showing the PR to say so; null while none does. */
  running: StateChangeOperation | null;
  notice: StateChainNotice | null;
}

const IDLE: StateChainState = { running: null, notice: null };
const chains = new Map<string, StateChainState>();
const listeners = new Set<() => void>();

/** One chain per workspace user and pull request, shared by every box showing that PR. */
export function stateChainKey(owner: string, prUrl: string): string {
  // The same PR opened by a link in another case shares its chain, notice and hold.
  return `${owner}\n${pullRequestIdentity(prUrl)}`;
}

export function stateChainState(key: string): StateChainState {
  return chains.get(key) ?? IDLE;
}

export function setStateChainState(key: string, next: StateChainState): void {
  if (!next.running && !next.notice) chains.delete(key);
  else chains.set(key, next);
  for (const listener of listeners) listener();
  unconfirmedStateChangesChanged();
}

// A close or reopen GitHub may have made holds the PR's merge, auto-merge and queue changes
// too, until Check Again's fresh read clears its notice.
registerUnconfirmedStateChanges(function* unknownStateChains() {
  for (const [key, state] of chains) {
    if (state.notice?.outcome.kind !== "unknown") continue;
    const split = key.indexOf("\n");
    yield { owner: key.slice(0, split), identity: key.slice(split + 1) };
  }
});

/** Every chain's notice now, with its key, for a list of what's left to see. */
export function stateChainNotices(): { key: string; notice: StateChainNotice }[] {
  return [...chains].flatMap(([key, state]) =>
    state.notice ? [{ key, notice: state.notice }] : [],
  );
}

/** Calls `listener` whenever any chain changes. */
export function subscribeStateChains(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The chain for `key`, re-read whenever any chain changes. */
export function useStateChainState(key: string): StateChainState {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => stateChainState(key),
    () => IDLE,
  );
}

/** What the notice for `outcome` says; null where the box or the pill already shows it. */
export function stateChainMessage(notice: StateChainNotice): string {
  const verb = notice.operation === "pull_request_close" ? "closed" : "reopened";
  const { outcome } = notice;
  const posted = outcome.commentPosted ? "Your comment was posted. " : "";
  switch (outcome.kind) {
    case "refused":
      return `${posted}The pull request wasn't ${verb}: ${outcome.message}`;
    case "unknown":
      return `${posted}GitHub may have ${verb} the pull request. Check it on GitHub before trying again.`;
    case "abandoned":
      return `${posted}The workspace account changed before the pull request was ${verb}. Check it on GitHub.`;
    case "skipped":
      return outcome.reason === "busy"
        ? `Another change to this pull request is still being made. Try again once it finishes.`
        : `${posted}The pull request wasn't ${verb}, because the account or the pull request changed. Check it on GitHub.`;
  }
}

/** What a notice offers, for its buttons and ⌘K rows alike. */
export type StateNoticeAction = "retry" | "check" | "open" | "dismiss";

export function stateNoticeActions(notice: StateChainNotice): StateNoticeAction[] {
  switch (notice.outcome.kind) {
    case "refused":
      return ["retry", "dismiss"];
    // Nothing more is sent until a fresh read; the notice can't be dismissed until then.
    case "unknown":
      return ["check", "open"];
    default:
      return ["open", "dismiss"];
  }
}

/** The label of a notice's action, as its button and its ⌘K row both show it. */
export function stateNoticeActionLabel(
  notice: StateChainNotice,
  action: StateNoticeAction,
  palette: boolean,
): string {
  const closing = notice.operation === "pull_request_close";
  switch (action) {
    case "retry":
      return palette ? (closing ? "Try Closing Again" : "Try Reopening Again") : "Try Again";
    case "check":
      return palette ? "Check Pull Request Again" : "Check Again";
    case "open":
      return palette ? "Open Pull Request on GitHub" : "Open on GitHub";
    case "dismiss":
      return palette ? (closing ? "Dismiss Close Notice" : "Dismiss Reopen Notice") : "Dismiss";
  }
}

/** Forget every chain, for tests. */
export function resetStateChainsForTests(): void {
  chains.clear();
  for (const listener of listeners) listener();
  unconfirmedStateChangesChanged();
}
