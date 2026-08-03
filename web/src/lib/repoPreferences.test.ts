import { afterEach, describe, expect, it, vi } from "vitest";
import { readLastSandboxRepo, writeLastSandboxRepo } from "./repoPreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("repoPreferences", () => {
  it("returns null when nothing is stored", () => {
    expect(readLastSandboxRepo()).toBeNull();
  });

  it("round-trips a written repo + branch", () => {
    writeLastSandboxRepo("https://github.com/org/repo.git", "main");
    expect(readLastSandboxRepo()).toEqual({
      url: "https://github.com/org/repo.git",
      branch: "main",
    });
  });

  it("keeps a repo with no branch (default-branch case)", () => {
    writeLastSandboxRepo("https://github.com/org/repo.git", "");
    expect(readLastSandboxRepo()).toEqual({
      url: "https://github.com/org/repo.git",
      branch: "",
    });
  });

  it("trims surrounding whitespace before storing", () => {
    writeLastSandboxRepo("  https://github.com/org/repo.git  ", "  dev  ");
    expect(readLastSandboxRepo()).toEqual({
      url: "https://github.com/org/repo.git",
      branch: "dev",
    });
  });

  it("clears the preference when the url is blank", () => {
    writeLastSandboxRepo("https://github.com/org/repo.git", "main");
    writeLastSandboxRepo("   ", "main");
    expect(readLastSandboxRepo()).toBeNull();
  });

  it("overwrites the previous value", () => {
    writeLastSandboxRepo("https://github.com/org/a.git", "main");
    writeLastSandboxRepo("https://github.com/org/b.git", "dev");
    expect(readLastSandboxRepo()).toEqual({
      url: "https://github.com/org/b.git",
      branch: "dev",
    });
  });

  it("returns null for a stored entry with a blank url (defensive)", () => {
    localStorage.setItem("omnigent:last-sandbox-repo", JSON.stringify({ url: "  ", branch: "x" }));
    expect(readLastSandboxRepo()).toBeNull();
  });

  it("returns null for malformed stored json (defensive)", () => {
    localStorage.setItem("omnigent:last-sandbox-repo", "not json");
    expect(readLastSandboxRepo()).toBeNull();
  });

  it("never throws when storage is inaccessible", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() => writeLastSandboxRepo("https://github.com/org/repo.git", "main")).not.toThrow();
    expect(readLastSandboxRepo()).toBeNull();
  });
});
