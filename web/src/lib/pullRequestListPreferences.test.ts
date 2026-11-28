import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_PULL_REQUEST_LIST_DISPLAY,
  readIncludeSubAgentPullRequests,
  readPullRequestListDisplay,
  writeIncludeSubAgentPullRequests,
  writePullRequestListDisplay,
} from "./pullRequestListPreferences";

const KEY = "omnigent:pull-request-list-display";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("pullRequestListPreferences", () => {
  it("defaults to grouping by status, most recently updated first", () => {
    expect(readPullRequestListDisplay()).toEqual({
      grouping: "status",
      order: "updated",
      direction: "descending",
    });
  });

  it("round-trips a display", () => {
    const display = { grouping: "repository", order: "size", direction: "ascending" } as const;
    writePullRequestListDisplay(display);
    expect(readPullRequestListDisplay()).toEqual(display);
  });

  it("gives a display saved without a direction its order's natural direction", () => {
    localStorage.setItem(KEY, JSON.stringify({ grouping: "none", order: "number" }));
    expect(readPullRequestListDisplay()).toEqual({
      grouping: "none",
      order: "number",
      direction: "descending",
    });
    localStorage.setItem(KEY, JSON.stringify({ grouping: "status", order: "title" }));
    expect(readPullRequestListDisplay().direction).toBe("ascending");
  });

  it("falls back per field for unknown or malformed values", () => {
    localStorage.setItem(
      KEY,
      JSON.stringify({ grouping: "author", order: "title", direction: "sideways" }),
    );
    expect(readPullRequestListDisplay()).toEqual({
      grouping: "status",
      order: "title",
      direction: "ascending",
    });
    localStorage.setItem(KEY, "not json");
    expect(readPullRequestListDisplay()).toEqual(DEFAULT_PULL_REQUEST_LIST_DISPLAY);
    localStorage.setItem(KEY, "[]");
    expect(readPullRequestListDisplay()).toEqual(DEFAULT_PULL_REQUEST_LIST_DISPLAY);
  });

  it("tolerates storage that throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("full");
    });
    expect(() =>
      writePullRequestListDisplay({ grouping: "none", order: "title", direction: "ascending" }),
    ).not.toThrow();
    expect(readPullRequestListDisplay()).toEqual(DEFAULT_PULL_REQUEST_LIST_DISPLAY);
  });
});

describe("includeSubAgentPullRequests", () => {
  it("is on until the user turns it off, and the choice persists", () => {
    expect(readIncludeSubAgentPullRequests()).toBe(true);
    writeIncludeSubAgentPullRequests(false);
    expect(readIncludeSubAgentPullRequests()).toBe(false);
    writeIncludeSubAgentPullRequests(true);
    expect(readIncludeSubAgentPullRequests()).toBe(true);
  });
});
