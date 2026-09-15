import { afterEach, describe, expect, it } from "vitest";
import {
  BACKGROUND_SESSION_TITLES_HEADER,
  BACKGROUND_SESSION_TITLES_STORAGE_KEY,
  backgroundSessionTitlesRequestHeaders,
  readBackgroundSessionTitlesEnabled,
  writeBackgroundSessionTitlesEnabled,
} from "./backgroundSessionTitlesPreferences";

describe("backgroundSessionTitlesPreferences", () => {
  afterEach(() => localStorage.clear());

  it("defaults on when storage is unset", () => {
    expect(readBackgroundSessionTitlesEnabled()).toBe(true);
    expect(backgroundSessionTitlesRequestHeaders()).toEqual({});
  });

  it("persists an off preference in localStorage", () => {
    writeBackgroundSessionTitlesEnabled(false);

    expect(localStorage.getItem(BACKGROUND_SESSION_TITLES_STORAGE_KEY)).toBe("off");
    expect(readBackgroundSessionTitlesEnabled()).toBe(false);
    expect(backgroundSessionTitlesRequestHeaders()).toEqual({
      [BACKGROUND_SESSION_TITLES_HEADER]: "off",
    });
  });

  it("restores the default by clearing the off value", () => {
    localStorage.setItem(BACKGROUND_SESSION_TITLES_STORAGE_KEY, "off");

    writeBackgroundSessionTitlesEnabled(true);

    expect(localStorage.getItem(BACKGROUND_SESSION_TITLES_STORAGE_KEY)).toBeNull();
    expect(readBackgroundSessionTitlesEnabled()).toBe(true);
  });
});
