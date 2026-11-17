import { describe, expect, it } from "vitest";

import { SETTINGS_MAIN_CONTROL, type SettingsEntry, settingsEntry } from "./catalog/types";
import {
  parseSettingsHash,
  settingsControlProps,
  settingsFileInputProps,
  settingsItemPath,
  settingsRevealPart,
  settingsTemplatedName,
} from "./settingsControl";

const row = settingsEntry({
  id: "fixture-row",
  section: "general",
  label: "Row",
  description: "A row.",
  parts: { reset: "Reset to default", file: "Choose file", copy: "Copy command" },
  nameTemplates: {
    copy: {
      before: "Copy ",
      after: " command",
      variants: { done: { before: "Copied ", after: " command" } },
    },
  },
  nameAlternatives: { reset: { busy: "Resetting…" } },
  presence: { file: { overlay: "reset" } },
  overlays: { reset: {} },
});

describe("settingsControlProps", () => {
  it("names a row's main control by its label and description", () => {
    expect(settingsControlProps(row)).toEqual({
      "aria-labelledby": "settings-fixture-row-label",
      "aria-describedby": "settings-fixture-row-description",
      "data-settings-control": "fixture-row:",
    });
  });

  it("names a part by its declared name", () => {
    expect(settingsControlProps(row, "reset")).toEqual({
      "aria-label": "Reset to default",
      "data-settings-control": "fixture-row:reset",
    });
  });

  it("names a templated part around its runtime value, or a variant's text", () => {
    expect(settingsControlProps(row, "copy", { runtime: "pnpm test" })).toEqual({
      "aria-label": "Copy pnpm test command",
      "data-settings-control": "fixture-row:copy",
    });
    expect(settingsControlProps(row, "copy", { runtime: "pnpm test", variant: "done" })).toEqual({
      "aria-label": "Copied pnpm test command",
      "data-settings-control": "fixture-row:copy",
    });
  });

  it("falls back to the static name for a blank runtime value or an unknown variant", () => {
    expect(settingsControlProps(row, "copy", { runtime: "  " })["aria-label"]).toBe("Copy command");
    expect(
      settingsControlProps(row, "copy", { runtime: "x", variant: "missing" })["aria-label"],
    ).toBe("Copy command");
  });

  it("names a part by one of its declared alternatives", () => {
    expect(settingsControlProps(row, "reset", { alternative: "busy" })["aria-label"]).toBe(
      "Resetting…",
    );
  });

  it("throws for an alternative the part doesn't declare", () => {
    expect(() => settingsControlProps(row, "reset", { alternative: "missing" as never })).toThrow(
      '"missing" is not a declared alternative name of fixture-row:reset',
    );
  });

  it("gives a file input its part's id and keeps it out of the tab order", () => {
    expect(settingsFileInputProps(row, "file")).toEqual({
      "data-settings-file-input": "fixture-row:file",
      tabIndex: -1,
    });
  });
});

describe("settingsTemplatedName", () => {
  it("is undefined for a part with no template and no name", () => {
    expect(settingsTemplatedName(row, "missing", { runtime: "x" })).toBeUndefined();
  });
});

describe("settingsItemPath", () => {
  it("opens a row on its section page, and a part by its control id", () => {
    expect(settingsItemPath(row)).toBe("/settings/general#fixture-row");
    expect(settingsItemPath(row, "reset")).toBe("/settings/general#fixture-row:reset");
  });

  it("names the main control by a trailing colon, apart from the row", () => {
    expect(settingsItemPath(row, SETTINGS_MAIN_CONTROL)).toBe("/settings/general#fixture-row:");
  });

  it("round-trips an id and part that the hash encodes", () => {
    const odd = settingsEntry({
      id: "odd%id",
      section: "general",
      label: "Odd",
      description: "d",
      parts: { "a b": "A b" },
    });
    const hash = `#${settingsItemPath(odd, "a b").split("#")[1]}`;
    expect(parseSettingsHash(hash)).toEqual({
      id: "odd%id",
      part: "a b",
    });
  });

  it("adds the harness page for a harness's row", () => {
    const harnessRow = { ...row, harness: "fixture harness" } as SettingsEntry;
    expect(settingsItemPath(harnessRow, "reset")).toBe(
      "/settings/general/fixture%20harness#fixture-row:reset",
    );
  });
});

describe("parseSettingsHash", () => {
  it("reads a row, or one of its parts, with the id decoded", () => {
    expect(parseSettingsHash("#fixture-row")).toEqual({ id: "fixture-row" });
    expect(parseSettingsHash("#fixture-row:reset")).toEqual({ id: "fixture-row", part: "reset" });
    expect(parseSettingsHash("#a%20b")).toEqual({ id: "a b" });
    expect(parseSettingsHash("fixture-row")).toEqual({ id: "fixture-row" });
  });

  it("tells the row apart from its main control", () => {
    expect(parseSettingsHash("#fixture-row:")).toEqual({
      id: "fixture-row",
      part: SETTINGS_MAIN_CONTROL,
    });
  });

  it("splits an encoded separator out of the id, so it can't move the part", () => {
    expect(parseSettingsHash("#a%3Ab:part")).toEqual({ id: "a:b", part: "part" });
  });

  it("ignores hashes that name nothing", () => {
    expect(parseSettingsHash("")).toBeNull();
    expect(parseSettingsHash("#")).toBeNull();
    expect(parseSettingsHash("#:reset")).toBeNull();
    expect(parseSettingsHash("#%E0%A4%A")).toBeNull();
  });
});

describe("settingsRevealPart", () => {
  it("reveals a row through its own opener, or the control that leads to it", () => {
    expect(settingsRevealPart(row)).toBeUndefined();
    expect(settingsRevealPart(row, "reset")).toBe("reset");
    expect(settingsRevealPart(row, "file")).toBe("reset");
  });

  it("doesn't read a part a hash names from the object's prototype", () => {
    const redirected = { ...row, reachedBy: { note: "fix" } } as SettingsEntry;
    expect(settingsRevealPart(redirected, "constructor")).toBe("constructor");
    expect(settingsRevealPart(redirected, "toString")).toBe("toString");
  });

  it("follows an explicit redirect", () => {
    const redirected = settingsEntry({
      id: "fixture-redirect",
      section: "general",
      label: "Redirect",
      description: "d",
      parts: { note: "Note", fix: "Fix" },
      reachedBy: { note: "fix" },
    });
    expect(settingsRevealPart(redirected, "note")).toBe("fix");
  });
});
