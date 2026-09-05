import { afterEach, describe, expect, it, vi } from "vitest";
import {
  COMPOSER_SEND_SHORTCUT_STORAGE_KEY,
  DEFAULT_SUBMIT_WITH_MOD_ENTER,
  isComposerSendKey,
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

describe("isComposerSendKey", () => {
  it("keeps Enter and the legacy modifier chord in default mode", () => {
    expect(isComposerSendKey({ key: "Enter" }, false, false)).toBe(true);
    expect(isComposerSendKey({ key: "Enter", shiftKey: true }, false, false)).toBe(false);
    expect(isComposerSendKey({ key: "Enter", metaKey: true }, false, false)).toBe(true);
    expect(isComposerSendKey({ key: "Enter", ctrlKey: true }, false, false)).toBe(true);
  });

  it("uses Command/Ctrl+Enter only for the alternate shortcut", () => {
    expect(isComposerSendKey({ key: "Enter" }, true, false)).toBe(false);
    expect(isComposerSendKey({ key: "Enter", metaKey: true }, true, false)).toBe(true);
    expect(isComposerSendKey({ key: "Enter", ctrlKey: true }, true, false)).toBe(true);
  });

  it("never submits from composition, modified chords, or mobile Enter", () => {
    expect(isComposerSendKey({ key: "Enter", metaKey: true, isComposing: true }, true, false)).toBe(
      false,
    );
    expect(isComposerSendKey({ key: "Enter", metaKey: true, shiftKey: true }, true, false)).toBe(
      false,
    );
    expect(isComposerSendKey({ key: "Enter", metaKey: true }, true, true)).toBe(false);
  });
});
