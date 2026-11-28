// What a comment box offers while pending reviews are on: Comment or Start a review, Add to
// review, or nothing to send while a read, a held add or a half-done start says to wait.
// A start that made the review but not the comment is remembered, pinned to that review.

import type { QueryClient } from "@tanstack/react-query";
import { useSyncExternalStore } from "react";

import type { PullRequestCommentDraft } from "@/newPullRequestPage/hooks/usePullRequests";

import {
  composerChoice,
  type DraftPlace,
  type OutdatedReview,
  type PendingReviewRead,
  type PendingReviewState,
  samePlace,
  type SentDraft,
} from "./pendingReview";
import type { PendingHold, PendingSentAs } from "./pendingReviewChanges";
import { hostPendingIdentity } from "./hostPendingRoute";

/** The same login through the same session's host: the host's own logins are all empty. */
const sameSender = (left: PendingSentAs, right: PendingSentAs) =>
  left.login.toLowerCase() === right.login.toLowerCase() &&
  hostPendingIdentity(left.host) === hostPendingIdentity(right.host);

/**
 * A start that made `reviewId` without adding its comment. While it's kept, the draft at
 * `place` goes only into that review; a read sent after it that no longer has that review
 * pending marks it `gone`, and only the person's choice drops it.
 */
export interface PendingRecovery {
  id: string;
  owner: string;
  prUrl: string;
  sentAs: PendingSentAs;
  reviewId: string;
  place: DraftPlace;
  sent: SentDraft | null;
  createdAt: number;
  gone: boolean;
}

const recoveries = new Map<string, PendingRecovery>();
const listeners = new Set<() => void>();
let version = 0;
let serial = 0;

function changed(): void {
  version += 1;
  for (const listener of listeners) listener();
}

/** Remember a start that made `reviewId`; one per place, the newest replacing the last. */
export function recordPendingRecovery(
  recovery: Omit<PendingRecovery, "id" | "gone" | "createdAt">,
): PendingRecovery {
  for (const [id, existing] of recoveries) {
    // Only this scope's own pin at this place is replaced; another host's stays kept.
    if (
      existing.owner === recovery.owner &&
      existing.prUrl.toLowerCase() === recovery.prUrl.toLowerCase() &&
      sameSender(existing.sentAs, recovery.sentAs) &&
      samePlace(existing.place, recovery.place)
    ) {
      recoveries.delete(id);
    }
  }
  serial += 1;
  const kept = {
    ...recovery,
    id: `pending-recovery-${serial}`,
    createdAt: Date.now(),
    gone: false,
  };
  recoveries.set(kept.id, kept);
  changed();
  return kept;
}

export function dropPendingRecovery(id: string): void {
  if (recoveries.delete(id)) changed();
}

/** The workspace user's recoveries on `prUrl`. */
export function pendingRecoveriesOf(owner: string, prUrl: string): PendingRecovery[] {
  const wanted = prUrl.toLowerCase();
  return [...recoveries.values()].filter(
    (recovery) => recovery.owner === owner && recovery.prUrl.toLowerCase() === wanted,
  );
}

/**
 * Mark each recovery of `sentAs` on `prUrl` gone whose review a read sent after it no longer
 * has pending. A read sent before it, or one that failed, says nothing.
 */
export function resolvePendingRecoveries(
  owner: string,
  prUrl: string,
  sentAs: PendingSentAs,
  read: PendingReviewRead,
): void {
  let moved = false;
  for (const recovery of pendingRecoveriesOf(owner, prUrl)) {
    if (recovery.gone) continue;
    if (!sameSender(recovery.sentAs, sentAs)) continue;
    if (read.issued_at === undefined || read.issued_at <= recovery.createdAt) continue;
    if (read.review?.id === recovery.reviewId) continue;
    recoveries.set(recovery.id, { ...recovery, gone: true });
    moved = true;
  }
  if (moved) changed();
}

export function usePendingRecoveriesVersion(): number {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => version,
    () => 0,
  );
}

export function resetPendingRecoveriesForTests(): void {
  recoveries.clear();
  views.clear();
  mounted.clear();
  serial = 0;
  changed();
}

/** An add GitHub didn't confirm. */
export type HeldAdd = Extract<PendingHold, { kind: "draft_add" }>;

/** What a box at one place offers now. */
export type PendingComposerMode =
  | { kind: "today" }
  | { kind: "checking" }
  | { kind: "unreadable" }
  | { kind: "comment_or_start" }
  | { kind: "add_to_review"; reviewId: string; outdated: OutdatedReview | null }
  /** An add GitHub didn't confirm: nothing more is sent from here until it's checked. */
  | { kind: "held"; hold: HeldAdd }
  /** A start made `recovery.reviewId`: only Add into it. */
  | { kind: "recovering"; recovery: PendingRecovery }
  /** That review is no longer pending: the person decides what the text is for. */
  | { kind: "recovery_gone"; recovery: PendingRecovery };

