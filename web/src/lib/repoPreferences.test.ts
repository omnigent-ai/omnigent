import { afterEach, describe, expect, it, vi } from "vitest";
import { readLastSandboxRepos, writeLastSandboxRepos } from "./repoPreferences";

const KEY = "omnigent:last-sandbox-repos";
const LEGACY_SINGLE_REPO_KEY = "omnigent:last-sandbox-repo";
const CLAUDE = "agent-claude";
const CODEX = "agent-codex";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("repoPreferences", () => {
  it("returns [] when nothing is stored", () => {
    expect(readLastSandboxRepos(CLAUDE)).toEqual([]);
  });

  it("round-trips a list of repos + branches for the agent that launched with them", () => {
    writeLastSandboxRepos(CLAUDE, [
      { url: "https://github.com/org/a.git", branch: "main" },
      { url: "https://github.com/org/b.git", branch: "" },
    ]);
    expect(readLastSandboxRepos(CLAUDE)).toEqual([
      { url: "https://github.com/org/a.git", branch: "main" },
      { url: "https://github.com/org/b.git", branch: "" },
    ]);
  });

  it("keeps the repos remembered for one agent from seeding another", () => {
    writeLastSandboxRepos(CLAUDE, [{ url: "https://github.com/org/a.git", branch: "" }]);
    expect(readLastSandboxRepos(CODEX)).toEqual([]);

    writeLastSandboxRepos(CODEX, [{ url: "https://github.com/org/b.git", branch: "dev" }]);
    expect(readLastSandboxRepos(CLAUDE)).toEqual([
      { url: "https://github.com/org/a.git", branch: "" },
    ]);
    expect(readLastSandboxRepos(CODEX)).toEqual([
      { url: "https://github.com/org/b.git", branch: "dev" },
    ]);
  });

  it("reads nothing and stores nothing without an agent", () => {
    writeLastSandboxRepos(null, [{ url: "https://github.com/org/a.git", branch: "" }]);
    expect(localStorage.getItem(KEY)).toBeNull();
    expect(readLastSandboxRepos(null)).toEqual([]);
    expect(readLastSandboxRepos(undefined)).toEqual([]);
  });

  it("trims surrounding whitespace and drops blank-url entries before storing", () => {
    writeLastSandboxRepos(CLAUDE, [
      { url: "  https://github.com/org/repo.git  ", branch: "  dev  " },
      { url: "   ", branch: "main" },
    ]);
    expect(readLastSandboxRepos(CLAUDE)).toEqual([
      { url: "https://github.com/org/repo.git", branch: "dev" },
    ]);
  });

  it("clears only that agent's entry when its list is empty (or all-blank)", () => {
    writeLastSandboxRepos(CLAUDE, [{ url: "https://github.com/org/a.git", branch: "main" }]);
    writeLastSandboxRepos(CODEX, [{ url: "https://github.com/org/b.git", branch: "" }]);
    writeLastSandboxRepos(CLAUDE, [{ url: "   ", branch: "" }]);
    expect(readLastSandboxRepos(CLAUDE)).toEqual([]);
    expect(readLastSandboxRepos(CODEX)).toEqual([
      { url: "https://github.com/org/b.git", branch: "" },
    ]);
    expect(JSON.parse(localStorage.getItem(KEY) ?? "{}")).not.toHaveProperty(CLAUDE);
  });

  it("removes the preference once the last agent's entry is cleared", () => {
    writeLastSandboxRepos(CLAUDE, [{ url: "https://github.com/org/a.git", branch: "main" }]);
    writeLastSandboxRepos(CLAUDE, []);
    expect(localStorage.getItem(KEY)).toBeNull();
  });

  it("overwrites the agent's previous list", () => {
    writeLastSandboxRepos(CLAUDE, [{ url: "https://github.com/org/a.git", branch: "main" }]);
    writeLastSandboxRepos(CLAUDE, [{ url: "https://github.com/org/b.git", branch: "dev" }]);
    expect(readLastSandboxRepos(CLAUDE)).toEqual([
      { url: "https://github.com/org/b.git", branch: "dev" },
    ]);
  });

  it("ignores the browser-wide list an older build wrote rather than guessing its agent", () => {
    localStorage.setItem(
      KEY,
      JSON.stringify([{ url: "https://github.com/org/legacy.git", branch: "main" }]),
    );
    expect(readLastSandboxRepos(CLAUDE)).toEqual([]);
    // The next launch replaces the old shape instead of merging into it.
    writeLastSandboxRepos(CLAUDE, [{ url: "https://github.com/org/a.git", branch: "" }]);
    expect(readLastSandboxRepos(CLAUDE)).toEqual([
      { url: "https://github.com/org/a.git", branch: "" },
    ]);
    expect(readLastSandboxRepos(CODEX)).toEqual([]);
  });

  it("ignores the single-repo preference written by builds before multi-repo", () => {
    localStorage.setItem(
      LEGACY_SINGLE_REPO_KEY,
      JSON.stringify({ url: "https://github.com/org/legacy.git", branch: "main" }),
    );
    expect(readLastSandboxRepos(CLAUDE)).toEqual([]);
  });

  it("drops entries with a blank url (defensive)", () => {
    localStorage.setItem(KEY, JSON.stringify({ [CLAUDE]: [{ url: "  ", branch: "x" }] }));
    expect(readLastSandboxRepos(CLAUDE)).toEqual([]);
  });

  it("returns [] for malformed stored json or a non-list entry (defensive)", () => {
    localStorage.setItem(KEY, "not json");
    expect(readLastSandboxRepos(CLAUDE)).toEqual([]);
    localStorage.setItem(
      KEY,
      JSON.stringify({ [CLAUDE]: { url: "https://github.com/org/a.git" } }),
    );
    expect(readLastSandboxRepos(CLAUDE)).toEqual([]);
  });

  it("never throws when storage is inaccessible", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() =>
      writeLastSandboxRepos(CLAUDE, [{ url: "https://github.com/org/repo.git", branch: "main" }]),
    ).not.toThrow();
    expect(readLastSandboxRepos(CLAUDE)).toEqual([]);
  });

  it("strips URL userinfo so a tokenized URL is not persisted (secret at rest)", () => {
    writeLastSandboxRepos(CLAUDE, [
      { url: "https://x-access-token:s3cr3tpat@github.com/org/repo.git", branch: "main" },
    ]);
    expect(readLastSandboxRepos(CLAUDE)).toEqual([
      { url: "https://github.com/org/repo.git", branch: "main" },
    ]);
    expect(localStorage.getItem(KEY) ?? "").not.toContain("s3cr3tpat");
  });

  it("strips userinfo from a stored tokenized URL on read (defensive)", () => {
    localStorage.setItem(
      KEY,
      JSON.stringify({
        [CLAUDE]: [{ url: "https://user:PAT@github.com/org/repo.git", branch: "dev" }],
      }),
    );
    expect(readLastSandboxRepos(CLAUDE)).toEqual([
      { url: "https://github.com/org/repo.git", branch: "dev" },
    ]);
  });
});
