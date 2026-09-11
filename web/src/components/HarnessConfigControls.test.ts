import { describe, expect, it } from "vitest";
import { defaultModelLabel, nativeModelLabel } from "./HarnessConfigControls";

describe("nativeModelLabel", () => {
  it.each([
    ["system.ai.claude-opus-4-6", "Opus", "Opus 4.6"],
    ["system.ai.claude-opus-4-8[1m]", "Opus", "Opus 4.8 (1M context)"],
    ["databricks-claude-sonnet-4-6", "Sonnet", "Sonnet 4.6"],
    ["claude-haiku-4-5-20251001", "Haiku", "Haiku 4.5"],
    ["claude-sonnet-5[1m]", "Sonnet (1M context)", "Sonnet 5 (1M context)"],
  ])("completes the family label using %s", (model, displayName, expected) => {
    expect(nativeModelLabel({ id: "alias", model, displayName })).toBe(expected);
  });

  it("uses the versioned id when the catalog omits model", () => {
    expect(nativeModelLabel({ id: "claude-opus-4-6", displayName: "Opus" })).toBe("Opus 4.6");
  });

  it("does not guess the version of an unresolved alias", () => {
    expect(nativeModelLabel({ id: "opus", displayName: "Opus" })).toBe("Opus");
    expect(nativeModelLabel({ id: "opus" })).toBe("opus");
  });

  it("preserves explicit catalog labels and other vendors", () => {
    expect(
      nativeModelLabel({ id: "opus", model: "claude-opus-4-6", displayName: "Team model" }),
    ).toBe("Team model");
    expect(
      nativeModelLabel({ id: "opus", model: "claude-opus-4-6", displayName: "Opus 4.6" }),
    ).toBe("Opus 4.6");
    expect(nativeModelLabel({ id: "gpt-5.5", displayName: "GPT-5.5" })).toBe("GPT-5.5");
  });

  it("uses the same resolved name for the default choice", () => {
    expect(
      defaultModelLabel([
        { id: "opus", model: "claude-opus-4-6", displayName: "Opus", isDefault: true },
      ]),
    ).toBe("Default (Opus 4.6)");
  });
});
