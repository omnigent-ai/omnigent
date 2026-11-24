// Drive ResizeObserver-based width measurement in jsdom, which has neither layout nor observers.

import { act } from "@testing-library/react";
import { vi } from "vitest";

/**
 * Makes every element report `width` and returns a function that changes it and
 * notifies every observer, as a panel resize would. Undo with `vi.restoreAllMocks`
 * and `vi.unstubAllGlobals`.
 */
export function installResizableWidth(initialWidth: number): (width: number) => void {
  let width = initialWidth;
  const callbacks = new Set<ResizeObserverCallback>();
  class StubResizeObserver {
    private readonly callback: ResizeObserverCallback;
    constructor(callback: ResizeObserverCallback) {
      this.callback = callback;
    }
    observe() {
      callbacks.add(this.callback);
    }
    unobserve() {}
    disconnect() {
      callbacks.delete(this.callback);
    }
  }
  vi.stubGlobal("ResizeObserver", StubResizeObserver);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
    () =>
      ({
        width,
        height: 0,
        top: 0,
        left: 0,
        right: width,
        bottom: 0,
        x: 0,
        y: 0,
        toJSON: () => ({}),
      }) as DOMRect,
  );
  return (next) => {
    width = next;
    act(() => {
      for (const callback of [...callbacks]) callback([], {} as ResizeObserver);
    });
  };
}
