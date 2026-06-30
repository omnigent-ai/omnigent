import { afterEach, describe, expect, it, vi } from "vitest";
import { readHarnessOptions, writeHarnessOption } from "./modePreferences";

const KEY = "omnigent:last-mode-by-harness";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("modePreferences (per-harness options)", () => {
  it("returns an empty object when nothing is stored for a harness", () => {
    // A first-time visitor has no picks on record — read says so ({}) so the
    // composer seeds the harness defaults / leaves knobs unselected.
    expect(readHarnessOptions("claude-native")).toEqual({});
  });

  it("returns an empty object for a null/empty harness", () => {
    writeHarnessOption("claude-native", { mode: "auto" });
    expect(readHarnessOptions(null)).toEqual({});
    expect(readHarnessOptions(undefined)).toEqual({});
    expect(readHarnessOptions("")).toEqual({});
  });

  it("round-trips written option knobs", () => {
    writeHarnessOption("claude-native", { mode: "plan", model: "opus", effort: "high" });
    expect(readHarnessOptions("claude-native")).toEqual({
      mode: "plan",
      model: "opus",
      effort: "high",
    });
  });

  it("merges a partial patch, preserving the other knobs", () => {
    // The whole point of remembering independently: setting the model later
    // must not clobber an already-stored mode/effort.
    writeHarnessOption("claude-native", { mode: "plan", effort: "high" });
    writeHarnessOption("claude-native", { model: "opus" });
    expect(readHarnessOptions("claude-native")).toEqual({
      mode: "plan",
      model: "opus",
      effort: "high",
    });
  });

  it("keeps each harness's options independent", () => {
    // A Codex pick must not leak into Claude Code's slot.
    writeHarnessOption("claude-native", { mode: "auto" });
    writeHarnessOption("codex-native", { mode: "full-access" });
    writeHarnessOption("cursor-native", { mode: "yolo" });
    expect(readHarnessOptions("claude-native")).toEqual({ mode: "auto" });
    expect(readHarnessOptions("codex-native")).toEqual({ mode: "full-access" });
    expect(readHarnessOptions("cursor-native")).toEqual({ mode: "yolo" });
  });

  it("overwrites a knob's previous value for the same harness", () => {
    writeHarnessOption("claude-native", { mode: "auto" });
    writeHarnessOption("claude-native", { mode: "plan" });
    expect(readHarnessOptions("claude-native")).toEqual({ mode: "plan" });
  });

  it("ignores a null/empty harness on write", () => {
    writeHarnessOption(null, { mode: "auto" });
    writeHarnessOption("", { mode: "auto" });
    expect(localStorage.getItem(KEY)).toBeNull();
  });

  it("migrates the legacy bare-string value to { mode } on read", () => {
    // This store originally held a single mode string per harness. A returning
    // user's pre-generalization data must still resolve so their remembered
    // mode survives — not reset to default.
    localStorage.setItem(KEY, JSON.stringify({ "claude-native": "plan" }));
    expect(readHarnessOptions("claude-native")).toEqual({ mode: "plan" });
  });

  it("drops non-string fields and structurally-corrupt entries", () => {
    localStorage.setItem(
      KEY,
      JSON.stringify({
        "claude-native": { model: "opus", effort: 5, nested: { a: 1 } },
        "codex-native": ["not", "an", "object"],
        "cursor-native": 42,
      }),
    );
    // Only the string-valued field survives; the bad fields/entries fall away.
    expect(readHarnessOptions("claude-native")).toEqual({ model: "opus" });
    expect(readHarnessOptions("codex-native")).toEqual({});
    expect(readHarnessOptions("cursor-native")).toEqual({});
  });

  it("tolerates a corrupted blob", () => {
    localStorage.setItem(KEY, "not json{");
    expect(readHarnessOptions("claude-native")).toEqual({});
    // A later write recovers — it doesn't propagate the corruption.
    writeHarnessOption("claude-native", { mode: "plan" });
    expect(readHarnessOptions("claude-native")).toEqual({ mode: "plan" });
  });

  it("never throws when storage is inaccessible", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() => writeHarnessOption("claude-native", { mode: "auto" })).not.toThrow();
    expect(readHarnessOptions("claude-native")).toEqual({});
  });
});
