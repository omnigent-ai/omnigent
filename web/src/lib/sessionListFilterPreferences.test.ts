import { afterEach, describe, expect, it, vi } from "vitest";

import {
  readSessionListFilter,
  readSessionPullRequestFilter,
  writeSessionListFilter,
  writeSessionPullRequestFilter,
} from "./sessionListFilterPreferences";
import {
  DEFAULT_SESSION_LIST_FILTER,
  SESSION_PULL_REQUEST_FILTERS,
  type SessionListFilter,
} from "./sessionListFilters";

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

describe("readSessionListFilter", () => {
  const LIST_FILTER_KEY = "omnigent:session-list-filter";

  it("keeps no filter on when nothing is stored", () => {
    expect(readSessionListFilter()).toEqual(DEFAULT_SESSION_LIST_FILTER);
  });

  it("round-trips every filter", () => {
    const filter: SessionListFilter = {
      pullRequest: "open",
      status: "attention",
      harness: "native:codex",
      project: { kind: "project", id: "p-1" },
    };
    writeSessionListFilter(filter);
    expect(readSessionListFilter()).toEqual(filter);
  });

  it("keeps the Unknown harness choice, sessions without a harness, through a reload", () => {
    writeSessionListFilter({ ...DEFAULT_SESSION_LIST_FILTER, harness: "" });
    expect(readSessionListFilter().harness).toBe("");
  });

  it("carries over the pull request filter stored on its own, until the filters are stored", () => {
    writeSessionPullRequestFilter("draft");
    expect(readSessionListFilter()).toEqual({
      ...DEFAULT_SESSION_LIST_FILTER,
      pullRequest: "draft",
    });
    writeSessionListFilter({ ...DEFAULT_SESSION_LIST_FILTER, status: "done" });
    expect(readSessionListFilter()).toEqual({ ...DEFAULT_SESSION_LIST_FILTER, status: "done" });
  });

  it("falls back to All for each filter it can't read, and for a value that isn't JSON", () => {
    localStorage.setItem(LIST_FILTER_KEY, JSON.stringify({ status: "working", harness: 3 }));
    expect(readSessionListFilter()).toEqual({ ...DEFAULT_SESSION_LIST_FILTER, status: "working" });
    localStorage.setItem(LIST_FILTER_KEY, "{");
    expect(readSessionListFilter()).toEqual(DEFAULT_SESSION_LIST_FILTER);
  });

  it("survives storage that throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("full");
    });
    expect(() => writeSessionListFilter(DEFAULT_SESSION_LIST_FILTER)).not.toThrow();
    expect(readSessionListFilter()).toEqual(DEFAULT_SESSION_LIST_FILTER);
  });
});
