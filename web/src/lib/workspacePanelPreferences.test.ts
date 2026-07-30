import { afterEach, describe, expect, it } from "vitest";
import {
  normalizeWorkspacePanelDefault,
  readDefaultWorkspacePanelOpen,
  readWorkspacePanelDefault,
  WORKSPACE_PANEL_DEFAULT,
  writeWorkspacePanelDefault,
} from "./workspacePanelPreferences";

const STORAGE_KEY = "omnigent:default-workspace-panel";

afterEach(() => {
  localStorage.clear();
});

describe("workspacePanelPreferences — read/write", () => {
  it("returns open when nothing is stored", () => {
    expect(readWorkspacePanelDefault()).toBe(WORKSPACE_PANEL_DEFAULT);
    expect(readDefaultWorkspacePanelOpen()).toBe(true);
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("stores collapsed and clears the key for open", () => {
    writeWorkspacePanelDefault("collapsed");
    expect(readWorkspacePanelDefault()).toBe("collapsed");
    expect(readDefaultWorkspacePanelOpen()).toBe(false);
    expect(localStorage.getItem(STORAGE_KEY)).toBe("collapsed");

    writeWorkspacePanelDefault("open");
    expect(readWorkspacePanelDefault()).toBe("open");
    expect(readDefaultWorkspacePanelOpen()).toBe(true);
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });
});

describe("normalizeWorkspacePanelDefault", () => {
  it("passes through valid values", () => {
    expect(normalizeWorkspacePanelDefault("open")).toBe("open");
    expect(normalizeWorkspacePanelDefault("collapsed")).toBe("collapsed");
  });

  it("maps unknown, null, and garbage to open", () => {
    expect(normalizeWorkspacePanelDefault("closed")).toBe("open");
    expect(normalizeWorkspacePanelDefault("bogus")).toBe("open");
    expect(normalizeWorkspacePanelDefault(null)).toBe("open");
    expect(normalizeWorkspacePanelDefault(undefined)).toBe("open");
  });
});
