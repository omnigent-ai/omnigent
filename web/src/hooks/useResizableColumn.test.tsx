import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useResizableColumn } from "./useResizableColumn";

const STORAGE_KEY = "omnigent:test-column-width";

interface ColumnResult {
  current: ReturnType<typeof useResizableColumn>;
}

// Container whose left edge sits at `left`, so a pointer at clientX renders clientX - left.
function attachContainer(result: ColumnResult, left = 100): void {
  const container = document.createElement("div");
  container.getBoundingClientRect = () => ({ left }) as DOMRect;
  result.current.containerRef.current = container;
}

function pressHandle(result: ColumnResult): void {
  act(() =>
    result.current.handleProps.onMouseDown({ preventDefault: () => {} } as React.MouseEvent),
  );
}

function moveTo(clientX: number): void {
  act(() => {
    window.dispatchEvent(new MouseEvent("mousemove", { clientX }));
  });
}

function release(): void {
  act(() => {
    window.dispatchEvent(new MouseEvent("mouseup"));
  });
}

// Returns the preventDefault spy so tests can check whether the key was handled.
function pressKey(result: ColumnResult, key: string) {
  const preventDefault = vi.fn();
  act(() =>
    result.current.handleProps.onKeyDown({
      key,
      preventDefault,
    } as unknown as React.KeyboardEvent),
  );
  return preventDefault;
}

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("useResizableColumn storage", () => {
  it("never touches storage without a storage key", () => {
    const getItem = vi.spyOn(Storage.prototype, "getItem");
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const { result } = renderHook(() => useResizableColumn(200, 100, 480));

    pressKey(result, "ArrowRight");

    expect(result.current.width).toBe(216);
    expect(getItem).not.toHaveBeenCalled();
    expect(setItem).not.toHaveBeenCalled();
  });

  it("reads a stored positive width, and uses the default when the key is missing", () => {
    const missing = renderHook(() => useResizableColumn(200, 100, 480, STORAGE_KEY));
    expect(missing.result.current.width).toBe(200);
    missing.unmount();

    localStorage.setItem(STORAGE_KEY, "300");
    const stored = renderHook(() => useResizableColumn(200, 100, 480, STORAGE_KEY));
    expect(stored.result.current.width).toBe(300);
    stored.unmount();
  });

  it.each(["", "abc", "0", "-40", "Infinity", "NaN"])(
    "falls back to the default for the stored value %j",
    (stored) => {
      localStorage.setItem(STORAGE_KEY, stored);
      const { result } = renderHook(() => useResizableColumn(200, 100, 480, STORAGE_KEY));
      expect(result.current.width).toBe(200);
    },
  );

  it("ignores storage failures on read and write", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    const { result } = renderHook(() => useResizableColumn(200, 100, 480, STORAGE_KEY));
    expect(result.current.width).toBe(200);

    pressKey(result, "ArrowRight");
    expect(result.current.width).toBe(216);
  });
});

