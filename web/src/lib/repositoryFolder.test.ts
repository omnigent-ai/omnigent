import { describe, expect, it } from "vitest";

import { type HostWorktree, matchWorktree } from "@/hooks/useHostWorktrees";

import {
  diffRootFor,
  repositoryFolderOf,
  repositoryPathOf,
  repositoryRootIn,
  workspaceInsideLargerRepository,
  workspacePathInRepository,
} from "./repositoryFolder";

function worktree(path: string, isMain = false): HostWorktree {
  return { path, branch: isMain ? "main" : "feature", is_main: isMain, detached: false };
}

const WORKTREES = [
  worktree("/repo", true),
  worktree("/repo/.worktrees/feature"),
  worktree("/repo/.worktrees/feature-two/"),
  worktree("/elsewhere/sibling"),
  // On POSIX a backslash is part of a name, here the last character of one.
  worktree("/repo/worktree\\"),
];

// A workspace that is a folder of a larger repository, which has a worktree inside it too.
const LARGER = [worktree("/monorepo", true), worktree("/monorepo/app/.worktrees/feature")];

describe("workspaceInsideLargerRepository", () => {
  it.each([
    ["its own repository's root", "/repo", WORKTREES, false],
    ["its own root given with a trailing slash", "/repo/", WORKTREES, false],
    ["a linked worktree's root", "/repo/.worktrees/feature", WORKTREES, false],
    ["a folder of a larger repository", "/monorepo/app", LARGER, true],
    ["a folder inside a linked worktree", "/repo/.worktrees/feature/src", WORKTREES, true],
    ["outside every listed worktree", "/scratch", WORKTREES, false],
    [
      "its own root, listed with the other separator",
      "C:\\repo",
      [worktree("C:/repo", true)],
      false,
    ],
    ["its own root, with the drive in another case", "c:/repo", [worktree("C:/repo", true)], false],
    ["a folder of a larger Windows repository", "C:\\mono\\app", [worktree("c:/mono", true)], true],
  ])("for %s", (_case, workspace, worktrees, inside) => {
    expect(workspaceInsideLargerRepository(workspace, worktrees)).toBe(inside);
  });
});

describe("diffRootFor", () => {
  it.each([
    ["a worktree in it", "/monorepo/app/.worktrees/feature/src", "/monorepo/app", LARGER],
    ["another folder of the worktree holding it", "/repo/docs", "/repo/src", WORKTREES],
  ])(
    "stays on a workspace inside a larger repository, even for %s",
    (_case, directory, workspace, worktrees) => {
      expect(diffRootFor(directory, workspace, worktrees)).toBe(workspace);
    },
  );

  it.each([
    ["no directory chosen", null, "/repo"],
    ["the workspace itself", "/repo", "/repo"],
    ["an ordinary workspace subfolder", "/repo/src/lib", "/repo"],
    ["a nested worktree's root", "/repo/.worktrees/feature", "/repo/.worktrees/feature"],
    [
      "a folder inside a nested worktree",
      "/repo/.worktrees/feature/src",
      "/repo/.worktrees/feature",
    ],
    [
      "a worktree root given with a trailing slash",
      "/repo/.worktrees/feature/",
      "/repo/.worktrees/feature",
    ],
    [
      "a worktree listed with a trailing slash",
      "/repo/.worktrees/feature-two/a",
      "/repo/.worktrees/feature-two",
    ],
    ["a sibling whose name only shares a prefix", "/repo/.worktrees/feature-three", "/repo"],
    ["a folder no worktree holds", "/tmp/scratch", "/repo"],
    ["a worktree outside the workspace", "/elsewhere/sibling/src", "/elsewhere/sibling"],
    ["a worktree whose POSIX name ends in a backslash", "/repo/worktree\\/src", "/repo/worktree\\"],
  ])("reviews the right root for %s", (_case, directory, expected) => {
    expect(diffRootFor(directory, "/repo", WORKTREES)).toBe(expected);
  });

  it("keeps a drive root's separator for a worktree at the root of a drive", () => {
    const drives = [worktree("C:\\repo", true), worktree("D:\\")];
    expect(diffRootFor("D:\\src", "C:\\repo", drives)).toBe("D:\\");
  });

  it("keeps the workspace while the worktrees are unknown", () => {
    expect(diffRootFor("/repo/.worktrees/feature", "/repo", [])).toBe("/repo");
  });
});

describe("repositoryRootIn", () => {
  it.each([
    ["the workspace itself", "/repo", "", "/repo"],
    ["a folder", "/repo", ".worktrees/feature", "/repo/.worktrees/feature"],
    [
      "a folder of a workspace written with a slash",
      "/repo/",
      ".worktrees/feature",
      "/repo/.worktrees/feature",
    ],
    ["a folder of the filesystem root", "/", "project", "/project"],
    ["a folder of a Windows drive's root", "C:\\", "project", "C:\\project"],
    ["a folder of a Windows workspace", "C:\\repo\\", "project", "C:\\repo/project"],
  ])("names %s", (_case, workspace, repository, root) => {
    expect(repositoryRootIn(workspace, repository)).toBe(root);
  });

  it("names a root the worktree matcher finds", () => {
    const listed = [worktree("C:/project")];
    expect(matchWorktree(listed, repositoryRootIn("C:\\", "project"))).toBe(listed[0]);
  });
});

