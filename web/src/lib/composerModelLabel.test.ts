import { describe, expect, it } from "vitest";

import {
  compactModelTriggerLabel,
  defaultModelLabel,
  formatModelEffortStatusLabel,
  formatStatusEffortLabel,
  formatStatusModelLabel,
  nativeModelLabel,
  normalizeEffortLabel,
} from "@/lib/composerModelLabel";

describe("raw model labels", () => {
  it.each([
    "claude-opus-4-8",
    "system.ai.claude-opus-4-8[1m]",
    "databricks-claude-sonnet-4-6",
    "claude-sonnet-4-6-20260101",
    "gpt-5.6-luna",
    "provider/custom-Model_v2",
  ])("shows the exact model ID %s, not its display name", (model) => {
    const row = { id: "alias", model, displayName: "Friendly name", isDefault: true };
    expect(nativeModelLabel(row)).toBe(model);
    expect(defaultModelLabel([row])).toBe(`Default (${model})`);
    expect(compactModelTriggerLabel(defaultModelLabel([row]))).toBe(model);
    expect(formatStatusModelLabel(model)).toBe(model);
    expect(formatStatusModelLabel(model, [row])).toBe(model);
    expect(formatStatusModelLabel("alias", [row])).toBe(model);
  });

  it.each(["sonnet", "sonnet_5", "opus[1m]", "Unrecognized-ID"])(
    "does not rewrite the unresolved ID %s",
    (id) => {
      expect(nativeModelLabel({ id, displayName: "Friendly name" })).toBe(id);
      expect(formatStatusModelLabel(id)).toBe(id);
      expect(compactModelTriggerLabel(id)).toBe(id);
    },
  );

  it("does not fold catalog prefixes or conflate context variants", () => {
    const rows = [{ id: "opus", model: "claude-opus-4-8", displayName: "Opus" }];
    expect(formatStatusModelLabel("system.ai.claude-opus-4-8", rows)).toBe(
      "system.ai.claude-opus-4-8",
    );
    expect(formatStatusModelLabel("claude-opus-4-8[1m]", rows)).toBe("claude-opus-4-8[1m]");
  });

  it("does not replace a Codex ID with its catalog display name on arrival", () => {
    const model = "gpt-5.6-luna";
    expect(formatStatusModelLabel(model)).toBe(model);
    expect(formatStatusModelLabel(model, [{ id: model, displayName: "GPT-5.6 Luna" }])).toBe(model);
  });

  it("retains the unknown and unmarked default states", () => {
    expect(defaultModelLabel([{ id: "opus" }])).toBe("Default");
    expect(formatStatusModelLabel(null)).toBeNull();
    expect(formatStatusModelLabel("  ")).toBeNull();
  });
});

describe("effort labels", () => {
  it("normalizes effort independently of the model ID", () => {
    expect(normalizeEffortLabel("xhigh")).toBe("xHigh");
    expect(normalizeEffortLabel("XHIGH")).toBe("xHigh");
    expect(formatStatusEffortLabel("high")).toBe("High");
    expect(formatStatusEffortLabel(null)).toBeNull();
    expect(formatStatusEffortLabel("")).toBeNull();
  });

  it("joins the unmodified model ID and effort", () => {
    expect(formatModelEffortStatusLabel("claude-opus-4-8[1m]", "xhigh")).toBe(
      "claude-opus-4-8[1m] xHigh",
    );
    expect(formatModelEffortStatusLabel("gpt-5.5", null)).toBe("gpt-5.5");
    expect(formatModelEffortStatusLabel(null, "high")).toBe("High");
    expect(formatModelEffortStatusLabel(null, null)).toBeNull();
  });
});
