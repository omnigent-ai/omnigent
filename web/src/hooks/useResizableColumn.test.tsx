import { act, fireEvent, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useResizableColumn } from "./useResizableColumn";

const storageKey = "test:column-width";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("useResizableColumn", () => {
  it("preserves the terminal defaults without storing a preference", () => {
    const { result } = renderHook(() => useResizableColumn());
    expect(result.current.width).toBe(176);
    expect(result.current.handleProps["aria-label"]).toBe("Resize terminal list");
    expect(localStorage.length).toBe(0);
  });

  it.each([
    [null, 260],
    ["invalid", 260],
    ["Infinity", 260],
    ["0", 260],
    ["-20", 260],
    ["100", 200],
    ["300", 300],
    ["700", 400],
  ])("restores and clamps stored width %s", (stored, expected) => {
    if (stored !== null) localStorage.setItem(storageKey, stored);
    const { result } = renderHook(() => useResizableColumn(260, 200, 400, storageKey));
    expect(result.current.width).toBe(expected);
  });

  it("persists a drag only on release and restores it on remount", () => {
    const { result, unmount } = renderHook(() => useResizableColumn(260, 200, 400, storageKey));
    const container = document.createElement("div");
    vi.spyOn(container, "getBoundingClientRect").mockReturnValue({ left: 100 } as DOMRect);
    result.current.containerRef.current = container;

    act(() => {
      result.current.handleProps.onMouseDown({
        preventDefault: vi.fn(),
      } as unknown as React.MouseEvent);
      fireEvent.mouseMove(window, { clientX: 420 });
    });
    expect(result.current.width).toBe(320);
    expect(localStorage.getItem(storageKey)).toBeNull();
    expect(document.body.style.cursor).toBe("col-resize");

    act(() => {
      fireEvent.mouseMove(window, { clientX: 440 });
      fireEvent.mouseUp(window);
    });
    expect(localStorage.getItem(storageKey)).toBe("340");
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
    unmount();

    const restored = renderHook(() => useResizableColumn(260, 200, 400, storageKey));
    expect(restored.result.current.width).toBe(340);
  });

  it("exposes accessible keyboard resizing and clamps at both bounds", () => {
    const { result } = renderHook(() => useResizableColumn(260, 250, 280, storageKey));
    const press = (key: string) => {
      const preventDefault = vi.fn();
      act(() =>
        result.current.handleProps.onKeyDown({
          key,
          preventDefault,
        } as unknown as React.KeyboardEvent),
      );
      return preventDefault;
    };

    expect(result.current.handleProps).toMatchObject({
      role: "separator",
      tabIndex: 0,
      "aria-orientation": "vertical",
      "aria-valuenow": 260,
      "aria-valuemin": 250,
      "aria-valuemax": 280,
    });
    expect(press("ArrowRight")).toHaveBeenCalledOnce();
    expect(result.current.width).toBe(276);
    press("ArrowRight");
    expect(result.current.width).toBe(280);
    press("ArrowLeft");
    expect(result.current.width).toBe(264);
    press("ArrowLeft");
    expect(result.current.handleProps["aria-valuenow"]).toBe(250);
    expect(localStorage.getItem(storageKey)).toBe("250");
    expect(press("Enter")).not.toHaveBeenCalled();
    expect(result.current.width).toBe(250);
  });

  it.each(["click", "vertical movement", "ArrowRight", "outward drag"])(
    "keeps the preferred width after a clamped %s",
    (gesture) => {
      localStorage.setItem(storageKey, "340");
      const { result, rerender } = renderHook(
        ({ min, max }) => useResizableColumn(260, min, max, storageKey),
        { initialProps: { min: 200, max: 400 } },
      );
      rerender({ min: 200, max: 240 });
      expect(result.current.width).toBe(240);
      expect(result.current.handleProps["aria-valuemax"]).toBe(240);
      const container = document.createElement("div");
      vi.spyOn(container, "getBoundingClientRect").mockReturnValue({ left: 100 } as DOMRect);
      result.current.containerRef.current = container;
      act(() => {
        if (gesture === "ArrowRight") {
          result.current.handleProps.onKeyDown({
            key: gesture,
            preventDefault: vi.fn(),
          } as unknown as React.KeyboardEvent);
        } else {
          result.current.handleProps.onMouseDown({
            clientX: 340,
            preventDefault: vi.fn(),
          } as unknown as React.MouseEvent);
          if (gesture === "vertical movement") {
            fireEvent.mouseMove(window, { clientX: 340, clientY: 100 });
          } else if (gesture === "outward drag") {
            fireEvent.mouseMove(window, { clientX: 400 });
          }
          fireEvent.mouseUp(window);
        }
      });
      expect(localStorage.getItem(storageKey)).toBe("340");
      rerender({ min: 200, max: 400 });
      expect(result.current.width).toBe(340);
      rerender({ min: 360, max: 400 });
      expect(result.current.width).toBe(360);

      rerender({ min: 200, max: 240 });
      act(() =>
        result.current.handleProps.onKeyDown({
          key: "ArrowLeft",
          preventDefault: vi.fn(),
        } as unknown as React.KeyboardEvent),
      );
      expect(result.current.width).toBe(224);
      expect(localStorage.getItem(storageKey)).toBe("224");
      act(() => {
        result.current.handleProps.onMouseDown({
          clientX: 324,
          preventDefault: vi.fn(),
        } as unknown as React.MouseEvent);
        fireEvent.mouseMove(window, { clientX: 310 });
        fireEvent.mouseUp(window);
      });
      expect(localStorage.getItem(storageKey)).toBe("210");
      rerender({ min: 200, max: 400 });
      expect(result.current.width).toBe(210);
    },
  );

  it("allows resizing when storage is unavailable and cleans up an interrupted drag", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("Storage unavailable");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("Storage unavailable");
    });
    const { result, unmount } = renderHook(() => useResizableColumn(260, 200, 400, storageKey));
    expect(result.current.width).toBe(260);
    act(() =>
      result.current.handleProps.onKeyDown({
        key: "ArrowRight",
        preventDefault: vi.fn(),
      } as unknown as React.KeyboardEvent),
    );
    expect(result.current.width).toBe(276);
    act(() =>
      result.current.handleProps.onMouseDown({
        preventDefault: vi.fn(),
      } as unknown as React.MouseEvent),
    );
    unmount();
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
  });
});
