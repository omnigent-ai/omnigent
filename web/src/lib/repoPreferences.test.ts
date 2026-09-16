import { afterEach, describe, expect, it, vi } from "vitest";
import { readLastSandboxRepos, writeLastSandboxRepos } from "./repoPreferences";

const KEY = "omnigent:last-sandbox-repos";
const LEGACY_KEY = "omnigent:last-sandbox-repo";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("repoPreferences", () => {
  it("returns [] when nothing is stored", () => {
    expect(readLastSandboxRepos()).toEqual([]);
  });

  it("round-trips a list of repos + branches", () => {
    writeLastSandboxRepos([
      { url: "https://github.com/org/a.git", branch: "main" },
      { url: "https://github.com/org/b.git", branch: "" },
    ]);
    expect(readLastSandboxRepos()).toEqual([
      { url: "https://github.com/org/a.git", branch: "main" },
      { url: "https://github.com/org/b.git", branch: "" },
    ]);
  });

  it("trims surrounding whitespace and drops blank-url entries before storing", () => {
    writeLastSandboxRepos([
      { url: "  https://github.com/org/repo.git  ", branch: "  dev  " },
      { url: "   ", branch: "main" },
    ]);
    expect(readLastSandboxRepos()).toEqual([
      { url: "https://github.com/org/repo.git", branch: "dev" },
    ]);
  });

  it("clears the preference when the list is empty (or all-blank)", () => {
    writeLastSandboxRepos([{ url: "https://github.com/org/repo.git", branch: "main" }]);
    writeLastSandboxRepos([]);
    expect(readLastSandboxRepos()).toEqual([]);
    expect(localStorage.getItem(KEY)).toBeNull();
  });

  it("overwrites the previous list", () => {
    writeLastSandboxRepos([{ url: "https://github.com/org/a.git", branch: "main" }]);
    writeLastSandboxRepos([{ url: "https://github.com/org/b.git", branch: "dev" }]);
    expect(readLastSandboxRepos()).toEqual([
      { url: "https://github.com/org/b.git", branch: "dev" },
    ]);
  });

  it("migrates a single-repo preference written by an older build", () => {
    localStorage.setItem(
      LEGACY_KEY,
      JSON.stringify({ url: "https://github.com/org/legacy.git", branch: "main" }),
    );
    expect(readLastSandboxRepos()).toEqual([
      { url: "https://github.com/org/legacy.git", branch: "main" },
    ]);
  });

  it("drops entries with a blank url (defensive)", () => {
    localStorage.setItem(KEY, JSON.stringify([{ url: "  ", branch: "x" }]));
    expect(readLastSandboxRepos()).toEqual([]);
  });

  it("returns [] for malformed stored json (defensive)", () => {
    localStorage.setItem(KEY, "not json");
    expect(readLastSandboxRepos()).toEqual([]);
  });

  it("never throws when storage is inaccessible", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() =>
      writeLastSandboxRepos([{ url: "https://github.com/org/repo.git", branch: "main" }]),
    ).not.toThrow();
    expect(readLastSandboxRepos()).toEqual([]);
  });

  it("strips URL userinfo so a tokenized URL is not persisted (secret at rest)", () => {
    writeLastSandboxRepos([
      { url: "https://x-access-token:s3cr3tpat@github.com/org/repo.git", branch: "main" },
    ]);
    expect(readLastSandboxRepos()).toEqual([
      { url: "https://github.com/org/repo.git", branch: "main" },
    ]);
    expect(localStorage.getItem(KEY) ?? "").not.toContain("s3cr3tpat");
  });

  it("strips userinfo from a stored tokenized URL on read (defensive)", () => {
    localStorage.setItem(
      KEY,
      JSON.stringify([{ url: "https://user:PAT@github.com/org/repo.git", branch: "dev" }]),
    );
    expect(readLastSandboxRepos()).toEqual([
      { url: "https://github.com/org/repo.git", branch: "dev" },
    ]);
  });
});
