import { afterEach, describe, expect, it } from "vitest";
import {
  readPanelSizePreference,
  readPanelSizePreferences,
  writePanelSizePreference,
} from "./panelSizePreferences";

const STORAGE_KEY = "omnigent:panel-size-preferences";

afterEach(() => {
  localStorage.clear();
});

describe("panelSizePreferences", () => {
  it("returns an empty object when nothing is stored", () => {
    // Empty storage must be a clean "no preferences yet" state, not an error.
    expect(readPanelSizePreferences()).toEqual({});
  });

  it("round-trips valid widths and preserves unrelated fields", () => {
    writePanelSizePreference("pushPanelWidthPx", 840);
    writePanelSizePreference("inlinePanelWidthPx", 420);

    // Both values must survive separate writes; a write for one panel must not
    // erase the other panel's preference.
    expect(readPanelSizePreferences()).toEqual({
      pushPanelWidthPx: 840,
      inlinePanelWidthPx: 420,
    });
  });

  it("ignores malformed JSON", () => {
    // Corrupt localStorage should not break app boot.
    localStorage.setItem(STORAGE_KEY, "}{not json");
    expect(readPanelSizePreferences()).toEqual({});
  });

  it("validates each field independently", () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        pushPanelWidthPx: 700,
        inlinePanelWidthPx: -1,
        commentsPanelWidthPx: "wide",
      }),
    );

    // The valid push-panel width is retained while invalid sibling fields are
    // dropped, proving one bad field does not poison the whole record.
    expect(readPanelSizePreferences()).toEqual({ pushPanelWidthPx: 700 });
    expect(readPanelSizePreference("inlinePanelWidthPx")).toBeNull();
  });
});
