import { describe, expect, it } from "vitest";

import { CLAUDE_NATIVE_MODELS } from "@/lib/claudeNativeModels";
import { CODEX_NATIVE_MODELS, isCodexNativeModel } from "@/lib/codexNativeModels";
import { isModelImplicitlySelected } from "./ChatPage";

describe("CLAUDE_NATIVE_MODELS", () => {
  it("offers Claude Code tier aliases, not pinned version IDs", () => {
    // Pinned IDs ("claude-opus-4-7") break the moment a user's Claude
    // Code drops that version — the runner injects `/model <id>` and
    // Claude Code rejects the unknown model. Aliases resolve to whatever
    // the installed version supports, so the list never drifts. Guard
    // against a regression back to version-numbered IDs.
    const ids = CLAUDE_NATIVE_MODELS.map((m) => m.id);
    // Capability order, most powerful first. Fable is temporarily withheld.
    expect(ids).toEqual(["opus", "sonnet", "haiku"]);
    for (const id of ids) {
      expect(id).not.toMatch(/\d/); // an alias carries no version digits
    }
  });

  it("labels each alias by tier", () => {
    expect(CLAUDE_NATIVE_MODELS.map((m) => m.label)).toEqual(["Opus", "Sonnet", "Haiku"]);
  });
});

describe("CODEX_NATIVE_MODELS", () => {
  it("offers Codex subscription-tier model ids", () => {
    expect(CODEX_NATIVE_MODELS.map((m) => m.id)).toEqual([
      "gpt-5.5",
      "gpt-5.4",
      "gpt-5.4-mini",
    ]);
  });

  it("accepts Codex-compatible sticky model ids but rejects Claude aliases", () => {
    expect(isCodexNativeModel("gpt-5.4")).toBe(true);
    expect(isCodexNativeModel("databricks-gpt-5-5")).toBe(true);
    expect(isCodexNativeModel("gpt-5.1-codex")).toBe(true);
    expect(isCodexNativeModel("opus")).toBe(false);
    expect(isCodexNativeModel("claude-opus-4-8")).toBe(false);
  });
});

describe("isModelImplicitlySelected", () => {
  it("matches a tier alias against the bound spec's concrete versioned model", () => {
    // The core of the alias switch: a spec pinned to a brand-new version
    // (Opus 4.8) must still light up the "opus" row, and a now-retired
    // version (4.7) must not break matching — both resolve to the tier.
    expect(isModelImplicitlySelected("opus", "anthropic/claude-opus-4-8")).toBe(true);
    expect(isModelImplicitlySelected("opus", "anthropic/claude-opus-4-7")).toBe(true);
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
