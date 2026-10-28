import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_OPEN_SUBAGENTS_IN_PANEL,
  OPEN_SUBAGENTS_IN_PANEL_STORAGE_KEY,
  readOpenSubagentsInPanel,
  writeOpenSubagentsInPanel,
} from "./subagentPanelPreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("subagentPanelPreferences", () => {
  it("defaults to off so sub-agents keep opening full screen", () => {
    expect(DEFAULT_OPEN_SUBAGENTS_IN_PANEL).toBe(false);
    expect(readOpenSubagentsInPanel()).toBe(false);
  });

  it("stores on and clears the key when turned back off", () => {
    writeOpenSubagentsInPanel(true);
    expect(localStorage.getItem(OPEN_SUBAGENTS_IN_PANEL_STORAGE_KEY)).toBe("on");
    expect(readOpenSubagentsInPanel()).toBe(true);

    writeOpenSubagentsInPanel(false);
    expect(localStorage.getItem(OPEN_SUBAGENTS_IN_PANEL_STORAGE_KEY)).toBeNull();
    expect(readOpenSubagentsInPanel()).toBe(false);
  });

  it("reads any other stored value as off", () => {
    localStorage.setItem(OPEN_SUBAGENTS_IN_PANEL_STORAGE_KEY, "true");
    expect(readOpenSubagentsInPanel()).toBe(false);
  });

  it("never throws when storage is inaccessible", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(writeOpenSubagentsInPanel(true)).toBe(false);
    expect(writeOpenSubagentsInPanel(false)).toBe(false);
    expect(readOpenSubagentsInPanel()).toBe(false);
  });
});
