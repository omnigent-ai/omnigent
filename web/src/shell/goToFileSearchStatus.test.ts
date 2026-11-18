import { describe, expect, it } from "vitest";

import type { WorkspaceFile, WorkspaceFileSearchResult } from "@/hooks/useWorkspaceChangedFiles";

import {
  GO_TO_FILE_RESULT_LIMIT,
  GO_TO_FILE_VISIBLE_LIMIT,
  type GoToFileSearchSnapshot,
  goToFileSearchState,
  goToFileStatusMessage,
} from "./goToFileSearchStatus";

function files(count: number): WorkspaceFile[] {
  return Array.from({ length: count }, (_, index) => ({
    path: `src/file${index}.ts`,
    name: `file${index}.ts`,
    type: "file",
    bytes: null,
    modified_at: null,
  }));
}

function response(overrides: Partial<WorkspaceFileSearchResult> = {}): WorkspaceFileSearchResult {
  return { files: files(3), truncated: false, hasMore: false, unavailable: false, ...overrides };
}

/** A search that answered the typed query. */
function settled(overrides: Partial<GoToFileSearchSnapshot> = {}): GoToFileSearchSnapshot {
  return {
    query: "file",
    searchedQuery: "file",
    serveable: true,
    data: response(),
    isSuccess: true,
    isError: false,
    isFetching: false,
    isPlaceholderData: false,
    ...overrides,
  };
}

function message(snapshot: GoToFileSearchSnapshot, matchCount: number): string | null {
  return goToFileStatusMessage(goToFileSearchState(snapshot), snapshot.query, matchCount);
}

describe("goToFileSearchState", () => {
  it("is idle until something other than spaces is typed", () => {
    expect(goToFileSearchState(settled({ query: "", searchedQuery: "" })).phase).toBe("idle");
    expect(goToFileSearchState(settled({ query: "   " })).phase).toBe("idle");
  });

  it("is unavailable when the workspace is known to be unreachable, even with old results", () => {
    const state = goToFileSearchState(settled({ serveable: false }));
    expect(state).toEqual({ phase: "unavailable", response: undefined });
  });

  it("is unavailable when the settled response says the workspace couldn't be searched", () => {
    const data = response({ files: [], unavailable: true });
    expect(goToFileSearchState(settled({ data }))).toEqual({
      phase: "unavailable",
      response: undefined,
    });
    expect(goToFileSearchState(settled({ serveable: undefined, data })).phase).toBe("unavailable");
  });

  it("doesn't let an earlier unavailable answer override a new query or a retry", () => {
    const data = response({ files: [], unavailable: true });
    expect(goToFileSearchState(settled({ query: "files", data })).phase).toBe("searching");
    expect(goToFileSearchState(settled({ data, isFetching: true })).phase).toBe("searching");
    expect(goToFileSearchState(settled({ data, isPlaceholderData: true })).phase).toBe("searching");
  });

  it("keeps searching while an unknown workspace may still answer", () => {
    const state = goToFileSearchState(
      settled({ serveable: undefined, data: undefined, isSuccess: false, isFetching: true }),
    );
    expect(state.phase).toBe("searching");
  });

  it("reports an error only once the failed search for the typed text has stopped", () => {
    const failed = { data: undefined, isSuccess: false, isError: true };
    expect(goToFileSearchState(settled(failed))).toEqual({ phase: "error", response: undefined });
    // A retry is running, or the text has changed since the failure: not an error yet.
    expect(goToFileSearchState(settled({ ...failed, isFetching: true })).phase).toBe("searching");
    expect(goToFileSearchState(settled({ ...failed, query: "files" })).phase).toBe("searching");
  });

  it("is searching, not settled, while the debounce or a request is pending", () => {
    expect(goToFileSearchState(settled({ query: "fil" })).phase).toBe("searching");
    expect(goToFileSearchState(settled({ isFetching: true })).phase).toBe("searching");
    expect(goToFileSearchState(settled({ isPlaceholderData: true })).phase).toBe("searching");
    expect(
      goToFileSearchState(settled({ data: undefined, isSuccess: false, isFetching: true })).phase,
    ).toBe("searching");
  });

  it("passes on the earlier response while searching, for rows that still match", () => {
    const earlier = response();
    expect(goToFileSearchState(settled({ query: "file1", data: earlier })).response).toBe(earlier);
  });

  it("settles when the response for the typed text has arrived, ignoring outer spaces", () => {
    const data = response();
    expect(goToFileSearchState(settled({ query: " file ", data }))).toEqual({
      phase: "settled",
      response: data,
    });
  });
});

describe("goToFileStatusMessage", () => {
  it("prompts before anything is typed", () => {
    expect(message(settled({ query: "" }), 0)).toBe("Type part of a file name or path.");
  });

  it("explains an unreachable workspace and a failed search", () => {
    expect(message(settled({ serveable: false }), 3)).toBe(
      "This session's files aren't available right now.",
    );
    expect(message(settled({ data: undefined, isSuccess: false, isError: true }), 0)).toBe(
      "Couldn't search this workspace. Check that the session is connected and try again.",
    );
  });

  it("never says nothing matched while a search is still running", () => {
    expect(message(settled({ isFetching: true }), 0)).toBe("Searching…");
    expect(message(settled({ query: "zzz" }), 0)).toBe("Searching…");
  });

  it("says nothing when a complete answer's rows are all shown", () => {
    expect(message(settled(), 3)).toBeNull();
    expect(message(settled(), GO_TO_FILE_VISIBLE_LIMIT)).toBeNull();
  });

  it("says when only the top matches are drawn", () => {
    expect(message(settled({ data: response({ files: files(150) }) }), 150)).toBe(
      `Showing the top ${GO_TO_FILE_VISIBLE_LIMIT} matches. Keep typing to narrow the search.`,
    );
  });

  it("treats has_more and a full page as possibly incomplete", () => {
    const capped = "Showing the first matches. Keep typing to narrow the search.";
    expect(message(settled({ data: response({ hasMore: true }) }), 3)).toBe(capped);
    // A page at the server's cap may be incomplete even without has_more.
    const full = response({ files: files(GO_TO_FILE_RESULT_LIMIT) });
    expect(message(settled({ data: full }), 40)).toBe(capped);
    expect(message(settled({ data: full }), GO_TO_FILE_RESULT_LIMIT)).toBe(
      `Showing the top ${GO_TO_FILE_VISIBLE_LIMIT} matches. Keep typing to narrow the search.`,
    );
  });

  it("says when the server stopped before covering the workspace, ahead of other notes", () => {
    const data = response({
      files: files(GO_TO_FILE_RESULT_LIMIT),
      truncated: true,
      hasMore: true,
    });
    expect(message(settled({ data }), GO_TO_FILE_RESULT_LIMIT)).toBe(
      "The search stopped before covering the whole workspace. Keep typing to narrow it.",
    );
  });

  it("never reads an incomplete empty answer as no files", () => {
    expect(message(settled({ data: response({ files: [], truncated: true }) }), 0)).toBe(
      "No matches found before the search stopped. Keep typing to narrow it.",
    );
    // Only directories came back from a capped page, so no file rows remain.
    expect(message(settled({ data: response({ hasMore: true }) }), 0)).toBe(
      "No files found among the first matches. Keep typing to narrow the search.",
    );
  });

  it("names the query when a complete answer has no files", () => {
    expect(
      message(
        settled({ query: " readme ", searchedQuery: "readme", data: response({ files: [] }) }),
        0,
      ),
    ).toBe("No files match “readme”.");
  });
});
