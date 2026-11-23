import { afterEach, describe, expect, it, vi } from "vitest";

import { createCommitComparisons, inMemoryCustomComparisons } from "@/lib/commitComparisons";
import { commitRevisions, readCustomComparison } from "@/lib/customComparison";
import { fakeComparisonOwner } from "@/test/workingCopyComparisons";

const WORKSPACE = { hostId: "host_1", root: "/repo" };
const FIRST = "a".repeat(40);
const SECOND = "b".repeat(40);

/** A view of session s1 that may read its workspace, as the Diffs view's commit review is. */
const commitView = {
  kind: "file",
  enabled: true,
  sessionHostId: "host_1",
  root: "/repo",
} as const;

function commitComparisons(spec = commitRevisions(FIRST)) {
  let fake: ReturnType<typeof fakeComparisonOwner> | null = null;
  const dispose = vi.fn();
  const comparisons = createCommitComparisons(spec, (customComparisons) => {
    fake = fakeComparisonOwner({
      customComparisons,
      readIntent: () => "head",
      watchRoute: (_target, onRoute) => {
        queueMicrotask(() => onRoute({ state: "served" }));
        return { stop: () => {}, ask: () => {} };
      },
    });
    return { owner: fake.owner, dispose };
  });
  return { comparisons, fake: fake!, dispose };
}

/** Lets the owner's route answer land. */
const settle = () =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, 0);
  });

afterEach(() => window.localStorage.clear());

describe("inMemoryCustomComparisons", () => {
  it("reads what was set for any session, telling listeners only of a change", () => {
    const comparisons = inMemoryCustomComparisons();
    const listener = vi.fn();
    comparisons.subscribe(listener);
    expect(comparisons.read("s1", WORKSPACE)).toBeNull();
    comparisons.set({ base: "v1", head: "v2" });
    comparisons.set({ base: "v1", head: "v2" });
    expect(comparisons.read("s2", WORKSPACE)).toEqual({ base: "v1", head: "v2" });
    expect(listener).toHaveBeenCalledTimes(1);
    comparisons.clear("s1", WORKSPACE);
    expect(comparisons.read("s1", WORKSPACE)).toBeNull();
    expect(listener).toHaveBeenCalledTimes(2);
  });
});

describe("createCommitComparisons", () => {
  it("lists the commit first, then the next one shown, and never saves either", async () => {
    const { comparisons, fake } = commitComparisons();
    fake.owner.register("s1", commitView);
    await settle();
    expect(fake.listings.map((listing) => listing.spec)).toEqual([commitRevisions(FIRST)]);
    expect(fake.owner.snapshot("s1").custom).toEqual(commitRevisions(FIRST));

    comparisons.show(commitRevisions(SECOND));
    await settle();
    expect(fake.listings.at(0)!.signal.aborted).toBe(true);
    expect(fake.listings.at(-1)!.spec).toEqual(commitRevisions(SECOND));
    // The session's own typed comparison, saved per workspace, is untouched.
    expect(readCustomComparison("s1", WORKSPACE)).toBeNull();
  });

  it("lists nothing without a commit to show", async () => {
    const { comparisons, fake } = commitComparisons();
    comparisons.show(null);
    fake.owner.register("s1", commitView);
    await settle();
    expect(fake.listings).toEqual([]);
  });

  it("stops its owner when disposed", () => {
    const { comparisons, dispose } = commitComparisons();
    comparisons.dispose();
    expect(dispose).toHaveBeenCalledTimes(1);
  });
});
