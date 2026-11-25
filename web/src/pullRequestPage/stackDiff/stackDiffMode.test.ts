import { describe, expect, it } from "vitest";

import type { StackComparisonState } from "./stackComparisonState";
import { comparison, identity } from "./stackDiffFixtures";
import {
  effectiveStackDiff,
  type StackDiffInputs,
  stackDiffFallbackMessage,
  wantsStackComparison,
} from "./stackDiffMode";

const ACCEPTED: StackComparisonState = {
  kind: "accepted",
  identity: identity(),
  comparison: comparison(),
};
const refused = (reason: "parent_closed" | "not_carried" | "in_base"): StackComparisonState => ({
  kind: "accepted",
  identity: identity(),
  comparison: comparison({ separable: false, reason, mergeBase: null, files: [] }),
});

const inputs = (overrides: Partial<StackDiffInputs> = {}): StackDiffInputs => ({
  intent: "automatic",
  automaticPolicy: true,
  baseIsParent: false,
  comparison: ACCEPTED,
  ...overrides,
});

describe("effectiveStackDiff", () => {
  it("shows only this PR's changes under Automatic where they can be told apart", () => {
    expect(effectiveStackDiff(inputs())).toEqual({ kind: "this-pr", comparison: comparison() });
  });

  it("moves Automatic with the policy, and leaves a manual choice where it is", () => {
    expect(effectiveStackDiff(inputs({ automaticPolicy: false }))).toEqual({
      kind: "all",
      fallback: null,
    });
    expect(effectiveStackDiff(inputs({ intent: "this-pr", automaticPolicy: false })).kind).toBe(
      "this-pr",
    );
    expect(effectiveStackDiff(inputs({ intent: "all" }))).toEqual({ kind: "all", fallback: null });
  });

  it("shows all changes when the PR's base already is its parent's branch", () => {
    expect(effectiveStackDiff(inputs({ baseIsParent: true }))).toEqual({
      kind: "all",
      fallback: null,
    });
    expect(wantsStackComparison(inputs({ baseIsParent: true }))).toBe(false);
    expect(wantsStackComparison(inputs({ baseIsParent: true, intent: "this-pr" }))).toBe(true);
  });

  for (const reason of ["parent_closed", "not_carried", "in_base"] as const) {
    it(`says why only after "This PR only" was chosen, for ${reason}`, () => {
      expect(effectiveStackDiff(inputs({ comparison: refused(reason) }))).toEqual({
        kind: "all",
        fallback: null,
      });
      expect(
        effectiveStackDiff(inputs({ intent: "this-pr", comparison: refused(reason) })),
      ).toEqual({ kind: "all", fallback: reason });
    });
  }

  it("shows nothing while comparing, and nothing but Reload once stale", () => {
    expect(effectiveStackDiff(inputs({ comparison: { kind: "idle" } })).kind).toBe("comparing");
    expect(
      effectiveStackDiff(inputs({ comparison: { kind: "reading", identity: identity() } })).kind,
    ).toBe("comparing");
    expect(effectiveStackDiff(inputs({ comparison: { kind: "stale" } }))).toEqual({
      kind: "stale",
    });
  });

  it("falls back to all changes on a failed read, saying so only after a manual choice", () => {
    const failed: StackComparisonState = {
      kind: "failed",
      identity: identity(),
      unsupported: false,
    };
    expect(effectiveStackDiff(inputs({ comparison: failed }))).toEqual({
      kind: "all",
      fallback: null,
    });
    expect(effectiveStackDiff(inputs({ intent: "this-pr", comparison: failed }))).toEqual({
      kind: "all",
      fallback: "failed",
    });
    // A shell that can't compare isn't a failure to report.
    expect(
      effectiveStackDiff(
        inputs({ intent: "this-pr", comparison: { ...failed, unsupported: true } }),
      ),
    ).toEqual({ kind: "all", fallback: null });
  });

  it("names the parent in each fallback", () => {
    for (const fallback of ["parent_closed", "not_carried", "in_base", "failed"] as const) {
      expect(stackDiffFallbackMessage(fallback, "#100")).toContain("#100");
    }
  });
});
