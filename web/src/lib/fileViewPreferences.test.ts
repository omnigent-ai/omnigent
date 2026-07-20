import { afterEach, describe, expect, it } from "vitest";
import {
  DEFAULT_FILE_VIEW_PREFERENCES,
  readFileViewPreferences,
  writeFileViewPreferences,
} from "./fileViewPreferences";

const STORAGE_KEY = "omnigent:file-view-preferences";

afterEach(() => {
  localStorage.clear();
});

describe("fileViewPreferences", () => {
  it("returns the defaults when nothing is stored", () => {
    // No write has happened — read must fall back to the hardcoded defaults,
    // not throw or return a partial object.
    expect(readFileViewPreferences()).toEqual(DEFAULT_FILE_VIEW_PREFERENCES);
  });

  it("round-trips a written preference", () => {
    writeFileViewPreferences({
      diffActive: true,
      diffLayout: "split",
      previewableViewMode: "source",
      hideWhitespace: true,
      wrapLines: true,
    });
    // The exact object written must come back — proves both the write
    // serialized and the read parsed/validated every field correctly.
    expect(readFileViewPreferences()).toEqual({
      diffActive: true,
      diffLayout: "split",
      previewableViewMode: "source",
      hideWhitespace: true,
      wrapLines: true,
    });
  });

  it("falls back to defaults on malformed JSON", () => {
    // A non-JSON string in the key must not throw; read swallows the parse
    // error and returns defaults so a corrupt entry can't break the viewer.
    localStorage.setItem(STORAGE_KEY, "}{not json");
    expect(readFileViewPreferences()).toEqual(DEFAULT_FILE_VIEW_PREFERENCES);
  });

  it("falls back to defaults when the stored value is not an object", () => {
    // Valid JSON but the wrong shape (an array / primitive) must be rejected
    // wholesale rather than treated as a preferences record.
    localStorage.setItem(STORAGE_KEY, JSON.stringify(["split"]));
    expect(readFileViewPreferences()).toEqual(DEFAULT_FILE_VIEW_PREFERENCES);
  });

  it("validates each field independently, defaulting only the invalid ones", () => {
    // diffActive is the right type (kept); diffLayout is an unknown string
    // (defaults to "unified"); previewableViewMode is missing (defaults to
    // "editor"). Proves a partial/garbage record still yields sane values for
    // the fields that are valid instead of being discarded entirely.
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ diffActive: true, diffLayout: "sideways" }));
    expect(readFileViewPreferences()).toEqual({
      diffActive: true,
      diffLayout: "unified",
      previewableViewMode: "editor",
      hideWhitespace: false,
      wrapLines: false,
    });
  });
});
