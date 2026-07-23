import { afterEach, describe, expect, it } from "vitest";
import {
  applySidebarFontSize,
  clampSidebarFontSizePx,
  readSidebarFontSizePx,
  SIDEBAR_FONT_SIZE_DEFAULT,
  writeSidebarFontSizePx,
} from "./sidebarFontPreferences";

const STORAGE_KEY = "omnigent:sidebar-font-size";

afterEach(() => {
  localStorage.clear();
  document.documentElement.style.removeProperty("--sidebar-font-size");
});

describe("sidebarFontPreferences", () => {
  it("defaults to the existing compact Sidebar size", () => {
    expect(readSidebarFontSizePx()).toBe(SIDEBAR_FONT_SIZE_DEFAULT);
  });

  it("clamps, persists, and applies the Sidebar size independently", () => {
    expect(clampSidebarFontSizePx(99)).toBe(16);
    writeSidebarFontSizePx(14);
    applySidebarFontSize(14);

    expect(localStorage.getItem(STORAGE_KEY)).toBe("14");
    expect(readSidebarFontSizePx()).toBe(14);
    expect(document.documentElement.style.getPropertyValue("--sidebar-font-size")).toBe("14px");
    expect(document.documentElement.style.getPropertyValue("--ui-font-scale")).toBe("");
  });
});
