import { describe, expect, it } from "vitest";
import { fileCommandTargetKey, pathToCopy, resolveFileCommandTarget } from "./target";

const options = { root: "/repo", hostId: "this-mac", conversationId: "conv_1" };

describe("resolveFileCommandTarget", () => {
  it.each([
    ["a workspace path", "src/main.ts", "", "src/main.ts", "/repo/src/main.ts"],
    ["a row in a browsed folder", "main.ts", "src", "src/main.ts", "/repo/src/main.ts"],
    [
      "a row in a browsed absolute folder",
      "main.ts",
      "/repo/src",
      "src/main.ts",
      "/repo/src/main.ts",
    ],
    [
      "an absolute path inside the workspace",
      "/repo/docs/a.md",
      "",
      "docs/a.md",
      "/repo/docs/a.md",
    ],
    ["a path outside the workspace", "/tmp/notes.md", "", null, "/tmp/notes.md"],
    ["a sibling that shares the root's prefix", "/repo-old/a.md", "", null, "/repo-old/a.md"],
  ])("names %s from the root and absolutely", (_case, path, base, relativePath, absolutePath) => {
    expect(resolveFileCommandTarget(path, { ...options, base })).toEqual({
      relativePath,
      absolutePath,
      conversationId: "conv_1",
      hostId: "this-mac",
      directory: false,
      deleted: false,
    });
  });

  it("joins with a Windows root's separator", () => {
    const target = resolveFileCommandTarget("docs\\notes.md", { ...options, root: "C:\\work\\" });
    expect(target.absolutePath).toBe("C:\\work\\docs\\notes.md");
    expect(
      resolveFileCommandTarget("C:\\work\\docs\\a.md", { ...options, root: "C:\\work" })
        .relativePath,
    ).toBe("docs\\a.md");
  });

  it("names the root itself with an empty relative path", () => {
    expect(resolveFileCommandTarget("/repo", options).relativePath).toBe("");
  });

  it("has no absolute path until the root is known", () => {
    const target = resolveFileCommandTarget("src/main.ts", { ...options, root: null });
    expect(target.relativePath).toBe("src/main.ts");
    expect(target.absolutePath).toBeNull();
  });
});

describe("pathToCopy", () => {
  it("prefers the full path, then the workspace path", () => {
    expect(pathToCopy(resolveFileCommandTarget("a.md", options))).toEqual({
      text: "/repo/a.md",
      absolute: true,
    });
    expect(pathToCopy(resolveFileCommandTarget("a.md", { ...options, root: null }))).toEqual({
      text: "a.md",
      absolute: false,
    });
  });
});

describe("pathToCopy for Copy relative path", () => {
  it.each([
    ["a nested file", "src/lib/a.ts", options, { text: "src/lib/a.ts", absolute: false }],
    ["a file at the workspace root", "a.md", options, { text: "a.md", absolute: false }],
    ["the workspace root itself", "/repo", options, { text: "/repo", absolute: true }],
    ["a file outside the workspace", "/etc/hosts", options, { text: "/etc/hosts", absolute: true }],
    [
      "a file before the workspace root is known",
      "src/a.ts",
      { ...options, root: null },
      { text: "src/a.ts", absolute: false },
    ],
  ])("copies %s from the workspace root where it can", (_, path, targetOptions, copied) => {
    expect(pathToCopy(resolveFileCommandTarget(path, targetOptions), "copy-relative-path")).toEqual(
      copied,
    );
  });

  it("copies an absolute path in full before the workspace root is known", () => {
    const outside = resolveFileCommandTarget("/etc/hosts", { ...options, root: null });
    expect(pathToCopy(outside, "copy-relative-path")).toEqual({
      text: "/etc/hosts",
      absolute: true,
    });
  });
});

describe("fileCommandTargetKey", () => {
  it("tells apart the same path in two sessions", () => {
    const first = resolveFileCommandTarget("a.md", options);
    const second = resolveFileCommandTarget("a.md", { ...options, conversationId: "conv_2" });
    expect(fileCommandTargetKey(first)).not.toBe(fileCommandTargetKey(second));
    expect(fileCommandTargetKey(first)).toBe(
      fileCommandTargetKey(resolveFileCommandTarget("a.md", options)),
    );
  });
});
