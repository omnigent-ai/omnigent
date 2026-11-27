import { describe, expect, it } from "vitest";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import {
  changesWithinFolder,
  listingsVersion,
  treeFilePathFilter,
  treeMetadata,
  treePathFilter,
  treeRootEntries,
  visibleEntries,
  withChangedFiles,
} from "./filesTreeListings";

const file = (path: string, bytes = 1, modified = 1): WorkspaceFile => ({
  path,
  name: path.split("/").at(-1)!,
  type: "file",
  bytes,
  modified_at: modified,
});
const folder = (path: string, leafDirectoryPath?: string): WorkspaceFile => ({
  path,
  name: path.split("/").at(-1)!,
  type: "directory",
  bytes: null,
  modified_at: 5,
  ...(leafDirectoryPath ? { leaf_directory_path: leafDirectoryPath } : {}),
});

describe("visibleEntries", () => {
  it("keeps everything when hidden files show", () => {
    const entries = [file(".env"), folder(".config", ".config/review/rules"), file("a.ts")];
    expect(visibleEntries(entries, true)).toEqual(entries);
  });

  it("leaves out dot-entries when hidden files are hidden", () => {
    const entries = [file(".env"), folder(".git"), file("a.ts")];
    expect(visibleEntries(entries, false).map((entry) => entry.path)).toEqual(["a.ts"]);
  });

  it("cuts a chain hint before its first hidden folder", () => {
    const [cut] = visibleEntries([folder("a", "a/b/.cache/c")], false);
    expect(cut?.leaf_directory_path).toBe("a/b");
    const [dropped] = visibleEntries([folder("a", "a/.cache/c")], false);
    expect(dropped).toEqual(folder("a"));
  });

  it("keeps a chain hint with no hidden folders", () => {
    const [kept] = visibleEntries([folder("review", "review/rules")], false);
    expect(kept?.leaf_directory_path).toBe("review/rules");
  });

  it("leaves out what an exclude matches, and brings it back once the patterns clear", () => {
    const entries = [file("pnpm.lock"), file("a.ts"), folder("node_modules"), folder("src")];
    const shown = (exclude: string) =>
      visibleEntries(entries, true, { include: "", exclude }).map((entry) => entry.path);
    expect(shown("*.lock, node_modules")).toEqual(["a.ts", "src"]);
    expect(shown("")).toEqual(["pnpm.lock", "a.ts", "node_modules", "src"]);
  });

  it("keeps every folder under an include, and only the files it matches", () => {
    const entries = [file("a.ts"), file("b.md"), folder("docs"), folder("src")];
    const shown = visibleEntries(entries, true, { include: "*.ts", exclude: "" });
    expect(shown.map((entry) => entry.path)).toEqual(["a.ts", "docs", "src"]);
  });

  it("cuts a chain hint before its first excluded folder", () => {
    const patterns = { include: "", exclude: "generated" };
    const [cut] = visibleEntries([folder("com", "com/app/generated/v1")], true, patterns);
    expect(cut?.leaf_directory_path).toBe("com/app");
  });
});

describe("treePathFilter", () => {
  const shows = (include: string, exclude: string, path: string, type: "file" | "directory") =>
    treePathFilter(true, { include, exclude })(path, type);

  // A trailing `/` or `/**` names a folder: never a file of the same name.
  it.each([
    ["build/", "build", "file", false],
    ["build/", "build", "directory", true],
    ["build/", "build/out.js", "file", true],
    ["build/**", "build", "file", false],
    ["build/**", "build", "directory", true],
    ["out/build/", "out/build", "file", false],
    ["out/build/", "out/build", "directory", true],
  ] as const)("a folder pattern %j matches %j as a %s: %s", (pattern, path, type, matches) => {
    expect(shows("", pattern, path, type)).toBe(!matches);
    // Folders always pass an include, so only a file shows whether the include matched.
    if (type === "file") expect(shows(pattern, "", path, type)).toBe(matches);
  });

  // Glob syntax lives in globMatches.json. What the tree adds: an exclude hides a file under
  // a folder it matches, while an include keeps only the files it matches itself.
  it.each([
    // [pattern, path, exclude hides it, include keeps it]
    ["node_modules", "a/node_modules/x/index.js", true, false],
    ["src", "vendor/src/main.ts", true, false],
    ["a/b", "a/b/c.ts", true, false],
  ] as const)(
    "%j against %j: exclude hides %s, include keeps %s",
    (pattern, path, hides, keeps) => {
      expect(shows("", pattern, path, "file")).toBe(!hides);
      expect(shows(pattern, "", path, "file")).toBe(keeps);
    },
  );

  it("reads a comma-separated list, ignoring blanks and spaces, keeping a brace's commas", () => {
    expect(shows("", " *.lock , ,*.log ", "out.log", "file")).toBe(false);
    expect(shows("", " *.lock , ,*.log ", "a.ts", "file")).toBe(true);
    expect(shows("*.{ts,tsx}, docs/**", "", "app.ts", "file")).toBe(true);
    expect(shows("*.{ts,tsx}, docs/**", "", "docs/a.md", "file")).toBe(true);
    expect(shows("*.{ts,tsx}, docs/**", "", "app.js", "file")).toBe(false);
  });

  it("lets an include through the files inside a matching folder", () => {
    expect(shows("src/**", "", "src/a/b.ts", "file")).toBe(true);
    expect(shows("src/**", "", "lib/b.ts", "file")).toBe(false);
    expect(shows("src/**", "", "lib", "directory")).toBe(true);
  });

  it("lets an exclude win over an include", () => {
    expect(shows("*.ts", "*.test.ts", "a.test.ts", "file")).toBe(false);
  });

  it("still hides dot-entries while hidden files are hidden", () => {
    expect(treePathFilter(false, { include: "*.env", exclude: "" })(".env", "file")).toBe(false);
  });
});

