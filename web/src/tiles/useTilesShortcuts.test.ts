import { fireEvent, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { neighborTile, useTilesShortcuts } from "./useTilesShortcuts";

describe("neighborTile", () => {
  it("wraps around and starts at the first tile", () => {
    expect(neighborTile(["a", "b", "c"], "c", 1)).toBe("a");
    expect(neighborTile(["a", "b", "c"], "a", -1)).toBe("c");
    expect(neighborTile(["a", "b"], null, 1)).toBe("a");
    expect(neighborTile([], "a", 1)).toBeNull();
  });
});

describe("useTilesShortcuts", () => {
  function setup(fullScreen = false) {
    const onActivate = vi.fn();
    const onToggleFullScreen = vi.fn();
    renderHook(() =>
      useTilesShortcuts({
        ids: ["a", "b"],
        activeId: "a",
        onActivate,
        fullScreen,
        onToggleFullScreen,
      }),
    );
    return { onActivate, onToggleFullScreen };
  }

  it("switches tiles and toggles full screen", () => {
    const { onActivate, onToggleFullScreen } = setup();
    fireEvent.keyDown(window, { key: "]" });
    expect(onActivate).toHaveBeenCalledWith("b");
    fireEvent.keyDown(window, { key: "f" });
    expect(onToggleFullScreen).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onToggleFullScreen).toHaveBeenCalledTimes(1);
  });

  it("leaves full screen with Escape", () => {
    const { onToggleFullScreen } = setup(true);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onToggleFullScreen).toHaveBeenCalledTimes(1);
  });

  it("ignores keys typed into a field", () => {
    const { onActivate } = setup();
    const input = document.createElement("input");
    document.body.append(input);
    fireEvent.keyDown(input, { key: "]" });
    expect(onActivate).not.toHaveBeenCalled();
    input.remove();
  });
});
