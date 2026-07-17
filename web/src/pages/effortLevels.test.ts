import { describe, expect, it } from "vitest";
import type { NativeModelOption } from "@/lib/types";

import {
  effortLevelsForConv,
  shouldShowCodexPlanModeControl,
  shouldShowEffortPicker,
  shouldShowGoalControl,
  shouldShowModelPicker,
} from "./ChatPage";

const CODEX_MODEL_OPTIONS: NativeModelOption[] = [
  {
    id: "gpt-5.5",
    model: "databricks-gpt-5-5",
    displayName: "GPT-5.5",
    defaultReasoningEffort: "high",
    supportedReasoningEfforts: [
      { reasoningEffort: "low", description: "Low" },
      { reasoningEffort: "medium", description: "Medium" },
      { reasoningEffort: "high", description: "High" },
      { reasoningEffort: "xhigh", description: "Extra high" },
    ],
    isDefault: true,
  },
  {
    id: "gpt-5.4-mini",
    model: "databricks-gpt-5-4-mini",
    displayName: "GPT-5.4 mini",
    defaultReasoningEffort: "medium",
    supportedReasoningEfforts: [
      { reasoningEffort: "minimal", description: "Minimal" },
      { reasoningEffort: "low", description: "Low" },
      { reasoningEffort: "medium", description: "Medium" },
    ],
    isDefault: false,
  },
];

describe("effortLevelsForConv", () => {
  it("returns the Claude-native effort set for claude-native conversations", () => {
    const conv = { labels: { "omnigent.wrapper": "claude-code-native-ui" } };
    expect(effortLevelsForConv(conv)).toEqual(["low", "medium", "high", "xhigh", "max"]);
  });

  it("returns the default 3-level set for non-claude-native conversations", () => {
    // Non-native harnesses (claude-sdk, codex, openai-agents, ...) keep the
    // existing low/medium/high options — we only changed CN, not the
    // shared default.
    const conv = { labels: {} };
    expect(effortLevelsForConv(conv)).toEqual(["low", "medium", "high"]);
  });

  it("returns Codex-native efforts from the selected Codex model option", () => {
    const conv = { labels: { "omnigent.wrapper": "codex-native-ui" } };
    expect(effortLevelsForConv(conv, CODEX_MODEL_OPTIONS, "gpt-5.4-mini")).toEqual([
      "minimal",
      "low",
      "medium",
    ]);
  });

  it("returns an empty Codex-native effort set until Codex options load", () => {
    const conv = { labels: { "omnigent.wrapper": "codex-native-ui" } };
    expect(effortLevelsForConv(conv, [], null)).toEqual([]);
  });

  it("returns the default set when conv is null or labels are missing", () => {
    expect(effortLevelsForConv(null)).toEqual(["low", "medium", "high"]);
    expect(effortLevelsForConv(undefined)).toEqual(["low", "medium", "high"]);
    expect(effortLevelsForConv({})).toEqual(["low", "medium", "high"]);
  });

  it("does not match unrelated ui labels", () => {
    const conv = { labels: { "omnigent.ui": "terminal" } };
    expect(effortLevelsForConv(conv)).toEqual(["low", "medium", "high"]);
  });
});

describe("shouldShowModelPicker", () => {
  it("returns true for claude-code-native-ui wrapper", () => {
    const conv = { labels: { "omnigent.wrapper": "claude-code-native-ui" } };
    expect(shouldShowModelPicker(conv)).toBe(true);
  });

  it("returns true for codex-native-ui wrapper", () => {
    const conv = { labels: { "omnigent.wrapper": "codex-native-ui" } };
    expect(shouldShowModelPicker(conv)).toBe(true);
  });

  it("returns false for the old terminal-ui gate that was rejected on review", () => {
    // Pre-review the picker was gated on ``omnigent.ui === "terminal"``,
    // which would surface Anthropic models on any terminal-first wrapper.
    // The fix was to switch to the wrapper label; this pins that decision.
    const conv = { labels: { "omnigent.ui": "terminal" } };
    expect(shouldShowModelPicker(conv)).toBe(false);
  });

  it("returns false when labels are missing or conv is null/undefined", () => {
    expect(shouldShowModelPicker(null)).toBe(false);
    expect(shouldShowModelPicker(undefined)).toBe(false);
    expect(shouldShowModelPicker({})).toBe(false);
    expect(shouldShowModelPicker({ labels: {} })).toBe(false);
  });

  it("returns false for unrelated wrapper values", () => {
    const conv = { labels: { "omnigent.wrapper": "some-other-wrapper" } };
    expect(shouldShowModelPicker(conv)).toBe(false);
  });
});

describe("shouldShowEffortPicker", () => {
  it("returns false for terminal-UI sessions without the claude wrapper", () => {
    const conv = { labels: { "omnigent.ui": "terminal" } };
    expect(shouldShowEffortPicker(conv)).toBe(false);
  });

  it("returns true for claude-native wrapper sessions", () => {
    expect(
      shouldShowEffortPicker({ labels: { "omnigent.wrapper": "claude-code-native-ui" } }),
    ).toBe(true);
  });

  it("returns true for codex-native wrapper sessions", () => {
    // Codex-native uses Codex app-server `thread/settings/update`, not a
    // terminal slash command, but the UI control is now meaningful.
    expect(shouldShowEffortPicker({ labels: { "omnigent.wrapper": "codex-native-ui" } })).toBe(
      true,
    );
    expect(
      shouldShowEffortPicker({
        labels: {
          "omnigent.ui": "terminal",
          "omnigent.wrapper": "codex-native-ui",
        },
      }),
    ).toBe(true);
  });

  it("returns false for custom agents and missing labels", () => {
    expect(shouldShowEffortPicker({ labels: {} })).toBe(false);
    expect(shouldShowEffortPicker(null)).toBe(false);
    expect(shouldShowEffortPicker(undefined)).toBe(false);
    expect(shouldShowEffortPicker({})).toBe(false);
  });

  it("returns false for unrelated wrapper values", () => {
    const conv = { labels: { "omnigent.wrapper": "nessie" } };
    expect(shouldShowEffortPicker(conv)).toBe(false);
  });
});

describe("shouldShowCodexPlanModeControl", () => {
  it("returns true only for codex-native wrapper sessions", () => {
    expect(
      shouldShowCodexPlanModeControl({ labels: { "omnigent.wrapper": "codex-native-ui" } }),
    ).toBe(true);
    expect(
      shouldShowCodexPlanModeControl({
        labels: { "omnigent.wrapper": "claude-code-native-ui" },
      }),
    ).toBe(false);
    expect(shouldShowCodexPlanModeControl({ labels: { "omnigent.ui": "terminal" } })).toBe(false);
    expect(shouldShowCodexPlanModeControl(null)).toBe(false);
  });
});

describe("shouldShowGoalControl", () => {
  it("keeps the goal control Codex-only until a generic capability exists", () => {
    expect(shouldShowGoalControl({ labels: { "omnigent.wrapper": "codex-native-ui" } })).toBe(true);
    expect(shouldShowGoalControl({ labels: { "omnigent.wrapper": "claude-code-native-ui" } })).toBe(
      false,
    );
    expect(shouldShowGoalControl({ labels: {} })).toBe(false);
    expect(shouldShowGoalControl(null)).toBe(false);
  });
});