describe("treeFilePathFilter", () => {
  it("hides a file under an excluded or hidden folder", () => {
    const shows = treeFilePathFilter(false, { include: "", exclude: "dist" });
    expect(shows("dist/out/app.js")).toBe(false);
    expect(shows(".cache/app.js")).toBe(false);
    expect(shows("src/app.js")).toBe(true);
  });
});

describe("listingsVersion", () => {
  it("changes when a listing is a new result and not otherwise", () => {
    const root = [file("a.ts")];
    const sub = [file("src/b.ts")];
    const first = listingsVersion([
      { folder: "", entries: root },
      { folder: "src", entries: sub },
    ]);
    expect(
      listingsVersion([
        { folder: "", entries: root },
        { folder: "src", entries: sub },
      ]),
    ).toBe(first);
    expect(
      listingsVersion([
        { folder: "", entries: [...root] },
        { folder: "src", entries: sub },
      ]),
    ).not.toBe(first);
  });
});

describe("treeMetadata", () => {
  it("records sizes for files only and mtimes for both", () => {
    const meta = treeMetadata([{ folder: "", entries: [file("a.ts", 10, 20), folder("src")] }]);
    expect(meta.get("a.ts")).toEqual({ bytes: 10, modified_at: 20 });
    expect(meta.get("src")).toEqual({ bytes: null, modified_at: 5 });
  });
});

describe("withChangedFiles", () => {
  const change = (path: string, status: "created" | "modified" | "deleted" = "deleted") => ({
    path,
    name: path.split("/").at(-1)!,
    status,
    bytes: 3,
    modified_at: 9,
  });

  it("adds a deleted file to its loaded folder", () => {
    const result = withChangedFiles(
      [
        { folder: "", entries: [folder("src")] },
        { folder: "src", entries: [file("src/a.ts")] },
      ],
      [change("src/gone.ts")],
    );
    expect(
      result.find((listing) => listing.folder === "src")?.entries.map((entry) => entry.path),
    ).toEqual(["src/a.ts", "src/gone.ts"]);
  });

  it("waits for a real folder that isn't loaded yet", () => {
    const listings = [{ folder: "", entries: [folder("src")] }];
    const result = withChangedFiles(listings, [change("src/gone.ts")]);
    expect(result).toEqual(listings);
  });

  it("gives a deleted folder its own listing so it's never requested", () => {
    const result = withChangedFiles(
      [{ folder: "", entries: [file("a.ts")] }],
      [change("old/deep/gone.ts")],
    );
    const folders = new Map(
      result.map((listing) => [listing.folder, listing.entries.map((entry) => entry.path)]),
    );
    expect(folders.get("")).toEqual(["a.ts", "old"]);
    expect(folders.get("old")).toEqual(["old/deep"]);
    expect(folders.get("old/deep")).toEqual(["old/deep/gone.ts"]);
  });

  it("leaves files the listings already show alone", () => {
    const listings = [{ folder: "", entries: [file("a.ts")] }];
    expect(withChangedFiles(listings, [change("a.ts", "modified")])).toEqual(listings);
  });
});

describe("changesWithinFolder", () => {
  const change = (path: string) => ({
    path,
    name: path.split("/").at(-1)!,
    status: "modified" as const,
    bytes: 3,
    modified_at: 9,
  });
  const changes = [change("README.md"), change("src/app.ts"), change("src/lib/util.ts")];

  it("keeps every change at the workspace root", () => {
    expect(changesWithinFolder(changes, "", "/work/repo")).toBe(changes);
  });

  it("keeps a folder's changes, relative to it", () => {
    expect(changesWithinFolder(changes, "src", "/work/repo").map((each) => each.path)).toEqual([
      "app.ts",
      "lib/util.ts",
    ]);
  });

  it("finds a workspace folder's changes when it is browsed by its absolute path", () => {
    expect(
      changesWithinFolder(changes, "/work/repo/src/lib", "/work/repo").map((each) => each.path),
    ).toEqual(["util.ts"]);
  });

  it("has none for a folder outside the workspace", () => {
    expect(changesWithinFolder(changes, "/tmp", "/work/repo")).toEqual([]);
  });
});

describe("treeRootEntries", () => {
  const deleted = (path: string) => ({
    path,
    name: path.split("/").at(-1)!,
    status: "deleted" as const,
    bytes: 3,
    modified_at: 9,
  });
  const lockOut = { include: "", exclude: "*.lock" };

  it("counts a deleted file the globs let through, though the listing has none left", () => {
    const rows = treeRootEntries([file("pnpm.lock")], [deleted("README.md")], false, lockOut);
    expect(rows.map((row) => row.path)).toEqual(["README.md"]);
  });

  it("leaves out deleted files the globs or the hidden-files setting hide", () => {
    expect(treeRootEntries([file("pnpm.lock")], [deleted("yarn.lock")], false, lockOut)).toEqual(
      [],
    );
    expect(treeRootEntries([], [deleted(".env")], false)).toEqual([]);
    expect(treeRootEntries([], [deleted("docs/old.md")], false).map((row) => row.path)).toEqual([
      "docs",
    ]);
  });
});