describe("useResizableColumn keyboard", () => {
  it("steps 16px per arrow key and persists each change", () => {
    const { result, unmount } = renderHook(() => useResizableColumn(200, 100, 480, STORAGE_KEY));
    expect(result.current.handleProps).toMatchObject({
      role: "separator",
      "aria-orientation": "vertical",
      "aria-label": "Resize terminal list",
      tabIndex: 0,
      "aria-valuemin": 100,
      "aria-valuemax": 480,
    });

    expect(pressKey(result, "ArrowRight")).toHaveBeenCalledOnce();
    expect(result.current.width).toBe(216);
    expect(result.current.handleProps["aria-valuenow"]).toBe(216);
    expect(localStorage.getItem(STORAGE_KEY)).toBe("216");

    pressKey(result, "ArrowLeft");
    pressKey(result, "ArrowLeft");
    expect(result.current.width).toBe(184);
    expect(localStorage.getItem(STORAGE_KEY)).toBe("184");
    unmount();

    const remounted = renderHook(() => useResizableColumn(200, 100, 480, STORAGE_KEY));
    expect(remounted.result.current.width).toBe(184);
    remounted.unmount();
  });

  it("leaves other keys unhandled", () => {
    const { result } = renderHook(() => useResizableColumn(200, 100, 480, STORAGE_KEY));

    expect(pressKey(result, "a")).not.toHaveBeenCalled();
    expect(result.current.width).toBe(200);
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("does nothing at a clamp and keeps the stored preference", () => {
    localStorage.setItem(STORAGE_KEY, "400");
    const { result, rerender } = renderHook(
      ({ max }) => useResizableColumn(200, 100, max, STORAGE_KEY),
      { initialProps: { max: 300 } },
    );
    expect(result.current.width).toBe(300);

    expect(pressKey(result, "ArrowRight")).toHaveBeenCalledOnce();
    expect(result.current.width).toBe(300);
    expect(localStorage.getItem(STORAGE_KEY)).toBe("400");

    rerender({ max: 500 });
    expect(result.current.width).toBe(400);
  });

  it("replaces the preference when a step moves away from a clamp", () => {
    localStorage.setItem(STORAGE_KEY, "400");
    const { result } = renderHook(() => useResizableColumn(200, 100, 300, STORAGE_KEY));

    pressKey(result, "ArrowLeft");
    expect(result.current.width).toBe(284);
    expect(localStorage.getItem(STORAGE_KEY)).toBe("284");
  });
});

describe("useResizableColumn dynamic max", () => {
  it("clamps the rendered width when max shrinks and restores the preference when max grows", () => {
    localStorage.setItem(STORAGE_KEY, "400");
    const { result, rerender } = renderHook(
      ({ max }) => useResizableColumn(200, 100, max, STORAGE_KEY),
      { initialProps: { max: 480 } },
    );
    expect(result.current.width).toBe(400);

    rerender({ max: 300 });
    expect(result.current.width).toBe(300);
    expect(result.current.handleProps["aria-valuenow"]).toBe(300);
    expect(result.current.handleProps["aria-valuemax"]).toBe(300);
    expect(localStorage.getItem(STORAGE_KEY)).toBe("400");

    rerender({ max: 500 });
    expect(result.current.width).toBe(400);
  });

  it("gives the minimum precedence when max is below min", () => {
    const { result } = renderHook(() => useResizableColumn(200, 150, 120));
    expect(result.current.width).toBe(150);
  });
});

describe("useResizableColumn drag", () => {
  it("keeps the preference when a drag returns to the rendered width", () => {
    localStorage.setItem(STORAGE_KEY, "400");
    const { result, rerender } = renderHook(
      ({ max }) => useResizableColumn(200, 100, max, STORAGE_KEY),
      { initialProps: { max: 300 } },
    );
    attachContainer(result);
    expect(result.current.width).toBe(300);

    pressHandle(result);
    moveTo(380); // pointer 280 past the container edge at 100
    expect(result.current.width).toBe(280);
    moveTo(400); // back to the clamp
    expect(result.current.width).toBe(300);
    release();

    expect(result.current.width).toBe(300);
    expect(localStorage.getItem(STORAGE_KEY)).toBe("400");
    rerender({ max: 500 });
    expect(result.current.width).toBe(400);
  });

  it("commits a drag that ends at a different width and persists it", () => {
    const { result } = renderHook(() => useResizableColumn(200, 100, 480, STORAGE_KEY));
    attachContainer(result);

    pressHandle(result);
    expect(document.body.style.cursor).toBe("col-resize");
    expect(document.body.style.userSelect).toBe("none");
    moveTo(350); // pointer 250
    expect(result.current.width).toBe(250);
    release();

    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
    expect(result.current.width).toBe(250);
    expect(localStorage.getItem(STORAGE_KEY)).toBe("250");
  });

  it("restores body styles and persists nothing when unmounted mid-drag", () => {
    const { result, unmount } = renderHook(() => useResizableColumn(200, 100, 480, STORAGE_KEY));
    attachContainer(result);

    pressHandle(result);
    moveTo(350);
    expect(document.body.style.cursor).toBe("col-resize");
    unmount();

    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });
});
