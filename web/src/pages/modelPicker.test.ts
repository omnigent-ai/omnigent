import { describe, expect, it } from "vitest";

import { CLAUDE_NATIVE_MODELS } from "@/lib/claudeNativeModels";
import {
  codexEffortLevelsForModel,
  findNativeModelOption,
  isCodexNativeModel,
} from "@/lib/codexNativeModels";
import type { NativeModelOption } from "@/lib/types";
import { isModelImplicitlySelected } from "./ChatPage";

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

// Codex's bundled rows spell the version with a dot and repeat that spelling in
// `model`, where the catalog spells the same models `databricks-gpt-5-6-…`.
// Sol carries an "ultra" rung Luna does not.
const CODEX_BUNDLED_OPTIONS: NativeModelOption[] = [
  {
    id: "gpt-5.6-sol",
    model: "gpt-5.6-sol",
    displayName: "GPT-5.6-Sol",
    supportedReasoningEfforts: [
      { reasoningEffort: "low" },
      { reasoningEffort: "medium" },
      { reasoningEffort: "high" },
      { reasoningEffort: "xhigh" },
      { reasoningEffort: "max" },
      { reasoningEffort: "ultra" },
    ],
    isDefault: true,
  },
  {
    id: "gpt-5.6-luna",
    model: "gpt-5.6-luna",
    displayName: "GPT-5.6-Luna",
    supportedReasoningEfforts: [
      { reasoningEffort: "low" },
      { reasoningEffort: "medium" },
      { reasoningEffort: "high" },
      { reasoningEffort: "xhigh" },
      { reasoningEffort: "max" },
    ],
    isDefault: false,
  },
];

describe("CLAUDE_NATIVE_MODELS", () => {
  it("offers Claude Code tier aliases, not pinned version IDs", () => {
    // Pinned IDs ("claude-opus-4-7") break the moment a user's Claude
    // Code drops that version — the runner injects `/model <id>` and
    // Claude Code rejects the unknown model. Aliases resolve to whatever
    // the installed version supports, so the list never drifts. Guard
    // against a regression back to version-numbered IDs.
    const ids = CLAUDE_NATIVE_MODELS.map((m) => m.id);
    // Capability order, most powerful first. "sonnet_5" is the one
    // exception: Claude Code's single custom /model slot, an opt-in for the
    // newer Sonnet offered alongside the default "sonnet" alias (which stays
    // bound to 4.6). The default alias is unchanged.
    expect(ids).toEqual(["fable", "opus", "sonnet", "sonnet_5", "haiku"]);
    for (const id of ids) {
      if (id === "sonnet_5") continue;
      expect(id).not.toMatch(/\d/); // an alias carries no version digits
    }
  });

  it("labels each alias by tier", () => {
    expect(CLAUDE_NATIVE_MODELS.map((m) => m.label)).toEqual([
      "Fable",
      "Opus",
      "Sonnet",
      "Sonnet 5",
      "Haiku",
    ]);
  });
});

