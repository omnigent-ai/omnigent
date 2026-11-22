// One commit's changes for a review that opens it on its own: a comparison owner
// whose typed comparison lives only in memory, so the session's own comparison,
// its saved choice and every other view of its changes stay as they were.

import { type RevisionSpec, sameRevisions } from "@/lib/customComparison";
import type {
  ComparisonOwnerDependencies,
  WorkingCopyComparisonOwner,
} from "@/lib/workingCopyComparisonOwner";

type CustomComparisons = NonNullable<ComparisonOwnerDependencies["customComparisons"]>;

export interface CommitComparisons {
  /** Lists only what `show` asked for; register views of the commit here. */
  owner: WorkingCopyComparisonOwner;
  /** Compares `spec` for every session this owner serves; null compares nothing. */
  show: (spec: RevisionSpec | null) => void;
  dispose: () => void;
}

/**
 * The in-memory typed comparison an owner reads in place of the saved ones.
 * A person's choice of base in this owner never outlives it, and never reaches storage.
 */
export function inMemoryCustomComparisons(): CustomComparisons & {
  set: (spec: RevisionSpec | null) => void;
} {
  let shown: RevisionSpec | null = null;
  const listeners = new Set<() => void>();
  const set = (spec: RevisionSpec | null) => {
    if (sameRevisions(shown, spec)) return;
    shown = spec;
    for (const listener of listeners) listener();
  };
  return {
    read: () => shown,
    write: (_sessionId, _workspace, spec) => set(spec),
    clear: () => set(null),
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    set,
  };
}

/**
 * A review's own owner for the commit it opens, showing `spec` from the start, so
 * the first listing is the commit's. `createOwner` wires it, as to the desktop app.
 */
export function createCommitComparisons(
  spec: RevisionSpec,
  createOwner: (customComparisons: CustomComparisons) => {
    owner: WorkingCopyComparisonOwner;
    dispose: () => void;
  },
): CommitComparisons {
  const comparisons = inMemoryCustomComparisons();
  comparisons.set(spec);
  const { owner, dispose } = createOwner(comparisons);
  return { owner, show: comparisons.set, dispose };
}
