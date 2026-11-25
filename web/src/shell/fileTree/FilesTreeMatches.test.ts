import { describe, expect, it } from "vitest";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import { matchTreePaths } from "./FilesTreeMatches";

const match = (path: string, type: WorkspaceFile["type"] = "file"): WorkspaceFile => ({
  path,
  name: path.split("/").at(-1)!,
  type,
  bytes: null,
  modified_at: null,
});

describe("matchTreePaths", () => {
  it("lists each match under its folders, once each", () => {
    expect(
      matchTreePaths([
        match("src/ui/button.ts"),
        match("src/ui/button.test.ts"),
        match("button.md"),
      ]),
    ).toEqual(["src/", "src/ui/", "src/ui/button.ts", "src/ui/button.test.ts", "button.md"]);
  });

  it("marks a matching folder as a folder", () => {
    expect(matchTreePaths([match("src/buttons", "directory")])).toEqual(["src/", "src/buttons/"]);
  });

  it("has nothing for no matches", () => {
    expect(matchTreePaths([])).toEqual([]);
  });
});
