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
  it("defaults to the full tree (All) when nothing is stored", () => {
    // The whole point: with no saved choice the scope is "All"
    // (changedOnly false), not "Changed".
    expect(readFilesPanelPreferences()).toEqual(DEFAULT_FILES_PANEL_PREFERENCES);
    expect(DEFAULT_FILES_PANEL_PREFERENCES.changedOnly).toBe(false);
  });

  it("defaults the Changed scope to its flat list (changedTreeView false)", () => {
    // The Changed scope opens as a flat list; the tree layout is opt-in.
    expect(DEFAULT_FILES_PANEL_PREFERENCES.changedTreeView).toBe(false);
  });

  it("round-trips a written preference", () => {
    writeFilesPanelPreferences({ changedOnly: true, sort: "alpha", changedTreeView: true });
    expect(readFilesPanelPreferences()).toEqual({
      changedOnly: true,
      sort: "alpha",
      changedTreeView: true,
    });
  });

  it("defaults changedTreeView when the stored field has the wrong type", () => {
    // A record present but with a non-boolean changedTreeView must default the
    // field rather than pass a garbage value through to the panel.
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ changedTreeView: "yes" }));
    expect(readFilesPanelPreferences().changedTreeView).toBe(false);
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

  it("defaults changedOnly when the stored field has the wrong type", () => {
    // A record present but with a non-boolean changedOnly must default the
    // field rather than pass a garbage value through to the panel.
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ changedOnly: "yes" }));
    expect(readFilesPanelPreferences()).toEqual({
      changedOnly: false,
      sort: "recent",
      changedTreeView: false,
    });
  });

  it("defaults sort when the stored value is invalid", () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ changedOnly: true, sort: "bogus" }));
    expect(readFilesPanelPreferences()).toEqual({
      changedOnly: true,
      sort: "recent",
      changedTreeView: false,
    });
  });
});
