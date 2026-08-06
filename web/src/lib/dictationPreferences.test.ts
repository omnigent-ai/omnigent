import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_DICTATION_PREFERENCES,
  readDictationPreferences,
  writeDictationPreferences,
} from "./dictationPreferences";

const STORAGE_KEY = "omnigent:dictation-preferences";

afterEach(() => localStorage.clear());

describe("dictationPreferences", () => {
  it("defaults to current auto, en-US, and default-microphone behavior", () => {
    expect(readDictationPreferences()).toEqual(DEFAULT_DICTATION_PREFERENCES);
  });

  it("round-trips typed preferences", () => {
    writeDictationPreferences({
      path: "server",
      browserLanguage: "fr-FR",
      microphoneDeviceId: "mic-2",
    });
    expect(readDictationPreferences()).toEqual({
      path: "server",
      browserLanguage: "fr-FR",
      microphoneDeviceId: "mic-2",
    });
  });

  it("normalizes language tags and removes storage for defaults", () => {
    writeDictationPreferences({
      path: "browser",
      browserLanguage: "  en-us ",
      microphoneDeviceId: null,
    });
    expect(readDictationPreferences().browserLanguage).toBe("en-US");

    writeDictationPreferences(DEFAULT_DICTATION_PREFERENCES);
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("defaults malformed storage and invalid fields independently", () => {
    localStorage.setItem(STORAGE_KEY, "not json");
    expect(readDictationPreferences()).toEqual(DEFAULT_DICTATION_PREFERENCES);

    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ path: "remote", browserLanguage: "", microphoneDeviceId: 4 }),
    );
    expect(readDictationPreferences()).toEqual(DEFAULT_DICTATION_PREFERENCES);
  });

  it("bounds malformed language and device values", () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        path: "server",
        browserLanguage: "not_a_language",
        microphoneDeviceId: "x".repeat(1025),
      }),
    );
    expect(readDictationPreferences()).toEqual({
      path: "server",
      browserLanguage: "en-US",
      microphoneDeviceId: null,
    });
  });

  it("normalizes stored microphone device ids", () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        path: "server",
        browserLanguage: "en-US",
        microphoneDeviceId: "  mic-2  ",
      }),
    );
    expect(readDictationPreferences().microphoneDeviceId).toBe("mic-2");

    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        path: "server",
        browserLanguage: "en-US",
        microphoneDeviceId: " default ",
      }),
    );
    expect(readDictationPreferences().microphoneDeviceId).toBeNull();
  });

  it("does not throw when storage is unavailable", () => {
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    expect(readDictationPreferences()).toEqual(DEFAULT_DICTATION_PREFERENCES);
    getItem.mockRestore();

    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    expect(() =>
      writeDictationPreferences({
        path: "server",
        browserLanguage: "en-US",
        microphoneDeviceId: null,
      }),
    ).not.toThrow();
    setItem.mockRestore();
  });
});
