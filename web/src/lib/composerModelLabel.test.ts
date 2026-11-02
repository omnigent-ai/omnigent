import { describe, expect, it } from "vitest";
import type { NativeModelOption } from "@/lib/types";

import {
  compactModelTriggerLabel,
  defaultModelLabel,
  formatModelEffortStatusLabel,
  formatStatusEffortLabel,
  formatStatusModelLabel,
  nativeModelLabel,
  nativeModelTooltip,
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
    expect(compactModelTriggerLabel(defaultModelLabel([row]))).toBe(
      displayName.replace(" (1M context)", " 1M"),
    );
    expect(formatStatusModelLabel(model)).toBe(model);
    expect(formatStatusModelLabel(model, [row])).toBe(displayName);
    expect(formatStatusModelLabel(id, [row])).toBe(displayName);
    expect(row).toEqual({ id, model, displayName, isDefault: true });
  });

  it("condenses context capacity in the composer button label", () => {
    expect(compactModelTriggerLabel("Opus (1M context)")).toBe("Opus 1M");
    expect(compactModelTriggerLabel("Default (Opus (1M context))")).toBe("Opus 1M");
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

  it.each(["system.ai.gpt-6-astra", "databricks-gpt-6-astra"])(
    "hides the catalog namespace when %s is only a transport label",
    (model) => {
      const row = Object.freeze({ id: model, model, displayName: model, isDefault: true });
      expect(nativeModelLabel(row)).toBe("gpt-6-astra");
      expect(defaultModelLabel([row])).toBe("Default (gpt-6-astra)");
      expect(formatStatusModelLabel(model, [row])).toBe("gpt-6-astra");
      expect(row.model).toBe(model);
    },
  );

  it("preserves a deliberate display name that contains a catalog namespace", () => {
    expect(
      nativeModelLabel({
        id: "gpt-6-astra",
        model: "system.ai.gpt-6-astra",
        displayName: "system.ai.gpt-6-astra (managed)",
      }),
    ).toBe("system.ai.gpt-6-astra (managed)");
  });

  it.each(["system.ai.gpt-6-astra", "databricks-gpt-6-astra"])(
    "formats provider-qualified Pi labels for %s without changing selection IDs",
    (displayName) => {
      const id = `omnigent-openai/${displayName}`;
      const row = Object.freeze({ id, model: id, displayName });
      expect(nativeModelLabel(row)).toBe("gpt-6-astra");
      expect(formatStatusModelLabel(id, [row])).toBe("gpt-6-astra");
      expect(row).toEqual({ id, model: id, displayName });
      expect(nativeModelLabel({ ...row, displayName: `${displayName} (team)` })).toBe(
        `${displayName} (team)`,
      );
    },
  );

  it("prefers an exact catalog ID over another row's provider model", () => {
    const rows = [
      { id: "alias", model: "selected-id", displayName: "Alias target" },
      { id: "selected-id", model: "provider/other", displayName: "Selected model" },
    ];
    expect(formatStatusModelLabel("selected-id", rows)).toBe("Selected model");
    expect(formatStatusModelLabel("provider/other", rows)).toBe("Selected model");
  });

  it("resolves catalog prefixes without conflating context variants", () => {
    const rows = [{ id: "opus", model: "claude-opus-4-8", displayName: "Opus" }];
    expect(formatStatusModelLabel("system.ai.claude-opus-4-8", rows)).toBe("Opus");
    expect(formatStatusModelLabel("claude-opus-4-8[1m]", rows)).toBe("claude-opus-4-8[1m]");
  });

  it("replaces a raw status ID with its display name when metadata arrives", () => {
    const model = "gpt-5.6-luna";
    expect(formatStatusModelLabel(model)).toBe(model);
    expect(formatStatusModelLabel(model, [{ id: model, displayName: "GPT-5.6 Luna" }])).toBe(
      "GPT-5.6 Luna",
    );
  });

  it.each([
    ["gpt-6.1-sol", "system.ai.gpt-6-1-sol", "GPT-6.1-Sol"],
    ["SYSTEM.AI.GPT-5-6-LUNA", "gpt-5.6-luna", "GPT-5.6 Luna"],
    ["databricks-claude-sonnet-5", "claude-sonnet-5", "Sonnet 5"],
    ["system.ai.glm-5-3", "glm-5.3", "GLM-5.3"],
    ["GEMINI-2.5-PRO", "gemini-2.5-pro", "Gemini 2.5 Pro"],
    ["QWEN3-CODER", "qwen3-coder", "Qwen3 Coder"],
    ["provider/CUSTOM-MODEL", "provider/custom-model", "Team model"],
  ])("uses the same catalog name for every spelling of %s", (reported, id, displayName) => {
    const option = Object.freeze({ id, model: id, displayName });
    expect(formatStatusModelLabel(reported, [option])).toBe(nativeModelLabel(option));
    expect(formatStatusModelLabel(reported, [option])).toBe(displayName);
    expect(option.id).toBe(id);
  });

  it("keeps both context variants distinct when resolving alternate spellings", () => {
    const rows = [
      { id: "opus", model: "claude-opus-4-8", displayName: "Opus 4.8" },
      { id: "opus[1m]", model: "claude-opus-4-8[1m]", displayName: "Opus 4.8 (1M context)" },
    ];
    expect(formatStatusModelLabel("SYSTEM.AI.CLAUDE-OPUS-4-8[1M]", rows)).toBe(
      "Opus 4.8 (1M context)",
    );
    expect(formatStatusModelLabel("SYSTEM.AI.CLAUDE-OPUS-4-8", rows.toReversed())).toBe("Opus 4.8");
    expect(formatStatusModelLabel("provider-a/custom", [{ id: "provider-b/custom" }])).toBe(
      "provider-a/custom",
    );
  });

  it("resolves Cursor labels without crashing on null model IDs", () => {
    const rows = [
      { id: "auto", model: null, displayName: "Auto" },
      { id: "claude-opus-4-8", model: null, displayName: "Claude Opus 4.8" },
      { id: null, model: null, displayName: "Broken row" },
    ] as unknown as NativeModelOption[];
    expect(formatStatusModelLabel("default", rows)).toBe("default");
    expect(formatStatusModelLabel("databricks-claude-opus-4-8", rows)).toBe("Claude Opus 4.8");
    expect(formatStatusModelLabel("claude-opus-4-8[1m]", rows)).toBe("claude-opus-4-8[1m]");
  });

  it("names the exact model a row launches in its hover text", () => {
    expect(nativeModelTooltip({ id: "opus", model: "system.ai.claude-opus-4-8[1m]" })).toBe(
      "system.ai.claude-opus-4-8[1m]",
    );
    // A label that differs from the id only in case still gets the exact id.
    expect(nativeModelTooltip({ id: "GLM-5.3", displayName: "glm-5.3" })).toBe("GLM-5.3");
    // A namespaced id shown without its prefix keeps the full id on hover.
    const transport = "system.ai.glm-5-3";
    expect(nativeModelTooltip({ id: transport, model: transport, displayName: transport })).toBe(
      transport,
    );
  });

  it.each([
    ["has no display name", { id: "glm-5.3" }],
    ["repeats its id as the label", { id: "glm-5.3", model: "glm-5.3", displayName: "glm-5.3" }],
    ["is the Fusion row", { id: "fusion", fusion: { combos: [], default: "fusion-a-sidekick-b" } }],
  ])("has no hover text when the row %s", (_case, row) => {
    expect(nativeModelTooltip(row)).toBeUndefined();
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
