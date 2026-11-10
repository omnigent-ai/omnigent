import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useResizableColumn } from "./useResizableColumn";

const STORAGE_KEY = "test:column-width";
interface Column {
  current: ReturnType<typeof useResizableColumn>;
}

function startDrag(result: Column, clientX = 300) {
  act(() =>
    result.current.handleProps.onMouseDown({
      button: 0,
      clientX,
      preventDefault: () => {},
    } as React.MouseEvent),
  );
}

function move(clientX: number, clientY = 0) {
  act(() => window.dispatchEvent(new MouseEvent("mousemove", { clientX, clientY })));
}

function release() {
  act(() => window.dispatchEvent(new MouseEvent("mouseup")));
}

function press(result: Column, key: string) {
  const preventDefault = vi.fn();
  act(() =>
    result.current.handleProps.onKeyDown({ key, preventDefault } as unknown as React.KeyboardEvent),
  );
  return preventDefault;
}

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("useResizableColumn", () => {
  it("keeps the existing defaults without persisting a width", () => {
    const { result } = renderHook(() => useResizableColumn());
    expect(result.current.width).toBe(176);
    expect(result.current.handleProps["aria-label"]).toBe("Resize terminal list");
    press(result, "ArrowRight");
    expect(result.current.width).toBe(192);
    expect(localStorage.length).toBe(0);
  });

  it.each([
    [null, 260],
    ["bad", 260],
    ["Infinity", 260],
    ["0", 260],
    ["-1", 260],
    ["100", 200],
    ["300", 300],
    ["600", 400],
  ])("validates and clamps stored width %s", (stored, expected) => {
    if (stored !== null) localStorage.setItem(STORAGE_KEY, stored);
    const { result } = renderHook(() => useResizableColumn(260, 200, 400, STORAGE_KEY));
    expect(result.current.width).toBe(expected);
  });

  it("resizes from the grabbed edge and persists only on release", () => {
    const { result, unmount } = renderHook(() => useResizableColumn(260, 200, 400, STORAGE_KEY));
    startDrag(result, 500);
    move(560);
    expect(result.current.width).toBe(320);
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
    move(500);
    expect(result.current.width).toBe(260);
    move(540);
    release();
    expect(localStorage.getItem(STORAGE_KEY)).toBe("300");
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
    unmount();
    const restored = renderHook(() => useResizableColumn(260, 200, 400, STORAGE_KEY));
    expect(restored.result.current.width).toBe(300);
  });

  it("exposes keyboard sizing and accessible bounds", () => {
    const { result } = renderHook(() => useResizableColumn(208, 200, 224, STORAGE_KEY));
    expect(result.current.handleProps).toMatchObject({
      role: "separator",
      tabIndex: 0,
      "aria-orientation": "vertical",
      "aria-valuenow": 208,
      "aria-valuemin": 200,
      "aria-valuemax": 224,
    });
    expect(press(result, "Enter")).not.toHaveBeenCalled();
    expect(press(result, "ArrowRight")).toHaveBeenCalledOnce();
    expect(result.current.width).toBe(224);
    expect(localStorage.getItem(STORAGE_KEY)).toBe("224");
    press(result, "ArrowLeft");
    press(result, "ArrowLeft");
    expect(result.current.handleProps["aria-valuenow"]).toBe(200);
    expect(localStorage.getItem(STORAGE_KEY)).toBe("200");
  });

  it.each(["click", "vertical movement", "outward drag", "ArrowRight"])(
    "preserves a constrained preference after a no-op %s",
    (gesture) => {
      localStorage.setItem(STORAGE_KEY, "400");
      const { result, rerender } = renderHook(
        ({ maximum }) => useResizableColumn(260, 200, maximum, STORAGE_KEY),
        { initialProps: { maximum: 500 } },
      );
      rerender({ maximum: 300 });
      expect(result.current.width).toBe(300);
      if (gesture === "ArrowRight") press(result, gesture);
      else {
        startDrag(result);
        if (gesture === "vertical movement") move(300, 30);
        if (gesture === "outward drag") move(340);
        release();
      }
      expect(localStorage.getItem(STORAGE_KEY)).toBe("400");
      rerender({ maximum: 500 });
      expect(result.current.width).toBe(400);
      rerender({ maximum: 300 });
      press(result, "ArrowLeft");
      expect(localStorage.getItem(STORAGE_KEY)).toBe("284");
      rerender({ maximum: 500 });
      expect(result.current.width).toBe(284);
    },
  );

  it("resizes without storage and releases an interrupted drag on unmount", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("Storage unavailable");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("Storage unavailable");
    });
    const { result, unmount } = renderHook(() => useResizableColumn(260, 200, 400, STORAGE_KEY));
    press(result, "ArrowRight");
    expect(result.current.width).toBe(276);
    startDrag(result);
    move(350);
    expect(result.current.width).toBe(326);
    expect(document.body.style.cursor).toBe("col-resize");
    unmount();
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
  });
});
