import { afterEach, describe, expect, it } from "vitest";
import {
  AGENTS_VIEW_DEFAULT,
  normalizeAgentsViewMode,
  readAgentsViewDefault,
  writeAgentsViewDefault,
} from "./agentsViewPreferences";

const STORAGE_KEY = "omnigent:default-agents-view";

afterEach(() => {
  localStorage.clear();
});

describe("agentsViewPreferences — read/write", () => {
  it("returns list when nothing is stored", () => {
    expect(readAgentsViewDefault()).toBe(AGENTS_VIEW_DEFAULT);
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("stores graph and clears the key for list", () => {
    writeAgentsViewDefault("graph");
    expect(readAgentsViewDefault()).toBe("graph");
    expect(localStorage.getItem(STORAGE_KEY)).toBe("graph");

    writeAgentsViewDefault("list");
    expect(readAgentsViewDefault()).toBe("list");
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("falls back to list when the stored value is unknown", () => {
    localStorage.setItem(STORAGE_KEY, "tree");
    expect(readAgentsViewDefault()).toBe("list");
  });
});

describe("normalizeAgentsViewMode", () => {
  it("passes through valid values", () => {
    expect(normalizeAgentsViewMode("list")).toBe("list");
    expect(normalizeAgentsViewMode("graph")).toBe("graph");
  });

  it("maps unknown, null, and garbage to list", () => {
    expect(normalizeAgentsViewMode("tree")).toBe("list");
    expect(normalizeAgentsViewMode("bogus")).toBe("list");
    expect(normalizeAgentsViewMode(null)).toBe("list");
    expect(normalizeAgentsViewMode(undefined)).toBe("list");
  });
});
