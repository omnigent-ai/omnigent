import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { createAutomaticFeaturePreference } from "./automaticFeaturePreference";

// Storage, events and other windows are the stored choice preference's; these pin the intents.
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
});
