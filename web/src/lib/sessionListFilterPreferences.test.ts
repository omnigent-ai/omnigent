import { afterEach, describe, expect, it, vi } from "vitest";

import {
  readSessionPullRequestFilter,
  writeSessionPullRequestFilter,
} from "./sessionListFilterPreferences";
import { SESSION_PULL_REQUEST_FILTERS } from "./sessionListFilters";

const STORAGE_KEY = "omnigent:session-pull-request-filter";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("sessionListFilterPreferences", () => {
  it("shows all sessions when nothing is stored", () => {
    expect(readSessionPullRequestFilter()).toBe("all");
  });

  it("round-trips every filter", () => {
    for (const filter of SESSION_PULL_REQUEST_FILTERS) {
      writeSessionPullRequestFilter(filter);
      expect(readSessionPullRequestFilter()).toBe(filter);
    }
  });

  it("falls back to all sessions for a value this build doesn't know", () => {
    localStorage.setItem(STORAGE_KEY, "starred");
    expect(readSessionPullRequestFilter()).toBe("all");
    localStorage.setItem(STORAGE_KEY, "");
    expect(readSessionPullRequestFilter()).toBe("all");
  });

  it("survives storage that throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("full");
    });
    expect(() => writeSessionPullRequestFilter("open")).not.toThrow();
    expect(readSessionPullRequestFilter()).toBe("all");
  });
});
