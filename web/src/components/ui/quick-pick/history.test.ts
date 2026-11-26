import { beforeEach, describe, expect, it } from "vitest";

import {
  QUICK_PICK_HISTORY_LIMIT,
  QUICK_PICK_HISTORY_STORAGE_KEY,
  readQuickPickHistory,
  recordQuickPickHistory,
  removeQuickPickHistory,
} from "./history";

beforeEach(() => {
  localStorage.clear();
});

describe("QuickPick history", () => {
  it("records most recent first, without duplicates, per namespace", () => {
    recordQuickPickHistory("commands", "a");
    recordQuickPickHistory("commands", "b");
    recordQuickPickHistory("commands", "a");
    recordQuickPickHistory("sessions", "s1");
    expect(readQuickPickHistory("commands")).toEqual(["a", "b"]);
    expect(readQuickPickHistory("sessions")).toEqual(["s1"]);
  });

  it("treats a namespace named like an object's built-in property as its own", () => {
    for (const namespace of ["constructor", "toString", "__proto__", "hasOwnProperty"]) {
      expect(readQuickPickHistory(namespace)).toEqual([]);
      recordQuickPickHistory(namespace, "a");
      recordQuickPickHistory(namespace, "b");
      expect(readQuickPickHistory(namespace)).toEqual(["b", "a"]);
      removeQuickPickHistory(namespace, "b");
      expect(readQuickPickHistory(namespace)).toEqual(["a"]);
    }
    // Each survives a reload from storage, apart from the others.
    expect(readQuickPickHistory("__proto__")).toEqual(["a"]);
    expect(readQuickPickHistory("commands")).toEqual([]);
  });

  it("caps each namespace", () => {
    for (let index = 0; index < QUICK_PICK_HISTORY_LIMIT + 5; index++) {
      recordQuickPickHistory("commands", `item-${index}`);
    }
    const ids = readQuickPickHistory("commands");
    expect(ids).toHaveLength(QUICK_PICK_HISTORY_LIMIT);
    expect(ids[0]).toBe(`item-${QUICK_PICK_HISTORY_LIMIT + 4}`);
  });

  it("removes an entry", () => {
    recordQuickPickHistory("commands", "a");
    recordQuickPickHistory("commands", "b");
    removeQuickPickHistory("commands", "a");
    expect(readQuickPickHistory("commands")).toEqual(["b"]);
  });

  it("treats corrupt storage as empty and recovers on the next write", () => {
    localStorage.setItem(QUICK_PICK_HISTORY_STORAGE_KEY, "{not json");
    expect(readQuickPickHistory("commands")).toEqual([]);
    localStorage.setItem(QUICK_PICK_HISTORY_STORAGE_KEY, JSON.stringify({ commands: [1, "a"] }));
    expect(readQuickPickHistory("commands")).toEqual(["a"]);
    recordQuickPickHistory("commands", "b");
    expect(readQuickPickHistory("commands")).toEqual(["b", "a"]);
  });
});
