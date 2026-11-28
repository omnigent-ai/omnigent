// What the header's state pill says about a change it started: while a Close, Reopen or draft
// change is on its way to GitHub, and after one GitHub refused or may not have made. The notice
// under the header keeps the whole message and its actions.

import type { StateChangeOperation } from "@/lib/pullRequestStateHolds";

import {
  LIFECYCLE_ACTIONS,
  type LifecycleActionId,
  type LifecycleNotice,
} from "./pullRequestLifecycleActions";
import type { StateChainNotice } from "./pullRequestStateChain";
import { STATE_CHANGES } from "./pullRequestStateChanges";

export type StatePillProgress =
  { kind: "idle" } | { kind: "running"; label: string } | { kind: "failed"; label: string };

/** The draft changes the pill offers; auto-merge's live in the merge bar. */
const PILL_LIFECYCLE: ReadonlySet<LifecycleActionId> = new Set([
  "convert_to_draft",
  "ready_for_review",
]);

const FAILED_OUTCOMES: ReadonlySet<string> = new Set(["refused", "unknown", "abandoned"]);

/** The pill's progress from the running change and the last notice of each kind. */
export function statePillProgress({
  stateRunning,
  lifecycleRunning,
  stateNotice,
  lifecycleNotice,
}: {
  stateRunning: StateChangeOperation | null;
  lifecycleRunning: LifecycleActionId | null;
  stateNotice: StateChainNotice | null;
  lifecycleNotice: LifecycleNotice | null;
}): StatePillProgress {
  if (stateRunning) return { kind: "running", label: STATE_CHANGES[stateRunning].running };
  if (lifecycleRunning && PILL_LIFECYCLE.has(lifecycleRunning)) {
    return { kind: "running", label: LIFECYCLE_ACTIONS[lifecycleRunning].running };
  }
  if (stateNotice && FAILED_OUTCOMES.has(stateNotice.outcome.kind)) {
    return { kind: "failed", label: STATE_CHANGES[stateNotice.operation].failed };
  }
  if (
    lifecycleNotice &&
    PILL_LIFECYCLE.has(lifecycleNotice.id) &&
    FAILED_OUTCOMES.has(lifecycleNotice.outcome.kind)
  ) {
    return { kind: "failed", label: LIFECYCLE_ACTIONS[lifecycleNotice.id].failed };
  }
  return { kind: "idle" };
}
