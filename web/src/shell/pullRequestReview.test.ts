import { describe, expect, it } from "vitest";
import type { PullRequestChangedFile } from "@/hooks/usePullRequests";
import { filterChangedPaths, selectVisibleFiles } from "./pullRequestReview";

function changed(path: string, previous_path?: string): PullRequestChangedFile {
  return {
    path,
    name: path.split("/").pop()!,
    previous_path,
    status: previous_path ? "renamed" : "modified",
    lines_added: 1,
    lines_removed: 0,
    bytes: null,
    modified_at: null,
  };
}

const FILES = [
  changed("src/shell/PullRequestPanel.tsx"),
  changed("src/lib/clipboard.ts", "src/utils/copy.ts"),
  changed("README.md"),
];
const paths = (files: PullRequestChangedFile[]) => files.map((file) => file.path);

describe("filterChangedPaths", () => {
  it.each([
    ["", FILES.map((file) => file.path)],
    ["   ", FILES.map((file) => file.path)],
    ["pullrequest", ["src/shell/PullRequestPanel.tsx"]],
    [" README ", ["README.md"]],
    ["src\\shell\\", ["src/shell/PullRequestPanel.tsx"]],
    ["src/", ["src/shell/PullRequestPanel.tsx", "src/lib/clipboard.ts"]],
    ["utils/copy", ["src/lib/clipboard.ts"]],
    ["missing", []],
  ])("matches %j", (query, expected) => {
    expect(paths(filterChangedPaths(FILES, query))).toEqual(expected);
  });
});

describe("selectVisibleFiles", () => {
  it("returns every file for empty inputs", () => {
    expect(selectVisibleFiles(FILES, { pathFilter: "", hiddenPaths: new Set() })).toEqual(FILES);
  });

  it("combines the path filter with hidden paths", () => {
    const visible = selectVisibleFiles(FILES, {
      pathFilter: "src",
      hiddenPaths: new Set(["src/lib/clipboard.ts", "README.md"]),
    });
    expect(paths(visible)).toEqual(["src/shell/PullRequestPanel.tsx"]);
  });
});
