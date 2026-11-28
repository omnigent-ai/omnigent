// Which comparison of a stacked PR with its parent the page shows, as a pure state machine.
// A comparison is shown only for the exact commits, description and reader it was read for;
// once anything moves under an accepted one, only Reload starts over.

import type { StackComparison } from "@/lib/pullRequestStacks/stackCompare";

/** Everything a comparison was asked for; any change of it is another comparison. */
export interface StackComparisonIdentity {
  kind: "stack-comparison";
  /** The read scope the PR reads under and the reader this Mac proved for it. */
  reader: string;
  repository: string;
  number: number;
  parentNumber: number;
  parentState: string;
  childHead: string;
  parentHead: string;
  nativeBase: string;
  /** The description as the page read it, compared as is so an edit counts at once. */
  childBody: string;
  /** GitHub's stack as the view showed it, so a retarget of the stack counts too. */
  nativeStack: string | null;
}

export function stackComparisonKey(identity: StackComparisonIdentity): string {
  return JSON.stringify([
    identity.reader,
    identity.repository,
    identity.number,
    identity.parentNumber,
    identity.parentState,
    identity.childHead,
    identity.parentHead,
    identity.nativeBase,
    identity.childBody,
    identity.nativeStack,
  ]);
}

export type StackComparisonState =
  | { kind: "idle" }
  | { kind: "reading"; identity: StackComparisonIdentity }
  | { kind: "accepted"; identity: StackComparisonIdentity; comparison: StackComparison }
  /** The read failed, or this Mac's shell can't compare: the page shows all changes. */
  | { kind: "failed"; identity: StackComparisonIdentity; unsupported: boolean }
  /** The stack moved under the comparison: nothing shows until Reload. */
  | { kind: "stale" }
  /** Reload is reading the PR and its stack again; nothing compares until they're in. */
  | { kind: "reloading" };

export type StackComparisonEvent =
  /**
   * What the page would compare now, or null when it can't; `read` says whether it should
   * read it now. A shown comparison goes stale on any change, read or not.
   */
  | { type: "identity"; identity: StackComparisonIdentity | null; read: boolean }
  | { type: "answer"; identity: StackComparisonIdentity; comparison: StackComparison | null }
  | { type: "failed"; identity: StackComparisonIdentity; changed: boolean }
  /** The person asked to try a failed read again. */
  | { type: "retry" }
  | { type: "reload-started" }
  | { type: "reload-settled"; ok: boolean }
  /**
   * The person refreshed the pull request, and what the page shows has caught up with it: compare
   * again from scratch, whatever was shown or failed before.
   */
  | { type: "refresh" };

export const IDLE_STACK_COMPARISON: StackComparisonState = { kind: "idle" };
const STALE: StackComparisonState = { kind: "stale" };

export const sameStackIdentity = (
  state: StackComparisonState,
  identity: StackComparisonIdentity | null,
) =>
  !!identity &&
  "identity" in state &&
  stackComparisonKey(state.identity) === stackComparisonKey(identity);

export function stackComparisonReducer(
  state: StackComparisonState,
  event: StackComparisonEvent,
): StackComparisonState {
  switch (event.type) {
    case "identity": {
      const { identity, read } = event;
      if (state.kind === "stale" || state.kind === "reloading") return state;
      if (sameStackIdentity(state, identity)) return state;
      // Anything moving under a shown comparison makes it stale, never another comparison.
      if (state.kind === "accepted") return STALE;
      return identity && read ? { kind: "reading", identity } : IDLE_STACK_COMPARISON;
    }
    case "answer":
      if (state.kind !== "reading" || !sameStackIdentity(state, event.identity)) return state;
      return event.comparison
        ? { kind: "accepted", identity: state.identity, comparison: event.comparison }
        : { kind: "failed", identity: state.identity, unsupported: true };
    case "failed":
      if (state.kind !== "reading" || !sameStackIdentity(state, event.identity)) return state;
      // The PR moved since the page read it: the page's own copy is out of date.
      return event.changed
        ? STALE
        : { kind: "failed", identity: state.identity, unsupported: false };
    case "retry":
      return state.kind === "failed" && !state.unsupported ? IDLE_STACK_COMPARISON : state;
    case "reload-started":
      return state.kind === "stale" ? { kind: "reloading" } : state;
    case "reload-settled":
      if (state.kind !== "reloading") return state;
      // A failed reload leaves Reload to try again.
      return event.ok ? IDLE_STACK_COMPARISON : STALE;
    case "refresh":
      return state.kind === "idle" ? state : IDLE_STACK_COMPARISON;
  }
}
