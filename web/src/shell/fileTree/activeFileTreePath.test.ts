import { describe, expect, it } from "vitest";
import { activeFileTreePath } from "./activeFileTreePath";

describe("activeFileTreePath", () => {
  it.each([
    ["a workspace file at the root", "src/a.ts", "", "/repo", "src/a.ts"],
    ["an absolute path inside the workspace", "/repo/src/a.ts", "", "/repo", "src/a.ts"],
    ["a file under a re-rooted subfolder", "src/shell/a.ts", "src", "/repo", "shell/a.ts"],
    ["an absolute file under a re-rooted subfolder", "/repo/src/a.ts", "src", "/repo/", "a.ts"],
    ["a file under a folder outside the workspace", "/tmp/x/n.md", "/tmp", "/repo", "x/n.md"],
    ["a host-root location", "/tmp/n.md", "/", "/repo", "tmp/n.md"],
    [
      "an absolute workspace file under the host root",
      "/repo/src/a.ts",
      "/",
      "/repo",
      "repo/src/a.ts",
    ],
    ["a relative workspace file under the host root", "src/a.ts", "/", "/repo", "repo/src/a.ts"],
    [
      "an absolute workspace file under its parent",
      "/work/repo/a.ts",
      "/work",
      "/work/repo",
      "repo/a.ts",
    ],
    [
      "a relative workspace file under its parent",
      "src/a.ts",
      "/work",
      "/work/repo/",
      "repo/src/a.ts",
    ],
  ])("places %s", (_name, file, location, root, expected) => {
    expect(activeFileTreePath(file, location, root)).toBe(expected);
  });

  it.each([
    ["no file", null, "", "/repo"],
    ["a file outside the workspace, browsing the root", "/tmp/n.md", "", "/repo"],
    ["a file beside the re-rooted folder", "lib/a.ts", "src", "/repo"],
    ["a sibling folder sharing the prefix", "srcs/a.ts", "src", "/repo"],
    ["a workspace file while browsing outside it", "src/a.ts", "/tmp", "/repo"],
    ["an absolute file when the workspace root is unknown", "/repo/a.ts", "", null],
    ["a relative file outside its browsed parent's sibling", "src/a.ts", "/work-old", "/work/repo"],
    ["a relative file browsing outside with no known root", "src/a.ts", "/", null],
    ["the browsed folder itself", "/repo/src", "src", "/repo"],
  ])("leaves the tree alone for %s", (_name, file, location, root) => {
    expect(activeFileTreePath(file, location, root)).toBeNull();
  });
});
