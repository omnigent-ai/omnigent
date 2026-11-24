import { afterEach, describe, expect, it, vi } from "vitest";
import {
  MAX_REMEMBERED_RICH_DIFFS,
  readRichMarkdownPaths,
  resetUnsavedRichMarkdownPaths,
  subscribeRichMarkdownPaths,
  writeRichMarkdownPath,
} from "./markdownDiffPreference";

const STORAGE_KEY = "omnigent.diffReview.richMarkdownFiles";

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
  resetUnsavedRichMarkdownPaths();
});

describe("rich Markdown diff preference", () => {
  it("starts with every file showing its source diff", () => {
    expect(readRichMarkdownPaths("changes:one").size).toBe(0);
  });

  it("remembers each review's choices separately", () => {
    writeRichMarkdownPath("changes:one", "README.md", true);
    writeRichMarkdownPath("pull-request:two", "docs/guide.md", true);
    expect([...readRichMarkdownPaths("changes:one")]).toEqual(["README.md"]);
    expect([...readRichMarkdownPaths("pull-request:two")]).toEqual(["docs/guide.md"]);
    writeRichMarkdownPath("changes:one", "README.md", false);
    expect(readRichMarkdownPaths("changes:one").size).toBe(0);
  });

  it("returns the same set until a choice changes", () => {
    writeRichMarkdownPath("scope", "a.md", true);
    const first = readRichMarkdownPaths("scope");
    expect(readRichMarkdownPaths("scope")).toBe(first);
    writeRichMarkdownPath("scope", "b.md", true);
    expect(readRichMarkdownPaths("scope")).not.toBe(first);
  });

  it("keeps only the newest choices", () => {
    for (let index = 0; index <= MAX_REMEMBERED_RICH_DIFFS; index += 1) {
      writeRichMarkdownPath("scope", `file-${index}.md`, true);
    }
    const paths = readRichMarkdownPaths("scope");
    expect(paths.size).toBe(MAX_REMEMBERED_RICH_DIFFS);
    expect(paths.has("file-0.md")).toBe(false);
    expect(paths.has(`file-${MAX_REMEMBERED_RICH_DIFFS}.md`)).toBe(true);
  });

  it("treats corrupt storage as no choices", () => {
    window.localStorage.setItem(STORAGE_KEY, "{not json");
    expect(readRichMarkdownPaths("scope").size).toBe(0);
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([1, "scope\u0000ok.md"]));
    expect([...readRichMarkdownPaths("scope")]).toEqual(["ok.md"]);
  });

  it("keeps a choice for this session when storage refuses it", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    expect(writeRichMarkdownPath("scope", "a.md", true)).toBe(false);
    expect([...readRichMarkdownPaths("scope")]).toEqual(["a.md"]);
  });

  it("tells listeners about changes in this window and others", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeRichMarkdownPaths(listener);
    writeRichMarkdownPath("scope", "a.md", true);
    window.dispatchEvent(new StorageEvent("storage", { key: STORAGE_KEY }));
    window.dispatchEvent(new StorageEvent("storage", { key: "unrelated" }));
    expect(listener).toHaveBeenCalledTimes(2);
    unsubscribe();
  });
});
