// The summary of a pending review being finished here: seeded once from the summary GitHub has,
// then the person's own. A later read never overwrites what they typed; a summary that changed
// on GitHub since is offered instead, and only a confirmed submit or discard clears it. Each is
// kept for this window, like a regular review's draft, so a reload doesn't lose it.

import { useSyncExternalStore } from "react";
import { z } from "zod";

import { createWindowDrafts, type DraftWriteOutcome } from "./reviewDraftStorage";
import {
  EMPTY_REVIEW,
  normalizedBody,
  readReviewSubmit,
  type ReviewSubmitEntry,
  updateReviewSubmit,
} from "./reviewSubmit";

export interface PendingSummary {
  /** The text in the field. */
  text: string;
  /** GitHub's summary this was last seeded from or reconciled with. */
  seededFrom: string;
  /** The person changed the field since it was seeded. */
  edited: boolean;
}

/**
 * One pending review's summary for one workspace user, PR and account scope (the host's or a
 * named login's). Another scope's text never shows in it, and comes back when that scope does.
 */
export function pendingSummaryKey(
  owner: string,
  prUrl: string,
  scope: { login: string; namedReader: string | null },
  reviewId: string,
): string {
  return [
    owner,
    prUrl.toLowerCase(),
    scope.namedReader ? `named:${scope.namedReader.toLowerCase()}` : "host",
    scope.login.toLowerCase(),
    reviewId,
  ].join("\n");
}

const summaries = new Map<string, PendingSummary>();
const listeners = new Set<() => void>();
let version = 0;

function changed(): void {
  version += 1;
  for (const listener of listeners) listener();
}

const keptSummaries = createWindowDrafts({
  namespace: "pending-summaries",
  schema: z.object({ text: z.string(), seededFrom: z.string(), edited: z.boolean() }),
});

let loaded = false;

function ensureLoaded(): void {
  if (loaded) return;
  loaded = true;
  for (const { key, value } of keptSummaries.loadAll()) summaries.set(key, value);
}

/** Sets `key`'s summary, or clears it, and keeps that for this window. */
function put(key: string, summary: PendingSummary | null): DraftWriteOutcome {
  if (summary) summaries.set(key, summary);
  else summaries.delete(key);
  changed();
  return keptSummaries.save(key, summary);
}

export function readPendingSummary(key: string): PendingSummary | null {
  ensureLoaded();
  return summaries.get(key) ?? null;
}

/**
 * The summary as Finish opens on it: seeded from `githubBody` the first time for this review,
 * and kept as it is every time after.
 */
export function openPendingSummary(key: string, githubBody: string): PendingSummary {
  ensureLoaded();
  const existing = summaries.get(key);
  if (existing) return existing;
  const seeded = { text: githubBody, seededFrom: githubBody, edited: false };
  put(key, seeded);
  return seeded;
}

/** `addition` after `existing`, unless one already holds the other; neither text is dropped. */
export function mergedSummary(existing: string, addition: string): string {
  const kept = normalizedBody(existing);
  const added = normalizedBody(addition);
  if (!added || kept.includes(added)) return existing;
  if (!kept || added.includes(kept)) return addition;
  return `${existing.trimEnd()}\n\n${addition.trim()}`;
}

const editable = (entry: ReviewSubmitEntry) =>
  entry.status.kind === "idle" || entry.status.kind === "refused";

/**
 * A summary typed in the account's regular review before its first inline comment started a
 * pending review moves into that review once: merged with whatever its summary holds, then
 * cleared from the regular review so it's neither sent twice nor moved again. A review on its
 * way, or held unconfirmed, on either side stays as it is. The summary is kept before the
 * regular copy goes; if either step can't be kept, the text stays where it was.
 */
