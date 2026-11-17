import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { setTileTextSize, stepTextSize, useTileTextSize } from "./tileTextSize";

afterEach(() => localStorage.clear());

describe("tile text size", () => {
  it("steps along the scale and stops at its ends", () => {
    expect(stepTextSize(1, 1)).toBe(1.15);
    expect(stepTextSize(1.3, 1)).toBe(1.3);
    expect(stepTextSize(0.85, -1)).toBe(0.85);
  });

  it("remembers a size per session and forgets the default", () => {
    const { result } = renderHook(() => useTileTextSize("conv_a"));
    act(() => setTileTextSize("conv_a", 1.3));
    expect(result.current).toBe(1.3);
    act(() => setTileTextSize("conv_a", 1));
    expect(result.current).toBe(1);
    expect(localStorage.getItem("omnigent:tiles-text-size")).toBe("{}");
  });
});
