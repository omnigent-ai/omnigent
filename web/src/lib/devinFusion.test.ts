import { describe, expect, it } from "vitest";

import {
  applyFusionChange,
  currentFusionCombo,
  fusionEffortChoices,
  fusionFastAvailable,
  fusionLeadChoices,
  fusionPriorityAvailable,
  fusionSidekickChoices,
  isFusionModelUid,
  resolveFusionModelUid,
} from "./devinFusion";
import type { FusionCombo, FusionDescriptor } from "./types";

function combo(over: Partial<FusionCombo> & { modelUid: string }): FusionCombo {
  return {
    lead: "claude-fable-5.1",
    leadLabel: "Claude Fable 5.1",
    effort: "medium",
    fast: false,
    sidekick: "swe-2-medium",
    sidekickLabel: "SWE-2 Medium",
    priority: false,
    ...over,
  };
}

// Two leads; fable has medium (swe-2-medium / swe-2-high) + high (+ a fast high);
// opus has high with a priority-capable sidekick and a bare-family sidekick.
const DESC: FusionDescriptor = {
  default: "fusion-fable-medium-swe2medium",
  combos: [
    combo({ modelUid: "fusion-fable-medium-swe2medium" }),
    combo({
      modelUid: "fusion-fable-medium-swe2high",
      sidekick: "swe-2-high",
      sidekickLabel: "SWE-2 High",
    }),
    combo({ modelUid: "fusion-fable-high-swe2medium", effort: "high" }),
    combo({ modelUid: "fusion-fable-high-fast-swe2medium", effort: "high", fast: true }),
    combo({
      modelUid: "fusion-opus-high-sol",
      lead: "claude-opus-5",
      leadLabel: "Claude Opus 5",
      effort: "high",
      sidekick: "gpt-5-6-sol-high",
      sidekickLabel: "GPT-5.6 Sol High",
    }),
    combo({
      modelUid: "fusion-opus-high-sol-priority",
      lead: "claude-opus-5",
      leadLabel: "Claude Opus 5",
      effort: "high",
      sidekick: "gpt-5-6-sol-high",
      sidekickLabel: "GPT-5.6 Sol High",
      priority: true,
    }),
  ],
};

describe("isFusionModelUid", () => {
  it("recognizes combo ids and the bare family, rejects others", () => {
    expect(isFusionModelUid("fusion-fable-medium-swe2medium")).toBe(true);
    expect(isFusionModelUid("fusion")).toBe(true);
    expect(isFusionModelUid("claude-opus-5-high")).toBe(false);
    expect(isFusionModelUid(null)).toBe(false);
  });
});

describe("currentFusionCombo", () => {
  it("returns the matching combo", () => {
    expect(currentFusionCombo(DESC, "fusion-fable-high-swe2medium").effort).toBe("high");
  });

  it("falls back to the default when the id is unknown (e.g. bare 'fusion')", () => {
    expect(currentFusionCombo(DESC, "fusion").modelUid).toBe("fusion-fable-medium-swe2medium");
  });

  it("resolves a stale combo id to the default (the id both display and submit use)", () => {
    // A saved id the current catalog no longer offers (host switch / retired
    // combo). Both the displayed selectors and the submitted model_override read
    // through this resolver, so they can never disagree: the stale id resolves to
    // a real combo rather than being shown as default yet submitted as-is.
    expect(currentFusionCombo(DESC, "fusion-claude-opus-5-high-sidekick-swe-2-max").modelUid).toBe(
      DESC.default,
    );
  });
});

describe("choice lists", () => {
  it("lists distinct leads in first-seen order with labels", () => {
    expect(fusionLeadChoices(DESC)).toEqual([
      { value: "claude-fable-5.1", label: "Claude Fable 5.1" },
      { value: "claude-opus-5", label: "Claude Opus 5" },
    ]);
  });

  it("lists a lead's efforts ascending", () => {
    expect(fusionEffortChoices(DESC, "claude-fable-5.1")).toEqual(["medium", "high"]);
    expect(fusionEffortChoices(DESC, "claude-opus-5")).toEqual(["high"]);
  });

  it("filters sidekicks by lead/effort/fast", () => {
    expect(
      fusionSidekickChoices(DESC, "claude-fable-5.1", "medium", false).map((s) => s.value),
    ).toEqual(["swe-2-medium", "swe-2-high"]);
    // The fast high pairing has only swe-2-medium.
    expect(
      fusionSidekickChoices(DESC, "claude-fable-5.1", "high", true).map((s) => s.value),
    ).toEqual(["swe-2-medium"]);
  });
});

describe("availability", () => {
  it("reports fast availability per lead+effort", () => {
    expect(fusionFastAvailable(DESC, "claude-fable-5.1", "high")).toBe(true);
    expect(fusionFastAvailable(DESC, "claude-fable-5.1", "medium")).toBe(false);
  });

  it("reports priority availability per sidekick", () => {
    expect(fusionPriorityAvailable(DESC, "claude-opus-5", "high", false, "gpt-5-6-sol-high")).toBe(
      true,
    );
    expect(fusionPriorityAvailable(DESC, "claude-fable-5.1", "medium", false, "swe-2-medium")).toBe(
      false,
    );
  });
});

describe("resolveFusionModelUid repair", () => {
  it("keeps a fully valid selection", () => {
    expect(
      resolveFusionModelUid(DESC, {
        lead: "claude-fable-5.1",
        effort: "high",
        fast: true,
        sidekick: "swe-2-medium",
        priority: false,
      }),
    ).toBe("fusion-fable-high-fast-swe2medium");
  });

  it("repairs an effort the new lead lacks", () => {
    // Opus has no "medium"; fall to its available "high".
    expect(
      resolveFusionModelUid(DESC, {
        lead: "claude-opus-5",
        effort: "medium",
        fast: false,
        sidekick: "swe-2-medium",
        priority: false,
      }),
    ).toBe("fusion-opus-high-sol");
  });

  it("drops fast when the target has no fast pairing", () => {
    expect(
      resolveFusionModelUid(DESC, {
        lead: "claude-fable-5.1",
        effort: "medium",
        fast: true,
        sidekick: "swe-2-medium",
        priority: false,
      }),
    ).toBe("fusion-fable-medium-swe2medium");
  });

  it("drops priority when the target sidekick has none", () => {
    expect(
      resolveFusionModelUid(DESC, {
        lead: "claude-fable-5.1",
        effort: "medium",
        fast: false,
        sidekick: "swe-2-medium",
        priority: true,
      }),
    ).toBe("fusion-fable-medium-swe2medium");
  });
});

describe("applyFusionChange", () => {
  it("switches lead and repairs the rest", () => {
    // From a fable-medium combo, switching to opus repairs effort medium->high.
    expect(
      applyFusionChange(DESC, "fusion-fable-medium-swe2medium", { lead: "claude-opus-5" }),
    ).toBe("fusion-opus-high-sol");
  });

  it("changes only the named facet, carrying the rest", () => {
    expect(
      applyFusionChange(DESC, "fusion-fable-medium-swe2medium", { sidekick: "swe-2-high" }),
    ).toBe("fusion-fable-medium-swe2high");
  });

  it("toggles priority on a capable sidekick", () => {
    expect(applyFusionChange(DESC, "fusion-opus-high-sol", { priority: true })).toBe(
      "fusion-opus-high-sol-priority",
    );
  });
});
