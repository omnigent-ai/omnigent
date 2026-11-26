import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { autoHue, setTileHue, TILE_HUES, useTileColor } from "./tileColors";
import { TileColorPicker } from "./TileColorPicker";

afterEach(() => localStorage.clear());

describe("autoHue", () => {
  it("picks a stable palette hue per session", () => {
    expect(autoHue("conv_a")).toBe(autoHue("conv_a"));
    expect(TILE_HUES.map((entry) => entry.hue)).toContain(autoHue("conv_b"));
  });
});

describe("useTileColor", () => {
  it("follows a picked hue and returns to Auto", () => {
    const { result } = renderHook(() => useTileColor("conv_a"));
    expect(result.current.picked).toBe(false);
    expect(result.current.hue).toBe(autoHue("conv_a"));

    act(() => setTileHue("conv_a", 214));
    expect(result.current).toMatchObject({ hue: 214, picked: true, solid: "hsl(214 70% 52%)" });

    act(() => setTileHue("conv_a", null));
    expect(result.current.picked).toBe(false);
    expect(JSON.parse(localStorage.getItem("omnigent:tiles-colors") ?? "{}")).toEqual({});
  });

  it("ignores stored hues outside the palette", () => {
    localStorage.setItem("omnigent:tiles-colors", JSON.stringify({ conv_a: 7, conv_b: 0 }));
    expect(renderHook(() => useTileColor("conv_a")).result.current.picked).toBe(false);
    expect(renderHook(() => useTileColor("conv_b")).result.current.hue).toBe(0);
  });
});

describe("TileColorPicker", () => {
  function Harness() {
    const color = useTileColor("conv_a");
    return <TileColorPicker sessionId="conv_a" title="Example" color={color} />;
  }

  it("opens from the stripe and applies a swatch", () => {
    render(<Harness />);
    fireEvent.click(screen.getByTestId("tile-color-stripe"));
    fireEvent.click(screen.getByRole("button", { name: "Violet" }));
    expect(screen.getByTestId("tile-color-stripe")).toHaveStyle({
      backgroundColor: "hsl(268 70% 52%)",
    });
  });
});