describe("repositoryFolderOf", () => {
  // Containment boundaries live in `pathInWorktree`'s matrix; these check the arguments' order.
  it.each([
    ["the workspace", "/repo/", "/repo", ""],
    ["a nested worktree", "/repo/.worktrees/feature", "/repo", ".worktrees/feature"],
    ["the workspace's parent, outside it", "/", "/repo", null],
    ["a Windows worktree", "c:\\repo\\.worktrees\\feature", "C:/repo", ".worktrees/feature"],
  ])("names %s as the host does", (_case, root, workspace, expected) => {
    expect(repositoryFolderOf(root, workspace)).toBe(expected);
  });
});

describe("paths between a worktree's review and its session", () => {
  const FOLDER = ".worktrees/feature";

  it("names a worktree's file relative to the workspace", () => {
    expect(workspacePathInRepository("src/a.ts", FOLDER)).toBe(".worktrees/feature/src/a.ts");
    expect(workspacePathInRepository("src/a.ts", "")).toBe("src/a.ts");
  });

  it("finds a session's path in the review, or nothing outside its folder", () => {
    expect(repositoryPathOf(".worktrees/feature/src/a.ts", FOLDER, "/repo")).toBe("src/a.ts");
    expect(repositoryPathOf("/repo/.worktrees/feature/src/a.ts", FOLDER, "/repo")).toBe("src/a.ts");
    expect(repositoryPathOf("src/a.ts", FOLDER, "/repo")).toBeNull();
    expect(repositoryPathOf(".worktrees/feature-two/a.ts", FOLDER, "/repo")).toBeNull();
    expect(repositoryPathOf("C:\\repo\\.worktrees\\feature\\src\\a.ts", FOLDER, "C:\\repo")).toBe(
      "src/a.ts",
    );
    expect(repositoryPathOf("C:\\elsewhere\\a.ts", FOLDER, "C:\\repo")).toBeNull();
    expect(repositoryPathOf("C:/repo/.worktrees/feature/src/a.ts", FOLDER, "C:/repo")).toBe(
      "src/a.ts",
    );
    expect(repositoryPathOf("C:/repo/src/a.ts", "", "C:/repo")).toBe("src/a.ts");
    expect(repositoryPathOf("/src/a.ts", "", "/")).toBe("src/a.ts");
    // A workspace at a filesystem root, or written with a trailing separator.
    expect(repositoryPathOf("/worktree/a.ts", "worktree", "/")).toBe("a.ts");
    // On a POSIX workspace a drive-like name is a file, however the path reaches it.
    expect(repositoryPathOf("C:\\a.ts", "", "/repo")).toBe("C:\\a.ts");
    expect(repositoryPathOf("/repo/C:\\a.ts", "", "/repo")).toBe("C:\\a.ts");
    expect(workspacePathInRepository("C:\\a.ts", "")).toBe("C:\\a.ts");
    expect(repositoryPathOf(".worktrees/feature/C:\\a.ts", FOLDER, "/repo")).toBe("C:\\a.ts");
    expect(repositoryPathOf("/repo/.worktrees/feature/C:\\a.ts", FOLDER, "/repo")).toBe("C:\\a.ts");
    expect(repositoryPathOf("worktree/a.ts", "worktree", "/")).toBe("a.ts");
    expect(repositoryPathOf("C:\\worktree\\a.ts", "worktree", "C:\\")).toBe("a.ts");
    expect(repositoryPathOf("worktree/a.ts", "worktree", "C:\\")).toBe("a.ts");
    expect(repositoryPathOf("/repo/.worktrees/feature/a.ts", FOLDER, "/repo/")).toBe("a.ts");
    expect(repositoryPathOf("C:\\repo\\.worktrees\\feature\\a.ts", FOLDER, "C:\\repo\\")).toBe(
      "a.ts",
    );
    // A Windows host's case doesn't matter, and the file keeps its own spelling.
    expect(repositoryPathOf("C:\\repo\\.WORKTREES\\Feature\\src\\A.ts", FOLDER, "C:\\repo")).toBe(
      "src/A.ts",
    );
    expect(repositoryPathOf(".WORKTREES/Feature/src/a.ts", FOLDER, "C:\\repo")).toBe("src/a.ts");
    expect(repositoryPathOf(".worktrees/feature", FOLDER, "/repo")).toBeNull();
    expect(repositoryPathOf("/elsewhere/a.ts", FOLDER, "/repo")).toBeNull();
    expect(repositoryPathOf("/repo/src/a.ts", "", "/repo")).toBe("src/a.ts");
  });

  it.each([
    ["a workspace file, no folder", "src/a.ts", "", "src/a.ts"],
    ["a file in the folder", ".worktrees/feature/src/a.ts", ".worktrees/feature", "src/a.ts"],
    [
      "a sibling sharing the folder's name",
      ".worktrees/feature-two/a.ts",
      ".worktrees/feature",
      null,
    ],
    ["an absolute path it can't place", "/repo/a.ts", "", null],
  ])("maps %s with no workspace known", (_case, path, repository, expected) => {
    expect(repositoryPathOf(path, repository, null)).toBe(expected);
  });

  it("maps a UNC share's files, written with either separator and in another case", () => {
    const share = "\\\\server\\share\\repo";
    expect(repositoryPathOf(`${share}\\.worktrees\\feature\\a.ts`, FOLDER, share)).toBe("a.ts");
    expect(repositoryPathOf("//Server/Share/Repo/.worktrees/feature/a.ts", FOLDER, share)).toBe(
      "a.ts",
    );
    expect(repositoryFolderOf(`${share}\\.worktrees\\feature`, share)).toBe(".worktrees/feature");
    expect(repositoryPathOf("\\\\server\\other\\a.ts", FOLDER, share)).toBeNull();
  });
});