describe("Codex model-list helpers", () => {
  it("matches Codex picker aliases and provider-facing model ids", () => {
    expect(findNativeModelOption(CODEX_MODEL_OPTIONS, "gpt-5.5")?.id).toBe("gpt-5.5");
    expect(findNativeModelOption(CODEX_MODEL_OPTIONS, "databricks-gpt-5-5")?.id).toBe("gpt-5.5");
    expect(isCodexNativeModel(CODEX_MODEL_OPTIONS, "gpt-5.4-mini")).toBe(true);
    expect(isCodexNativeModel(CODEX_MODEL_OPTIONS, "databricks-gpt-5-4-mini")).toBe(true);
    expect(isCodexNativeModel(CODEX_MODEL_OPTIONS, "opus")).toBe(false);
  });

  it("derives effort levels from the matched Codex model", () => {
    expect(codexEffortLevelsForModel(CODEX_MODEL_OPTIONS, "gpt-5.4-mini")).toEqual([
      "minimal",
      "low",
      "medium",
    ]);
  });

  it("resolves a catalog-spelled session model onto its Codex row", () => {
    // A Databricks launch records `databricks-gpt-5-6-luna` while Codex lists
    // `gpt-5.6-luna`, so comparing the spellings verbatim finds nothing and the
    // session inherits Sol's ladder — including "ultra", which Luna rejects.
    expect(findNativeModelOption(CODEX_BUNDLED_OPTIONS, "databricks-gpt-5-6-luna")?.id).toBe(
      "gpt-5.6-luna",
    );
    expect(codexEffortLevelsForModel(CODEX_BUNDLED_OPTIONS, "databricks-gpt-5-6-luna")).toEqual([
      "low",
      "medium",
      "high",
      "xhigh",
      "max",
    ]);
  });

  it("offers no effort levels until the model resolves to a Codex row", () => {
    // Codex reports `isDefault` off its bundled catalog, so it stays put even
    // when the session launched on something else. Borrowing that row's ladder
    // offers levels the running model rejects — "xhigh" here is only GPT-5.5's.
    // Both an unresolved model and an id Codex never advertised get nothing.
    expect(codexEffortLevelsForModel(CODEX_MODEL_OPTIONS, null)).toEqual([]);
    expect(codexEffortLevelsForModel(CODEX_MODEL_OPTIONS, "gpt-5.4")).toEqual([]);
  });
});

describe("isModelImplicitlySelected", () => {
  it("matches a tier alias against the bound spec's concrete versioned model", () => {
    // The core of the alias switch: a spec pinned to a brand-new version
    // (Opus 4.8) must still light up the "opus" row, and a now-retired
    // version (4.7) must not break matching — both resolve to the tier.
    expect(isModelImplicitlySelected("opus", "anthropic/claude-opus-4-8")).toBe(true);
    expect(isModelImplicitlySelected("opus", "anthropic/claude-opus-4-7")).toBe(true);
    // The default "sonnet" row is bound to 4.6, so a 4.6 pin lights it up.
    expect(isModelImplicitlySelected("sonnet", "anthropic/claude-sonnet-4-6")).toBe(true);
    // Fable's concrete id (claude-fable-5) must light up the "fable" row.
    expect(isModelImplicitlySelected("fable", "anthropic/claude-fable-5")).toBe(true);
    // ucode gateway IDs carry the tier token too, so the same row lights up.
    expect(isModelImplicitlySelected("haiku", "databricks-claude-haiku-4-5")).toBe(true);
    expect(isModelImplicitlySelected("fable", "databricks-claude-fable-5")).toBe(true);
  });

  it("matches when llmModel is already the bare alias", () => {
    expect(isModelImplicitlySelected("opus", "opus")).toBe(true);
  });

  it("routes Sonnet 5 to its own opt-in row instead of the default Sonnet row", () => {
    // Both ids happen to contain the substring "sonnet", so the default
    // "sonnet" row (4.6) must not also light up for a Sonnet 5 pin.
    expect(isModelImplicitlySelected("sonnet_5", "anthropic/claude-sonnet-5")).toBe(true);
    expect(isModelImplicitlySelected("sonnet", "anthropic/claude-sonnet-5")).toBe(false);
    expect(isModelImplicitlySelected("sonnet_5", "databricks-claude-sonnet-5")).toBe(true);
    // The default 4.6 lights the generic "sonnet" row, never the opt-in row.
    expect(isModelImplicitlySelected("sonnet", "anthropic/claude-sonnet-4-6")).toBe(true);
    expect(isModelImplicitlySelected("sonnet_5", "anthropic/claude-sonnet-4-6")).toBe(false);
  });

  it("does not cross-match a different tier", () => {
    expect(isModelImplicitlySelected("opus", "anthropic/claude-sonnet-4-6")).toBe(false);
    expect(isModelImplicitlySelected("haiku", "anthropic/claude-opus-4-8")).toBe(false);
    expect(isModelImplicitlySelected("fable", "anthropic/claude-opus-4-8")).toBe(false);
    expect(isModelImplicitlySelected("opus", "anthropic/claude-fable-5")).toBe(false);
  });

  it("returns false when no model is bound", () => {
    expect(isModelImplicitlySelected("opus", null)).toBe(false);
  });
});
