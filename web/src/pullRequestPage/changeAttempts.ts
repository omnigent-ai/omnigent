// Each send of a change to a PR's state is one attempt, named on the notice it leaves. A page
// that owns its toasts' recovery records the attempts it started, by that scope, and the PRs a
// batch left alone, so it can list what's left to see. Where GitHub may have made a change, the
// page report it was sent with waits here for Check Again: only a fresh read showing that very
// change made reports it, and only once, to that attempt's page.

import { useSyncExternalStore } from "react";

import type { StateChangeOperation } from "@/lib/pullRequestStateHolds";
import type { PullRequestActionReport, PullRequestOwnAction } from "@/lib/pullRequestActionOrigin";
import type { AutoMergeKnown } from "@/lib/pullRequestAutoMerge";

import type { LifecycleActionId } from "./pullRequestLifecycleActions";

let attempts = 0;

/** A new attempt's id, unique for this window. */
export function newChangeAttempt(): string {
  attempts += 1;
  return `attempt-${attempts}`;
}

const unconfirmed = new Map<string, PullRequestActionReport>();

/** Keep `report` for an attempt GitHub may have made, until a Check Again settles it. */
export function keepUnconfirmedReport(attempt: string, report: PullRequestActionReport): void {
  unconfirmed.set(attempt, report);
}

/** The report kept for `attempt`, handed out once. */
export function takeUnconfirmedReport(attempt: string): PullRequestActionReport | undefined {
  const report = unconfirmed.get(attempt);
  unconfirmed.delete(attempt);
  return report;
}

/** A change Check Again can confirm, by its store's own name. */
export type CheckedChange =
  StateChangeOperation | Extract<LifecycleActionId, "convert_to_draft" | "ready_for_review">;

/** What a fresh read said about the PR. */
export interface FreshPullRequestFacts {
  state: "OPEN" | "CLOSED" | "MERGED";
  isDraft: boolean;
  /**
   * Its reviewers, requested ones as PENDING; null where the answer didn't say (an older host),
   * which is not the same as none.
   */
  reviewers: readonly { login: string; kind: string; state: string }[] | null;
  /** Its auto-merge as this read said; unknown where it didn't (an older shell), not off. */
  autoMerge: AutoMergeKnown;
}

const MADE: Record<CheckedChange, (facts: FreshPullRequestFacts) => boolean> = {
  pull_request_close: (facts) => facts.state === "CLOSED",
  pull_request_reopen: (facts) => facts.state === "OPEN",
  // A closed PR keeps its draft flag, so only an open one says either was made.
  ready_for_review: (facts) => facts.state === "OPEN" && !facts.isDraft,
  convert_to_draft: (facts) => facts.state === "OPEN" && facts.isDraft,
};

const OWN_ACTION: Record<CheckedChange, PullRequestOwnAction> = {
  pull_request_close: "closed",
  pull_request_reopen: "reopened",
  ready_for_review: "marked_ready",
  convert_to_draft: "converted_to_draft",
};

/** The page action `change` made, where `facts` show it made; null where they contradict it. */
export function changeMadeBy(
  change: CheckedChange,
  facts: FreshPullRequestFacts,
): PullRequestOwnAction | null {
  return MADE[change](facts) ? OWN_ACTION[change] : null;
}

/**
 * Whether a fresh read shows `login` withdrawn as a reviewer: no longer asked as a user, its
 * own review aside and a team of the same name too. A read with no reviewers can't tell.
 */
export function withdrawalMadeBy(
  login: string,
  facts: FreshPullRequestFacts,
): "removed" | "still_asked" | "cant_tell" {
  if (!facts.reviewers) return "cant_tell";
  const wanted = login.toLowerCase();
  const asked = facts.reviewers.some(
    (reviewer) =>
      reviewer.kind === "user" &&
      reviewer.state === "PENDING" &&
      reviewer.login.toLowerCase() === wanted,
  );
  return asked ? "still_asked" : "removed";
}

/**
 * Settle `attempt`'s kept report after a fresh read confirmed where the PR stands: it reports
 * the change where `facts` show it made, and is dropped either way.
 */
export function settleUnconfirmedReport(
  attempt: string,
  change: CheckedChange,
  facts: FreshPullRequestFacts,
): void {
  const report = takeUnconfirmedReport(attempt);
  const made = changeMadeBy(change, facts);
  if (!report || !made) return;
  // The read may have run under the login's newer sign-in, which `succeeded` turns away.
  if (report.confirmedByRead) report.confirmedByRead(made);
  else report.succeeded(made);
}

/** An attempt a page started, as it names and recovers it; it holds no write authority. */
export interface RecordedAttempt {
  id: string;
  /** The page's recovery scope it was started under. */
  scope: string;
  /** The workspace user it was started for. */
  owner: string;
  url: string;
  number: number;
  /** As the page names the PR, e.g. "#12 Fix the build", kept for when its row has gone. */
  name: string;
  /** An Undo, or its retry, which may go once the PR left the list. */
  evenUnlisted: boolean;
  /** A batch's step, whose skip its batch lists. */
  inBatch?: boolean;
}

/** One PR a batch left alone, and why. */
export interface SkippedChange {
  url: string;
  name: string;
  reason: string;
}

/** The PRs one batch left alone, until dismissed. */
export interface SkippedBatch {
  id: string;
  scope: string;
  owner: string;
  /** What it was, e.g. "Close". */
  change: string;
  skipped: readonly SkippedChange[];
}

const recorded = new Map<string, RecordedAttempt>();
const skippedBatches = new Map<string, SkippedBatch>();
const listeners = new Set<() => void>();
let version = 0;

function changed() {
  version += 1;
  for (const listener of listeners) listener();
}

/** Record an attempt `scope`'s page started. */
export function recordChangeAttempt(attempt: RecordedAttempt): void {
  recorded.set(attempt.id, attempt);
  changed();
}

/** The attempt `id`, where a page recorded it. */
export function recordedChangeAttempt(id: string): RecordedAttempt | undefined {
  return recorded.get(id);
}

/** Record what a batch left alone. */
export function recordSkippedBatch(batch: SkippedBatch): void {
  if (batch.skipped.length === 0) return;
  skippedBatches.set(batch.id, batch);
  changed();
}

/** The batches `scope`'s page left PRs alone in, oldest first. */
export function skippedBatchesOf(scope: string): SkippedBatch[] {
  return [...skippedBatches.values()].filter((batch) => batch.scope === scope);
}

/** Dismiss one batch's skipped PRs; nothing else goes with it. */
export function dismissSkippedBatch(id: string): void {
  if (skippedBatches.delete(id)) changed();
}

/** A number that moves whenever a page records an attempt or a batch's skips change. */
export function useChangeAttemptsVersion(): number {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => version,
    () => version,
  );
}

/** Forget every kept report and record, for tests. */
export function resetChangeAttemptsForTests(): void {
  unconfirmed.clear();
  recorded.clear();
  skippedBatches.clear();
  changed();
}
