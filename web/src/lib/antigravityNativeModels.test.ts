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

  it("resolves a selected effort back to its exact CLI launch id", () => {
    const groups = antigravityModelGroups(LIVE_AGY_MODELS);
    const selected = antigravityModelGroupForModel(groups, "gemini-3.8-flash-high");

    expect(selected?.id).toBe("gemini-3.8-flash");
    expect(selected?.efforts.find((effort) => effort.value === "high")?.modelId).toBe(
      "gemini-3.8-flash-high",
    );
  });
});
