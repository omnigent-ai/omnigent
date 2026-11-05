import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  DEFAULT_SHOW_PROJECT_HEADER_ACTIONS_ON_TOUCH,
  SHOW_PROJECT_HEADER_ACTIONS_ON_TOUCH_STORAGE_KEY,
  readShowProjectHeaderActionsOnTouch,
  useShowProjectHeaderActionsOnTouch,
  writeShowProjectHeaderActionsOnTouch,
} from "./projectHeaderActionPreferences";

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("projectHeaderActionPreferences", () => {
  it("defaults to off when missing or invalid storage is present", () => {
    expect(DEFAULT_SHOW_PROJECT_HEADER_ACTIONS_ON_TOUCH).toBe(false);
    expect(readShowProjectHeaderActionsOnTouch()).toBe(false);

    for (const value of ["invalid", "TRUE", "null"]) {
      localStorage.setItem(SHOW_PROJECT_HEADER_ACTIONS_ON_TOUCH_STORAGE_KEY, value);
      expect(readShowProjectHeaderActionsOnTouch()).toBe(false);
    }
  });

  it("persists explicit on and off values", () => {
    writeShowProjectHeaderActionsOnTouch(true);
    expect(readShowProjectHeaderActionsOnTouch()).toBe(true);
    expect(localStorage.getItem(SHOW_PROJECT_HEADER_ACTIONS_ON_TOUCH_STORAGE_KEY)).toBe("true");

    writeShowProjectHeaderActionsOnTouch(false);
    expect(readShowProjectHeaderActionsOnTouch()).toBe(false);
    expect(localStorage.getItem(SHOW_PROJECT_HEADER_ACTIONS_ON_TOUCH_STORAGE_KEY)).toBe("false");
  });

  it("notifies same-tab subscribers immediately after a write", () => {
    const { result } = renderHook(() => useShowProjectHeaderActionsOnTouch());
    expect(result.current).toBe(false);

    act(() => writeShowProjectHeaderActionsOnTouch(true));
    expect(result.current).toBe(true);
  });

  it("refreshes from its own storage event and ignores unrelated keys", () => {
    const { result } = renderHook(() => useShowProjectHeaderActionsOnTouch());
    expect(result.current).toBe(false);
    localStorage.setItem(SHOW_PROJECT_HEADER_ACTIONS_ON_TOUCH_STORAGE_KEY, "true");
    localStorage.setItem("some:other:key", "true");

    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: "some:other:key" }));
    });

    expect(result.current).toBe(false);

    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", { key: SHOW_PROJECT_HEADER_ACTIONS_ON_TOUCH_STORAGE_KEY }),
      );
    });
    expect(result.current).toBe(true);
  });
});
