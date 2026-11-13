import { describe, expect, it } from "vitest";
import { sideChatTabDensity } from "./SideChatTab";

describe("sideChatTabDensity", () => {
  it.each([
    [0, "regular"],
    [3, "regular"],
    [4, "compact"],
    [6, "compact"],
    [7, "dense"],
    [20, "dense"],
  ] as const)("packs %i side-chat tabs as %s", (count, density) => {
    expect(sideChatTabDensity(count)).toBe(density);
  });
});
