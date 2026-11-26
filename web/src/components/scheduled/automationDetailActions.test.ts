import { describe, expect, it } from "vitest";

import { isCommandListed } from "@/store/paletteCommands";

import {
  AUTOMATION_SAVING,
  type AutomationDetailActionId,
  type AutomationDetailFacts,
  automationDetailActions,
  RUN_IN_PROGRESS,
} from "./automationDetailActions";
import { OWNER_ONLY_REASONS } from "./ownerOnlyActions";

const FACTS: AutomationDetailFacts = {
  readOnly: false,
  busy: false,
  triggerMode: false,
  runStarting: false,
  paused: false,
  canShare: true,
};

function actionsFor(overrides: Partial<AutomationDetailFacts>) {
  const actions = automationDetailActions({ ...FACTS, ...overrides });
  return {
    listed: actions.filter((action) => isCommandListed(action.availability)).map(({ id }) => id),
    reason: (id: AutomationDetailActionId) => {
      const availability = actions.find((action) => action.id === id)!.availability;
      return availability.available ? undefined : availability.reason;
    },
  };
}

describe("automationDetailActions", () => {
  it("offers the owner's actions, with Pause or Resume for the current state", () => {
    expect(actionsFor({}).listed).toEqual(["run-now", "edit", "share", "pause", "delete"]);
    expect(actionsFor({ paused: true }).listed).toContain("resume");
    expect(actionsFor({ paused: true }).listed).not.toContain("pause");
    expect(actionsFor({ triggerMode: true }).listed).not.toContain("run-now");
    expect(actionsFor({ canShare: false }).listed).not.toContain("share");
  });

  it("greys each action as the page's buttons do, saying why", () => {
    const shared = actionsFor({ readOnly: true, canShare: false });
    expect(shared.reason("run-now")).toBe(OWNER_ONLY_REASONS.runNow);
    expect(shared.reason("share")).toBe(OWNER_ONLY_REASONS.share);
    expect(shared.reason("delete")).toBe(OWNER_ONLY_REASONS.delete);
    expect(actionsFor({ busy: true }).reason("edit")).toBe(AUTOMATION_SAVING);
    expect(actionsFor({ runStarting: true }).reason("run-now")).toBe(RUN_IN_PROGRESS);
    expect(actionsFor({ shareDisabledReason: "Sharing is off." }).reason("share")).toBe(
      "Sharing is off.",
    );
  });
});
