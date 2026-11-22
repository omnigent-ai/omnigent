// The composer's Add menu actions that also run from ⌘K: attach files, start a side chat,
// set the goal and toggle Plan mode. The menu's items read their availability here too.

import {
  AVAILABLE,
  type CommandAvailability,
  HIDDEN,
  unavailableBecause,
} from "@/store/paletteCommands";

export type ComposerAddActionId = "attach" | "side-chat" | "goal" | "plan";

export const ATTACH_UNAVAILABLE = "Files can't be attached right now.";
export const GOAL_UNAVAILABLE = "The goal can't change right now.";
export const PLAN_BUSY = "Plan mode can't change right now.";

/** What the Add menu is told about the composer. */
export interface ComposerAddFacts {
  attachDisabled: boolean;
  sideChat: boolean;
  showGoal: boolean;
  goal: boolean;
  goalDisabled: boolean;
  goalActive: boolean;
  /** Why Goal can't run, when it has no handler here. */
  goalDescription: string;
  showPlan: boolean;
  plan: boolean;
  planDisabled: boolean;
  planActive: boolean;
  planUnavailableReason: string;
}

export interface ComposerAddAction {
  id: ComposerAddActionId;
  /** The name ⌘K lists it under, which says what it does outside the menu. */
  label: string;
  availability: CommandAvailability;
}

/** An item the menu may hide, grey while its composer can't handle it, or grey while busy. */
function menuItemAvailability(item: {
  shown: boolean;
  handled: boolean;
  unhandledReason: string;
  disabled: boolean;
  disabledReason: string;
}): CommandAvailability {
  if (!item.shown) return HIDDEN;
  if (!item.handled) return unavailableBecause(item.unhandledReason);
  if (item.disabled) return unavailableBecause(item.disabledReason);
  return AVAILABLE;
}

export function composerAddActions(facts: ComposerAddFacts): ComposerAddAction[] {
  return [
    {
      id: "attach",
      label: "Attach files and images…",
      availability: facts.attachDisabled ? unavailableBecause(ATTACH_UNAVAILABLE) : AVAILABLE,
    },
    {
      id: "side-chat",
      label: "Start a new side chat",
      availability: facts.sideChat ? AVAILABLE : HIDDEN,
    },
    {
      id: "goal",
      label: facts.goalActive ? "View or update goal…" : "Set goal…",
      availability: menuItemAvailability({
        shown: facts.showGoal,
        handled: facts.goal,
        unhandledReason: facts.goalDescription,
        disabled: facts.goalDisabled,
        disabledReason: GOAL_UNAVAILABLE,
      }),
    },
    {
      id: "plan",
      label: facts.planActive ? "Exit Plan mode" : "Enter Plan mode",
      availability: menuItemAvailability({
        shown: facts.showPlan,
        handled: facts.plan,
        unhandledReason: facts.planUnavailableReason,
        disabled: facts.planDisabled,
        disabledReason: PLAN_BUSY,
      }),
    },
  ];
}

/** The action `id` among `actions`. */
export function composerAddAction(
  actions: readonly ComposerAddAction[],
  id: ComposerAddActionId,
): ComposerAddAction {
  return actions.find((action) => action.id === id)!;
}
