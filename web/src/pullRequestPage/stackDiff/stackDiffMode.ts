// What a stacked PR's Changes tab shows: the person's choice, the live Automatic policy, and
// the comparison, kept apart, and the one effective view derived from them.

import type { StackComparison, StackCompareRefusal } from "@/lib/pullRequestStacks/stackCompare";
import type { StackComparisonState } from "./stackComparisonState";

/** What the person chose; Automatic follows the policy as it changes. */
export type StackDiffIntent = "automatic" | "this-pr" | "all";

export const STACK_DIFF_INTENTS: readonly StackDiffIntent[] = ["automatic", "this-pr", "all"];

export function isStackDiffIntent(value: unknown): value is StackDiffIntent {
  return STACK_DIFF_INTENTS.includes(value as StackDiffIntent);
}

/** Why a chosen "This PR only" shows all changes instead. */
export type StackDiffFallback = StackCompareRefusal | "failed";

export type EffectiveStackDiff =
  | { kind: "all"; fallback: StackDiffFallback | null }
  | { kind: "comparing" }
  | { kind: "this-pr"; comparison: StackComparison }
  | { kind: "stale" };

export interface StackDiffInputs {
  intent: StackDiffIntent;
  /** The Automatic policy now: whether Automatic shows only this PR where it can. */
  automaticPolicy: boolean;
  /** The PR's base is its parent's branch, so all its changes are already its own. */
  baseIsParent: boolean;
  comparison: StackComparisonState;
}

/** Whether the page should compare at all for these choices. */
export function wantsStackComparison({
  intent,
  automaticPolicy,
  baseIsParent,
}: Omit<StackDiffInputs, "comparison">): boolean {
  if (intent === "this-pr") return true;
  return intent === "automatic" && automaticPolicy && !baseIsParent;
}

/**
 * The view the tab shows. A fallback is named only after the person chose "This PR only";
 * Automatic falls back to all changes quietly.
 */
export function effectiveStackDiff(inputs: StackDiffInputs): EffectiveStackDiff {
  if (!wantsStackComparison(inputs)) return { kind: "all", fallback: null };
  const manual = inputs.intent === "this-pr";
  const state = inputs.comparison;
  switch (state.kind) {
    case "idle":
    case "reading":
    case "reloading":
      return { kind: "comparing" };
    case "stale":
      return { kind: "stale" };
    case "failed":
      return { kind: "all", fallback: manual && !state.unsupported ? "failed" : null };
    case "accepted": {
      const { comparison } = state;
      if (comparison.separable) return { kind: "this-pr", comparison };
      return { kind: "all", fallback: manual ? comparison.reason : null };
    }
  }
}

/** What a fallback tells the person, about the parent `parent` names (such as "#100"). */
export function stackDiffFallbackMessage(fallback: StackDiffFallback, parent: string): string {
  switch (fallback) {
    case "not_carried":
      return `${parent} changed after this PR was pushed, so its changes can't be told apart. Showing all changes.`;
    case "in_base":
      return `${parent}'s changes are already in the base branch, so all changes are this PR's own.`;
    case "parent_closed":
      return `${parent} was closed. Showing all changes.`;
    case "failed":
      return `Couldn't compare this PR with ${parent}. Showing all changes.`;
  }
}
