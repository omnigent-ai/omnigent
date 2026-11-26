import { afterEach, describe, expect, it } from "vitest";
import {
  readPanelSharePreference,
  readPanelSizePreference,
  readPanelSizePreferences,
  writePanelSharePreference,
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
        filesTreeWidthPx: 300,
        unknownWidthPx: 9,
      }),
    );

    // The valid widths are retained while invalid and unknown sibling fields are
    // dropped, proving one bad field does not poison the whole record.
    expect(readPanelSizePreferences()).toEqual({ pushPanelWidthPx: 700, filesTreeWidthPx: 300 });
    expect(readPanelSizePreference("inlinePanelWidthPx")).toBeNull();
    expect(readPanelSizePreference("filesTreeWidthPx")).toBe(300);
  });

  it("keeps a tree's share of its page apart from pixel widths, each kept by the other's writes", () => {
    writePanelSharePreference("filesTreeShare", 0.3);
    writePanelSizePreference("pushPanelWidthPx", 840);
    writePanelSharePreference("pullRequestReviewTreeShare", 0.25);

    expect(readPanelSizePreferences()).toEqual({
      filesTreeShare: 0.3,
      pushPanelWidthPx: 840,
      pullRequestReviewTreeShare: 0.25,
    });
    expect(readPanelSharePreference("changesReviewTreeShare")).toBeNull();
    writePanelSharePreference("filesTreeShare", null);
    expect(readPanelSharePreference("filesTreeShare")).toBeNull();
    expect(readPanelSharePreference("pullRequestReviewTreeShare")).toBe(0.25);
  });

  it("drops a share outside 0 to 1, on read and on write", () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        filesTreeShare: 1,
        changesReviewTreeShare: 0,
        pullRequestReviewTreeShare: 0.4,
      }),
    );
    expect(readPanelSizePreferences()).toEqual({ pullRequestReviewTreeShare: 0.4 });
    writePanelSharePreference("filesTreeShare", 1.5);
    writePanelSharePreference("changesReviewTreeShare", Number.NaN);
    expect(readPanelSharePreference("filesTreeShare")).toBeNull();
    expect(readPanelSharePreference("changesReviewTreeShare")).toBeNull();
  });
});
