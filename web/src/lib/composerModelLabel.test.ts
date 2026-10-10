import { describe, expect, it } from "vitest";

import {
  buildComposerSessionDescriptor,
  isCostRoutingEligible,
  compactModelTriggerLabel,
  composerModelChipLabel,
  defaultModelLabel,
  formatModelEffortStatusLabel,
  formatStatusEffortLabel,
  formatStatusModelLabel,
  nativeModelLabel,
  normalizeEffortLabel,
} from "@/lib/composerModelLabel";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";

describe("shared routing eligibility", () => {
  it.each([
    [false, "claude-sdk", null, true, true, false, false],
    [true, "claude-sdk", null, false, false, false, true],
    [true, "codex", "parent", true, true, true, false],
    [true, "claude-native", null, true, true, false, true],
    [true, "codex-native", null, false, true, false, false],
    [true, "codex-native", null, false, false, true, true],
    [true, "devin-native", null, true, true, true, false],
  ] as const)(
    "matches the composer gates %s %s %s %s %s %s",
    (enabled, harness, parentSessionId, gateway, external, oss, expected) => {
      const info = {
        ...FALLBACK_SERVER_INFO,
        smart_routing_enabled: enabled,
        smart_routing_sources: { external, oss },
      };
      const session = {
        ...buildComposerSessionDescriptor(harness, {}),
        labels: {},
        parentSessionId,
        agentName: "agent",
      };
      expect(
        isCostRoutingEligible(info, session, { gateway_inference: { [harness]: gateway } }),
      ).toBe(expected);
      expect(isCostRoutingEligible("loading", session)).toBe(false);
      expect(isCostRoutingEligible(info, { ...session, agentName: null })).toBe(false);
    },
  );
});

describe("composer session descriptors", () => {
  it("preserves the resolved harness, actual wrapper and session metadata without mutation", () => {
    const labels = Object.freeze({ "omnigent.wrapper": "claude-code-native-ui", custom: "value" });
    const descriptor = buildComposerSessionDescriptor("devin-native", labels, "parent", true);
    expect(descriptor).toEqual({
      harness: "devin-native",
      labels,
      parentSessionId: "parent",
      inferenceConfigured: true,
    });
    expect(descriptor.labels).toBe(labels);
    expect(labels).toEqual({ "omnigent.wrapper": "claude-code-native-ui", custom: "value" });
  });

  it.each([
    ["devin-native", null],
    ["codex-native", "High"],
  ] as const)("never synthesizes wrapper evidence for label-less %s", (harness, effortLabel) => {
    const descriptor = buildComposerSessionDescriptor(harness, null);
    expect(descriptor.labels).toEqual({});
    expect(descriptor.harness).toBe(harness);
    expect(
      composerModelChipLabel({
        session: descriptor,
        model: "model",
        modelOptions: [{ id: "model", supportedReasoningEfforts: [{ reasoningEffort: "high" }] }],
        effort: "high",
      }).effortLabel,
    ).toBe(effortLabel);
  });
});

describe("composer chip labels", () => {
  const nativeSession = { labels: { "omnigent.wrapper": "claude-code-native-ui" } };
  it.each([
    ["Default (Opus 5.5 (1M context))", "Opus 5.5 1M"],
    [null, "Claude Code"],
  ])("shares the native label and effort for %s", (modelSummary, expected) => {
    expect(
      composerModelChipLabel({
        modelSummary,
        nativeDisplayName: "Claude Code",
        session: nativeSession,
        effort: "medium",
      }),
    ).toEqual({ label: expected, effortLabel: "Medium" });
  });
  it("uses the harness then Session fallback", () => {
    expect(composerModelChipLabel({ harnessLabel: "Aria · Claude SDK" }).label).toBe(
      "Aria · Claude SDK",
    );
    expect(composerModelChipLabel({}).label).toBe("Session");
  });
  it("does not show effort for unsupported sessions", () => {
    expect(
      composerModelChipLabel({
        session: { labels: { "omnigent.wrapper": "cursor-native-ui" } },
        effort: "high",
      }).effortLabel,
    ).toBeNull();
  });
  it.each(["codex-native-ui", "devin-native-ui"])(
    "uses the per-model effort ladder for %s",
    (wrapper) => {
      const inputs = {
        session: { labels: { "omnigent.wrapper": wrapper } },
        model: "model",
        effort: "medium",
      };
      expect(composerModelChipLabel(inputs).effortLabel).toBeNull();
      expect(
        composerModelChipLabel({
          ...inputs,
          modelOptions: [{ id: "model", supportedReasoningEfforts: [] }],
        }).effortLabel,
      ).toBeNull();
      const modelOptions = [
        { id: "model", supportedReasoningEfforts: [{ reasoningEffort: "medium" }] },
      ];
      expect(composerModelChipLabel({ ...inputs, modelOptions }).effortLabel).toBe("Medium");
      expect(
        composerModelChipLabel({ ...inputs, model: null, modelOptions }).effortLabel,
      ).toBeNull();
    },
  );
  it("preserves the composer's loading and smart-routing states", () => {
    expect(
      composerModelChipLabel({ modelLabelLoading: true, nativeDisplayName: "Claude Code" }).label,
    ).toBe("");
    expect(
      composerModelChipLabel({
        routingOn: true,
        modelLabelLoading: true,
        session: nativeSession,
        effort: "high",
      }),
    ).toEqual({ label: "Smart Routing", effortLabel: null });
  });
});

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
