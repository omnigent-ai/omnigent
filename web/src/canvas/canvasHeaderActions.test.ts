import { describe, expect, it } from "vitest";

import { canvasHeaderActions } from "./canvasHeaderActions";

describe("canvasHeaderActions", () => {
  it("offers the header's buttons for each canvas layout", () => {
    expect(canvasHeaderActions(null)).toEqual([]);
    expect(
      canvasHeaderActions({ compact: true, focused: false }).map(({ label }) => label),
    ).toEqual(["Back to canvas"]);
    expect(
      canvasHeaderActions({ compact: false, focused: false }).map(({ label }) => label),
    ).toEqual(["Focus conversation", "Close conversation pane"]);
    expect(canvasHeaderActions({ compact: false, focused: true })[0]?.label).toBe(
      "Show canvas beside conversation",
    );
  });
});
