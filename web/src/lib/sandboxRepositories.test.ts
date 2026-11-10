import { describe, expect, it } from "vitest";
import {
  isValidSandboxBranch,
  repositoryIdentity,
  repositorySelectionError,
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

describe("isValidSandboxRepoUrl unsafe URL components", () => {
  it.each([
    "https://git.example.com/team?token=value/sample.git",
    "https://git.example.com/te\u0000am/sample.git",
    "git@git.example.com:team\u007f/sample.git",
  ])("rejects query components and controls (%j)", (url) => {
    expect(isValidSandboxRepoUrl(url)).toBe(false);
  });
});

describe("isValidSandboxRepoUrl host validation", () => {
  it.each([
    "https://bad..example.com/team/sample.git",
    "https://-bad.example.com/team/sample.git",
    "git@host/path:team/sample.git",
    "git@host\\path:team/sample.git",
    "git@host]:team/sample.git",
    "git@-host:team/sample.git",
    "https://git.example.com:bad/team/sample.git",
    "https://git.example.com:65536/team/sample.git",
    "https://[invalid]/team/sample.git",
    "https://é.example.com/team/sample.git",
    "git@[2001:db8::1:team/sample.git",
    "git@[2001:db8::1]team/sample.git",
    "git@[2001:db8::1]:",
    "git@[2001:db8::1]:/",
    "git@[2001:db8::invalid]:team/sample.git",
    "git@[git.example.com]:team/sample.git",
    "git@[192.0.2.1]:team/sample.git",
    "git@[fe80::1%interface]:team/sample.git",
  ])("rejects malformed hosts and ports (%s)", (url) => {
    expect(isValidSandboxRepoUrl(url)).toBe(false);
  });
  it.each([
    "https://git.example.com:8443/team/sample.git",
    "https://work_git.example.com/team/sample.git",
    "https://_git.example_.com/team/sample.git",
    "https://[::1]/team/sample.git",
    "https://git.example.com./team/sample.git",
    "git@[2001:db8::1]:team/sample.git",
    "git@[::1]:sample.git",
    "git@[::ffff:192.0.2.1]:sample.git",
    "git@work_git:team/sample.git",
    "git@bad_.example.com:team/sample.git",
    "git@café:team/sample.git",
    `git@${"a".repeat(64)}:team/sample.git`,
  ])("accepts supported authorities (%s)", (url) => {
    expect(isValidSandboxRepoUrl(url)).toBe(true);
  });
});

describe("repositoryIdentity", () => {
  it("matches bracketed SSH IPv6 URLs to HTTPS catalog entries", () => {
    expect(repositoryIdentity("git@[2001:db8::1]:team/sample.git")).toBe(
      repositoryIdentity("https://[2001:db8::1]/team/sample"),
    );
  });
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
    "feature//next",
    "@",
    "feature\u007f",
    ".hidden",
    "feature/.hidden",
    "feature.lock",
    "feature.lock/next",
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

describe("repositorySelectionError", () => {
  const capabilities = { catalog: [], urlInput: true, branchSelection: true };
  it.each([
    [{ url: "", branch: "" }, null],
    [{ url: "https://git.example.com/team/sample.git", branch: "release" }, null],
    [{ url: "https://user:secret@git.example.com/team/sample.git" }, "without credentials"],
    [{ url: "https://git.example.com/team/sample.git#release" }, "without credentials"],
    [{ url: "https://git.example.com/team/sample.git", branch: "bad branch" }, "valid branch"],
    [{ url: "https://git.example.com/team/sample.git", branch: "b".repeat(256) }, "256 characters"],
    [{ url: "", branch: "release" }, "Choose a repository"],
  ] as const)("validates a separate URL and branch (%j)", (repository, error) => {
    const result = repositorySelectionError(repository, capabilities);
    if (error === null) expect(result).toBeNull();
    else expect(result).toContain(error);
  });
});
