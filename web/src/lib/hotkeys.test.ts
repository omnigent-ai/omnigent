import { describe, expect, it } from "vitest";

import { hasCommandModifier } from "./hotkeys";

describe("hasCommandModifier", () => {
  it("requires Meta exclusively on macOS", () => {
    expect(hasCommandModifier({ metaKey: true, ctrlKey: false }, true)).toBe(true);
    expect(hasCommandModifier({ metaKey: false, ctrlKey: true }, true)).toBe(false);
    expect(hasCommandModifier({ metaKey: true, ctrlKey: true }, true)).toBe(false);
  });

  it("requires Control exclusively on Windows and Linux", () => {
    expect(hasCommandModifier({ metaKey: false, ctrlKey: true }, false)).toBe(true);
    expect(hasCommandModifier({ metaKey: true, ctrlKey: false }, false)).toBe(false);
    expect(hasCommandModifier({ metaKey: true, ctrlKey: true }, false)).toBe(false);
  });
});
