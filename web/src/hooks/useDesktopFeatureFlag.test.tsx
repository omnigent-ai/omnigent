import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useDesktopFeatureFlag, useRememberedDesktopFeatureFlag } from "./useDesktopFeatureFlag";

type Callback = (value: unknown) => void;

function installShell(names: string[]) {
  const callbacks: Callback[] = [];
  const unsubscribe = vi.fn();
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = {
    kind: "electron",
    featureFlags: {
      subscribe: (_name: string, _options: unknown, callback: Callback) => {
        callbacks.push(callback);
        return unsubscribe;
      },
    },
    getDesktopFeatures: async () => ({ featureFlags: { names } }),
  };
  return { callbacks, unsubscribe };
}

afterEach(() => {
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
});

describe("useDesktopFeatureFlag", () => {
  it("follows the shell's values and unsubscribes on unmount", async () => {
    const shell = installShell(["gate"]);
    const { result, unmount } = renderHook(() => useDesktopFeatureFlag("gate", false));
    expect(result.current).toEqual({ value: false, resolved: false });
    await waitFor(() => expect(shell.callbacks).toHaveLength(1));
    act(() => shell.callbacks[0](true));
    expect(result.current).toEqual({ value: true, resolved: true });
    unmount();
    expect(shell.unsubscribe).toHaveBeenCalledTimes(1);
  });

  it("starts a reader mounted later from the shell's answer, never its fallback", async () => {
    const shell = installShell(["gate"]);
    const first = renderHook(() => useDesktopFeatureFlag("gate", false));
    await waitFor(() => expect(shell.callbacks).toHaveLength(1));
    act(() => shell.callbacks[0](true));
    const seen: unknown[] = [];
    const later = renderHook(() => {
      const state = useDesktopFeatureFlag("gate", false);
      seen.push(state);
      return state;
    });
    expect(seen[0]).toEqual({ value: true, resolved: true });
    await waitFor(() => expect(later.result.current).toEqual({ value: true, resolved: true }));
    expect(seen.every((state) => JSON.stringify(state) === '{"value":true,"resolved":true}')).toBe(
      true,
    );
    // It still follows the shell, and stops with nobody left.
    act(() => shell.callbacks[0](false));
    expect(later.result.current).toEqual({ value: false, resolved: true });
    first.unmount();
    later.unmount();
    expect(shell.unsubscribe).toHaveBeenCalledTimes(1);
    const fresh = renderHook(() => useDesktopFeatureFlag("gate", false));
    expect(fresh.result.current).toEqual({ value: false, resolved: false });
  });

  it("settles on the fallback in a browser", async () => {
    const { result } = renderHook(() => useDesktopFeatureFlag("gate", true, "resolved"));
    await waitFor(() => expect(result.current).toEqual({ value: true, resolved: true }));
  });
});

describe("useRememberedDesktopFeatureFlag", () => {
  afterEach(() => window.localStorage.clear());

  it("starts a new window from the last answer, then follows the shell", async () => {
    const first = installShell(["layout"]);
    const before = renderHook(() => useRememberedDesktopFeatureFlag("layout", false));
    // Nothing remembered yet: the fallback until the shell answers.
    expect(before.result.current).toBe(false);
    await waitFor(() => expect(first.callbacks).toHaveLength(1));
    act(() => first.callbacks[0](true));
    expect(before.result.current).toBe(true);
    before.unmount();

    // The next window paints the remembered answer before its shell answers.
    const next = installShell(["layout"]);
    const seen: boolean[] = [];
    const after = renderHook(() => {
      const value = useRememberedDesktopFeatureFlag("layout", false);
      seen.push(value);
      return value;
    });
    expect(seen[0]).toBe(true);
    await waitFor(() => expect(next.callbacks).toHaveLength(1));
    // A rollback still wins once the shell says so, and is remembered in turn.
    act(() => next.callbacks[0](false));
    expect(after.result.current).toBe(false);
    after.unmount();
    expect(renderHook(() => useRememberedDesktopFeatureFlag("layout", false)).result.current).toBe(
      false,
    );
  });

  it("starts from the fallback where storage holds nothing it can read", () => {
    window.localStorage.setItem("omnigent:desktop-flag-last:layout", "maybe");
    installShell(["layout"]);
    expect(renderHook(() => useRememberedDesktopFeatureFlag("layout", true)).result.current).toBe(
      true,
    );
  });
});
