// A comparison owner over fakes, for tests of the views that read it: lists
// wait until the test answers them, and the session's host serves comparisons.

import { act } from "@testing-library/react";

import type { BranchPointListing, ComparisonEntry } from "@/lib/workingCopyComparison";
import {
  readWorkingCopyBaseIntent,
  subscribeWorkingCopyBaseIntent,
} from "@/lib/workingCopyBasePreference";
import type { RevisionSpec } from "@/lib/customComparison";
import {
  type ComparisonOwnerDependencies,
  WorkingCopyComparisonOwner,
} from "@/lib/workingCopyComparisonOwner";
import { setWorkingCopyComparisonsForTests } from "@/lib/workingCopyComparisons";

/** A branch-point entry for `path`, modified unless `extra` says otherwise. */
export function comparisonEntry(
  path: string,
  extra: Partial<ComparisonEntry> = {},
): ComparisonEntry {
  return {
    path,
    name: path.split("/").at(-1) ?? path,
    status: "modified",
    bytes: 1,
    modified_at: null,
    lines_added: 1,
    lines_removed: 0,
    change: null,
    baseKind: "file",
    workingKind: "file",
    binary: false,
    problem: null,
    fingerprint: `fp-${path}`,
    ...extra,
  };
}

export interface PendingListing {
  refresh: boolean;
  signal: AbortSignal;
  /** The typed base and head listed, if any. */
  spec: RevisionSpec | null;
  resolve: (listing: BranchPointListing) => void;
}

/**
 * A fake-backed owner, with `extra` dependencies such as typed comparisons. Lists
 * wait in `listings` until answered.
 */
export function fakeComparisonOwner(extra: Partial<ComparisonOwnerDependencies> = {}) {
  const listings: PendingListing[] = [];
  const accessListeners = new Set<(sessionId: string) => void>();
  const owner = new WorkingCopyComparisonOwner({
    fetchListing: (_sessionId, { refresh, signal, spec }) =>
      new Promise((resolve) => {
        listings.push({ refresh, signal, spec, resolve });
      }),
    watchRoute: (_target, onRoute) => {
      queueMicrotask(() => onRoute({ state: "served" }));
      return { stop: () => {}, ask: () => {} };
    },
    readIntent: readWorkingCopyBaseIntent,
    subscribeIntent: subscribeWorkingCopyBaseIntent,
    now: () => Date.now(),
    setTimeout: (callback, ms) => setTimeout(callback, ms),
    clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
    documentVisible: () => true,
    subscribeAttention: () => () => {},
    subscribeAccessReady: (listener) => {
      accessListeners.add(listener);
      return () => accessListeners.delete(listener);
    },
    ...extra,
  });
  /** Answers the latest list with `entries` under comparison `id`. */
  const answer = async (entries: ComparisonEntry[], id = "cmp_1") => {
    const pending = listings.at(-1);
    if (!pending) throw new Error("No list is waiting for an answer");
    await act(async () => {
      pending.resolve({
        kind: "comparison",
        comparison: {
          id,
          ref: "origin/main",
          baseOid: "b".repeat(40),
          headOid: "h".repeat(40),
          refOid: "r".repeat(40),
          entries,
          unrepresentableNames: 0,
          listedAt: Date.now(),
        },
      });
    });
  };
  /** Says the session's files may be read again. */
  const accessReady = (sessionId: string) => {
    for (const listener of accessListeners) listener(sessionId);
  };
  return { owner, listings, answer, accessReady };
}

/** Installs a fake-backed owner as the document's; undo with `setWorkingCopyComparisonsForTests(null)`. */
export function installFakeComparisonOwner() {
  const fake = fakeComparisonOwner();
  setWorkingCopyComparisonsForTests(fake.owner);
  return fake;
}
