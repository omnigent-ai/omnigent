// What a person picked to compare a session's changes with: a saved base, or a typed base and
// head. A workspace tab's history keeps one per visit, so Back shows that comparison again.

import type { RevisionSpec } from "@/lib/customComparison";
import {
  WORKING_COPY_BASE_INTENTS,
  type WorkingCopyBaseIntent,
} from "@/lib/workingCopyBasePreference";

export type ComparisonChoice =
  | { kind: "saved"; intent: WorkingCopyBaseIntent }
  /** An empty `head` is the working copy. */
  | { kind: "typed"; base: string; head: string };

/** The choice a session shows: its typed base and head, else the saved base. */
export function comparisonChoiceOf(
  intent: WorkingCopyBaseIntent,
  custom: RevisionSpec | null,
): ComparisonChoice {
  return custom
    ? { kind: "typed", base: custom.base, head: custom.head }
    : { kind: "saved", intent };
}

export function sameComparisonChoice(
  left: ComparisonChoice | undefined,
  right: ComparisonChoice | undefined,
): boolean {
  if (left === undefined || right === undefined) return left === right;
  if (left.kind === "saved") return right.kind === "saved" && left.intent === right.intent;
  return right.kind === "typed" && left.base === right.base && left.head === right.head;
}

export function isWorkingCopyBaseIntent(value: string): value is WorkingCopyBaseIntent {
  return WORKING_COPY_BASE_INTENTS.some((intent) => intent === value);
}
