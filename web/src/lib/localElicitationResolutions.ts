// Subtract locally submitted answers from derived pending counts until the server
// reflects them, the POST fails, or a timeout passes. Never write the overlay to the
// react-query cache, so a stream frame already in flight cannot revert an answer.

import { useSyncExternalStore } from "react";
import type { Conversation } from "@/hooks/useConversations";

/** Stop holding a verdict whose POST has not returned after this long. */
export const SUBMIT_TIMEOUT_MS = 15_000;
/** The updates stream rescans every 4 s, so this outlasts one tick plus delivery slack. */
export const ACK_SETTLE_MS = 6_000;
/** Native prompts stay counted until the terminal confirms the delivery, within seconds. */
export const NATIVE_SETTLE_MS = 15_000;

interface Hold {
  /**
   * Each session whose server count still includes this prompt, mapped to that
   * count as last observed. Only sessions loaded at submit are held.
   */
  readonly counts: Map<string, number>;
  readonly nativeDelivery: boolean;
  /** Identifies the submission that owns the hold. */
  readonly token: number;
  timer: ReturnType<typeof setTimeout>;
}

export interface HeldSession {
  /** Answered prompts its last observed count includes. */
  readonly answers: number;
  /** That last observed count. */
  readonly observedCount: number;
}

export interface LocalElicitationResolutions {
  /** Held sessions by id. A session not loaded when its prompt was answered is not held. */
  readonly heldBySession: ReadonlyMap<string, HeldSession>;
  /** Elicitation ids answered locally and not yet released. */
  readonly answeredElicitationIds: ReadonlySet<string>;
}

// Insertion order is the age order: a re-begun elicitation moves to the end.
const holds = new Map<string, Hold>();
let lastObservedCounts = new Map<string, number>();
const listeners = new Set<() => void>();
const EMPTY_RESOLUTIONS: LocalElicitationResolutions = {
  heldBySession: new Map(),
  answeredElicitationIds: new Set(),
};
let resolutions: LocalElicitationResolutions = EMPTY_RESOLUTIONS;
let lastToken = 0;

function publish(): void {
  const heldBySession = new Map<string, HeldSession>();
  for (const hold of holds.values()) {
    // Every hold on a session carries the same observed count.
    for (const [sessionId, observedCount] of hold.counts) {
      const answers = (heldBySession.get(sessionId)?.answers ?? 0) + 1;
      heldBySession.set(sessionId, { answers, observedCount });
    }
  }
  resolutions = { heldBySession, answeredElicitationIds: new Set(holds.keys()) };
  for (const listener of listeners) listener();
}

function releaseHold(elicitationId: string): void {
  const hold = holds.get(elicitationId);
  if (hold === undefined) return;
  clearTimeout(hold.timer);
  holds.delete(elicitationId);
  publish();
}

/** Release up to `amount` holds for `sessionId`, oldest first. */
function releaseHoldsForCountDrop(sessionId: string, amount: number): boolean {
  let remaining = amount;
  let changed = false;
  for (const [elicitationId, hold] of holds) {
    if (remaining === 0) break;
    if (!hold.counts.delete(sessionId)) continue;
    remaining -= 1;
    changed = true;
    if (hold.counts.size === 0) {
      clearTimeout(hold.timer);
      holds.delete(elicitationId);
    }
  }
  return changed;
}

/** A held session's count as last observed; undefined when no hold has it. */
function heldCount(sessionId: string): number | undefined {
  for (const hold of holds.values()) {
    const count = hold.counts.get(sessionId);
    if (count !== undefined) return count;
  }
  return undefined;
}

/**
 * Hold the sidebar counts of `sessionIds` down by one for an answer that is
 * being submitted. Only sessions in the latest observation are held, since a
 * count first read after the answer may or may not include it. Re-beginning an
 * elicitation replaces its earlier hold.
 *
 * @param elicitationId - Prompt the user answered, e.g. `"elicit_abc123"`.
 * @param sessionIds - Sessions whose server count includes the prompt.
 * @param options.nativeDelivery - True for a native-terminal prompt, which
 *   the server keeps counted until the terminal confirms the delivery.
 * @returns A token that only this submission's acknowledge or cancel accepts.
 */
export function beginLocalElicitationResolution(
  elicitationId: string,
  sessionIds: readonly string[],
  options: { nativeDelivery: boolean },
): number {
  const previous = holds.get(elicitationId);
  if (previous !== undefined) clearTimeout(previous.timer);
  holds.delete(elicitationId);
  lastToken += 1;
  const counts = new Map<string, number>();
  for (const sessionId of sessionIds) {
    const count = lastObservedCounts.get(sessionId);
    if (count !== undefined) counts.set(sessionId, count);
  }
  holds.set(elicitationId, {
    counts,
    nativeDelivery: options.nativeDelivery,
    token: lastToken,
    timer: setTimeout(() => releaseHold(elicitationId), SUBMIT_TIMEOUT_MS),
  });
  publish();
  return lastToken;
}

