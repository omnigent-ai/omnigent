import { describe, expect, it } from "vitest";

import { selectedCodexModelId } from "./codexModelSelection";

describe("selectedCodexModelId", () => {
  it.each(["system.ai.gpt-test", "vendor/model", "bare-model", "databricks-model"])(
    "carries the selected provider ID without inspecting its spelling: %s",
    (model) => {
      expect(selectedCodexModelId("picker-id", [{ id: "picker-id", model }])).toBe(model);
    },
  );

  it("leaves defaults, old preferences, and incomplete rows unresolved", () => {
    expect(selectedCodexModelId(null, [{ id: "picker", model: "exact" }])).toBeUndefined();
    expect(selectedCodexModelId("old-choice", [{ id: "picker", model: "exact" }])).toBeUndefined();
    expect(selectedCodexModelId("picker", [{ id: "picker" }])).toBeUndefined();
  });
});
