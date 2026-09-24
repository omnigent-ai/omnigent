import { describe, expect, it } from "vitest";
import { defaultModelLabel, nativeModelLabel } from "./HarnessConfigControls";

describe("nativeModelLabel", () => {
  it.each([
    ["system.ai.claude-opus-4-6", "Opus"],
    ["system.ai.claude-opus-4-8[1m]", "Opus"],
    ["databricks-claude-sonnet-4-6", "Sonnet"],
    ["claude-haiku-4-5-20251001", "Haiku"],
    ["claude-sonnet-5[1m]", "Sonnet (1M context)"],
  ])("shows the advertised display name for %s", (model, displayName) => {
    expect(nativeModelLabel({ id: "alias", model, displayName })).toBe(displayName);
  });

  it("uses the display name when the catalog omits model", () => {
    expect(nativeModelLabel({ id: "claude-opus-4-6", displayName: "Opus" })).toBe("Opus");
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
    ).toBe("Default (Opus)");
  });
});
