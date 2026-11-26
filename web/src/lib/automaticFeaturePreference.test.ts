import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createAutomaticFeaturePreference } from "./automaticFeaturePreference";

describe("createAutomaticFeaturePreference", () => {
  const preference = createAutomaticFeaturePreference("omnigent:test-automatic-feature");

  afterEach(() => window.localStorage.clear());

  it("reads Automatic when nothing, or something unknown, is stored", () => {
    expect(preference.read()).toBe("automatic");
    window.localStorage.setItem(preference.storageKey, "sometimes");
    expect(preference.read()).toBe("automatic");
  });

  it("stores On and Off, and Automatic as nothing", () => {
    preference.write("off");
    expect(window.localStorage.getItem(preference.storageKey)).toBe("off");
    preference.write("on");
    expect(preference.read()).toBe("on");
    preference.write("automatic");
    expect(window.localStorage.getItem(preference.storageKey)).toBeNull();
  });

  it("keeps a mounted intent live across writes, other windows and bulk imports", () => {
    const { result } = renderHook(() => preference.useIntent());
    act(() => preference.write("off"));
    expect(result.current).toBe("off");

    act(() => {
      window.localStorage.setItem(preference.storageKey, "on");
      window.dispatchEvent(new StorageEvent("storage", { key: preference.storageKey }));
    });
    expect(result.current).toBe("on");

    act(() => {
      window.localStorage.removeItem(preference.storageKey);
      preference.notifyChanged();
    });
    expect(result.current).toBe("automatic");
  });

  it("ignores other keys' storage events", () => {
    const listener = vi.fn();
    const unsubscribe = preference.subscribe(listener);
    window.dispatchEvent(new StorageEvent("storage", { key: "omnigent:something-else" }));
    expect(listener).not.toHaveBeenCalled();
    unsubscribe();
  });
});