export function carryRegularSummary({
  regularKey,
  pendingKey,
  summaryKey,
  githubBody,
}: {
  regularKey: string;
  pendingKey: string;
  summaryKey: string;
  githubBody: string;
}): DraftWriteOutcome | "skipped" {
  ensureLoaded();
  const regular = readReviewSubmit(regularKey);
  const pending = readReviewSubmit(pendingKey);
  if (!editable(regular) || !editable(pending) || !normalizedBody(regular.body)) return "skipped";
  const existing = summaries.get(summaryKey) ?? null;
  const written = put(summaryKey, {
    text: mergedSummary(existing?.text ?? githubBody, regular.body),
    seededFrom: existing?.seededFrom ?? githubBody,
    edited: true,
  });
  if (written === "failed") {
    put(summaryKey, existing);
    return written;
  }
  const cleared = updateReviewSubmit(regularKey, () => EMPTY_REVIEW);
  if (cleared === "failed") {
    // The regular copy is still kept: put it back, and the summary as it was.
    updateReviewSubmit(regularKey, () => regular);
    put(summaryKey, existing);
    return cleared;
  }
  // The verdict comes too, unless one was already picked for the pending review.
  if (pending === EMPTY_REVIEW && regular.event !== EMPTY_REVIEW.event) {
    updateReviewSubmit(pendingKey, () => ({ ...EMPTY_REVIEW, event: regular.event }));
  }
  return written;
}

export function editPendingSummary(key: string, text: string): void {
  ensureLoaded();
  const existing = summaries.get(key);
  put(key, { text, seededFrom: existing?.seededFrom ?? "", edited: true });
}

/**
 * A read's summary against the field's: an unedited field follows GitHub's quietly, while an
 * edited one is kept and `conflict` says GitHub's changed since it was seeded.
 */
export function reconcilePendingSummary(
  key: string,
  githubBody: string,
): { summary: PendingSummary | null; conflict: boolean } {
  ensureLoaded();
  const existing = summaries.get(key);
  if (!existing || normalizedBody(existing.seededFrom) === normalizedBody(githubBody)) {
    return { summary: existing ?? null, conflict: false };
  }
  if (!existing.edited) {
    const followed = { text: githubBody, seededFrom: githubBody, edited: false };
    put(key, followed);
    return { summary: followed, conflict: false };
  }
  return {
    summary: existing,
    conflict: normalizedBody(existing.text) !== normalizedBody(githubBody),
  };
}

/** Use GitHub's: the field takes GitHub's summary, as if just seeded. */
export function takeGitHubsPendingSummary(key: string, githubBody: string): void {
  ensureLoaded();
  put(key, { text: githubBody, seededFrom: githubBody, edited: false });
}

/** Keep mine: the field stays, and GitHub's summary now is the one it was reconciled with. */
export function keepMyPendingSummary(key: string, githubBody: string): void {
  ensureLoaded();
  const existing = summaries.get(key);
  if (!existing) return;
  put(key, { ...existing, seededFrom: githubBody });
}

/**
 * After a confirmed submit: clears the field only if it still holds what was sent, so text
 * typed after pressing Submit survives.
 */
export function pendingSummarySubmitted(key: string, sentText: string): void {
  ensureLoaded();
  const existing = summaries.get(key);
  if (!existing || existing.text !== sentText) return;
  put(key, null);
}

/** After a confirmed discard: the review is gone, and so is its summary. */
export function pendingSummaryDiscarded(key: string): void {
  ensureLoaded();
  if (summaries.has(key)) put(key, null);
}

export function usePendingSummary(key: string | null): PendingSummary | null {
  useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => version,
    () => 0,
  );
  return key ? readPendingSummary(key) : null;
}

/** A reload: memory goes, and what this window kept is read again. */
export function reloadPendingSummariesForTests(): void {
  summaries.clear();
  loaded = false;
  ensureLoaded();
  changed();
}

export function resetPendingSummariesForTests(): void {
  keptSummaries.clearForTests();
  summaries.clear();
  loaded = false;
  changed();
}
