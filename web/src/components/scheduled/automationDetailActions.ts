// The automation page's header actions as plain facts, shared by its buttons and ⌘K: run
// now, edit, share, pause or resume, and delete, with why each can't run.

import {
  AVAILABLE,
  type CommandAvailability,
  HIDDEN,
  unavailableBecause,
} from "@/store/paletteCommands";

import { OWNER_ONLY_REASONS } from "./ownerOnlyActions";

export type AutomationDetailActionId = "run-now" | "edit" | "share" | "pause" | "resume" | "delete";

export const AUTOMATION_SAVING = "A change to this automation is still saving.";
export const RUN_IN_PROGRESS = "This run is already in progress.";

export interface AutomationDetailFacts {
  /** Shared with the viewer, who may only read it. */
  readOnly: boolean;
  /** One of its writes is in flight; the actions wait for it. */
  busy: boolean;
  /** It runs when its trigger fires, so there's no Run now. */
  triggerMode: boolean;
  /** A run just started and its row hasn't appeared yet. */
  runStarting: boolean;
  paused: boolean;
  /** The viewer may share it here. */
  canShare: boolean;
  /** Why Share can't run, such as sharing turned off. */
  shareDisabledReason?: string;
}

export interface AutomationDetailAction {
  id: AutomationDetailActionId;
  label: string;
  availability: CommandAvailability;
  destructive: boolean;
}

export function automationDetailActions(facts: AutomationDetailFacts): AutomationDetailAction[] {
  const ownerAction = (reason: string, blocked: string | null = null): CommandAvailability => {
    if (facts.readOnly) return unavailableBecause(reason);
    if (facts.busy) return unavailableBecause(AUTOMATION_SAVING);
    if (blocked) return unavailableBecause(blocked);
    return AVAILABLE;
  };
  const listedIf = (listed: boolean, availability: CommandAvailability) =>
    listed ? availability : HIDDEN;
  return [
    {
      id: "run-now",
      label: "Run automation now",
      availability: listedIf(
        !facts.triggerMode,
        ownerAction(OWNER_ONLY_REASONS.runNow, facts.runStarting ? RUN_IN_PROGRESS : null),
      ),
      destructive: false,
    },
    {
      id: "edit",
      label: "Edit automation…",
      availability: ownerAction(OWNER_ONLY_REASONS.edit),
      destructive: false,
    },
    {
      id: "share",
      label: "Share automation…",
      // A viewer sees Share greyed; the owner sees it only where sharing applies.
      availability: shareAvailability(facts),
      destructive: false,
    },
    {
      id: "pause",
      label: "Pause automation",
      availability: listedIf(!facts.paused, ownerAction(OWNER_ONLY_REASONS.pause)),
      destructive: false,
    },
    {
      id: "resume",
      label: "Resume automation",
      availability: listedIf(facts.paused, ownerAction(OWNER_ONLY_REASONS.resume)),
      destructive: false,
    },
    {
      id: "delete",
      label: "Delete automation",
      availability: ownerAction(OWNER_ONLY_REASONS.delete),
      destructive: true,
    },
  ];
}

function shareAvailability(facts: AutomationDetailFacts): CommandAvailability {
  if (facts.readOnly) return unavailableBecause(OWNER_ONLY_REASONS.share);
  if (!facts.canShare) return HIDDEN;
  if (facts.shareDisabledReason) return unavailableBecause(facts.shareDisabledReason);
  return AVAILABLE;
}

/** Whether `id` can run now, as the page's button is enabled. */
export function automationDetailActionAvailable(
  actions: readonly AutomationDetailAction[],
  id: AutomationDetailActionId,
): boolean {
  const action = actions.find((candidate) => candidate.id === id);
  return action?.availability.available === true;
}
