import { describe, expect, it } from "vitest";

import {
  antigravityModelGroupForModel,
  antigravityModelGroups,
} from "@/lib/antigravityNativeModels";
import type { NativeModelOption } from "@/lib/types";

const LIVE_AGY_MODELS: NativeModelOption[] = [
  { id: "gemini-3.8-flash-high", displayName: "Gemini 3.8 Flash (High)" },
  { id: "gemini-3.8-flash-medium", displayName: "Gemini 3.8 Flash (Medium)" },
  { id: "gemini-3.8-flash-low", displayName: "Gemini 3.8 Flash (Low)" },
  { id: "gemini-3.7", displayName: "Gemini 3.7" },
  { id: "gemini-3.1-pro-high", displayName: "Gemini 3.1 Pro (High)" },
  { id: "gemini-3.1-pro-low", displayName: "Gemini 3.1 Pro (Low)" },
  { id: "claude-sonnet-4-6", displayName: "Claude Sonnet 4.6" },
  { id: "claude-opus-4-6-thinking", displayName: "Claude Opus 4.6 Thinking" },
  { id: "gpt-oss-120b-medium", displayName: "GPT-OSS 120B Medium" },
];

describe("antigravityModelGroups", () => {
  it.each(["claude-sonnet-4-6", "gpt-oss-120b", "other-gemini-model"])(
    "keeps advertised %s effort siblings as standalone exact model choices",
    (familyId) => {
      const options: NativeModelOption[] = ["low", "medium", "high"].map((effort) => ({
        id: `${familyId}-${effort}`,
        displayName: `${familyId} ${effort}`,
        isDefault: effort === "medium",
      }));
      const groups = antigravityModelGroups(options);

      expect(groups).toEqual(
        options.map((option) => ({
          ...option,
          source: undefined,
          defaultModelId: option.id,
          efforts: [],
        })),
      );
      for (const option of options) {
        expect(antigravityModelGroupForModel(groups, option.id)?.defaultModelId).toBe(option.id);
      }
    },
  );

  it("derives effort choices only from actual sibling launch ids", () => {
    const groups = antigravityModelGroups(LIVE_AGY_MODELS);

    expect(groups.map((group) => group.displayName)).toEqual([
      "Gemini 3.8 Flash",
      "Gemini 3.7",
      "Gemini 3.1 Pro",
      "Claude Sonnet 4.6",
      "Claude Opus 4.6 Thinking",
      "GPT-OSS 120B Medium",
    ]);
    expect(groups[0]?.efforts).toEqual([
      { value: "low", label: "Low", modelId: "gemini-3.8-flash-low" },
      { value: "medium", label: "Medium", modelId: "gemini-3.8-flash-medium" },
      { value: "high", label: "High", modelId: "gemini-3.8-flash-high" },
    ]);
    // The CLI only exposed High and Low for this model, so Medium is absent.
    expect(groups[2]?.efforts.map((effort) => effort.value)).toEqual(["low", "high"]);
    // A suffix alone is not evidence of a selectable effort family.
    expect(groups[4]?.efforts).toEqual([]);
    expect(groups.at(-1)?.efforts).toEqual([]);
  });

  it("groups xHigh and Max siblings in ascending effort order", () => {
    const groups = antigravityModelGroups([
      { id: "gemini-4-neon-xhigh", displayName: "Gemini 4 Neon (XHigh)" },
      { id: "gemini-4-neon-high", displayName: "Gemini 4 Neon (High)" },
      { id: "gemini-4-neon-max", displayName: "Gemini 4 Neon (Max)" },
      { id: "gemini-4-neon-low", displayName: "Gemini 4 Neon (Low)" },
      { id: "gemini-4-neon-medium", displayName: "Gemini 4 Neon (Medium)" },
    ]);

    expect(groups).toHaveLength(1);
    expect(groups[0]?.id).toBe("gemini-4-neon");
    expect(groups[0]?.displayName).toBe("Gemini 4 Neon");
    expect(groups[0]?.efforts).toEqual([
      { value: "low", label: "Low", modelId: "gemini-4-neon-low" },
      { value: "medium", label: "Medium", modelId: "gemini-4-neon-medium" },
      { value: "high", label: "High", modelId: "gemini-4-neon-high" },
      { value: "xhigh", label: "xHigh", modelId: "gemini-4-neon-xhigh" },
      { value: "max", label: "Max", modelId: "gemini-4-neon-max" },
    ]);
  });

  it("groups a model whose only variants are High and xHigh", () => {
    const groups = antigravityModelGroups([
      { id: "gemini-4-neon-xhigh", displayName: "Gemini 4 Neon XHigh", isDefault: true },
      { id: "gemini-4-neon-high", displayName: "Gemini 4 Neon High" },
    ]);

    expect(groups).toEqual([
      {
        id: "gemini-4-neon",
        displayName: "Gemini 4 Neon",
        isDefault: true,
        source: undefined,
        defaultModelId: "gemini-4-neon-xhigh",
        efforts: [
          { value: "high", label: "High", modelId: "gemini-4-neon-high" },
          { value: "xhigh", label: "xHigh", modelId: "gemini-4-neon-xhigh" },
        ],
      },
    ]);
  });

  it("splits only the final effort token and keeps non-effort endings intact", () => {
    const groups = antigravityModelGroups([
      { id: "gemini-3-flash-lite-high", displayName: "Gemini 3 Flash Lite (High)" },
      { id: "gemini-3-flash-lite-xhigh", displayName: "Gemini 3 Flash Lite (XHigh)" },
      { id: "gemini-4-neon-preview", displayName: "Gemini 4 Neon Preview" },
      // A lone `-max` id is a model name, not an effort choice.
      { id: "gemini-2-ultra-max", displayName: "Gemini 2 Ultra Max" },
    ]);

    expect(groups.map((group) => [group.id, group.displayName])).toEqual([
      ["gemini-3-flash-lite", "Gemini 3 Flash Lite"],
      ["gemini-4-neon-preview", "Gemini 4 Neon Preview"],
      ["gemini-2-ultra-max", "Gemini 2 Ultra Max"],
    ]);
    expect(groups[0]?.efforts.map((effort) => effort.modelId)).toEqual([
      "gemini-3-flash-lite-high",
      "gemini-3-flash-lite-xhigh",
    ]);
    expect(groups[2]?.efforts).toEqual([]);
  });

  it("keeps siblings standalone when the bare family id is also advertised", () => {
    const options: NativeModelOption[] = [
      { id: "gemini-4-neon", displayName: "Gemini 4 Neon" },
      { id: "gemini-4-neon-high", displayName: "Gemini 4 Neon (High)" },
      { id: "gemini-4-neon-xhigh", displayName: "Gemini 4 Neon (XHigh)" },
    ];
    const groups = antigravityModelGroups(options);

    expect(groups.map((group) => group.id)).toEqual(options.map((option) => option.id));
    expect(groups.every((group) => group.efforts.length === 0)).toBe(true);
    expect(antigravityModelGroupForModel(groups, "gemini-4-neon")?.defaultModelId).toBe(
      "gemini-4-neon",
    );
  });

  it("resolves a selected effort back to its exact CLI launch id", () => {
    const groups = antigravityModelGroups(LIVE_AGY_MODELS);
    const selected = antigravityModelGroupForModel(groups, "gemini-3.8-flash-high");

    expect(selected?.id).toBe("gemini-3.8-flash");
    expect(selected?.efforts.find((effort) => effort.value === "high")?.modelId).toBe(
      "gemini-3.8-flash-high",
    );
  });
});
