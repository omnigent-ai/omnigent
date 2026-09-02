import { afterEach, describe, expect, it, vi } from "vitest";
import {
  COMPOSER_SEND_SHORTCUT_STORAGE_KEY,
  DEFAULT_SUBMIT_WITH_MOD_ENTER,
  parseSubmitWithModEnter,
  readSubmitWithModEnter,
  writeSubmitWithModEnter,
} from "./composerSendShortcutPreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("composerSendShortcutPreferences", () => {
  it("enables the alternate behavior only for the exact persisted value", () => {
    expect(parseSubmitWithModEnter("true")).toBe(true);
    expect(parseSubmitWithModEnter("false")).toBe(false);
    expect(parseSubmitWithModEnter("1")).toBe(false);
    expect(parseSubmitWithModEnter(null)).toBe(DEFAULT_SUBMIT_WITH_MOD_ENTER);
  });

  it("round-trips the opt-in and removes the default override", () => {
    writeSubmitWithModEnter(true);
    expect(readSubmitWithModEnter()).toBe(true);

    writeSubmitWithModEnter(false);
    expect(readSubmitWithModEnter()).toBe(false);
    expect(localStorage.getItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY)).toBeNull();
  });

  it("falls back safely when storage cannot be read or written", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });

    expect(() => writeSubmitWithModEnter(true)).not.toThrow();
    expect(readSubmitWithModEnter()).toBe(DEFAULT_SUBMIT_WITH_MOD_ENTER);
  });
});
