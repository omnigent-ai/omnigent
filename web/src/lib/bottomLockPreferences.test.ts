import { afterEach, describe, expect, it, vi } from "vitest";
import {
  readBottomLockEnabled,
  subscribeBottomLockEnabled,
  writeBottomLockEnabled,
} from "./bottomLockPreferences";

afterEach(() => {
  localStorage.clear();
});

describe("bottom lock preferences", () => {
  it("defaults to enabled and stores only the disabled override", () => {
    expect(readBottomLockEnabled()).toBe(true);

    writeBottomLockEnabled(false);
    expect(readBottomLockEnabled()).toBe(false);
    expect(localStorage.getItem("omnigent:bottom-lock")).toBe("false");

    writeBottomLockEnabled(true);
    expect(localStorage.getItem("omnigent:bottom-lock")).toBeNull();
  });

  it("notifies mounted chat views when the setting changes", () => {
    const onChange = vi.fn();
    const unsubscribe = subscribeBottomLockEnabled(onChange);

    writeBottomLockEnabled(false);
    expect(onChange).toHaveBeenCalledOnce();

    unsubscribe();
    writeBottomLockEnabled(true);
    expect(onChange).toHaveBeenCalledOnce();
  });
});
