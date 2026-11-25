import { describe, expect, it } from "vitest";

import type { WorkspaceFileSearchResult } from "@/hooks/useWorkspaceChangedFiles";

import { GO_TO_FILE_VISIBLE_LIMIT, type GoToFileSearchState } from "../goToFileSearchStatus";
import { RECENT_FILES_GROUP } from "./goToFileItems";
import { goToFileView, NO_RECENT_FILES } from "./goToFileView";

function response(paths: string[], extra: Partial<WorkspaceFileSearchResult> = {}) {
  return {
    files: paths.map((path) => ({ path, name: path.split("/").at(-1)!, type: "file" as const })),
    truncated: false,
    hasMore: false,
    unavailable: false,
    ...extra,
  } as WorkspaceFileSearchResult;
}

const settled = (paths: string[], extra?: Partial<WorkspaceFileSearchResult>) =>
  ({ phase: "settled", response: response(paths, extra) }) satisfies GoToFileSearchState;

describe("goToFileView", () => {
  it("lists the recent files before a query, under their heading", () => {
    const view = goToFileView({ phase: "idle", response: undefined }, "", ["src/a.ts", "b.md"]);
    expect(view.items.map((item) => [item.label, item.description, item.group])).toEqual([
      ["a.ts", "src", RECENT_FILES_GROUP],
      ["b.md", undefined, RECENT_FILES_GROUP],
    ]);
    expect(view.status).toBeNull();
  });

  it("says what to type when there are no recent files yet", () => {
    const view = goToFileView({ phase: "idle", response: undefined }, "", []);
    expect(view.items).toEqual([]);
    expect(view.emptyMessage).toBe(NO_RECENT_FILES);
  });

  it("ranks a name match above a folder match and marks where the text occurs", () => {
    const view = goToFileView(settled(["app/widget/main.ts", "src/widget.ts"]), "widget", []);
    expect(view.items.map((item) => item.value.path)).toEqual([
      "src/widget.ts",
      "app/widget/main.ts",
    ]);
    expect(view.items[0]!.highlights?.label).toEqual([{ start: 0, end: 6 }]);
    expect(view.items[1]!.highlights?.description).toEqual([{ start: 4, end: 10 }]);
    expect(view.status).toBeNull();
  });

  it("keeps only an earlier answer's rows that still match while a newer query runs", () => {
    const view = goToFileView(
      { phase: "searching", response: response(["src/widget.ts", "src/window.ts"]) },
      "widg",
      [],
    );
    expect(view.items.map((item) => item.value.path)).toEqual(["src/widget.ts"]);
    expect(view.status).toBe("Searching…");
  });

  it("names the query when nothing matches", () => {
    const view = goToFileView(settled([]), "nothing", ["recent.ts"]);
    expect(view.items).toEqual([]);
    expect(view.emptyMessage).toBe("No files match “nothing”.");
  });

  it("says a failed or unreachable search failed, not that nothing matched", () => {
    expect(goToFileView({ phase: "error", response: undefined }, "a", []).emptyMessage).toMatch(
      /Couldn't search this workspace/,
    );
    expect(
      goToFileView({ phase: "unavailable", response: undefined }, "a", []).emptyMessage,
    ).toMatch(/aren't available right now/);
  });

  it("shows the top matches and says there are more past the visible limit", () => {
    const paths = Array.from({ length: GO_TO_FILE_VISIBLE_LIMIT + 5 }, (_, i) => `f${i}.ts`);
    const view = goToFileView(settled(paths), "f", []);
    expect(view.items).toHaveLength(GO_TO_FILE_VISIBLE_LIMIT);
    expect(view.status).toMatch(/Showing the top/);
  });

  it("says when the search stopped before covering the workspace", () => {
    const view = goToFileView(settled(["a.ts"], { truncated: true }), "a", []);
    expect(view.items).toHaveLength(1);
    expect(view.status).toMatch(/stopped before covering the whole workspace/);
  });
});
