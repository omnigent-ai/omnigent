import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useVisibleViewportHeight } from "./useVisibleViewportHeight";

// The hook used to be iOS-shell-only, which left the Android shell and mobile
// browsers with no live viewport metrics — so dialogs there fell back to the
// LARGE viewport and hung below the URL bar / keyboard. It now runs everywhere;
// only the iOS-specific pan lock stays gated.

function stubVisualViewport(height: number, offsetTop = 0) {
  const listeners = new Map<string, Set<EventListener>>();
  const viewport = {
    height,
    offsetTop,
    addEventListener: (type: string, fn: EventListener) => {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type)!.add(fn);
    },
    removeEventListener: (type: string, fn: EventListener) => listeners.get(type)?.delete(fn),
  };
  Object.defineProperty(window, "visualViewport", { value: viewport, configurable: true });
  return {
    viewport,
    emit: (type: string) => listeners.get(type)?.forEach((fn) => fn(new Event(type))),
  };
}

function Probe() {
  useVisibleViewportHeight();
  return null;
}

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).omnigentNative;
  Reflect.deleteProperty(window, "visualViewport");
  document.documentElement.removeAttribute("style");
});

describe("useVisibleViewportHeight", () => {
  it("publishes the visible height and offset off the iOS shell", () => {
    stubVisualViewport(664, 0);
    render(<Probe />);
    const root = document.documentElement;
    expect(root.style.getPropertyValue("--omnigent-viewport-height")).toBe("664px");
    expect(root.style.getPropertyValue("--omnigent-viewport-offset")).toBe("0px");
  });

  it("reports a browser's visual-viewport offset rather than fighting it", () => {
    // Mobile Chrome pans the visual viewport to reveal a focused field; fixed
    // overlays stay on the layout viewport, so the offset must be added back.
    stubVisualViewport(400, 120);
    render(<Probe />);
    expect(document.documentElement.style.getPropertyValue("--omnigent-viewport-offset")).toBe(
      "120px",
    );
  });

  it("still snaps the iOS shell's keyboard pan back to the top", () => {
    (window as unknown as Record<string, unknown>).omnigentNative = { kind: "ios" };
    const scrollTo = vi.fn();
    Object.defineProperty(window, "scrollTo", { value: scrollTo, configurable: true });
    stubVisualViewport(400, 120);
    render(<Probe />);
    expect(scrollTo).toHaveBeenCalledWith(0, 0);
  });

  it("clears the published vars on unmount", () => {
    stubVisualViewport(664);
    const { unmount } = render(<Probe />);
    unmount();
    expect(document.documentElement.style.getPropertyValue("--omnigent-viewport-height")).toBe("");
    expect(document.documentElement.style.getPropertyValue("--omnigent-viewport-offset")).toBe("");
  });
});
