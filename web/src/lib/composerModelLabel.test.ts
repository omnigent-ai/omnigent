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

describe("catalog model labels", () => {
  it.each([
    ["system.ai.gpt-5-5", "system.ai.gpt-5-5", "GPT-5.5"],
    ["gpt-5.6-luna", "system.ai.gpt-5-6-luna", "GPT-5.6 Luna"],
    ["opus[1m]", "system.ai.claude-opus-4-8[1m]", "Opus 4.8 (1M context)"],
    ["custom", "provider/custom-Model_v2", "Team model"],
  ])("uses the advertised label for %s without changing IDs", (id, model, displayName) => {
    const row = Object.freeze({ id, model, displayName, isDefault: true });
    expect(nativeModelLabel(row)).toBe(displayName);
    expect(defaultModelLabel([row])).toBe(`Default (${displayName})`);
    expect(compactModelTriggerLabel(defaultModelLabel([row]))).toBe(displayName);
    expect(formatStatusModelLabel(model)).toBe(model);
    expect(formatStatusModelLabel(model, [row])).toBe(displayName);
    expect(formatStatusModelLabel(id, [row])).toBe(displayName);
    expect(row).toEqual({ id, model, displayName, isDefault: true });
  });

  it.each(["sonnet", "sonnet_5", "opus[1m]", "Unrecognized-ID"])(
    "does not rewrite %s when no display name is available",
    (id) => {
      expect(nativeModelLabel({ id })).toBe(id);
      expect(formatStatusModelLabel(id)).toBe(id);
      expect(compactModelTriggerLabel(id)).toBe(id);
    },
  );

  it("falls back to the provider model before an alias when the label is absent", () => {
    const row = { id: "alias", model: "provider/custom-Model_v2", isDefault: true };
    expect(nativeModelLabel(row)).toBe(row.model);
    expect(formatStatusModelLabel("alias", [row])).toBe(row.model);
    expect(defaultModelLabel([row])).toBe(`Default (${row.model})`);
  });

  it("uses an alias's display name without guessing a version", () => {
    expect(nativeModelLabel({ id: "opus", displayName: "Opus" })).toBe("Opus");
  });

  it("prefers an exact catalog ID over another row's provider model", () => {
    const rows = [
      { id: "alias", model: "selected-id", displayName: "Alias target" },
      { id: "selected-id", model: "provider/other", displayName: "Selected model" },
    ];
    expect(formatStatusModelLabel("selected-id", rows)).toBe("Selected model");
    expect(formatStatusModelLabel("provider/other", rows)).toBe("Selected model");
  });

  it("does not fold catalog prefixes or conflate context variants", () => {
    const rows = [{ id: "opus", model: "claude-opus-4-8", displayName: "Opus" }];
    expect(formatStatusModelLabel("system.ai.claude-opus-4-8", rows)).toBe(
      "system.ai.claude-opus-4-8",
    );
    expect(formatStatusModelLabel("claude-opus-4-8[1m]", rows)).toBe("claude-opus-4-8[1m]");
  });

  it("replaces a raw status ID with its display name when metadata arrives", () => {
    const model = "gpt-5.6-luna";
    expect(formatStatusModelLabel(model)).toBe(model);
    expect(formatStatusModelLabel(model, [{ id: model, displayName: "GPT-5.6 Luna" }])).toBe(
      "GPT-5.6 Luna",
    );
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

  it("joins the catalog display name and effort without exposing the wire ID", () => {
    const row = {
      id: "picker-alias",
      model: "provider/custom-Model_v2",
      displayName: "Team model",
    };
    expect(formatModelEffortStatusLabel(row.model, "xhigh", [row])).toBe("Team model xHigh");
  });
});
