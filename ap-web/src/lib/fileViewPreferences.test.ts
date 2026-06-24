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

  it("defaults previewableViewMode to the read-only preview", () => {
    // Issue #970: previewable files (markdown/html) open in the rendered
    // preview pane by default, matching how HTML already behaves. A fresh
    // user (no stored preference) must land on "preview", not the editor.
    expect(DEFAULT_FILE_VIEW_PREFERENCES.previewableViewMode).toBe("preview");
  });

  it("round-trips the markdown rich-text editor mode", () => {
    // Markdown now has three reachable modes (preview/editor/source), so
    // "editor" is a first-class stored value — not a fallback. It must
    // survive a write/read round-trip rather than being coerced away.
    writeFileViewPreferences({
      diffActive: false,
      diffLayout: "unified",
      previewableViewMode: "editor",
    });
    expect(readFileViewPreferences().previewableViewMode).toBe("editor");
  });

  it("round-trips a written preference", () => {
    writeFileViewPreferences({
      diffActive: true,
      diffLayout: "split",
      previewableViewMode: "source",
    });
    // The exact object written must come back — proves both the write
    // serialized and the read parsed/validated every field correctly.
    expect(readFileViewPreferences()).toEqual({
      diffActive: true,
      diffLayout: "split",
      previewableViewMode: "source",
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
    // "preview"). Proves a partial/garbage record still yields sane values for
    // the fields that are valid instead of being discarded entirely.
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ diffActive: true, diffLayout: "sideways" }));
    expect(readFileViewPreferences()).toEqual({
      diffActive: true,
      diffLayout: "unified",
      previewableViewMode: "preview",
    });
  });

  it("coerces an unknown previewableViewMode to preview", () => {
    // An out-of-range value (older build, hand-edited storage) must fall back
    // to the new default rather than a stale "editor".
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ previewableViewMode: "bogus" }));
    expect(readFileViewPreferences().previewableViewMode).toBe("preview");
  });

  it("migrates a pre-v2 record's editor mode to the preview default but keeps its diff prefs", () => {
    // Issue #970: the old build auto-persisted previewableViewMode:"editor"
    // (the prior default) for everyone who ever opened a file. A record with no
    // schema version can't prove that "editor" was a deliberate choice, so the
    // new preview default must win — while diffActive/diffLayout (unaffected by
    // the default change) are preserved across the migration.
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ diffActive: true, diffLayout: "split", previewableViewMode: "editor" }),
    );
    expect(readFileViewPreferences()).toEqual({
      diffActive: true,
      diffLayout: "split",
      previewableViewMode: "preview",
    });
  });

  it("honors an editor mode written under the current schema (a deliberate choice)", () => {
    // Once a record carries the current schema version, "editor" IS a real
    // choice and must survive — distinguishing it from the legacy auto-written
    // value migrated away above. writeFileViewPreferences stamps the version.
    writeFileViewPreferences({
      diffActive: false,
      diffLayout: "unified",
      previewableViewMode: "editor",
    });
    expect(readFileViewPreferences().previewableViewMode).toBe("editor");
  });
});