/**
 * Mark the POST for `elicitationId` as accepted by the server. The hold stays
 * until a count drop is observed or the settle window passes. A later
 * submission's hold is left alone.
 */
export function acknowledgeLocalElicitationResolution(elicitationId: string, token: number): void {
  const hold = holds.get(elicitationId);
  if (hold?.token !== token) return;
  clearTimeout(hold.timer);
  hold.timer = setTimeout(
    () => releaseHold(elicitationId),
    hold.nativeDelivery ? NATIVE_SETTLE_MS : ACK_SETTLE_MS,
  );
}

/** Drop the hold at once because its POST failed. A later submission's hold is left alone. */
export function cancelLocalElicitationResolution(elicitationId: string, token: number): void {
  if (holds.get(elicitationId)?.token === token) releaseHold(elicitationId);
}

/**
 * Reconcile holds against the server counts the sidebar has just read.
 *
 * Call with raw rows (before `applyLocalElicitationResolutions`). A decrease
 * against a session's previously observed count releases that many of its
 * holds, oldest first. Any change, including an increase from a new prompt,
 * becomes the observed count of the holds that remain. A held session whose
 * row was unloaded is compared with its count when last observed.
 *
 * @param rows - Loaded session rows with the server's pending counts.
 */
export function observeSessionPendingCounts(
  rows: readonly Pick<Conversation, "id" | "pending_elicitations_count">[],
): void {
  const previousCounts = lastObservedCounts;
  const currentCounts = new Map<string, number>();
  for (const row of rows) currentCounts.set(row.id, row.pending_elicitations_count ?? 0);
  let changed = false;
  for (const [sessionId, count] of currentCounts) {
    const previousCount = previousCounts.get(sessionId) ?? heldCount(sessionId);
    if (previousCount === undefined || count === previousCount) continue;
    if (count < previousCount) {
      changed = releaseHoldsForCountDrop(sessionId, previousCount - count) || changed;
    }
    for (const hold of holds.values()) {
      if (hold.counts.has(sessionId)) {
        hold.counts.set(sessionId, count);
        changed = true;
      }
    }
  }
  lastObservedCounts = currentCounts;
  if (changed) publish();
}

/**
 * Subtract held answers from the server counts of their sessions.
 *
 * Rows without a hold are returned as-is; the array is returned unchanged
 * when no row needs an adjustment. Apply once, to raw rows.
 *
 * A count below the session's observed count shows the server counting
 * held answers out, so they are not subtracted again before the drop is
 * observed.
 *
 * @param rows - Session rows carrying the server's pending counts.
 * @param held - The current holds, from `useLocalElicitationResolutions`.
 * @returns The same rows, with `pending_elicitations_count` lowered where held.
 */
export function applyLocalElicitationResolutions(
  rows: Conversation[],
  held: LocalElicitationResolutions,
): Conversation[] {
  const { heldBySession } = held;
  if (heldBySession.size === 0) return rows;
  let changed = false;
  const adjusted = rows.map((row) => {
    const session = heldBySession.get(row.id);
    if (session === undefined) return row;
    const count = row.pending_elicitations_count ?? 0;
    const countedOut = Math.min(session.answers, Math.max(0, session.observedCount - count));
    const effective = Math.max(0, count - (session.answers - countedOut));
    if (effective === count) return row;
    changed = true;
    return { ...row, pending_elicitations_count: effective };
  });
  return changed ? adjusted : rows;
}

/**
 * Sessions whose pending counts include a prompt answered in `sessionIds`:
 * each starting session plus its ancestors, which the server mirrors prompts into.
 *
 * @param sessionIds - The session the card belongs to and the session that
 *   owns the prompt, e.g. `["conv_parent", "conv_child"]`.
 * @param parentOf - Returns a session's parent id, or nullish for a root.
 */
export function affectedSessionIds(
  sessionIds: readonly (string | null | undefined)[],
  parentOf: (sessionId: string) => string | null | undefined,
): string[] {
  const affected = new Set<string>();
  for (const sessionId of sessionIds) {
    let current: string | null | undefined = sessionId;
    while (current && !affected.has(current)) {
      affected.add(current);
      current = parentOf(current);
    }
  }
  return [...affected];
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Current holds, read outside React. */
export function getLocalElicitationResolutions(): LocalElicitationResolutions {
  return resolutions;
}

/** Current holds; re-renders the caller when they change. */
export function useLocalElicitationResolutions(): LocalElicitationResolutions {
  return useSyncExternalStore(
    subscribe,
    getLocalElicitationResolutions,
    getLocalElicitationResolutions,
  );
}

/** Clear all holds and observations. For test isolation only. */
export function resetLocalElicitationResolutionsForTests(): void {
  for (const hold of holds.values()) clearTimeout(hold.timer);
  holds.clear();
  lastObservedCounts = new Map();
  publish();
}
