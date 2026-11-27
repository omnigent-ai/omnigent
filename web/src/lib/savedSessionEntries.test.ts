import { describe, expect, it } from "vitest";

import { MAX_SAVED_SESSIONS, withSavedSessionEntry } from "./savedSessionEntries";

describe("withSavedSessionEntry", () => {
  it("sets a session's entry as the newest, and removes it for undefined", () => {
    const saved = withSavedSessionEntry({ first: 1, second: 2 }, "first", 3);
    expect(Object.entries(saved)).toEqual([
      ["second", 2],
      ["first", 3],
    ]);
    expect(withSavedSessionEntry(saved, "second", undefined)).toEqual({ first: 3 });
  });

  it("drops the least recently written sessions past the limit", () => {
    let saved: Record<string, unknown> = {};
    for (const id of ["a", "b", "c"]) saved = withSavedSessionEntry(saved, id, id, 3);
    // Writing a again makes b the oldest.
    saved = withSavedSessionEntry(saved, "a", "a2", 3);
    saved = withSavedSessionEntry(saved, "d", "d", 3);
    expect(Object.keys(saved)).toEqual(["c", "a", "d"]);
  });

  it("keeps a hundred sessions by default", () => {
    let saved: Record<string, unknown> = {};
    for (let index = 0; index <= MAX_SAVED_SESSIONS; index++) {
      saved = withSavedSessionEntry(saved, `session-${index}`, index);
    }
    expect(Object.keys(saved)).toHaveLength(MAX_SAVED_SESSIONS);
    expect(saved).not.toHaveProperty("session-0");
  });
});