/** A held add comes first, then a recovery, then what the read says. */
export function pendingComposerMode({
  state,
  hold,
  recovery,
}: {
  state: PendingReviewState;
  hold: HeldAdd | null;
  recovery: PendingRecovery | null;
}): PendingComposerMode {
  if (hold) return { kind: "held", hold };
  if (recovery) {
    return recovery.gone ? { kind: "recovery_gone", recovery } : { kind: "recovering", recovery };
  }
  const choice = composerChoice(state);
  switch (choice.kind) {
    case "today":
      return { kind: "today" };
    case "checking":
      return { kind: "checking" };
    case "unreadable":
      return { kind: "unreadable" };
    case "comment_or_start":
      return { kind: "comment_or_start" };
    case "add_to_review":
      return { kind: "add_to_review", reviewId: choice.reviewId, outdated: choice.outdated };
  }
}

const sameAnchor = (draft: PullRequestCommentDraft, place: DraftPlace) =>
  place.kind === "reply" ||
  (!!draft.anchor &&
    draft.anchor.path === place.path &&
    draft.anchor.side === place.side &&
    draft.anchor.line === place.line &&
    (draft.anchor.start_line ?? null) === place.startLine &&
    draft.anchor.head_sha === place.headSha &&
    draft.anchor.base_sha === place.baseSha);

/**
 * What a confirmed add does to the draft it was sent from:
 * - `cleared`: still exactly that draft (the same send's id, body and place, nothing uploading);
 * - `kept`: the person changed it since; it's left, and told the comment was added;
 * - `other`: it's another send's now, or uploading; nothing of it is touched.
 */
export function clearSentPendingDraft(
  queryClient: QueryClient,
  sent: SentDraft,
  keptMessage: string,
): "cleared" | "kept" | "other" {
  let result: "cleared" | "kept" | "other" = "other";
  queryClient.setQueryData<PullRequestCommentDraft>(sent.draftKey, (current) => {
    if (!current || (current.uploading ?? 0) > 0) return current;
    // Waiting on another send, this one's or none: that send's answer decides, not this.
    const ownsPending = current.sendingId === sent.sentId;
    if (current.pending && !ownsPending) return current;
    if (current.sendingId !== undefined && !ownsPending) return current;
    if (
      current.sentId === sent.sentId &&
      current.body === sent.body &&
      sameAnchor(current, sent.place)
    ) {
      result = "cleared";
      return { body: "", pending: false, error: null, generation: (current.generation ?? 0) + 1 };
    }
    // Another send's text, answered already: left alone.
    if (current.sentId !== undefined && current.sentId !== sent.sentId) return current;
    result = "kept";
    return {
      ...current,
      pending: ownsPending ? false : current.pending,
      sendingId: undefined,
      sentId: undefined,
      error: keptMessage,
    };
  });
  return result;
}

// Per draft, the view a box last showed it in: the workspace user, PR, place and the login the
// page reads as. Its epoch moves on every change, so an answer from an earlier view, even one
// the page has since come back to, never changes what a later view shows.
const views = new Map<string, { key: string; epoch: number }>();

// Per draft, how many boxes show it now; when the last one goes, its view is retired, so a box
// mounted later, even in the same view, never revives an answer sent before.
const mounted = new Map<string, number>();

/**
 * A box showing `draftKey` in `viewKey` mounted; the returned function unmounts it. The last
 * box to go retires the view, but only the view it showed: one a later render has already
 * moved to, as when other lines are selected, stays live for its own sends.
 */
export function mountComposerView(draftKey: readonly string[], viewKey: string): () => void {
  const name = draftKey.join("\n");
  mounted.set(name, (mounted.get(name) ?? 0) + 1);
  return () => {
    const left = (mounted.get(name) ?? 1) - 1;
    if (left > 0) {
      mounted.set(name, left);
      return;
    }
    mounted.delete(name);
    const current = views.get(name);
    if (current?.key !== viewKey) return;
    views.set(name, { key: "", epoch: current.epoch + 1 });
  };
}

/** Note that a box shows `draftKey` in the view `viewKey`; the view's token now. */
export function noteComposerView(
  draftKey: readonly string[],
  viewKey: string,
): { key: string; epoch: number } {
  const name = draftKey.join("\n");
  const current = views.get(name);
  if (current?.key === viewKey) return current;
  const next = { key: viewKey, epoch: (current?.epoch ?? 0) + 1 };
  views.set(name, next);
  return next;
}

/** Whether `draftKey` is still shown in the view `token` was taken in. */
export function composerViewCurrent(
  draftKey: readonly string[],
  token: { key: string; epoch: number },
): boolean {
  const current = views.get(draftKey.join("\n"));
  return current?.key === token.key && current.epoch === token.epoch;
}
