import { describe, expect, it } from "vitest";

import { isCommandAvailable, isCommandListed } from "@/store/paletteCommands";

import {
  ATTACH_UNAVAILABLE,
  type ComposerAddFacts,
  composerAddAction,
  composerAddActions,
  GOAL_UNAVAILABLE,
  PLAN_BUSY,
} from "./composerAddActions";

const FACTS: ComposerAddFacts = {
  attachDisabled: false,
  sideChat: true,
  showGoal: true,
  goal: true,
  goalDisabled: false,
  goalActive: false,
  goalDescription: "Available after starting a session",
  showPlan: true,
  plan: true,
  planDisabled: false,
  planActive: false,
  planUnavailableReason: "This harness does not support plan mode",
};

const action = (facts: Partial<ComposerAddFacts>, id: Parameters<typeof composerAddAction>[1]) =>
  composerAddAction(composerAddActions({ ...FACTS, ...facts }), id);
const reason = (facts: Partial<ComposerAddFacts>, id: Parameters<typeof composerAddAction>[1]) => {
  const { availability } = action(facts, id);
  return availability.available ? undefined : availability.reason;
};

describe("composerAddActions", () => {
  it("offers each action the menu offers, named for what it does", () => {
    const actions = composerAddActions(FACTS);
    expect(actions.map(({ label }) => label)).toEqual([
      "Attach files and images…",
      "Start a new side chat",
      "Set goal…",
      "Enter Plan mode",
    ]);
    expect(actions.every(({ availability }) => isCommandAvailable(availability))).toBe(true);
    expect(action({ goalActive: true }, "goal").label).toBe("View or update goal…");
    expect(action({ planActive: true }, "plan").label).toBe("Exit Plan mode");
  });

  it("hides what the menu hides, and greys what it greys, saying why", () => {
    expect(isCommandListed(action({ sideChat: false }, "side-chat").availability)).toBe(false);
    expect(isCommandListed(action({ showGoal: false }, "goal").availability)).toBe(false);
    expect(isCommandListed(action({ showPlan: false }, "plan").availability)).toBe(false);
    expect(reason({ attachDisabled: true }, "attach")).toBe(ATTACH_UNAVAILABLE);
    expect(reason({ goal: false }, "goal")).toBe(FACTS.goalDescription);
    expect(reason({ goalDisabled: true }, "goal")).toBe(GOAL_UNAVAILABLE);
    expect(reason({ plan: false }, "plan")).toBe(FACTS.planUnavailableReason);
    expect(reason({ planDisabled: true }, "plan")).toBe(PLAN_BUSY);
  });
});
