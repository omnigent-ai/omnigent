import { describe, expect, it } from "vitest";
import {
  isValidSandboxBranch,
  isValidSandboxRepoUrl,
  isValidSandboxWorkspaceLength,
} from "./sandboxRepositories";

describe("isValidSandboxRepoUrl repository names", () => {
  it.each(["repo", "repo.git/", ".hidden", "repo-1.2_3", "..."])(
    "accepts a filesystem-safe repository name: %s",
    (name) => expect(isValidSandboxRepoUrl(`https://host/org/${name}`)).toBe(true),
  );

  it.each(["repo?tab=readme", "my+repo", ".", "..", ".git", "café", "repo%20name"])(
    "rejects a repository name the server refuses: %s",
    (name) => {
      expect(isValidSandboxRepoUrl(`https://host/org/${name}`)).toBe(false);
      expect(isValidSandboxRepoUrl(`git@host:${name}`)).toBe(false);
    },
  );
});

describe("isValidSandboxWorkspaceLength", () => {
  const url = "https://github.com/org/repo";
  const remaining = 256 - url.length - 1;

  it.each<[string, string, boolean]>([
    [url + "r".repeat(256 - url.length), "", true],
    [url + "r".repeat(257 - url.length), "", false],
    [url, "b".repeat(remaining), true],
    [url, "b".repeat(remaining + 1), false],
    [`  ${url}  `, ` ${"b".repeat(remaining)} `, true],
    [url, "🚀".repeat(remaining), true],
    [url, "🚀".repeat(remaining + 1), false],
  ])(
    "checks the composed workspace's code-point length (%s, %s)",
    (repository, branch, expected) => {
      expect(isValidSandboxWorkspaceLength(repository, branch)).toBe(expected);
    },
  );
});

describe("isValidSandboxBranch", () => {
  it.each(["", "  ", "main", " feature/orders ", "release-1.2", "fix/café"])(
    "accepts a default or branch selection: %j",
    (branch) => expect(isValidSandboxBranch(branch)).toBe(true),
  );

  it.each([
    "my branch",
    "my\tbranch",
    "my\nbranch",
    "my\u00a0branch",
    "my\u0000branch",
    "feat#1",
    "a".repeat(40),
    "-main",
    "/main",
    "main/",
    "main.",
    "feature..next",
    "feature@{old}",
    "feature~1",
    "feature^1",
    "feature:next",
    "feature?next",
    "feature*next",
    "feature[next",
    "feature\\next",
  ])("rejects branches the managed workspace parser refuses: %j", (branch) => {
    expect(isValidSandboxBranch(branch)).toBe(false);
  });
});
