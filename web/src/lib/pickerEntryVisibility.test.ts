import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  HIDDEN_PICKER_AGENTS_STORAGE_KEY,
  readHiddenPickerAgents,
  resetHiddenPickerAgents,
  setPickerAgentHidden,
  subscribeHiddenPickerAgents,
  writeHiddenPickerAgents,
} from "./pickerEntryVisibility";

beforeEach(() => {
  window.localStorage.removeItem(HIDDEN_PICKER_AGENTS_STORAGE_KEY);
  // Drop the module's memoized snapshot so each test starts from the store.
  readHiddenPickerAgents();
});

afterEach(() => {
  window.localStorage.removeItem(HIDDEN_PICKER_AGENTS_STORAGE_KEY);
});

describe("readHiddenPickerAgents", () => {
  it("is empty when nothing is stored", () => {
    expect(readHiddenPickerAgents().size).toBe(0);
  });

  it("returns a stable reference while the stored value is unchanged", () => {
    // useSyncExternalStore compares snapshots by reference; a fresh Set per
    // call would re-render forever.
    writeHiddenPickerAgents(new Set(["polly"]));
    expect(readHiddenPickerAgents()).toBe(readHiddenPickerAgents());
  });

  it("returns a new reference after a write", () => {
    const before = readHiddenPickerAgents();
    writeHiddenPickerAgents(new Set(["polly"]));
    expect(readHiddenPickerAgents()).not.toBe(before);
    expect([...readHiddenPickerAgents()]).toEqual(["polly"]);
  });

  it("treats a corrupt entry as nothing hidden", () => {
    window.localStorage.setItem(HIDDEN_PICKER_AGENTS_STORAGE_KEY, "{not json");
    expect(readHiddenPickerAgents().size).toBe(0);
  });

  it("ignores non-string members", () => {
    window.localStorage.setItem(HIDDEN_PICKER_AGENTS_STORAGE_KEY, '["polly", 7, null]');
    expect([...readHiddenPickerAgents()]).toEqual(["polly"]);
  });
});

describe("writeHiddenPickerAgents", () => {
  it("removes the key when the set is empty, so an untouched pref stays absent", () => {
    writeHiddenPickerAgents(new Set(["polly"]));
    writeHiddenPickerAgents(new Set());
    expect(window.localStorage.getItem(HIDDEN_PICKER_AGENTS_STORAGE_KEY)).toBeNull();
  });

  it("stores names sorted, so the serialized value is stable", () => {
    writeHiddenPickerAgents(new Set(["polly", "debby"]));
    expect(window.localStorage.getItem(HIDDEN_PICKER_AGENTS_STORAGE_KEY)).toBe('["debby","polly"]');
  });
});

describe("setPickerAgentHidden", () => {
  it("adds and removes a single entry", () => {
    setPickerAgentHidden("polly", true);
    expect(readHiddenPickerAgents().has("polly")).toBe(true);
    setPickerAgentHidden("polly", false);
    expect(readHiddenPickerAgents().has("polly")).toBe(false);
  });

  it("does not notify when the value is unchanged", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeHiddenPickerAgents(listener);
    setPickerAgentHidden("polly", false); // already visible
    expect(listener).not.toHaveBeenCalled();
    unsubscribe();
  });
});

describe("subscribeHiddenPickerAgents", () => {
  it("notifies on write and stops after unsubscribe", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeHiddenPickerAgents(listener);
    setPickerAgentHidden("polly", true);
    expect(listener).toHaveBeenCalledTimes(1);
    unsubscribe();
    setPickerAgentHidden("debby", true);
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("notifies on a storage event from another tab", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeHiddenPickerAgents(listener);
    window.dispatchEvent(new StorageEvent("storage", { key: HIDDEN_PICKER_AGENTS_STORAGE_KEY }));
    expect(listener).toHaveBeenCalledTimes(1);
    unsubscribe();
  });

  it("ignores a storage event for an unrelated key", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeHiddenPickerAgents(listener);
    window.dispatchEvent(new StorageEvent("storage", { key: "omnigent:ui-font-size" }));
    expect(listener).not.toHaveBeenCalled();
    unsubscribe();
  });
});

describe("resetHiddenPickerAgents", () => {
  it("clears every hidden entry", () => {
    writeHiddenPickerAgents(new Set(["polly", "debby"]));
    resetHiddenPickerAgents();
    expect(readHiddenPickerAgents().size).toBe(0);
  });
});
