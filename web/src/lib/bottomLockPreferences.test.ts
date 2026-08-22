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
  it("defaults to disabled and stores only the enabled override", () => {
    expect(readBottomLockEnabled()).toBe(false);

    writeBottomLockEnabled(true);
    expect(readBottomLockEnabled()).toBe(true);
    expect(localStorage.getItem("omnigent:bottom-lock")).toBe("true");

    writeBottomLockEnabled(false);
    expect(localStorage.getItem("omnigent:bottom-lock")).toBeNull();
  });

  it("notifies mounted chat views when the setting changes", () => {
    const onChange = vi.fn();
    const unsubscribe = subscribeBottomLockEnabled(onChange);

    writeBottomLockEnabled(true);
    expect(onChange).toHaveBeenCalledOnce();

    unsubscribe();
    writeBottomLockEnabled(false);
    expect(onChange).toHaveBeenCalledOnce();
  });
});
