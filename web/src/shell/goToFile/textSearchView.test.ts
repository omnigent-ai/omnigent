import { describe, expect, it } from "vitest";

import { type ContentMatch, EMPTY_CONTENT_SEARCH } from "@/lib/contentSearch";

import { TEXT_SEARCH_IDLE, TEXT_SEARCH_SEARCHING, textSearchView } from "./textSearchView";

const input = { ...EMPTY_CONTENT_SEARCH, query: "needle" };
const match = (path: string, line: number): ContentMatch => ({
  path,
  line,
  column: 1,
  text: "needle here",
  match_start: 0,
  match_end: 6,
});
const result = (matches: ContentMatch[], extra = {}) => ({
  matches,
  truncated: false,
  warning: null,
  ...extra,
});

describe("textSearchView", () => {
  it("says what to type before a query, and how to fix a search that can't run", () => {
    expect(textSearchView({ phase: "idle" }, EMPTY_CONTENT_SEARCH).emptyMessage).toBe(
      TEXT_SEARCH_IDLE,
    );
    expect(
      textSearchView({ phase: "invalid", problem: "Turn off Whole word." }, input).emptyMessage,
    ).toBe("Turn off Whole word.");
  });

  it("keeps the last answer's lines while a newer search runs", () => {
    const view = textSearchView(
      { phase: "searching", previous: { result: result([match("a.ts", 3)]), input } },
      input,
    );
    expect(view.items.map((item) => item.value)).toEqual([
      { path: "a.ts", line: 3, column: 1, endColumn: 7 },
    ]);
    expect(view.status).toBe(TEXT_SEARCH_SEARCHING);
  });

  it("marks the last answer's lines for its own search, not for what is typed now", () => {
    const previous = { result: result([match("a.ts", 3)]), input };
    // "here" is typed meanwhile; the old lines still mark only "needle", where it was found.
    const typing = { ...input, query: "here" };
    const view = textSearchView({ phase: "searching", previous }, typing);
    const settled = textSearchView({ phase: "settled", result: previous.result }, input);
    expect(view.items.map((item) => item.highlights)).toEqual(
      settled.items.map((item) => item.highlights),
    );
  });

  it("counts the lines and files found", () => {
    const view = textSearchView(
      { phase: "settled", result: result([match("a.ts", 1), match("b.ts", 2)]) },
      input,
    );
    expect(view.items.map((item) => item.group)).toEqual(["a.ts", "b.ts"]);
    expect(view.status).toBe("2 results in 2 files");
  });

  it("says a search stopped short, or why the runner warned", () => {
    const capped = textSearchView(
      { phase: "settled", result: result([match("a.ts", 1)], { truncated: true }) },
      input,
    );
    expect(capped.status).toBe("1+ results in 1 file. Narrow the search to see the rest.");
    const warned = textSearchView(
      { phase: "settled", result: result([], { warning: "Skipped large files." }) },
      input,
    );
    expect(warned.status).toBe("Skipped large files.");
  });

  it("names the query when nothing matches, and says a failed search failed", () => {
    const none = textSearchView({ phase: "settled", result: result([]) }, input);
    expect(none.emptyMessage).toBe("No lines match “needle”.");
    expect(none.status).toBeNull();
    expect(
      textSearchView({ phase: "error", message: "The runner is offline." }, input).emptyMessage,
    ).toBe("Couldn't search this session's files. The runner is offline.");
  });
});
