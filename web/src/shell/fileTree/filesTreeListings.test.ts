import { describe, expect, it } from "vitest";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import {
  listingsVersion,
  treeMetadata,
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
