import { describe, expect, it } from "vitest";
import {
  CLAUDE_NATIVE_EFFORTS,
  PI_NATIVE_EFFORTS,
  defaultModelLabel,
  nativeEffortOptions,
  nativeModelLabel,
} from "./HarnessConfigControls";

describe("nativeEffortOptions", () => {
  const codexCatalog = [
    {
      id: "gpt-5.5",
      isDefault: true,
      supportedReasoningEfforts: [{ reasoningEffort: "medium" }, { reasoningEffort: "xhigh" }],
    },
    { id: "gpt-mini", supportedReasoningEfforts: [{ reasoningEffort: "low" }] },
  ];

  it("returns the fixed Claude Code and Pi ladders", () => {
    expect(nativeEffortOptions("claude-native", [], null)).toBe(CLAUDE_NATIVE_EFFORTS);
    expect(nativeEffortOptions("pi-native", [], "any")).toBe(PI_NATIVE_EFFORTS);
  });

  it("reads Codex levels from the pinned model, else the catalog default", () => {
    expect(nativeEffortOptions("codex-native", codexCatalog, "gpt-mini")).toEqual([
      { value: "low", label: "Low" },
    ]);
    expect(nativeEffortOptions("codex-native", codexCatalog, "")).toEqual([
      { value: "medium", label: "Medium" },
      { value: "xhigh", label: "xHigh" },
    ]);
    expect(nativeEffortOptions("codex-native", [], null)).toEqual([]);
  });

  it("returns null for harnesses without a create-time effort", () => {
    expect(nativeEffortOptions("cursor-native", codexCatalog, null)).toBeNull();
    expect(nativeEffortOptions(null, [], null)).toBeNull();
  });
});

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
