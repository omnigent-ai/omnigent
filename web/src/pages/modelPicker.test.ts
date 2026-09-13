import { describe, expect, it } from "vitest";

import { CLAUDE_NATIVE_MODELS } from "@/lib/claudeNativeModels";
import {
  codexEffortLevelsForModel,
  findNativeModelOption,
  isCodexNativeModel,
} from "@/lib/codexNativeModels";
import type { NativeModelOption } from "@/lib/types";

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

  it("does not throw when a native row carries a null model (cursor rows)", () => {
    // Cursor picker rows arrive as { id, displayName } with model === null on
    // the wire (typed model?: string). The fold fallback must null-guard, or
    // comparableModelId(null) throws "Cannot read properties of null
    // (reading 'trim')" and blanks the whole chat page.
    const cursorRows = [
      { id: "auto", model: null, displayName: "Auto" },
      { id: "gpt-5.3-codex", model: null, displayName: "Codex 5.3" },
      { id: "composer-2.5", model: null, displayName: "Composer 2.5" },
    ] as unknown as NativeModelOption[];
    expect(() => findNativeModelOption(cursorRows, "default")).not.toThrow();
    expect(findNativeModelOption(cursorRows, "default")).toBeNull();
    // Exact id still resolves against null-model rows.
    expect(findNativeModelOption(cursorRows, "composer-2.5")?.id).toBe("composer-2.5");
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
