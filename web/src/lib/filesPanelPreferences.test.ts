import { afterEach, describe, expect, it } from "vitest";
import {
  DEFAULT_FILES_PANEL_PREFERENCES,
  readFilesPanelPreferences,
  writeFilesPanelPreferences,
} from "./filesPanelPreferences";

const STORAGE_KEY = "omnigent:files-panel-preferences";

afterEach(() => {
  localStorage.clear();
});

describe("filesPanelPreferences", () => {
  it("defaults the sort order when nothing is stored", () => {
    expect(readFilesPanelPreferences()).toEqual(DEFAULT_FILES_PANEL_PREFERENCES);
    expect(DEFAULT_FILES_PANEL_PREFERENCES.sort).toBe("recent");
  });

  it("round-trips a written preference", () => {
    writeFilesPanelPreferences({ sort: "alpha" });
    expect(readFilesPanelPreferences()).toEqual({ sort: "alpha" });
  });

  it("falls back to defaults on malformed JSON", () => {
    // A non-JSON string must not throw; read swallows the parse error so a
    // corrupt entry can't break the panel.
    localStorage.setItem(STORAGE_KEY, "}{not json");
    expect(readFilesPanelPreferences()).toEqual(DEFAULT_FILES_PANEL_PREFERENCES);
  });

  it("falls back to defaults when the stored value is not an object", () => {
    // Valid JSON but the wrong shape (an array) must be rejected wholesale.
    localStorage.setItem(STORAGE_KEY, JSON.stringify([true]));
    expect(readFilesPanelPreferences()).toEqual(DEFAULT_FILES_PANEL_PREFERENCES);
  });

  it("defaults sort when the stored value is invalid", () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ sort: "bogus" }));
    expect(readFilesPanelPreferences()).toEqual({ sort: "recent" });
  });
});
