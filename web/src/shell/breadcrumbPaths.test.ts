import { describe, expect, it } from "vitest";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import {
  breadcrumbSegments,
  breadcrumbText,
  folderTitle,
  listingTreePaths,
  parentFolder,
  pathWithinFolder,
  revealedFolders,
  segmentFolder,
  workspaceFolderName,
} from "./breadcrumbPaths";

function folder(path: string, leafDirectoryPath?: string): WorkspaceFile {
  return {
    path,
    name: path.split("/").at(-1) ?? path,
    type: "directory",
    bytes: null,
    modified_at: null,
    leaf_directory_path: leafDirectoryPath,
  };
}

function file(path: string): WorkspaceFile {
  return { path, name: path.split("/").at(-1) ?? path, type: "file", bytes: 1, modified_at: null };
}

describe("breadcrumbSegments", () => {
  it("splits a workspace-relative path into folders and the file", () => {
    expect(breadcrumbSegments(".github/agents/config.yaml")).toEqual([
      { label: ".github", path: ".github", isFile: false },
      { label: "agents", path: ".github/agents", isFile: false },
      { label: "config.yaml", path: ".github/agents/config.yaml", isFile: true },
    ]);
  });

  it("keeps absolute paths absolute", () => {
    expect(breadcrumbSegments("/tmp/notes.md").map((segment) => segment.path)).toEqual([
      "/tmp",
      "/tmp/notes.md",
    ]);
  });

  it("splits Windows separators into forward-slash segment paths", () => {
    expect(breadcrumbSegments("docs\\notes.md").map((segment) => segment.path)).toEqual([
      "docs",
      "docs/notes.md",
    ]);
  });

  it("shows a file at the workspace root as one segment", () => {
    expect(breadcrumbSegments("README.md")).toEqual([
      { label: "README.md", path: "README.md", isFile: true },
    ]);
  });
});

describe("segment folders", () => {
  it("lists each segment's siblings: the folder holding it", () => {
    const [top, middle, name] = breadcrumbSegments("Macterm/App/AppState.swift");
    expect(segmentFolder(top)).toBe("");
    expect(segmentFolder(middle)).toBe("Macterm");
    expect(segmentFolder(name)).toBe("Macterm/App");
  });

  it("lists an absolute path's first folder beside the host root's other entries", () => {
    const [top, name] = breadcrumbSegments("/tmp/notes.md");
    expect(segmentFolder(top)).toBe("/");
    expect(segmentFolder(name)).toBe("/tmp");
  });

  it("selects the segment itself within the listed folder", () => {
    for (const segment of breadcrumbSegments("/tmp/logs/run.txt")) {
      expect(pathWithinFolder(segment.path, segmentFolder(segment))).toBe(segment.label);
    }
  });

  it("names the workspace and host roots", () => {
    expect(parentFolder("README.md")).toBe("");
    expect(parentFolder("/notes.md")).toBe("/");
    expect(parentFolder("/tmp/notes.md")).toBe("/tmp");
  });

  it("expresses the viewed file relative to the tree's folder", () => {
    expect(pathWithinFolder("src/shell/viewer.ts", "src")).toBe("shell/viewer.ts");
    expect(pathWithinFolder("README.md", "")).toBe("README.md");
    expect(pathWithinFolder("/notes.md", "/")).toBe("notes.md");
  });

  it("lists every folder between the tree's root and the revealed file", () => {
    expect(revealedFolders("shell/codeMirror/viewer.ts")).toEqual([
      "",
      "shell",
      "shell/codeMirror",
    ]);
    expect(revealedFolders("viewer.ts")).toEqual([""]);
  });

  it("titles the workspace root with the workspace's name", () => {
    expect(folderTitle("", "omnigent")).toBe("omnigent");
    expect(folderTitle("src/shell", "omnigent")).toBe("src/shell");
  });
});

describe("listingTreePaths", () => {
  it("marks folders with a trailing slash", () => {
    expect(listingTreePaths([folder("src"), file("README.md")])).toEqual({
      entries: ["src/", "README.md"],
      chains: [],
    });
  });

  it("adds a single-folder chain's deepest folder and treats the folders above it as listed", () => {
    expect(listingTreePaths([folder("java", "java/com/example")])).toEqual({
      entries: ["java/"],
      chains: [{ path: "java/com/example/", folders: ["java", "java/com"] }],
    });
  });

  it("ignores a chain leaf outside the folder", () => {
    expect(listingTreePaths([folder("java", "other/place")])).toEqual({
      entries: ["java/"],
      chains: [],
    });
  });
});

describe("breadcrumbText", () => {
  it.each([
    ["README.md", "README.md"],
    ["src/shell/FileViewer.tsx", "src › shell › FileViewer.tsx"],
    ["docs\\notes.md", "docs › notes.md"],
    ["a/b/c/d/e/file.py", "a › b › c › d › e › file.py"],
  ])("shows %s as %s, every folder included", (path, expected) => {
    expect(breadcrumbText(path)).toBe(expected);
  });
});

describe("workspaceFolderName", () => {
  it.each([
    ["/Users/me/repo", "repo"],
    ["/Users/me/repo/", "repo"],
    ["C:\\work\\", "work"],
    ["/", "Workspace"],
    [null, "Workspace"],
    [undefined, "Workspace"],
  ])("names root %s %s", (root, expected) => {
    expect(workspaceFolderName(root)).toBe(expected);
  });
});
