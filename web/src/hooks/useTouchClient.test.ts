import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { isTouchClient, useTouchClient } from "./useTouchClient";

/** A browser whose primary pointer is `pointer`. */
function withPointer(pointer: "fine" | "coarse") {
  vi.spyOn(window, "matchMedia").mockImplementation(
    (query: string) =>
      ({
        matches: query === `(pointer: ${pointer})`,
        media: query,
        addEventListener: () => {},
        removeEventListener: () => {},
      }) as unknown as MediaQueryList,
  );
}

/** The bridge a native shell injects before the page loads. */
function inShell(kind: "ios" | "android" | "electron") {
  (window as unknown as { omnigentNative?: { kind: string } }).omnigentNative = { kind };
}

afterEach(() => {
  delete (window as unknown as { omnigentNative?: unknown }).omnigentNative;
  vi.restoreAllMocks();
});

describe("useTouchClient", () => {
  it("is false in a desktop browser or the desktop app, whose pointer is a mouse", () => {
    withPointer("fine");
    expect(renderHook(() => useTouchClient()).result.current).toBe(false);
    inShell("electron");
    expect(isTouchClient()).toBe(false);
  });

  it("is true where the primary pointer is a finger, as in a phone's browser", () => {
    withPointer("coarse");
    expect(renderHook(() => useTouchClient()).result.current).toBe(true);
  });

  it.each(["ios", "android"] as const)("is true in the %s app, whatever its pointer", (kind) => {
    withPointer("fine");
    inShell(kind);
    expect(renderHook(() => useTouchClient()).result.current).toBe(true);
  });

  it("follows the primary pointer as it changes, and stops listening once unmounted", () => {
    let coarse = false;
    const listeners = new Set<() => void>();
    vi.spyOn(window, "matchMedia").mockImplementation(
      (query: string) =>
        ({
          matches: query === "(pointer: coarse)" && coarse,
          media: query,
          addEventListener: (_type: string, listener: () => void) => listeners.add(listener),
          removeEventListener: (_type: string, listener: () => void) => listeners.delete(listener),
        }) as unknown as MediaQueryList,
    );
    const { result, unmount } = renderHook(() => useTouchClient());
    expect(result.current).toBe(false);

    // A tablet's keyboard cover detaching makes a finger the primary pointer.
    coarse = true;
    act(() => {
      for (const listener of [...listeners]) listener();
    });
    expect(result.current).toBe(true);

    unmount();
    expect(listeners.size).toBe(0);
  });
});
