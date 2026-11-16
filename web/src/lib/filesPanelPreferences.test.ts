import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_FILES_PANEL_PREFERENCES,
  FILES_PANEL_SHOW_HIDDEN_STORAGE_KEY,
  readFilesPanelPreferences,
  readFilesPanelShowHidden,
  useFilesPanelShowHidden,
  writeFilesPanelPreferences,
  writeFilesPanelShowHidden,
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

describe("Files panel hidden-files preference", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    writeFilesPanelShowHidden(true);
  });

  it("shows hidden files until the user hides them, and remembers the choice", () => {
    expect(readFilesPanelShowHidden()).toBe(true);
    localStorage.setItem(FILES_PANEL_SHOW_HIDDEN_STORAGE_KEY, "sometimes");
    expect(readFilesPanelShowHidden()).toBe(true);

    writeFilesPanelShowHidden(false);
    expect(localStorage.getItem(FILES_PANEL_SHOW_HIDDEN_STORAGE_KEY)).toBe("false");
    expect(readFilesPanelShowHidden()).toBe(false);

    // Showing them again is the default, so nothing stays stored.
    writeFilesPanelShowHidden(true);
    expect(localStorage.getItem(FILES_PANEL_SHOW_HIDDEN_STORAGE_KEY)).toBeNull();
    expect(readFilesPanelShowHidden()).toBe(true);
  });

  it("keeps every mounted panel following the toggle when storage refuses writes", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    const first = renderHook(() => useFilesPanelShowHidden());
    const second = renderHook(() => useFilesPanelShowHidden());
    expect(first.result.current).toBe(true);

    act(() => writeFilesPanelShowHidden(false));
    expect(first.result.current).toBe(false);
    expect(second.result.current).toBe(false);
    expect(localStorage.getItem(FILES_PANEL_SHOW_HIDDEN_STORAGE_KEY)).toBeNull();

    act(() => writeFilesPanelShowHidden(true));
    expect(first.result.current).toBe(true);
    expect(second.result.current).toBe(true);
  });

  it("shows hidden files again even when storage can't remove the saved choice", () => {
    writeFilesPanelShowHidden(false);
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    const first = renderHook(() => useFilesPanelShowHidden());
    const second = renderHook(() => useFilesPanelShowHidden());
    expect(first.result.current).toBe(false);

    act(() => writeFilesPanelShowHidden(true));
    expect(first.result.current).toBe(true);
    expect(second.result.current).toBe(true);
    expect(localStorage.getItem(FILES_PANEL_SHOW_HIDDEN_STORAGE_KEY)).toBe("false");
  });

  it("lets another window's choice replace one storage refused here", () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    const panel = renderHook(() => useFilesPanelShowHidden());
    act(() => writeFilesPanelShowHidden(false));
    expect(panel.result.current).toBe(false);

    // Clearing sessionStorage elsewhere says nothing about this choice.
    setItem.mockRestore();
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: null, storageArea: sessionStorage }));
    });
    expect(panel.result.current).toBe(false);

    // Another window shows hidden files again, which clears the stored key.
    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", { key: FILES_PANEL_SHOW_HIDDEN_STORAGE_KEY, newValue: null }),
      );
    });
    expect(panel.result.current).toBe(true);
    expect(readFilesPanelShowHidden()).toBe(true);
  });
});
